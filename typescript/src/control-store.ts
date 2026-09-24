import { createHash, randomUUID } from "node:crypto";
import {
  appendFileSync,
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
} from "node:fs";
import { dirname } from "node:path";
import { canonical, MAX_CU, MAX_DATA, type BoundMessage } from "./message.js";
import {
  loadArtifact,
  predictResources,
  slot,
  type ResourceArtifact,
} from "./policy.js";
import {
  releaseRisk,
  type ControlObservation,
  type ResourceDecision,
  type ReleaseSnapshot,
  type RuntimeContext,
} from "./runtime.js";

export interface ControlStore {
  isSuspended(
    profileId: string,
    revision?: number,
    artifactDigest?: string,
  ): boolean;
  recordDecision(decision: ResourceDecision): void | Promise<void>;
  recordOutcome(observation: ControlObservation): void | Promise<void>;
}
export type RecoveryRequest = {
  release: ReleaseSnapshot;
  artifact: ResourceArtifact;
  bound: BoundMessage;
  context: RuntimeContext;
  actor: string;
  reason: string;
  now?: number;
};
type RecoveryAudit = Omit<RecoveryRequest, "artifact" | "now"> & {
  recoveryId: string;
  checkedAt: number;
};
type Quarantine = {
  profileId: string;
  revision: number;
  digest: string;
  rejectedSlot: string;
};
type JournalRecord = {
  schema_version: string;
  kind: "decision" | "outcome" | "recovery";
  payload: ResourceDecision | ControlObservation | RecoveryAudit;
};
const digestValid = (value: unknown): value is string =>
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const exactSlot = (value: unknown) =>
  slot(typeof value === "bigint" ? value.toString() : value);
const releaseKey = (profile: string, revision: number, digest: string) =>
  canonical([profile, revision, digest]);

/** Single-process append-only audit log. Invalid records never reach the journal. */
export class FileControlStore implements ControlStore {
  private readonly decisions = new Map<string, string>();
  private readonly outcomes = new Map<string, string>();
  private readonly recoveries = new Map<string, string>();
  private readonly quarantines = new Map<string, Quarantine>();
  private readonly recovered = new Map<string, string>();
  private readonly failureStreak = new Map<string, number>();
  private readonly failurePolicies = new Map<string, number>();
  private consumedBytes = 0;
  private prefixDigest = createHash("sha256").digest("hex");
  constructor(
    private readonly path: string,
    private readonly expectedMaxFailureStreak?: number,
  ) {
    if (
      expectedMaxFailureStreak !== undefined &&
      (!Number.isSafeInteger(expectedMaxFailureStreak) ||
        expectedMaxFailureStreak < 1 ||
        expectedMaxFailureStreak > 2 ** 32 - 1)
    )
      throw new Error("invalid_control_failure_policy");
    mkdirSync(dirname(path), { recursive: true });
    this.refresh();
  }
  private refresh() {
    if (!existsSync(this.path)) {
      if (this.consumedBytes) throw new Error("changed_control_journal");
      return;
    }
    const source = readFileSync(this.path);
    // Synchronous refresh and append serialize instances in this process. Verify
    // the previously consumed prefix too: truncation or edits cannot erase an
    // already observed quarantine. Multiple writer processes require locking and
    // are deliberately unsupported by this small local store.
    if (
      source.length < this.consumedBytes ||
      createHash("sha256")
        .update(source.subarray(0, this.consumedBytes))
        .digest("hex") !== this.prefixDigest
    )
      throw new Error("changed_control_journal");
    if (source.length === this.consumedBytes) return;
    if (source[source.length - 1] !== 10)
      throw new Error("incomplete_control_journal");
    const suffix = source.subarray(this.consumedBytes).toString("utf8");
    for (const line of suffix.split("\n").filter(Boolean))
      this.ingest(JSON.parse(line));
    this.consumedBytes = source.length;
    this.prefixDigest = createHash("sha256").update(source).digest("hex");
  }
  private mapFor(kind: JournalRecord["kind"]) {
    if (kind === "decision") return this.decisions;
    if (kind === "outcome") return this.outcomes;
    if (kind === "recovery") return this.recoveries;
    throw new Error("invalid_control_record");
  }
  private validateDecision(decision: ResourceDecision) {
    if (
      !decision.profileId ||
      !Number.isSafeInteger(decision.profileRevision) ||
      decision.profileRevision! < 1 ||
      !digestValid(decision.artifactDigest) ||
      !decision.prepared?.messageIdentity ||
      !decision.prediction ||
      decision.prediction.simulation_recommended !== false ||
      decision.reason !== "calibrated_resources" ||
      typeof decision.controlSelected !== "boolean" ||
      !Number.isFinite(decision.controlProbability) ||
      decision.controlProbability < 0 ||
      decision.controlProbability > 1 ||
      (decision.controlSelected && decision.controlProbability === 0)
    )
      throw new Error("invalid_control_decision");
    this.validateFailurePolicy(
      decision.profileId!,
      decision.profileRevision!,
      decision.artifactDigest!,
      decision.maxControlFailureStreak,
    );
    exactSlot(decision.context.currentSlot);
    const cu = decision.prediction.compute_unit_limit,
      data = decision.prediction.loaded_accounts_data_size_limit;
    if (
      !Number.isSafeInteger(cu) ||
      cu! <= 0 ||
      cu! > MAX_CU ||
      !Number.isSafeInteger(data) ||
      data! <= 0 ||
      data! > MAX_DATA
    )
      throw new Error("invalid_control_decision");
  }
  private validateFailurePolicy(
    profile: string,
    revision: number,
    digest: string,
    threshold: number | null,
  ) {
    const previous = this.failurePolicies.get(
      releaseKey(profile, revision, digest),
    );
    if (
      !Number.isSafeInteger(threshold) ||
      threshold! < 1 ||
      threshold! > 2 ** 32 - 1 ||
      (this.expectedMaxFailureStreak !== undefined &&
        threshold !== this.expectedMaxFailureStreak) ||
      (previous !== undefined && threshold !== previous)
    )
      throw new Error("incompatible_control_failure_policy");
  }
  private validateOutcome(outcome: ControlObservation): ResourceDecision {
    const frozen = this.decisions.get(outcome.observationId);
    if (!frozen) throw new Error("control_without_frozen_decision");
    const decision = JSON.parse(frozen) as ResourceDecision;
    if (
      !decision.controlSelected ||
      outcome.selectedBeforeOutcome !== true ||
      outcome.profileId !== decision.profileId ||
      outcome.profileRevision !== decision.profileRevision ||
      outcome.artifactDigest !== decision.artifactDigest ||
      outcome.probability !== decision.controlProbability ||
      outcome.decisionMessageIdentity !== decision.prepared!.messageIdentity ||
      canonical(outcome.prediction) !== canonical(decision.prediction)
    )
      throw new Error("control_outcome_decision_mismatch");
    const sim = outcome.simulation;
    if (
      !sim ||
      !["success", "incomplete", "failed"].includes(sim.status) ||
      !Number.isFinite(sim.elapsedMs) ||
      sim.elapsedMs < 0 ||
      !Number.isSafeInteger(sim.attempts) ||
      sim.attempts < 0
    )
      throw new Error("invalid_control_outcome");
    if (
      sim.slot !== null &&
      exactSlot(sim.slot) < exactSlot(decision.context.currentSlot)
    )
      throw new Error("control_outcome_predates_decision");
    const validMeasurement = (value: number | null, limit: number) =>
      value === null ||
      (Number.isSafeInteger(value) && value >= 0 && value <= limit);
    if (
      !validMeasurement(sim.computeUnits, MAX_CU) ||
      !validMeasurement(sim.loadedAccountsBytes, MAX_DATA) ||
      (sim.status !== "failed" && sim.slot === null) ||
      (sim.status === "success" &&
        (sim.computeUnits === null || sim.loadedAccountsBytes === null)) ||
      (sim.status === "incomplete" &&
        sim.computeUnits !== null &&
        sim.loadedAccountsBytes !== null) ||
      (sim.status === "failed" &&
        (sim.computeUnits !== null || sim.loadedAccountsBytes !== null))
    )
      throw new Error("invalid_control_outcome");
    const excess =
      sim.status !== "failed" &&
      ((sim.computeUnits !== null &&
        sim.computeUnits > decision.prediction!.compute_unit_limit!) ||
        (sim.loadedAccountsBytes !== null &&
          sim.loadedAccountsBytes >
            decision.prediction!.loaded_accounts_data_size_limit!));
    if (outcome.resourceExcess !== excess)
      throw new Error("incorrect_control_excess");
    return decision;
  }
  private validateRecovery(recovery: RecoveryAudit) {
    if (
      typeof recovery.actor !== "string" ||
      !recovery.actor.trim() ||
      typeof recovery.reason !== "string" ||
      !recovery.reason.trim() ||
      !Number.isFinite(recovery.checkedAt) ||
      recovery.checkedAt < 0
    )
      throw new Error("invalid_recovery_audit");
    const release = recovery.release,
      m = release.manifest;
    const artifact = loadArtifact(
      JSON.parse(release.artifact_canonical_json ?? "null"),
    );
    const context = {
      ...recovery.context,
      currentSlot: exactSlot(recovery.context.currentSlot),
    };
    const risk = releaseRisk(
      release,
      artifact,
      recovery.bound,
      context,
      recovery.checkedAt,
    );
    if (risk || context.budgetIndependent !== true)
      throw new Error("recovery_" + (risk ?? "unsupported_workload"));
    this.validateFailurePolicy(
      m.profile_id,
      m.revision,
      m.artifact_sha256,
      m.max_control_failure_streak,
    );
    if (
      !Number.isSafeInteger(m.revision) ||
      m.revision < 1 ||
      !digestValid(m.artifact_sha256)
    )
      throw new Error("invalid_recovery_release");
    const previous = [...this.quarantines.values()].filter(
      (q) => q.profileId === m.profile_id,
    );
    if (!previous.length) throw new Error("recovery_requires_quarantine");
    if (
      previous.some(
        (q) => m.revision <= q.revision || m.artifact_sha256 === q.digest,
      )
    )
      throw new Error("recovery_requires_new_revision_and_artifact");
    if (
      artifact.calibration_min_slot === null ||
      previous.some(
        (q) => slot(artifact.calibration_min_slot!) <= slot(q.rejectedSlot),
      )
    )
      throw new Error("recovery_requires_fresh_calibration");
    const prediction = predictResources(
      artifact,
      recovery.bound.features,
      context.context,
      context.currentSlot,
    );
    if (prediction.simulation_recommended)
      throw new Error("recovery_profile_unqualified");
    return releaseKey(m.profile_id, m.revision, m.artifact_sha256);
  }
  private ingest(record: JournalRecord, apply = true) {
    if (record?.schema_version !== "cu-pilot-controls-v2")
      throw new Error("incompatible_control_journal");
    const payload = record.payload;
    const id =
      record.kind === "recovery"
        ? (payload as RecoveryAudit)?.recoveryId
        : (payload as ResourceDecision | ControlObservation)?.observationId;
    if (typeof id !== "string" || !id)
      throw new Error("invalid_control_record");
    const map = this.mapFor(record.kind),
      encoded = canonical(payload),
      previous = map.get(id);
    if (previous) {
      if (previous !== encoded) throw new Error("conflicting_control_record");
      return;
    }
    let decision: ResourceDecision | undefined, recoveryKey: string | undefined;
    if (record.kind === "decision")
      this.validateDecision(payload as ResourceDecision);
    else if (record.kind === "outcome")
      decision = this.validateOutcome(payload as ControlObservation);
    else recoveryKey = this.validateRecovery(payload as RecoveryAudit);
    if (!apply) return;
    map.set(id, encoded);
    if (record.kind === "recovery") {
      const recovery = payload as RecoveryAudit;
      this.recovered.set(recovery.release.manifest.profile_id, recoveryKey!);
      this.failurePolicies.set(
        recoveryKey!,
        recovery.release.manifest.max_control_failure_streak,
      );
    } else if (record.kind === "decision") {
      const frozen = payload as ResourceDecision;
      this.failurePolicies.set(
        releaseKey(
          frozen.profileId!,
          frozen.profileRevision!,
          frozen.artifactDigest!,
        ),
        frozen.maxControlFailureStreak!,
      );
    } else if (decision) {
      const outcome = payload as ControlObservation;
      const key = releaseKey(
        decision.profileId!,
        decision.profileRevision!,
        decision.artifactDigest!,
      );
      const streak =
        outcome.simulation.status !== "success"
          ? (this.failureStreak.get(key) ?? 0) + 1
          : 0;
      this.failureStreak.set(key, streak);
      if (
        outcome.resourceExcess ||
        streak >= decision.maxControlFailureStreak!
      ) {
        const rejectedSlot =
          outcome.simulation.slot ??
          exactSlot(decision.context.currentSlot).toString();
        const old = this.quarantines.get(key);
        this.quarantines.set(key, {
          profileId: decision.profileId!,
          revision: decision.profileRevision!,
          digest: decision.artifactDigest!,
          rejectedSlot:
            old && slot(old.rejectedSlot) > slot(rejectedSlot)
              ? old.rejectedSlot
              : rejectedSlot,
        });
        this.recovered.delete(decision.profileId!);
      }
    }
  }
  private append(record: JournalRecord) {
    this.refresh();
    const id =
      record.kind === "recovery"
        ? (record.payload as RecoveryAudit).recoveryId
        : (record.payload as ResourceDecision | ControlObservation)
            .observationId;
    const map = this.mapFor(record.kind),
      encoded = canonical(record.payload),
      previous = map.get(id);
    this.ingest(record, false);
    if (previous === encoded) return;
    if (record.kind === "decision") {
      const decision = record.payload as ResourceDecision;
      if (
        this.isQuarantined(
          decision.profileId!,
          decision.profileRevision!,
          decision.artifactDigest!,
        )
      )
        throw new Error("profile_quarantined");
    }
    const fd = openSync(this.path, "a");
    try {
      appendFileSync(fd, canonical(record) + "\n", "utf8");
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    this.refresh();
  }
  isSuspended(profileId: string, revision?: number, artifactDigest?: string) {
    this.refresh();
    return this.isQuarantined(profileId, revision, artifactDigest);
  }
  private isQuarantined(
    profileId: string,
    revision?: number,
    artifactDigest?: string,
  ) {
    if (![...this.quarantines.values()].some((q) => q.profileId === profileId))
      return false;
    if (revision === undefined || artifactDigest === undefined) return true;
    const key = releaseKey(profileId, revision, artifactDigest);
    return this.quarantines.has(key) || this.recovered.get(profileId) !== key;
  }
  recordDecision(decision: ResourceDecision) {
    this.append({
      schema_version: "cu-pilot-controls-v2",
      kind: "decision",
      payload: { ...decision, unsignedMessage: null },
    });
  }
  recordOutcome(observation: ControlObservation) {
    this.append({
      schema_version: "cu-pilot-controls-v2",
      kind: "outcome",
      payload: observation,
    });
  }
  recover(request: RecoveryRequest): string {
    if (
      canonical(loadArtifact(request.artifact)) !==
      canonical(
        loadArtifact(
          JSON.parse(request.release.artifact_canonical_json ?? "null"),
        ),
      )
    )
      throw new Error("recovery_artifact_mismatch");
    const { artifact: _artifact, now, ...evidence } = request;
    const recoveryId = randomUUID();
    this.append({
      schema_version: "cu-pilot-controls-v2",
      kind: "recovery",
      payload: {
        ...evidence,
        recoveryId,
        checkedAt: now ?? Date.now() / 1000,
      },
    });
    return recoveryId;
  }
}
