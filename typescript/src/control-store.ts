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
import { canonical } from "./message.js";
import type { ControlObservation, ResourceDecision } from "./runtime.js";

export interface ControlStore {
  isSuspended(profileId: string): boolean;
  recordDecision(decision: ResourceDecision): void | Promise<void>;
  recordOutcome(observation: ControlObservation): void | Promise<void>;
}

/** Single-process append-only audit log. Torn/corrupt logs fail closed on reopen. */
export class FileControlStore implements ControlStore {
  private readonly decisions = new Map<string, string>();
  private readonly outcomes = new Map<string, string>();
  private readonly suspended = new Set<string>();
  constructor(
    private readonly path: string,
    private readonly maxFailureStreak = 3,
  ) {
    if (!Number.isSafeInteger(maxFailureStreak) || maxFailureStreak < 1)
      throw new Error("invalid_control_failure_policy");
    mkdirSync(dirname(path), { recursive: true });
    if (existsSync(path)) {
      const source = readFileSync(path, "utf8");
      if (source && !source.endsWith("\n"))
        throw new Error("incomplete_control_journal");
      for (const line of source.split("\n").filter(Boolean))
        this.ingest(JSON.parse(line));
    }
  }
  private readonly failureStreak = new Map<string, number>();
  private ingest(record: {
    schema_version: string;
    kind: string;
    payload: ResourceDecision | ControlObservation;
  }) {
    if (record.schema_version !== "cu-pilot-controls-v1")
      throw new Error("incompatible_control_journal");
    const payload = record.payload;
    if (typeof payload.observationId !== "string")
      throw new Error("invalid_control_record");
    const map =
      record.kind === "decision"
        ? this.decisions
        : record.kind === "outcome"
          ? this.outcomes
          : null;
    if (!map) throw new Error("invalid_control_record");
    const encoded = canonical(payload),
      previous = map.get(payload.observationId);
    if (previous) {
      if (previous !== encoded) throw new Error("conflicting_control_record");
      return;
    }
    map.set(payload.observationId, encoded);
    if (record.kind === "outcome") {
      const outcome = payload as ControlObservation;
      if (!this.decisions.has(outcome.observationId))
        throw new Error("control_without_frozen_decision");
      if (
        !outcome.profileId ||
        outcome.selectedBeforeOutcome !== true ||
        !Number.isFinite(outcome.probability) ||
        outcome.probability <= 0 ||
        outcome.probability > 1
      )
        throw new Error("invalid_control_outcome");
      const streak =
        outcome.simulation.status === "failed"
          ? (this.failureStreak.get(outcome.profileId) ?? 0) + 1
          : 0;
      this.failureStreak.set(outcome.profileId, streak);
      if (outcome.resourceExcess || streak >= this.maxFailureStreak)
        this.suspended.add(outcome.profileId);
    }
  }
  private append(
    kind: "decision" | "outcome",
    payload: ResourceDecision | ControlObservation,
  ) {
    const map = kind === "decision" ? this.decisions : this.outcomes,
      encoded = canonical(payload),
      previous = map.get(payload.observationId);
    if (previous) {
      if (previous !== encoded) throw new Error("conflicting_control_record");
      return;
    }
    const record = { schema_version: "cu-pilot-controls-v1", kind, payload };
    const fd = openSync(this.path, "a");
    try {
      appendFileSync(fd, canonical(record) + "\n", "utf8");
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    this.ingest(JSON.parse(canonical(record)));
  }
  isSuspended(profileId: string) {
    return this.suspended.has(profileId);
  }
  recordDecision(decision: ResourceDecision) {
    // Do not retain mutable builder objects or duplicate secrets; bound wire is unsigned.
    this.append("decision", { ...decision, unsignedMessage: null });
  }
  recordOutcome(observation: ControlObservation) {
    this.append("outcome", observation);
  }
}
