"""The single bound resource-estimation path. Production code never signs or sends."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import Field, StrictBool, StrictInt

from cu_pilot.binding import (
    LookupEvidence,
    bind_message,
    decode_wire,
    message_identity,
    replace_resources,
    slot_value,
    unsigned_wire,
)
from cu_pilot.resources import ResourceEstimator
from cu_pilot.rpc import RpcClient, RpcError, SimulationEstimate
from cu_pilot.schemas import (
    MAX_COMPUTE_UNITS,
    MAX_LOADED_ACCOUNT_BYTES,
    Features,
    Prediction,
    StrictModel,
)

if TYPE_CHECKING:
    from cu_pilot.lifecycle import ProfileRegistry


class EstimationContext(StrictModel):
    context: str = Field(min_length=1)
    current_slot: StrictInt = Field(ge=0)
    cluster_identity: str = Field(min_length=1)
    runtime_identity: str = Field(min_length=1)
    workload: str = Field(min_length=1)
    # An explicit application contract. False means simulate and retain max budgets.
    budget_independent: StrictBool = False
    commitment: Literal["processed", "confirmed", "finalized"] = "confirmed"
    max_lookup_age_slots: StrictInt = Field(default=32, ge=0)


class DecisionPlan(StrictModel):
    schema_version: Literal["cu-pilot-decision-v1"] = "cu-pilot-decision-v1"
    observation_id: str
    context: EstimationContext
    original_identity: str
    prepared_identity: str
    prepared_wire_base64: str
    features: Features
    durable_nonce: bool
    prediction: Prediction
    eligibility_reason: str
    artifact_version: str | None = None
    artifact_digest: str | None = None
    profile_id: str | None = None
    profile_revision: int | None = None
    policy_version: str = "bound-resources-v1"
    preparation_ms: float = Field(ge=0)
    state_reads: int = Field(default=0, ge=0)
    lookup_evidence: tuple[LookupEvidence, ...] = ()
    control_selected: bool = False
    control_probability: float = 0


class ResourceDecision(StrictModel):
    schema_version: Literal["cu-pilot-result-v1"] = "cu-pilot-result-v1"
    observation_id: str
    status: Literal["accepted_prediction", "simulation_success", "unresolved"]
    reason: str
    plan: DecisionPlan | None = None
    compute_unit_limit: int | None = None
    loaded_accounts_data_size_limit: int | None = None
    final_identity: str | None = None
    unsigned_transaction_base64: str | None = None
    observation_slot: int | None = None
    simulation: SimulationEstimate | None = None
    simulation_failure: dict[str, object] | None = None
    full_preparation_ms: float = Field(ge=0)
    resource_simulation_calls: int = 0
    rpc_attempts: int = 0
    retries: int = 0
    shadow: bool = False
    control_selected: bool = False
    control_probability: float = 0
    # This library has no preflight, payment, slippage or wallet-validation policy.
    required_validation_unchanged: Literal[True] = True


def artifact_digest(estimator: ResourceEstimator) -> str:
    from cu_pilot.lifecycle import artifact_digest as canonical_digest

    return canonical_digest(estimator.model.model_dump_json())


def prepare_decision(
    wire_base64: str,
    *,
    context: EstimationContext,
    estimator: ResourceEstimator | None = None,
    observation_id: str | None = None,
    lookups: Mapping[str, LookupEvidence] | None = None,
    rpc: RpcClient | None = None,
    registry: ProfileRegistry | None = None,
    profile_id: str | None = None,
) -> DecisionPlan:
    started = time.perf_counter()
    slot_value(context.current_slot)
    request_id = observation_id or str(uuid.uuid4())
    tables = dict(lookups or {})
    state_reads = 0
    from solders.message import MessageV0

    original = decode_wire(wire_base64)
    if isinstance(original.message, MessageV0):
        for lookup in original.message.address_table_lookups:
            key = str(lookup.account_key)
            if key not in tables:
                if rpc is None:
                    raise ValueError("lookup evidence requires an RPC or a fresh cached account")
                tables[key] = LookupEvidence.from_rpc(
                    key,
                    rpc.get_account_info(
                        key, min_context_slot=context.current_slot, commitment=context.commitment
                    ),
                )
                state_reads += 1
    bound = bind_message(
        wire_base64,
        current_slot=context.current_slot,
        lookups=tables,
        max_lookup_age_slots=context.max_lookup_age_slots,
    )
    prediction = Prediction(
        pattern_id=bound.features.pattern_id,
        simulation_recommended=True,
        reason="no_resource_artifact",
        explanation="No paired-resource model.",
    )
    digest = None
    if estimator is not None:
        prediction = estimator.predict(
            bound.features, context=context.context, current_slot=context.current_slot
        )
        digest = artifact_digest(estimator)
    reason = prediction.reason
    revision = None
    if not context.budget_independent:
        reason = "budget_sensitive_or_unreviewed_workload"
    elif registry is None or profile_id is None:
        reason = "profile_not_released"
    else:
        eligibility = registry.check(
            profile_id,
            current_slot=context.current_slot,
            context=context.context,
            cluster_identity=context.cluster_identity,
            runtime_identity=context.runtime_identity,
            workload=context.workload,
            program_ids=bound.features.program_ids,
        )
        revision = eligibility.revision
        if not eligibility.eligible:
            reason = eligibility.reason
        elif eligibility.artifact_sha256 != digest:
            reason = "active_artifact_mismatch"
    selected = False
    probability = 0.0
    if (
        registry is not None
        and profile_id is not None
        and revision is not None
        and not prediction.simulation_recommended
        and reason == prediction.reason
    ):
        assert prediction.compute_unit_limit is not None
        assert prediction.loaded_accounts_data_size_limit is not None
        control = registry.select_control(
            request_id=request_id,
            profile_id=profile_id,
            revision=revision,
            decision_version=digest or "none",
            eligible=True,
            compute_unit_limit=prediction.compute_unit_limit,
            loaded_accounts_data_size_limit=prediction.loaded_accounts_data_size_limit,
            current_slot=context.current_slot,
        )
        selected, probability = control.selected, control.probability
    return DecisionPlan(
        observation_id=request_id,
        context=context,
        original_identity=bound.original_identity,
        prepared_identity=bound.prepared_identity,
        prepared_wire_base64=bound.wire_base64,
        features=bound.features,
        durable_nonce=bound.durable_nonce,
        prediction=prediction,
        eligibility_reason=reason,
        artifact_version=(str(estimator.model.artifact_version) if estimator is not None else None),
        artifact_digest=digest,
        profile_id=profile_id,
        profile_revision=revision,
        preparation_ms=(time.perf_counter() - started) * 1000,
        state_reads=state_reads,
        lookup_evidence=tuple(tables.values()),
        control_selected=selected,
        control_probability=probability,
    )


def execute_decision(
    plan: DecisionPlan,
    *,
    rpc: RpcClient,
    shadow: bool = False,
    force_simulation: bool = False,
    margin: float = 0.1,
    registry: ProfileRegistry | None = None,
    current_slot: int | None = None,
) -> ResourceDecision:
    """Execute a frozen pre-label plan; useful for resumable prospective shadow collection."""
    started = time.perf_counter()
    calls_before, retries_before = rpc.call_count, rpc.retry_count
    execution_slot = plan.context.current_slot if current_slot is None else current_slot
    try:
        slot_value(execution_slot)
        if execution_slot < plan.context.current_slot:
            raise ValueError("execution slot precedes the decision")
        # Persisted/public plans are untrusted data. Re-derive all features from
        # the exact bytes, and require already-prepared maximum resource budgets.
        rebound = bind_message(
            plan.prepared_wire_base64,
            current_slot=execution_slot,
            lookups={table.address: table for table in plan.lookup_evidence},
            max_lookup_age_slots=plan.context.max_lookup_age_slots,
        )
        if (
            rebound.original_identity != plan.prepared_identity
            or rebound.prepared_identity != plan.prepared_identity
            or rebound.features != plan.features
            or rebound.durable_nonce != plan.durable_nonce
        ):
            raise ValueError("decision does not describe the prepared message")
    except ValueError:
        return ResourceDecision(
            observation_id=plan.observation_id,
            status="unresolved",
            reason="invalid_bound_plan",
            plan=plan,
            shadow=shadow,
            full_preparation_ms=plan.preparation_ms + (time.perf_counter() - started) * 1000,
        )
    prediction = plan.prediction
    fallback_reason = plan.eligibility_reason
    accepted = (
        not prediction.simulation_recommended
        and plan.eligibility_reason == prediction.reason
        and plan.profile_id is not None
    )
    # A stored plan alone cannot authorize a skip at a later time. Production callers
    # use estimate_resources, which checks the current registry again here.
    if accepted:
        if registry is None or current_slot is None or not plan.context.budget_independent:
            accepted = False
            fallback_reason = "fresh_profile_check_required"
        else:
            eligibility = registry.check(
                plan.profile_id or "",
                current_slot=execution_slot,
                context=plan.context.context,
                cluster_identity=plan.context.cluster_identity,
                runtime_identity=plan.context.runtime_identity,
                workload=plan.context.workload,
                program_ids=plan.features.program_ids,
            )
            accepted = (
                eligibility.eligible
                and eligibility.revision == plan.profile_revision
                and eligibility.artifact_sha256 == plan.artifact_digest
            )
            if not accepted:
                fallback_reason = (
                    eligibility.reason if not eligibility.eligible else "active_artifact_mismatch"
                )
            if accepted:
                try:
                    manifest, active_model = registry.load_active(plan.profile_id or "")
                    active_prediction = active_model.predict(
                        rebound.features, context=plan.context.context, current_slot=execution_slot
                    )
                    accepted = (
                        manifest.revision == plan.profile_revision
                        and manifest.artifact_sha256 == plan.artifact_digest
                        and active_prediction == prediction
                        and not active_prediction.simulation_recommended
                    )
                    if not accepted:
                        fallback_reason = "prediction_artifact_mismatch"
                    if accepted:
                        assert prediction.compute_unit_limit is not None
                        assert prediction.loaded_accounts_data_size_limit is not None
                        control = registry.select_control(
                            request_id=plan.observation_id,
                            profile_id=plan.profile_id or "",
                            revision=manifest.revision,
                            decision_version=plan.artifact_digest or "none",
                            eligible=True,
                            compute_unit_limit=prediction.compute_unit_limit,
                            loaded_accounts_data_size_limit=prediction.loaded_accounts_data_size_limit,
                            current_slot=plan.context.current_slot,
                        )
                        accepted = (
                            control.selected == plan.control_selected
                            and control.probability == plan.control_probability
                        )
                        if not accepted:
                            fallback_reason = "control_selection_mismatch"
                except ValueError:
                    accepted = False
                    fallback_reason = "profile_revalidation_failed"
    simulation = None
    simulation_started = False
    try:
        if accepted and not shadow and not force_simulation and not plan.control_selected:
            compute, data = (
                prediction.compute_unit_limit,
                prediction.loaded_accounts_data_size_limit,
            )
            status: Literal["accepted_prediction", "simulation_success"] = "accepted_prediction"
            reason = plan.eligibility_reason
        else:
            # No implicit blockhash refresh: the bytes we record are the bytes simulated.
            # The caller may explicitly refresh before a NEW decision; durable nonce stays intact.
            simulation_started = True
            simulation = rpc.simulate(
                plan.prepared_wire_base64,
                version=plan.features.version,
                min_context_slot=execution_slot,
                replace_recent_blockhash=False,
                require_loaded_data=True,
                commitment=plan.context.commitment,
                margin=margin,
            )
            compute, data = (
                ((simulation.compute_unit_limit + 99) // 100) * 100,
                simulation.loaded_accounts_data_size_limit,
            )
            if not plan.context.budget_independent:
                compute, data = MAX_COMPUTE_UNITS, MAX_LOADED_ACCOUNT_BYTES
            status = "simulation_success"
            reason = (
                "shadow" if shadow else "forced_simulation" if force_simulation else fallback_reason
            )
        if compute is None or data is None:
            raise RpcError("Both resource limits are required", code="missing_measurement")
        final = replace_resources(decode_wire(plan.prepared_wire_base64).message, compute, data)
        return ResourceDecision(
            observation_id=plan.observation_id,
            plan=plan,
            shadow=shadow,
            status=status,
            reason=reason,
            compute_unit_limit=compute,
            loaded_accounts_data_size_limit=data,
            final_identity=message_identity(final),
            unsigned_transaction_base64=unsigned_wire(final),
            simulation=simulation,
            observation_slot=simulation.slot if simulation else execution_slot,
            full_preparation_ms=plan.preparation_ms + (time.perf_counter() - started) * 1000,
            resource_simulation_calls=int(simulation_started),
            rpc_attempts=rpc.call_count - calls_before,
            retries=rpc.retry_count - retries_before,
            control_selected=plan.control_selected,
            control_probability=plan.control_probability,
        )
    except RpcError as exc:
        return ResourceDecision(
            observation_id=plan.observation_id,
            plan=plan,
            shadow=shadow,
            status="unresolved",
            reason=exc.code,
            simulation_failure=exc.evidence,
            full_preparation_ms=plan.preparation_ms + (time.perf_counter() - started) * 1000,
            resource_simulation_calls=int(simulation_started),
            rpc_attempts=rpc.call_count - calls_before,
            retries=rpc.retry_count - retries_before,
            control_selected=plan.control_selected,
            control_probability=plan.control_probability,
        )


def estimate_resources(
    wire_base64: str,
    *,
    rpc: RpcClient,
    context: EstimationContext,
    estimator: ResourceEstimator | None = None,
    registry: ProfileRegistry | None = None,
    profile_id: str | None = None,
    observation_id: str | None = None,
    lookups: Mapping[str, LookupEvidence] | None = None,
    shadow: bool = False,
    force_simulation: bool = False,
    before_simulation: Callable[[DecisionPlan], None] | None = None,
) -> ResourceDecision:
    started = time.perf_counter()
    rpc_attempts_before = rpc.call_count
    request_id = observation_id or str(uuid.uuid4())
    try:
        plan = prepare_decision(
            wire_base64,
            rpc=rpc,
            context=context,
            estimator=estimator,
            registry=registry,
            profile_id=profile_id,
            observation_id=request_id,
            lookups=lookups,
        )
    except (ValueError, RpcError):
        return ResourceDecision(
            observation_id=request_id,
            status="unresolved",
            reason="invalid_message_or_preexecution_evidence",
            full_preparation_ms=(time.perf_counter() - started) * 1000,
        )
    if before_simulation is not None:
        before_simulation(plan)  # Persist prediction BEFORE observing a label, or abort.
    decision = execute_decision(
        plan,
        rpc=rpc,
        shadow=shadow,
        force_simulation=force_simulation,
        registry=registry,
        current_slot=context.current_slot,
    )
    if registry is not None and plan.control_selected:
        sim = decision.simulation
        registry.record_control(
            plan.observation_id,
            success=sim is not None,
            compute_units=sim.units_consumed if sim else None,
            loaded_accounts_bytes=sim.loaded_accounts_bytes if sim else None,
            current_slot=sim.slot if sim else context.current_slot,
            elapsed_ms=sim.elapsed_ms if sim else decision.full_preparation_ms,
        )
    return decision.model_copy(
        update={
            "full_preparation_ms": (time.perf_counter() - started) * 1000,
            "rpc_attempts": rpc.call_count - rpc_attempts_before,
        }
    )
