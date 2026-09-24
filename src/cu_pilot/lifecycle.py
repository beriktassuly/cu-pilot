"""Durable releases, deployment evidence, and preselected control observations.

No network I/O occurs on the eligibility path. A caller schedules the bounded
deployment watcher; a failed or expired watcher cannot certify a deployment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import Field, StrictBool, StrictInt, model_validator

from cu_pilot.parsing import validate_pubkey
from cu_pilot.schemas import MAX_COMPUTE_UNITS, MAX_LOADED_ACCOUNT_BYTES, StrictModel

if TYPE_CHECKING:
    from cu_pilot.resources import ResourceEstimator

UPGRADEABLE_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
IMMUTABLE_LOADERS = frozenset(
    {"BPFLoader2111111111111111111111111111111111", "BPFLoader1111111111111111111111111111111111"}
)
NATIVE_LOADER = "NativeLoader1111111111111111111111111111111"
ProfileState = Literal["candidate", "shadow", "active", "suspended", "retired"]


def _slot(value: int) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("Slot must be an unsigned 64-bit integer")
    return value


def _timestamp(value: float | None) -> float:
    timestamp = time.time() if value is None else value
    if isinstance(timestamp, bool) or not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("Time must be finite and nonnegative")
    return timestamp


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def artifact_digest(payload: str | bytes) -> str:
    """Hash validated portable canonical JSON, ignoring file formatting."""
    from cu_pilot.resources import ResourceArtifact

    model = ResourceArtifact.model_validate_json(payload)
    return hashlib.sha256(_json(model.model_dump(mode="json")).encode()).hexdigest()


class ProfileManifest(StrictModel):
    schema_version: Literal["cu-pilot-lifecycle-v1"] = "cu-pilot-lifecycle-v1"
    profile_id: str = Field(min_length=1, max_length=128)
    revision: StrictInt = Field(gt=0, le=2**53 - 1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context: str = Field(min_length=1)
    cluster_identity: str = Field(min_length=1)
    runtime_identity: str = Field(min_length=1)
    workload_allowlist: tuple[str, ...] = Field(min_length=1)
    deployment_bindings: dict[str, str] = Field(min_length=1)
    dependencies: dict[str, tuple[str, ...]]
    dependency_closure_verified: StrictBool = False
    budget_independent: StrictBool = False
    evidence_min_slot: StrictInt = Field(ge=0, lt=2**64)
    evidence_max_slot: StrictInt = Field(ge=0, lt=2**64)
    provenance: Literal["historical", "simulation", "synthetic"]
    max_observation_age_slots: StrictInt = Field(default=10_000, ge=0, le=2**53 - 1)
    max_deployment_age_slots: StrictInt = Field(default=100, ge=0, le=2**53 - 1)
    max_deployment_age_seconds: float = Field(default=60.0, gt=0, le=3600, allow_inf_nan=False)
    control_probability: float = Field(default=0.01, ge=0, le=1, allow_inf_nan=False)
    max_control_failure_streak: StrictInt = Field(default=3, ge=1, le=2**32 - 1)

    @model_validator(mode="after")
    def validate_bindings(self) -> ProfileManifest:
        if self.evidence_min_slot > self.evidence_max_slot:
            raise ValueError("Evidence range is reversed")
        if set(self.dependencies) != set(self.deployment_bindings):
            raise ValueError("Declare dependencies, including empty closure, for every program")
        for program, dependencies in self.dependencies.items():
            validate_pubkey(program)
            if not set(dependencies) <= set(self.deployment_bindings):
                raise ValueError("Every known invoked dependency must have a deployment binding")
        for fingerprint in self.deployment_bindings.values():
            if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
                raise ValueError("Deployment fingerprints must be SHA-256 hex")
        return self


class DeploymentEvidence(StrictModel):
    program_id: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner: str
    deployment_slot: StrictInt | None = Field(default=None, ge=0, lt=2**64)
    observed_slot: StrictInt = Field(ge=0, lt=2**64)
    checked_at: float = Field(ge=0, allow_inf_nan=False)
    cluster_identity: str
    runtime_identity: str
    programdata_address: str | None = None


class Eligibility(StrictModel):
    eligible: bool
    reason: str
    profile_id: str
    revision: int | None = None
    artifact_sha256: str | None = None
    control_probability: float = 0


class ControlSelection(StrictModel):
    request_id: str = Field(min_length=1)
    profile_id: str
    revision: StrictInt = Field(gt=0)
    decision_version: str = Field(min_length=1)
    eligible: StrictBool
    probability: float = Field(ge=0, le=1)
    selected: StrictBool
    compute_unit_limit: StrictInt = Field(gt=0, le=MAX_COMPUTE_UNITS)
    loaded_accounts_data_size_limit: StrictInt = Field(gt=0, le=MAX_LOADED_ACCOUNT_BYTES)
    selected_slot: StrictInt = Field(ge=0, lt=2**64)


class AccountReader(Protocol):
    """Caller adapter must bound transport timeout/retries and redact errors."""

    def get_multiple_accounts(
        self, addresses: Sequence[str], *, min_context_slot: int, commitment: str
    ) -> dict[str, Any]: ...


def _base58(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = alphabet[digit] + encoded
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + encoded


def _account(value: Any) -> tuple[str, bool, bytes]:
    if not isinstance(value, dict) or type(value.get("executable")) is not bool:
        raise ValueError("Missing or malformed program account")
    owner = validate_pubkey(value.get("owner"))
    data = value.get("data")
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise ValueError("Deployment evidence requires full base64 account data")
    if not isinstance(data[0], str) or len(data[0]) > 24_000_000:
        raise ValueError("Program account data is invalid or too large")
    return owner, value["executable"], base64.b64decode(data[0], validate=True)


def deployment_identity(
    program_id: str,
    account: Any,
    *,
    programdata: Any = None,
    observed_slot: int,
    checked_at: float,
    cluster_identity: str,
    runtime_identity: str,
) -> DeploymentEvidence:
    """Verify loader-v3 pointers/metadata or fingerprint immutable/native programs.

    The v3 Program header is 36 bytes; its ProgramData header is always 45 bytes.
    Unknown loaders (including loader-v4) fail closed until verified support exists.
    """
    validate_pubkey(program_id)
    _slot(observed_slot)
    if not cluster_identity or not runtime_identity:
        raise ValueError("Deployment identity requires cluster and runtime identities")
    owner, executable, data = _account(account)
    if not executable:
        raise ValueError("Program is not executable")
    deployment_slot = None
    programdata_address = None
    payload = data
    if owner == UPGRADEABLE_LOADER:
        if len(data) != 36 or int.from_bytes(data[:4], "little") != 2:
            raise ValueError("Invalid loader-v3 Program state")
        programdata_address = _base58(data[4:36])
        pd_owner, pd_executable, pd_data = _account(programdata)
        if pd_owner != owner or pd_executable or len(pd_data) <= 45:
            raise ValueError("Invalid loader-v3 ProgramData account")
        if int.from_bytes(pd_data[:4], "little") != 3 or pd_data[12] not in (0, 1):
            raise ValueError("Invalid loader-v3 ProgramData state")
        deployment_slot = int.from_bytes(pd_data[4:12], "little")
        if deployment_slot >= observed_slot:
            raise ValueError("Deployment must precede observation (runtime visibility delay)")
        payload += pd_data
    elif owner not in IMMUTABLE_LOADERS and owner != NATIVE_LOADER:
        raise ValueError("Unsupported program loader; simulation required")
    fingerprint = hashlib.sha256(
        _json([program_id, owner, cluster_identity, runtime_identity]).encode() + payload
    ).hexdigest()
    return DeploymentEvidence(
        program_id=program_id,
        fingerprint=fingerprint,
        owner=owner,
        deployment_slot=deployment_slot,
        observed_slot=observed_slot,
        checked_at=checked_at,
        cluster_identity=cluster_identity,
        runtime_identity=runtime_identity,
        programdata_address=programdata_address,
    )


class ProfileRegistry:
    """SQLite snapshots atomically bind immutable artifact bytes and active revisions.

    Connections are operation-local so independent instances and threads can read
    safely while a release commits. Prior revisions and audit events are retained.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported lifecycle database version")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT NOT NULL, revision INTEGER NOT NULL, manifest TEXT NOT NULL,
                    artifact BLOB NOT NULL, state TEXT NOT NULL, quarantine TEXT,
                    suspended_slot TEXT, failure_streak INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS active (id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence (
                    program TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    id TEXT NOT NULL, revision INTEGER, action TEXT NOT NULL,
                    actor TEXT NOT NULL, reason TEXT NOT NULL, slot TEXT);
                CREATE TABLE IF NOT EXISTS controls (
                    request_id TEXT PRIMARY KEY, selection TEXT NOT NULL, outcome TEXT);
                CREATE TABLE IF NOT EXISTS executions (
                    observation_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quarantine (
                    id TEXT NOT NULL, digest TEXT NOT NULL, reason TEXT NOT NULL,
                    slot TEXT NOT NULL, PRIMARY KEY(id,digest));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('force_simulation','false');
                PRAGMA user_version=1;
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        profile_id: str,
        revision: int | None,
        action: str,
        actor: str,
        reason: str,
        slot: int | None = None,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("Operator action requires actor and reason")
        db.execute(
            "INSERT INTO audit(at,id,revision,action,actor,reason,slot) VALUES(?,?,?,?,?,?,?)",
            (
                time.time(),
                profile_id,
                revision,
                action,
                actor,
                reason,
                str(slot) if slot is not None else None,
            ),
        )

    def register(self, manifest: ProfileManifest, artifact: str | bytes, *, actor: str) -> None:
        from cu_pilot.resources import ResourceArtifact

        payload = artifact.encode() if isinstance(artifact, str) else artifact
        if len(payload) > 16 * 1024 * 1024:
            raise ValueError("Artifact exceeds registry size bound")
        model = ResourceArtifact.model_validate_json(payload)
        payload = _json(model.model_dump(mode="json")).encode()
        if artifact_digest(payload) != manifest.artifact_sha256:
            raise ValueError("Artifact digest mismatch")
        if model.context != manifest.context or model.source != manifest.provenance:
            raise ValueError("Artifact context/provenance differs from manifest")
        if model.max_slot != manifest.evidence_max_slot:
            raise ValueError("Manifest cannot manufacture fresh artifact observations")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT manifest,artifact FROM profiles WHERE id=? AND revision=?",
                (manifest.profile_id, manifest.revision),
            ).fetchone()
            if existing:
                if (
                    existing["manifest"] != manifest.model_dump_json()
                    or existing["artifact"] != payload
                ):
                    raise ValueError("Conflicting immutable profile revision")
                return
            suspended = db.execute(
                "SELECT suspended_slot FROM profiles WHERE id=? AND suspended_slot IS NOT NULL",
                (manifest.profile_id,),
            ).fetchall()
            if suspended:
                last_suspension = max(int(row[0]) for row in suspended)
                if (
                    model.calibration_min_slot is None
                    or model.calibration_min_slot <= last_suspension
                ):
                    raise ValueError("Recovery requires new evidence calibrated after suspension")
            db.execute(
                "INSERT INTO profiles(id,revision,manifest,artifact,state) VALUES(?,?,?,?,?)",
                (
                    manifest.profile_id,
                    manifest.revision,
                    manifest.model_dump_json(),
                    payload,
                    "candidate",
                ),
            )
            self._audit(db, manifest.profile_id, manifest.revision, "register", actor, "candidate")

    def transition(
        self,
        profile_id: str,
        revision: int,
        state: Literal["shadow", "retired"],
        *,
        actor: str,
        reason: str,
    ) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._profile(db, profile_id, revision)
            if state == "shadow" and row["state"] != "candidate":
                raise ValueError(
                    "Only candidate revisions enter shadow; recovery needs a new revision"
                )
            if state not in ("shadow", "retired") or row["state"] == "retired":
                raise ValueError("Invalid profile transition")
            db.execute(
                "UPDATE profiles SET state=? WHERE id=? AND revision=?",
                (state, profile_id, revision),
            )
            self._audit(db, profile_id, revision, state, actor, reason)

    @staticmethod
    def _profile(db: sqlite3.Connection, profile_id: str, revision: int) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM profiles WHERE id=? AND revision=?", (profile_id, revision)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown profile revision")
        return cast(sqlite3.Row, row)

    def record_deployments(self, observations: Sequence[DeploymentEvidence]) -> None:
        """Commit one verified watcher batch atomically; never accept older evidence."""
        if not observations:
            raise ValueError("Empty deployment observation batch")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for evidence in observations:
                row = db.execute(
                    "SELECT payload FROM evidence WHERE program=?", (evidence.program_id,)
                ).fetchone()
                if row:
                    old = DeploymentEvidence.model_validate_json(row[0])
                    if (
                        evidence.observed_slot < old.observed_slot
                        or evidence.checked_at < old.checked_at
                    ):
                        raise ValueError("Deployment evidence cannot move backwards")
                db.execute(
                    "INSERT OR REPLACE INTO evidence(program,payload) VALUES(?,?)",
                    (evidence.program_id, evidence.model_dump_json()),
                )
            cached = {row[0] for row in db.execute("SELECT program FROM evidence")}
            if cached <= {item.program_id for item in observations}:
                db.execute("INSERT OR REPLACE INTO settings VALUES('watcher_failure','false')")

    def watcher_failed(self) -> None:
        """Invalidate all cached checks immediately, retaining their forensic evidence."""
        with self._connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('watcher_failure','true')")
            self._audit(db, "*", None, "watcher_failure", "watcher", "deployment_read_failed")

    @staticmethod
    def _environment_reason(
        db: sqlite3.Connection,
        manifest: ProfileManifest,
        *,
        current_slot: int,
        context: str,
        cluster_identity: str,
        runtime_identity: str,
        workload: str,
        program_ids: Sequence[str],
        now: float,
    ) -> str | None:
        if context != manifest.context:
            return "context_mismatch"
        if (
            cluster_identity != manifest.cluster_identity
            or runtime_identity != manifest.runtime_identity
        ):
            return "runtime_or_cluster_mismatch"
        if workload not in manifest.workload_allowlist:
            return "workload_not_allowed"
        if not manifest.dependency_closure_verified:
            return "untracked_dependencies"
        if not manifest.budget_independent:
            return "budget_sensitive_workload"
        if not program_ids or not set(program_ids) <= set(manifest.deployment_bindings):
            return "untracked_program"
        age = current_slot - manifest.evidence_max_slot
        if age < 0 or age > manifest.max_observation_age_slots:
            return "stale_observations"
        failure = db.execute("SELECT value FROM settings WHERE key='watcher_failure'").fetchone()
        if failure and failure[0] == "true":
            return "deployment_watcher_failed"
        for program, expected in manifest.deployment_bindings.items():
            row = db.execute("SELECT payload FROM evidence WHERE program=?", (program,)).fetchone()
            if row is None:
                return "deployment_evidence_missing"
            actual = DeploymentEvidence.model_validate_json(row[0])
            if (
                actual.cluster_identity != cluster_identity
                or actual.runtime_identity != runtime_identity
            ):
                return "deployment_context_mismatch"
            if actual.fingerprint != expected:
                return "deployment_changed"
            slot_age = current_slot - actual.observed_slot
            time_age = now - actual.checked_at
            if (
                slot_age < 0
                or slot_age > manifest.max_deployment_age_slots
                or time_age < 0
                or time_age > manifest.max_deployment_age_seconds
            ):
                return "deployment_evidence_stale"
        return None

    def activate(
        self,
        profile_id: str,
        revision: int,
        *,
        actor: str,
        reason: str,
        current_slot: int,
        context: str,
        cluster_identity: str,
        runtime_identity: str,
        workload: str,
        program_ids: Sequence[str],
        now: float | None = None,
        rollback: bool = False,
    ) -> None:
        """Explicit release or rollback. Never refresh labels or clear quarantine."""
        _slot(current_slot)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._profile(db, profile_id, revision)
            if row["state"] != "shadow" or row["quarantine"] is not None:
                raise ValueError("Activation requires an unquarantined shadow revision")
            manifest = ProfileManifest.model_validate_json(row["manifest"])
            if db.execute(
                "SELECT 1 FROM quarantine WHERE id=? AND digest=?",
                (profile_id, manifest.artifact_sha256),
            ).fetchone():
                raise ValueError(
                    "A quarantined artifact cannot be reactivated under another revision"
                )
            if manifest.provenance == "synthetic":
                raise ValueError("Synthetic evidence cannot release an active profile")
            from cu_pilot.resources import ResourceArtifact, limit_risk

            model = ResourceArtifact.model_validate_json(row["artifact"])
            if not any(
                stats.train_count >= model.policy.min_samples
                and stats.calibration_count >= model.policy.min_calibration_samples
                and stats.calibration_upper_bound is not None
                and stats.calibration_upper_bound <= model.policy.max_joint_underestimation_rate
                and limit_risk(stats, model.policy) is None
                for stats in model.patterns.values()
            ):
                raise ValueError("Artifact has no qualified dual-resource pattern")
            rejected = self._environment_reason(
                db,
                manifest,
                current_slot=current_slot,
                context=context,
                cluster_identity=cluster_identity,
                runtime_identity=runtime_identity,
                workload=workload,
                program_ids=program_ids,
                now=_timestamp(now),
            )
            if rejected:
                raise ValueError(f"Profile release rejected: {rejected}")
            previous = db.execute(
                "SELECT revision FROM active WHERE id=?", (profile_id,)
            ).fetchone()
            if rollback and (previous is None or revision >= previous[0]):
                raise ValueError("Rollback must select an earlier retained revision")
            if previous:
                db.execute(
                    "UPDATE profiles SET state='shadow' "
                    "WHERE id=? AND revision=? AND state='active'",
                    (profile_id, previous[0]),
                )
            db.execute(
                "UPDATE profiles SET state='active' WHERE id=? AND revision=?",
                (profile_id, revision),
            )
            db.execute("INSERT OR REPLACE INTO active VALUES(?,?)", (profile_id, revision))
            self._audit(
                db,
                profile_id,
                revision,
                "rollback" if rollback else "activate",
                actor,
                reason,
                current_slot,
            )

    def rollback(self, profile_id: str, revision: int, **kwargs: Any) -> None:
        self.activate(profile_id, revision, rollback=True, **kwargs)

    @staticmethod
    def _suspend(
        db: sqlite3.Connection, profile_id: str, revision: int, reason: str, slot: int
    ) -> None:
        prior = ProfileRegistry._profile(db, profile_id, revision)
        if prior["suspended_slot"] is not None:
            slot = max(slot, int(prior["suspended_slot"]))
        manifest = ProfileManifest.model_validate_json(prior["manifest"])
        db.execute(
            "INSERT OR IGNORE INTO quarantine(id,digest,reason,slot) VALUES(?,?,?,?)",
            (profile_id, manifest.artifact_sha256, reason, str(slot)),
        )
        db.execute(
            "UPDATE profiles SET state='suspended',quarantine=COALESCE(quarantine,?),"
            "suspended_slot=? "
            "WHERE id=? AND revision=? AND state!='retired'",
            (reason, str(slot), profile_id, revision),
        )
        ProfileRegistry._audit(db, profile_id, revision, "suspend", "policy", reason, slot)

    def suspend(
        self, profile_id: str, revision: int, *, actor: str, reason: str, current_slot: int
    ) -> None:
        _slot(current_slot)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._audit(db, profile_id, revision, "operator_suspend", actor, reason, current_slot)
            self._suspend(db, profile_id, revision, reason, current_slot)

    def check(
        self,
        profile_id: str,
        *,
        current_slot: int,
        context: str,
        cluster_identity: str,
        runtime_identity: str,
        workload: str,
        program_ids: Sequence[str],
        now: float | None = None,
    ) -> Eligibility:
        _slot(current_slot)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            pointer = db.execute("SELECT revision FROM active WHERE id=?", (profile_id,)).fetchone()
            if pointer is None:
                return Eligibility(
                    eligible=False, reason="no_active_profile", profile_id=profile_id
                )
            row = self._profile(db, profile_id, pointer[0])
            manifest = ProfileManifest.model_validate_json(row["manifest"])
            reason = None
            if (
                db.execute("SELECT value FROM settings WHERE key='force_simulation'").fetchone()[0]
                == "true"
            ):
                reason = "force_simulation"
            elif row["state"] != "active" or row["quarantine"]:
                reason = "profile_" + row["state"]
            elif db.execute(
                "SELECT 1 FROM quarantine WHERE id=? AND digest=?",
                (profile_id, manifest.artifact_sha256),
            ).fetchone():
                reason = "artifact_quarantined"
                self._suspend(db, profile_id, manifest.revision, reason, current_slot)
            else:
                reason = self._environment_reason(
                    db,
                    manifest,
                    current_slot=current_slot,
                    context=context,
                    cluster_identity=cluster_identity,
                    runtime_identity=runtime_identity,
                    workload=workload,
                    program_ids=program_ids,
                    now=_timestamp(now),
                )
                if reason in {
                    "stale_observations",
                    "deployment_changed",
                    "deployment_evidence_stale",
                    "deployment_watcher_failed",
                    "deployment_evidence_missing",
                    "deployment_context_mismatch",
                }:
                    self._suspend(db, profile_id, manifest.revision, reason, current_slot)
            return Eligibility(
                eligible=reason is None,
                reason=reason or "active",
                profile_id=profile_id,
                revision=manifest.revision,
                artifact_sha256=manifest.artifact_sha256,
                control_probability=manifest.control_probability,
            )

    def active_snapshot(self, profile_id: str) -> tuple[ProfileManifest, bytes, str]:
        """One consistent read; users must still call check before accepting a decision."""
        with self._connection() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT p.manifest,p.artifact,p.state FROM profiles p JOIN active a "
                "ON p.id=a.id AND p.revision=a.revision WHERE p.id=?",
                (profile_id,),
            ).fetchone()
            if row is None:
                raise ValueError("No active profile")
            manifest = ProfileManifest.model_validate_json(row[0])
            quarantined = db.execute(
                "SELECT 1 FROM quarantine WHERE id=? AND digest=?",
                (profile_id, manifest.artifact_sha256),
            ).fetchone()
            state = "suspended" if quarantined and row[2] != "retired" else str(row[2])
            return manifest, bytes(row[1]), state

    def load_active(self, profile_id: str) -> tuple[ProfileManifest, ResourceEstimator]:
        """Load a validated release snapshot; eligibility still requires check()."""
        from cu_pilot.resources import ResourceArtifact, ResourceEstimator

        manifest, payload, state = self.active_snapshot(profile_id)
        if state != "active" or artifact_digest(payload) != manifest.artifact_sha256:
            raise ValueError("Current profile is inactive or artifact integrity failed")
        return manifest, ResourceEstimator(ResourceArtifact.model_validate_json(payload))

    def export_snapshot(self, profile_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Portable, expiring read snapshot for a local TypeScript process.

        Consumers must enforce snapshot/evidence expiry and refresh this file.
        Suspension propagation is bounded by that expiry, not instantaneous.
        """
        exported_at = _timestamp(now)
        with self._connection() as db:
            db.execute("BEGIN")
            pointer = db.execute("SELECT revision FROM active WHERE id=?", (profile_id,)).fetchone()
            if pointer is None:
                raise ValueError("No active profile")
            row = self._profile(db, profile_id, pointer[0])
            manifest = ProfileManifest.model_validate_json(row["manifest"])
            quarantine = db.execute(
                "SELECT reason FROM quarantine WHERE id=? AND digest=?",
                (profile_id, manifest.artifact_sha256),
            ).fetchone()
            exported_manifest = manifest.model_dump(mode="json")
            for field in ("evidence_min_slot", "evidence_max_slot"):
                exported_manifest[field] = str(exported_manifest[field])
            deployments = []
            for program in manifest.deployment_bindings:
                cached = db.execute(
                    "SELECT payload FROM evidence WHERE program=?", (program,)
                ).fetchone()
                if cached is not None:
                    deployment = json.loads(cached[0])
                    for field in ("deployment_slot", "observed_slot"):
                        if deployment[field] is not None:
                            deployment[field] = str(deployment[field])
                    deployments.append(deployment)
            settings = dict(db.execute("SELECT key,value FROM settings"))
            return dict(
                schema_version="cu-pilot-release-snapshot-v1",
                exported_at=exported_at,
                state="suspended" if quarantine and row["state"] != "retired" else row["state"],
                quarantine=row["quarantine"] or (quarantine[0] if quarantine else None),
                manifest=exported_manifest,
                artifact_canonical_json=bytes(row["artifact"]).decode(),
                deployments=deployments,
                force_simulation=settings.get("force_simulation") == "true",
                watcher_failed=settings.get("watcher_failure") == "true",
            )

    def force_simulation(self, enabled: bool, *, actor: str, reason: str) -> None:
        if type(enabled) is not bool:
            raise ValueError("Emergency switch must be a boolean")
        with self._connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('force_simulation',?)", (_json(enabled),)
            )
            self._audit(db, "*", None, "force_simulation", actor, reason)

    def select_control(
        self,
        *,
        request_id: str,
        profile_id: str,
        revision: int,
        decision_version: str,
        eligible: bool,
        compute_unit_limit: int,
        loaded_accounts_data_size_limit: int,
        current_slot: int,
    ) -> ControlSelection:
        """Persist random selection before the caller invokes the control simulation.

        Existing IDs return the original selection; changed decision inputs conflict.
        Sampling probability is taken from the immutable released manifest.
        """
        _slot(current_slot)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            profile = self._profile(db, profile_id, revision)
            manifest = ProfileManifest.model_validate_json(profile["manifest"])
            probability = manifest.control_probability
            selected = eligible and secrets.randbits(53) / 2**53 < probability
            selection = ControlSelection(
                request_id=request_id,
                profile_id=profile_id,
                revision=revision,
                decision_version=decision_version,
                eligible=eligible,
                probability=probability,
                selected=selected,
                compute_unit_limit=compute_unit_limit,
                loaded_accounts_data_size_limit=loaded_accounts_data_size_limit,
                selected_slot=current_slot,
            )
            existing = db.execute(
                "SELECT selection FROM controls WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                old = ControlSelection.model_validate_json(existing[0])
                if old.model_dump(exclude={"selected"}) != selection.model_dump(
                    exclude={"selected"}
                ):
                    raise ValueError("Conflicting control decision ID")
                return old
            active = db.execute("SELECT revision FROM active WHERE id=?", (profile_id,)).fetchone()
            if eligible and (
                profile["state"] != "active"
                or profile["quarantine"]
                or active is None
                or active[0] != revision
            ):
                raise ValueError("Eligible control decision must refer to the active revision")
            db.execute(
                "INSERT INTO controls(request_id,selection) VALUES(?,?)",
                (request_id, selection.model_dump_json()),
            )
            return selection

    def record_control(
        self,
        request_id: str,
        *,
        success: bool,
        compute_units: int | None,
        loaded_accounts_bytes: int | None,
        current_slot: int,
        elapsed_ms: float,
    ) -> None:
        """Retain failures; only complete successful controls test resource excess."""
        _slot(current_slot)
        if type(success) is not bool or not math.isfinite(elapsed_ms) or elapsed_ms < 0:
            raise ValueError("Invalid control outcome")
        for value in (compute_units, loaded_accounts_bytes):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid control resource measurement")
        outcome = _json(
            dict(
                success=success,
                compute_units=compute_units,
                loaded_accounts_bytes=loaded_accounts_bytes,
                slot=str(current_slot),
                elapsed_ms=elapsed_ms,
            )
        )
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT selection,outcome FROM controls WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Control must be selected before its outcome")
            selection = ControlSelection.model_validate_json(row[0])
            if not selection.selected:
                raise ValueError("Cannot attach a control result to an unselected decision")
            if current_slot < selection.selected_slot:
                raise ValueError("Control outcome predates selection")
            if row[1] is not None:
                if row[1] != outcome:
                    raise ValueError("Conflicting control outcome")
                return
            db.execute("UPDATE controls SET outcome=? WHERE request_id=?", (outcome, request_id))
            profile = self._profile(db, selection.profile_id, selection.revision)
            manifest = ProfileManifest.model_validate_json(profile["manifest"])
            complete = success and compute_units is not None and loaded_accounts_bytes is not None
            streak = 0 if complete else profile["failure_streak"] + 1
            db.execute(
                "UPDATE profiles SET failure_streak=? WHERE id=? AND revision=?",
                (streak, selection.profile_id, selection.revision),
            )
            if success and (
                compute_units is not None
                and compute_units > selection.compute_unit_limit
                or loaded_accounts_bytes is not None
                and loaded_accounts_bytes > selection.loaded_accounts_data_size_limit
            ):
                self._suspend(
                    db,
                    selection.profile_id,
                    selection.revision,
                    "control_resource_excess",
                    current_slot,
                )
            elif streak >= manifest.max_control_failure_streak:
                self._suspend(
                    db,
                    selection.profile_id,
                    selection.revision,
                    "control_deterioration",
                    current_slot,
                )

    def audit_events(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY sequence")]

    def record_execution(
        self,
        profile_id: str,
        revision: int,
        observation_id: str,
        *,
        success: bool,
        compute_units: int | None,
        loaded_accounts_bytes: int | None,
        compute_unit_limit: int,
        loaded_accounts_data_size_limit: int,
        current_slot: int,
    ) -> None:
        """Audit a caller-reconciled execution separately from simulation controls.

        The caller must first verify signature/message correspondence. Missing
        resources remain missing; any measured successful excess suspends the
        exact decision revision. Failed execution usage is never demand evidence.
        """
        _slot(current_slot)
        if not observation_id or type(success) is not bool:
            raise ValueError("Invalid execution observation")
        for value in (compute_units, loaded_accounts_bytes):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid execution resource measurement")
        if (
            type(compute_unit_limit) is not int
            or not 0 < compute_unit_limit <= MAX_COMPUTE_UNITS
            or type(loaded_accounts_data_size_limit) is not int
            or not 0 < loaded_accounts_data_size_limit <= MAX_LOADED_ACCOUNT_BYTES
        ):
            raise ValueError("Invalid decision resource limits")
        payload = _json(
            dict(
                profile_id=profile_id,
                revision=revision,
                success=success,
                compute_units=compute_units,
                loaded_accounts_bytes=loaded_accounts_bytes,
                compute_unit_limit=compute_unit_limit,
                loaded_accounts_data_size_limit=loaded_accounts_data_size_limit,
                slot=str(current_slot),
            )
        )
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._profile(db, profile_id, revision)
            existing = db.execute(
                "SELECT payload FROM executions WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise ValueError("Conflicting execution outcome")
                return
            db.execute("INSERT INTO executions VALUES(?,?)", (observation_id, payload))
            self._audit(
                db,
                profile_id,
                revision,
                "execution",
                "reconciler",
                "execution_observed",
                current_slot,
            )
            if success and (
                compute_units is not None
                and compute_units > compute_unit_limit
                or loaded_accounts_bytes is not None
                and loaded_accounts_bytes > loaded_accounts_data_size_limit
            ):
                self._suspend(db, profile_id, revision, "execution_resource_excess", current_slot)

    def control_records(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            return [
                dict(selection=json.loads(row[0]), outcome=json.loads(row[1]) if row[1] else None)
                for row in db.execute("SELECT selection,outcome FROM controls ORDER BY request_id")
            ]


def refresh_deployments(
    registry: ProfileRegistry,
    client: AccountReader,
    program_ids: Sequence[str],
    *,
    current_slot: int,
    cluster_identity: str,
    runtime_identity: str,
    commitment: Literal["confirmed", "finalized"] = "finalized",
    max_programs: int = 50,
    checked_at: float | None = None,
) -> list[DeploymentEvidence]:
    """Two bounded account batches at most; caller schedules/rate-limits refreshes.

    A v3 batch that crosses slots is rejected rather than claiming one coherent
    snapshot. A later refresh can retry. No caller exception text is persisted.
    """
    _slot(current_slot)
    programs = list(dict.fromkeys(program_ids))
    if not programs or not 1 <= max_programs <= 100 or len(programs) > max_programs:
        raise ValueError("Deployment batch exceeds configured program bound")
    for program in programs:
        validate_pubkey(program)
    if commitment not in ("confirmed", "finalized"):
        raise ValueError("Unsupported deployment commitment")
    try:
        result = client.get_multiple_accounts(
            programs, min_context_slot=current_slot, commitment=commitment
        )
        observed_slot = _slot(result["context"]["slot"])
        values = result["value"]
        if (
            observed_slot < current_slot
            or not isinstance(values, list)
            or len(values) != len(programs)
        ):
            raise ValueError("Invalid deployment account batch")
        pointers: list[str] = []
        for account in values:
            owner, _, data = _account(account)
            if owner == UPGRADEABLE_LOADER:
                if len(data) != 36 or int.from_bytes(data[:4], "little") != 2:
                    raise ValueError("Invalid loader-v3 Program state")
                pointers.append(_base58(data[4:36]))
        programdata: dict[str, Any] = {}
        if pointers:
            second = client.get_multiple_accounts(
                pointers, min_context_slot=observed_slot, commitment=commitment
            )
            if _slot(second["context"]["slot"]) != observed_slot or len(second["value"]) != len(
                pointers
            ):
                raise ValueError("Deployment batches span different slots")
            programdata = dict(zip(pointers, second["value"], strict=True))
        now = _timestamp(checked_at)
        observations = []
        for program, account in zip(programs, values, strict=True):
            owner, _, data = _account(account)
            observations.append(
                deployment_identity(
                    program,
                    account,
                    programdata=programdata.get(_base58(data[4:36]))
                    if owner == UPGRADEABLE_LOADER
                    else None,
                    observed_slot=observed_slot,
                    checked_at=now,
                    cluster_identity=cluster_identity,
                    runtime_identity=runtime_identity,
                )
            )
        registry.record_deployments(observations)
        return observations
    except Exception:
        registry.watcher_failed()
        raise ValueError("Deployment refresh failed; cached evidence invalidated") from None


def runtime_identity_from_version(version: dict[str, Any]) -> str:
    """Build identity, not proof that every runtime feature activation is unchanged."""
    core = version.get("solana-core")
    features = version.get("feature-set")
    if (
        not isinstance(core, str)
        or not core
        or len(core) > 100
        or type(features) is not int
        or not 0 <= features < 2**32
    ):
        raise ValueError("RPC version lacks a supported runtime build identity")
    return f"rpc:{core}:feature-set:{features}"
