import { randomUUID, randomInt } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import {
  type Rpc,
  type SolanaRpcApi,
  type Base64EncodedWireTransaction,
} from "@solana/kit";
import {
  bindMessage,
  prepareResources,
  sha256,
  canonical,
  MAX_CU,
  MAX_DATA,
  type BuilderMessage,
  type LookupEvidence,
  type BoundMessage,
  decodeBuilder,
} from "./message.js";
import {
  loadArtifact,
  predictResources,
  roundedLimit,
  slot,
  type ResourceArtifact,
  type Prediction,
} from "./policy.js";
import type { ControlStore } from "./control-store.js";

export type ReleaseSnapshot = {
  schema_version: "cu-pilot-release-snapshot-v1";
  exported_at: number;
  state: string;
  quarantine: string | null;
  force_simulation: boolean;
  watcher_failed: boolean;
  artifact_canonical_json?: string;
  manifest: {
    schema_version: string;
    profile_id: string;
    revision: number;
    artifact_sha256: string;
    context: string;
    cluster_identity: string;
    runtime_identity: string;
    workload_allowlist: string[];
    deployment_bindings: Record<string, string>;
    dependencies: Record<string, string[]>;
    dependency_closure_verified: boolean;
    budget_independent: boolean;
    evidence_min_slot: string;
    evidence_max_slot: string;
    max_observation_age_slots: number;
    max_deployment_age_slots: number;
    max_deployment_age_seconds: number;
    control_probability: number;
  };
  deployments: {
    program_id: string;
    fingerprint: string;
    owner: string;
    deployment_slot: string | null;
    observed_slot: string;
    checked_at: number;
    cluster_identity: string;
    runtime_identity: string;
  }[];
};
export type RuntimeContext = {
  context: string;
  cluster: string;
  runtime: string;
  workload: string;
  currentSlot: bigint;
  budgetIndependent?: boolean;
};
export function releaseRisk(
  release: ReleaseSnapshot | undefined,
  artifact: ResourceArtifact,
  bound: BoundMessage,
  context: RuntimeContext,
  now = Date.now() / 1000,
): string | null {
  if (!release) return "missing_release";
  const m = release.manifest;
  if (
    !Number.isFinite(now) ||
    typeof release.force_simulation !== "boolean" ||
    typeof release.watcher_failed !== "boolean"
  )
    return "invalid_release_policy";
  if (
    artifact.source === "synthetic" ||
    artifact.evidence_origin === "synthetic"
  )
    return "synthetic_release_forbidden";
  if (
    !m ||
    ![
      m.max_observation_age_slots,
      m.max_deployment_age_slots,
      m.max_deployment_age_seconds,
    ].every((n) => Number.isSafeInteger(n) && n >= 0) ||
    !Number.isFinite(m.control_probability) ||
    m.control_probability < 0 ||
    m.control_probability > 1
  )
    return "invalid_release_policy";
  if (
    release.schema_version !== "cu-pilot-release-snapshot-v1" ||
    m.schema_version !== "cu-pilot-lifecycle-v1"
  )
    return "incompatible_release";
  if (release.force_simulation) return "force_simulation";
  if (release.state !== "active" || release.quarantine !== null)
    return "profile_inactive";
  if (release.watcher_failed) return "deployment_watcher_failed";
  if (
    !release.artifact_canonical_json ||
    sha256(release.artifact_canonical_json) !== m.artifact_sha256 ||
    canonical(loadArtifact(JSON.parse(release.artifact_canonical_json))) !==
      canonical(artifact)
  )
    return "artifact_mismatch";
  if (
    m.context !== context.context ||
    m.cluster_identity !== context.cluster ||
    m.runtime_identity !== context.runtime
  )
    return "deployment_context_mismatch";
  if (!m.workload_allowlist.includes(context.workload))
    return "workload_not_allowed";
  if (m.dependency_closure_verified !== true || m.budget_independent !== true)
    return "unsupported_workload";
  if (
    !Number.isFinite(release.exported_at) ||
    release.exported_at > now ||
    now - release.exported_at > m.max_deployment_age_seconds
  )
    return "stale_release_snapshot";
  if (
    context.currentSlot < slot(m.evidence_max_slot) ||
    context.currentSlot - slot(m.evidence_max_slot) >
      BigInt(m.max_observation_age_slots)
  )
    return "stale_profile_evidence";
  if (
    slot(m.evidence_max_slot) !== slot(artifact.max_slot) ||
    slot(m.evidence_min_slot) > slot(m.evidence_max_slot)
  )
    return "artifact_evidence_mismatch";
  if (
    canonical(Object.keys(m.dependencies).sort()) !==
    canonical(Object.keys(m.deployment_bindings).sort())
  )
    return "untracked_dependency";
  for (const dependencies of Object.values(m.dependencies))
    if (
      !Array.isArray(dependencies) ||
      dependencies.some((p) => !(p in m.deployment_bindings))
    )
      return "untracked_dependency";
  const pending = [...bound.features.program_ids],
    seen = new Set<string>();
  while (pending.length) {
    const p = pending.pop()!;
    if (seen.has(p)) continue;
    seen.add(p);
    if (!(p in m.deployment_bindings) || !(p in m.dependencies))
      return "untracked_dependency";
    pending.push(...m.dependencies[p]!);
  }
  for (const p of Object.keys(m.deployment_bindings)) {
    const d = release.deployments.find((d) => d.program_id === p);
    if (
      !d ||
      d.fingerprint !== m.deployment_bindings[p] ||
      d.cluster_identity !== context.cluster ||
      d.runtime_identity !== context.runtime
    )
      return "deployment_changed";
    if (
      d.deployment_slot !== null &&
      (artifact.calibration_min_slot === null ||
        slot(d.deployment_slot) >= slot(artifact.calibration_min_slot))
    )
      return "deployment_not_covered_by_calibration";
    if (
      !Number.isFinite(d.checked_at) ||
      d.checked_at > now ||
      now - d.checked_at > m.max_deployment_age_seconds ||
      context.currentSlot < slot(d.observed_slot) ||
      context.currentSlot - slot(d.observed_slot) >
        BigInt(m.max_deployment_age_slots)
    )
      return "stale_deployment_evidence";
  }
  return null;
}
export type EstimateOptions = RuntimeContext & {
  rpc: Pick<Rpc<SolanaRpcApi>, "simulateTransaction">;
  artifact?: ResourceArtifact;
  release?: ReleaseSnapshot;
  lookup?: LookupEvidence;
  maxLookupAgeSlots?: bigint;
  expectedWire?: Uint8Array;
  forceSimulation?: boolean;
  shadow?: boolean;
  commitment?: "processed" | "confirmed" | "finalized";
  timeoutMs?: number;
  maxAttempts?: number;
  abortSignal?: AbortSignal;
  controlDraw?: number;
  controlStore?: ControlStore;
  onControl?: (event: ControlObservation) => void | Promise<void>;
};
export type SimulationObservation = {
  status: "success" | "failed";
  reason: string;
  slot: string | null;
  computeUnits: number | null;
  loadedAccountsBytes: number | null;
  elapsedMs: number;
  attempts: number;
};
export type ControlObservation = {
  observationId: string;
  decisionMessageIdentity: string;
  profileId: string | null;
  profileRevision: number | null;
  artifactDigest: string | null;
  selectedBeforeOutcome: true;
  probability: number;
  prediction: Prediction;
  simulation: SimulationObservation;
  resourceExcess: boolean;
};
export type ResourceDecision = {
  observationId: string;
  status: "prediction" | "simulation" | "unresolved";
  reason: string;
  prediction: Prediction | null;
  profileId: string | null;
  profileRevision: number | null;
  modelVersion: string | null;
  artifactDigest: string | null;
  context: RuntimeContext;
  original: BoundMessage | null;
  prepared: BoundMessage | null;
  final: BoundMessage | null;
  unsignedMessage: BuilderMessage | null;
  limits: { computeUnits: number; loadedAccountsBytes: number } | null;
  observationSlot: string | null;
  timings: { totalMs: number; simulationMs: number };
  simulation: SimulationObservation | null;
  controlSelected: boolean;
  controlProbability: number;
  provenance: "local-profile" | "simulation" | "unresolved";
  preflightPolicy: "caller-owned";
};

function retryable(error: unknown): boolean {
  const e = error as {
    name?: string;
    context?: { __code?: number; statusCode?: number };
    cause?: { code?: string };
  };
  return (
    e.name === "TimeoutError" ||
    e.context?.__code === -32005 ||
    e.context?.__code === -32016 ||
    (e.context?.__code === 8100002 &&
      [408, 429, 500, 502, 503, 504].includes(e.context.statusCode ?? 0)) ||
    ["ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN"].includes(
      e.cause?.code ?? "",
    )
  );
}
async function simulate(
  bound: BoundMessage,
  options: EstimateOptions,
): Promise<SimulationObservation> {
  const start = performance.now(),
    max = options.maxAttempts ?? 3,
    timeout = options.timeoutMs ?? 10000;
  if (
    !Number.isInteger(max) ||
    max < 1 ||
    max > 5 ||
    !Number.isInteger(timeout) ||
    timeout < 1 ||
    timeout > 120000
  )
    throw new Error("invalid_rpc_policy");
  for (let attempt = 1; attempt <= max; attempt++) {
    if (options.abortSignal?.aborted)
      return {
        status: "failed",
        reason: "cancelled",
        slot: null,
        computeUnits: null,
        loadedAccountsBytes: null,
        elapsedMs: performance.now() - start,
        attempts: attempt - 1,
      };
    const timer = AbortSignal.timeout(timeout),
      signal = options.abortSignal
        ? AbortSignal.any([options.abortSignal, timer])
        : timer;
    try {
      const response = await options.rpc
        .simulateTransaction(bound.wireBase64 as Base64EncodedWireTransaction, {
          encoding: "base64",
          sigVerify: false,
          replaceRecentBlockhash: false,
          commitment: options.commitment ?? "confirmed",
          minContextSlot: options.currentSlot,
        })
        .send({ abortSignal: signal });
      const value = response.value;
      const observed = response.context.slot;
      if (
        typeof observed !== "bigint" ||
        observed < 0n ||
        observed >= 1n << 64n
      )
        return {
          status: "failed",
          reason: "invalid_simulation_context",
          slot: null,
          computeUnits: null,
          loadedAccountsBytes: null,
          elapsedMs: performance.now() - start,
          attempts: attempt,
        };
      const base = {
        slot: observed.toString(),
        elapsedMs: performance.now() - start,
        attempts: attempt,
      };
      if (observed < options.currentSlot)
        return {
          status: "failed",
          reason: "stale_simulation_context",
          computeUnits: null,
          loadedAccountsBytes: null,
          ...base,
        };
      if (value.err !== null)
        return {
          status: "failed",
          reason: "transaction_error",
          computeUnits: null,
          loadedAccountsBytes: null,
          ...base,
        };
      const cu = value.unitsConsumed,
        data = value.loadedAccountsDataSize;
      if (
        typeof cu !== "bigint" ||
        cu < 0n ||
        cu > BigInt(MAX_CU) ||
        typeof data !== "number" ||
        !Number.isSafeInteger(data) ||
        data < 0 ||
        data > MAX_DATA
      )
        return {
          status: "failed",
          reason: "missing_or_invalid_measurement",
          computeUnits: null,
          loadedAccountsBytes: null,
          ...base,
        };
      return {
        status: "success",
        reason: "measured_resources",
        computeUnits: Number(cu),
        loadedAccountsBytes: data,
        ...base,
      };
    } catch (error) {
      if (options.abortSignal?.aborted)
        return {
          status: "failed",
          reason: "cancelled",
          slot: null,
          computeUnits: null,
          loadedAccountsBytes: null,
          elapsedMs: performance.now() - start,
          attempts: attempt,
        };
      if (attempt === max || (!retryable(error) && !timer.aborted))
        return {
          status: "failed",
          reason: timer.aborted ? "simulation_timeout" : "simulation_rpc_error",
          slot: null,
          computeUnits: null,
          loadedAccountsBytes: null,
          elapsedMs: performance.now() - start,
          attempts: attempt,
        };
      try {
        await delay(Math.min(100 * 2 ** (attempt - 1), 1000), undefined, {
          signal: options.abortSignal,
        });
      } catch {
        return {
          status: "failed",
          reason: "cancelled",
          slot: null,
          computeUnits: null,
          loadedAccountsBytes: null,
          elapsedMs: performance.now() - start,
          attempts: attempt,
        };
      }
    }
  }
  throw new Error("unreachable");
}

/** No signing, sending, prediction service, or implicit preflight changes. */
export async function estimateResources(
  message: BuilderMessage,
  options: EstimateOptions,
): Promise<ResourceDecision> {
  const start = performance.now();
  const liveRelease = options.release;
  const d: ResourceDecision = {
    observationId: randomUUID(),
    status: "unresolved",
    reason: "unresolved",
    prediction: null,
    profileId: options.release?.manifest.profile_id ?? null,
    profileRevision: options.release?.manifest.revision ?? null,
    modelVersion: options.artifact?.artifact_version ?? null,
    artifactDigest: options.release?.manifest.artifact_sha256 ?? null,
    context: {
      context: options.context,
      cluster: options.cluster,
      runtime: options.runtime,
      workload: options.workload,
      currentSlot: options.currentSlot,
    },
    original: null,
    prepared: null,
    final: null,
    unsignedMessage: null,
    limits: null,
    observationSlot: null,
    timings: { totalMs: 0, simulationMs: 0 },
    simulation: null,
    controlSelected: false,
    controlProbability: 0,
    provenance: "unresolved",
    preflightPolicy: "caller-owned",
  };
  try {
    // Snapshot mutable evidence and caller options before any asynchronous work.
    options = {
      ...options,
      lookup: options.lookup ? structuredClone(options.lookup) : undefined,
      release: options.release ? structuredClone(options.release) : undefined,
    };
    if (
      typeof options.currentSlot !== "bigint" ||
      options.currentSlot < 0n ||
      options.currentSlot >= 2n ** 64n
    )
      throw new Error("invalid_current_slot");
    d.original = bindMessage(message, options.lookup, options.expectedWire);
    if (
      d.original.features.lookup_table_count &&
      (!options.lookup ||
        typeof options.lookup.checkedSlot !== "bigint" ||
        options.lookup.checkedSlot < 0n ||
        options.lookup.checkedSlot >= 2n ** 64n ||
        (options.maxLookupAgeSlots !== undefined &&
          (typeof options.maxLookupAgeSlots !== "bigint" ||
            options.maxLookupAgeSlots < 0n)) ||
        options.lookup.cluster !== options.cluster ||
        options.lookup.checkedSlot > options.currentSlot ||
        options.currentSlot - options.lookup.checkedSlot >
          (options.maxLookupAgeSlots ?? 32n))
    )
      throw new Error("stale_lookup_evidence");
    const originalSnapshot = decodeBuilder(
      Buffer.from(d.original.wireBase64, "base64"),
      options.lookup,
    );
    // Lifetime metadata is not encoded on wire; preserve the caller's original
    // last-valid height/nonce contract without retaining a mutable reference.
    const snapshot = {
      ...originalSnapshot,
      lifetimeConstraint: structuredClone(message.lifetimeConstraint),
    } as BuilderMessage;
    const prepared = prepareResources(snapshot);
    d.prepared = bindMessage(prepared, options.lookup);
    let reason = "missing_artifact";
    let artifact: ResourceArtifact | undefined;
    if (options.artifact) {
      artifact = loadArtifact(options.artifact);
      d.prediction = predictResources(
        artifact,
        d.prepared.features,
        options.context,
        options.currentSlot,
      );
      reason =
        releaseRisk(options.release, artifact, d.prepared, options) ??
        d.prediction.reason;
    }
    if (reason === "calibrated_resources" && !options.controlStore)
      reason = "control_store_required";
    if (
      options.release &&
      options.controlStore?.isSuspended(
        options.release.manifest.profile_id,
        options.release.manifest.revision,
        options.release.manifest.artifact_sha256,
      )
    )
      reason = "profile_quarantined";
    if (options.budgetIndependent !== true)
      reason = "budget_sensitive_workload";
    let eligible =
      reason === "calibrated_resources" && !options.forceSimulation;
    d.controlProbability = eligible
      ? (options.release?.manifest.control_probability ?? 0)
      : 0;
    if (
      !Number.isFinite(d.controlProbability) ||
      d.controlProbability < 0 ||
      d.controlProbability > 1
    )
      throw new Error("invalid_control_policy");
    const draw = options.controlDraw ?? randomInt(0, 2 ** 32) / 2 ** 32;
    if (draw < 0 || draw >= 1 || !Number.isFinite(draw))
      throw new Error("invalid_control_draw");
    d.controlSelected = eligible && draw < d.controlProbability;
    d.reason = reason;
    if (eligible) {
      await options.controlStore!.recordDecision(structuredClone(d));
      const changed = canonical(liveRelease) !== canonical(options.release);
      const risk = changed
        ? "release_changed_during_preparation"
        : releaseRisk(options.release, artifact!, d.prepared, options);
      const suspended = options.controlStore!.isSuspended(
        d.profileId!,
        d.profileRevision!,
        d.artifactDigest!,
      );
      if (risk || suspended) {
        eligible = false;
        reason = suspended ? "profile_quarantined" : risk!;
        d.reason = reason;
      }
    }
    if (eligible && !options.shadow && !d.controlSelected) {
      d.status = "prediction";
      d.observationSlot = options.currentSlot.toString();
      d.reason = "calibrated_resources";
      d.provenance = "local-profile";
      d.limits = {
        computeUnits: d.prediction!.compute_unit_limit!,
        loadedAccountsBytes: d.prediction!.loaded_accounts_data_size_limit!,
      };
    } else {
      d.simulation = await simulate(d.prepared, options);
      d.timings.simulationMs = d.simulation.elapsedMs;
      d.observationSlot = d.simulation.slot;
      if (d.controlSelected && d.prediction) {
        const event: ControlObservation = {
          observationId: d.observationId,
          decisionMessageIdentity: d.prepared.messageIdentity,
          profileId: d.profileId,
          profileRevision: d.profileRevision,
          artifactDigest: d.artifactDigest,
          selectedBeforeOutcome: true,
          probability: d.controlProbability,
          prediction: d.prediction,
          simulation: d.simulation,
          resourceExcess:
            d.simulation.status === "success" &&
            (d.simulation.computeUnits! > d.prediction.compute_unit_limit! ||
              d.simulation.loadedAccountsBytes! >
                d.prediction.loaded_accounts_data_size_limit!),
        };
        await options.controlStore!.recordOutcome(structuredClone(event));
        await options.onControl?.(structuredClone(event));
      }
      if (d.simulation.status !== "success") {
        d.reason = d.simulation.reason;
        return d;
      }
      const cu = roundedLimit(d.simulation.computeUnits!, 1000, 100),
        data = roundedLimit(d.simulation.loadedAccountsBytes!, 1000, 32768);
      if (cu > MAX_CU || data > MAX_DATA) {
        d.reason = "required_limit_exceeds_cap";
        return d;
      }
      d.status = "simulation";
      d.reason = options.shadow
        ? "shadow_simulation"
        : d.controlSelected
          ? "control_simulation"
          : options.forceSimulation
            ? "force_simulation"
            : reason;
      d.provenance = "simulation";
      d.limits = { computeUnits: cu, loadedAccountsBytes: data };
      if (options.budgetIndependent !== true)
        d.limits = { computeUnits: MAX_CU, loadedAccountsBytes: MAX_DATA };
    }
    d.unsignedMessage = prepareResources(
      {
        ...decodeBuilder(
          Buffer.from(d.prepared.wireBase64, "base64"),
          options.lookup,
        ),
        lifetimeConstraint: snapshot.lifetimeConstraint,
      } as BuilderMessage,
      d.limits!.computeUnits,
      d.limits!.loadedAccountsBytes,
    );
    d.final = bindMessage(d.unsignedMessage, options.lookup);
    return d;
  } catch (error) {
    const known = new Set([
      "message_wire_mismatch",
      "stale_lookup_evidence",
      "invalid_current_slot",
      "invalid_resource_configuration",
      "incompatible_artifact",
      "invalid_rpc_policy",
      "invalid_control_policy",
      "invalid_control_draw",
      "missing_lookup_evidence",
    ]);
    d.status = "unresolved";
    d.reason =
      error instanceof Error && known.has(error.message)
        ? error.message
        : "invalid_input_or_artifact";
    d.limits = null;
    d.unsignedMessage = null;
    d.final = null;
    return d;
  } finally {
    d.timings.totalMs = performance.now() - start;
  }
}
