import test from "node:test";
import assert from "node:assert/strict";
import {
  appendFileSync,
  mkdtempSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  FileControlStore,
  type ControlStore,
  type RecoveryRequest,
} from "../src/control-store.js";
import {
  buildTransferBatch,
  FIXTURE_PAYER,
  FIXTURE_RECIPIENT,
} from "../src/builder.js";
import { bindMessage, canonical, sha256 } from "../src/message.js";
import { loadArtifact } from "../src/policy.js";
import {
  estimateResources,
  type ControlObservation,
  type EstimateOptions,
  type ReleaseSnapshot,
} from "../src/runtime.js";

const message = buildTransferBatch({
  version: "legacy",
  payer: FIXTURE_PAYER,
  destinations: [FIXTURE_RECIPIENT],
  amounts: [1n],
  blockhash: "11111111111111111111111111111111",
  lastValidBlockHeight: 999n,
});
const bound = bindMessage(message);
function release(offset = 0, revision = 1) {
  const data = JSON.parse(
    readFileSync("../tests/fixtures/resource_contract.json", "utf8"),
  ).artifact;
  data.source = "simulation";
  data.label_source = "simulation";
  data.evidence_origin = "local-runtime";
  const stats = data.patterns[Object.keys(data.patterns)[0]!];
  data.patterns = { [bound.features.pattern_id]: stats };
  stats.program_ids = bound.features.program_ids;
  stats.instruction_data_length_ranges =
    bound.features.instruction_data_lengths.map((n) => ({
      minimum: n,
      maximum: n,
      missing_seen: false,
    }));
  for (const [key, range] of Object.entries(stats.numerical_ranges)) {
    const n = bound.features[key as keyof typeof bound.features];
    Object.assign(range as object, {
      minimum: n,
      maximum: n,
      missing_seen: n === null,
    });
  }
  for (const obj of [data, stats])
    for (const field of [
      "training_max_slot",
      "calibration_min_slot",
      "max_slot",
    ])
      obj[field] = (BigInt(obj[field]) + BigInt(offset)).toString();
  const artifact = loadArtifact(data),
    now = Date.now() / 1000;
  const programs = [...new Set(bound.features.program_ids)];
  const snapshot: ReleaseSnapshot = {
    schema_version: "cu-pilot-release-snapshot-v1",
    exported_at: now,
    state: "active",
    quarantine: null,
    force_simulation: false,
    watcher_failed: false,
    artifact_canonical_json: canonical(artifact),
    manifest: {
      schema_version: "cu-pilot-lifecycle-v1",
      profile_id: "recoverable",
      revision,
      artifact_sha256: sha256(canonical(artifact)),
      context: artifact.context,
      cluster_identity: "test",
      runtime_identity: "test",
      workload_allowlist: ["batch"],
      deployment_bindings: Object.fromEntries(
        programs.map((p) => [p, "fixture"]),
      ),
      dependencies: Object.fromEntries(programs.map((p) => [p, []])),
      dependency_closure_verified: true,
      budget_independent: true,
      evidence_min_slot: String(offset),
      evidence_max_slot: artifact.max_slot,
      max_observation_age_slots: 1000,
      max_deployment_age_slots: 100,
      max_deployment_age_seconds: 60,
      control_probability: 1,
      max_control_failure_streak: 3,
    },
    deployments: programs.map((p) => ({
      program_id: p,
      fingerprint: "fixture",
      owner: "fixture",
      deployment_slot: null,
      observed_slot: artifact.max_slot,
      checked_at: now,
      cluster_identity: "test",
      runtime_identity: "test",
    })),
  };
  const context = {
    context: artifact.context,
    cluster: "test",
    runtime: "test",
    workload: "batch",
    currentSlot: BigInt(offset + 400),
    budgetIndependent: true,
  };
  return { artifact, release: snapshot, context };
}
function rpc(cu = 2000n, slot = 401n): EstimateOptions["rpc"] {
  return {
    simulateTransaction: () => ({
      send: async () => ({
        context: { slot },
        value: { err: null, unitsConsumed: cu, loadedAccountsDataSize: 128 },
      }),
    }),
  } as unknown as EstimateOptions["rpc"];
}
function failedRpc(slot = 401n): EstimateOptions["rpc"] {
  return {
    simulateTransaction: () => ({
      send: async () => ({
        context: { slot },
        value: { err: { InstructionError: [0, "Custom"] }, unitsConsumed: 1n },
      }),
    }),
  } as unknown as EstimateOptions["rpc"];
}
function measurementRpc(
  value: Record<string, unknown>,
): EstimateOptions["rpc"] {
  return {
    simulateTransaction: () => ({
      send: async () => ({ context: { slot: 401n }, value }),
    }),
  } as unknown as EstimateOptions["rpc"];
}
function newStore() {
  const path = join(
    mkdtempSync(join(tmpdir(), "cu-pilot-recovery-")),
    "controls.jsonl",
  );
  return { path, store: new FileControlStore(path) };
}
async function selected(store: FileControlStore) {
  let event: ControlObservation | undefined;
  const proxy: ControlStore = {
    isSuspended: (...args) => store.isSuspended(...args),
    recordDecision: (decision) => store.recordDecision(decision),
    recordOutcome: (outcome) => {
      event = outcome;
    },
  };
  const initial = release();
  const result = await estimateResources(message, {
    ...initial,
    ...initial.context,
    controlStore: proxy,
    rpc: rpc(),
    controlDraw: 0,
  });
  assert.equal(result.status, "simulation");
  assert.ok(event);
  return { event, initial };
}
function recovery(): RecoveryRequest {
  return {
    ...release(500, 2),
    bound,
    actor: "operator",
    reason: "Fresh prospective calibration reviewed",
  };
}

test("control outcomes match frozen selection, limits, identity and version before journal writes", async () => {
  const { path, store } = newStore();
  const { event } = await selected(store);
  const original = readFileSync(path, "utf8");
  const mutations = [
    { ...event, profileId: "other" },
    { ...event, profileRevision: 2 },
    { ...event, artifactDigest: "a".repeat(64) },
    { ...event, probability: 0.5 },
    { ...event, decisionMessageIdentity: "other" },
    { ...event, prediction: { ...event.prediction, compute_unit_limit: 5 } },
    { ...event, resourceExcess: false },
    { ...event, simulation: { ...event.simulation, slot: "399" } },
  ];
  for (const bad of mutations) {
    assert.throws(() => store.recordOutcome(bad));
    assert.equal(readFileSync(path, "utf8"), original);
    assert.equal(new FileControlStore(path).isSuspended("recoverable"), false);
  }
  store.recordOutcome(event);
  store.recordOutcome(event);
  assert.equal(
    store.isSuspended("recoverable", 1, event.artifactDigest!),
    true,
  );
});

test("audited fresh recovery survives reopen and keeps old revisions and digest aliases quarantined", async () => {
  const { path, store } = newStore();
  const { event, initial } = await selected(store);
  store.recordOutcome(event);
  const request = recovery(),
    digest = request.release.manifest.artifact_sha256;
  assert.equal(store.isSuspended("recoverable", 2, digest), true);
  const id = store.recover(request);
  assert.ok(id);
  const reopened = new FileControlStore(path);
  assert.equal(reopened.isSuspended("recoverable", 2, digest), false);
  assert.equal(
    reopened.isSuspended(
      "recoverable",
      1,
      initial.release.manifest.artifact_sha256,
    ),
    true,
  );
  assert.equal(
    reopened.isSuspended(
      "recoverable",
      3,
      initial.release.manifest.artifact_sha256,
    ),
    true,
  );
  assert.equal(reopened.isSuspended("recoverable", 3, digest), true);
  const records = readFileSync(path, "utf8")
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l));
  assert.deepEqual(
    records.map((r) => r.kind),
    ["decision", "outcome", "recovery"],
  );
  assert.equal(records[2].payload.actor, "operator");
  const released = structuredClone(request.release);
  released.manifest.control_probability = 0;
  const result = await estimateResources(message, {
    ...request.context,
    artifact: request.artifact,
    release: released,
    controlStore: reopened,
    rpc: rpc(600n, 901n),
  });
  assert.equal(result.status, "prediction");
});

test("stale, incompatible, reused or unqualified recovery never clears quarantine", async () => {
  const { path, store } = newStore();
  const { event } = await selected(store);
  store.recordOutcome(event);
  const original = readFileSync(path, "utf8");
  const cases: RecoveryRequest[] = [];
  const stale = recovery();
  stale.release.exported_at -= 61;
  cases.push(stale);
  const incompatible = recovery();
  incompatible.release.manifest.runtime_identity = "other";
  cases.push(incompatible);
  const sameRevision = recovery();
  sameRevision.release.manifest.revision = 1;
  cases.push(sameRevision);
  cases.push({
    ...release(0, 2),
    bound,
    actor: "operator",
    reason: "old artifact",
  });
  const oldCalibration = recovery();
  for (const obj of [
    oldCalibration.artifact,
    ...Object.values(oldCalibration.artifact.patterns),
  ]) {
    obj.training_max_slot = "279";
    obj.calibration_min_slot = "280";
  }
  oldCalibration.release.artifact_canonical_json = canonical(
    oldCalibration.artifact,
  );
  oldCalibration.release.manifest.artifact_sha256 = sha256(
    canonical(oldCalibration.artifact),
  );
  cases.push(oldCalibration);
  const badActor = recovery();
  badActor.actor = " ";
  cases.push(badActor);
  for (const request of cases) {
    assert.throws(() => store.recover(request));
    assert.equal(readFileSync(path, "utf8"), original);
    assert.equal(
      new FileControlStore(path).isSuspended(
        "recoverable",
        2,
        request.release.manifest.artifact_sha256,
      ),
      true,
    );
  }
});

test("an unselected decision cannot acquire a control label", async () => {
  const { store } = newStore(),
    initial = release();
  initial.release.manifest.control_probability = 0;
  const decision = await estimateResources(message, {
    ...initial,
    ...initial.context,
    controlStore: store,
    rpc: rpc(),
  });
  assert.equal(decision.status, "prediction");
  assert.throws(
    () =>
      store.recordOutcome({
        observationId: decision.observationId,
        decisionMessageIdentity: decision.prepared!.messageIdentity,
        profileId: decision.profileId,
        profileRevision: decision.profileRevision,
        artifactDigest: decision.artifactDigest,
        selectedBeforeOutcome: true,
        probability: 0,
        prediction: decision.prediction!,
        resourceExcess: false,
        simulation: {
          status: "success",
          reason: "fixture",
          slot: "401",
          computeUnits: 600,
          loadedAccountsBytes: 128,
          elapsedMs: 1,
          attempts: 1,
        },
      }),
    /decision_mismatch/,
  );
});

test("new excess re-suspends a recovered revision and failure policy cannot change on reopen", async () => {
  const { path, store } = newStore();
  const { event } = await selected(store);
  store.recordOutcome(event);
  const request = recovery();
  store.recover(request);
  const result = await estimateResources(message, {
    ...request.context,
    artifact: request.artifact,
    release: request.release,
    controlStore: store,
    rpc: rpc(2000n, 901n),
    controlDraw: 0,
  });
  assert.equal(result.status, "simulation");
  assert.equal(
    store.isSuspended(
      "recoverable",
      2,
      request.release.manifest.artifact_sha256,
    ),
    true,
  );
  assert.equal(
    new FileControlStore(path).isSuspended(
      "recoverable",
      2,
      request.release.manifest.artifact_sha256,
    ),
    true,
  );
  assert.throws(() => new FileControlStore(path, 4), /failure_policy/);
});

test("live journal instances see each other's quarantines and reject stale decisions", async () => {
  const { path, store } = newStore();
  const other = new FileControlStore(path);
  const { event } = await selected(store);
  const decision = JSON.parse(readFileSync(path, "utf8").trim()).payload;
  assert.equal(other.isSuspended("recoverable"), false);
  store.recordOutcome(event);
  assert.equal(other.isSuspended("recoverable"), true);
  const source = readFileSync(path, "utf8");
  assert.throws(
    () => other.recordDecision({ ...decision, observationId: "late-request" }),
    /profile_quarantined/,
  );
  assert.equal(readFileSync(path, "utf8"), source);
  const request = recovery();
  other.recover(request);
  assert.equal(
    store.isSuspended(
      "recoverable",
      2,
      request.release.manifest.artifact_sha256,
    ),
    false,
  );
});

test("journal refresh rejects prefix mutation, truncation and interrupted append", async () => {
  for (const mutation of ["edit", "truncate", "partial"] as const) {
    const { path, store } = newStore();
    await selected(store);
    const source = readFileSync(path, "utf8");
    if (mutation === "edit")
      writeFileSync(path, source.replace("recoverable", "recoverablE"));
    else if (mutation === "truncate") writeFileSync(path, "");
    else appendFileSync(path, '{"unfinished":');
    assert.throws(() => store.isSuspended("recoverable"), /control_journal/);
  }
});

test("shared released thresholds survive restart and preserve the next decision's policy", async () => {
  const cases = JSON.parse(
    readFileSync("../tests/fixtures/lifecycle_policy_contract.json", "utf8"),
  ).control_cases;
  for (const scenario of cases) {
    const { path } = newStore(),
      initial = release();
    initial.release.manifest.max_control_failure_streak = scenario.threshold;
    initial.release.manifest.control_probability = 0.5;
    for (let index = 0; index < scenario.success.length; index++) {
      const result = await estimateResources(message, {
        ...initial,
        ...initial.context,
        controlStore: new FileControlStore(path),
        rpc: scenario.success[index] ? rpc(600n) : failedRpc(),
        controlDraw: 0,
      });
      assert.equal(result.controlSelected, true);
      assert.equal(result.maxControlFailureStreak, scenario.threshold);
      const reopened = new FileControlStore(path);
      assert.equal(
        reopened.isSuspended("recoverable"),
        scenario.suspended[index],
      );
      let calls = 0;
      const next = await estimateResources(message, {
        ...initial,
        ...initial.context,
        controlStore: reopened,
        controlDraw: 0.9,
        rpc: {
          simulateTransaction: () => {
            calls++;
            return {
              send: async () => ({
                context: { slot: 401n },
                value: {
                  err: null,
                  unitsConsumed: 600n,
                  loadedAccountsDataSize: 128,
                },
              }),
            };
          },
        } as unknown as EstimateOptions["rpc"],
      });
      assert.equal(
        next.status,
        scenario.suspended[index] ? "simulation" : "prediction",
      );
      assert.equal(calls, scenario.suspended[index] ? 1 : 0);
    }
    const records = readFileSync(path, "utf8")
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));
    assert.ok(
      records.every((r) => r.schema_version === "cu-pilot-controls-v2"),
    );
    assert.ok(
      records
        .filter((r) => r.kind === "decision")
        .every((r) => r.payload.maxControlFailureStreak === scenario.threshold),
    );
  }
});

test("one journal enforces separate release thresholds and rejects same-release policy changes", async () => {
  const { path, store } = newStore(),
    first = release(),
    second = release();
  first.release.manifest.max_control_failure_streak = 1;
  first.release.manifest.profile_id = "one";
  second.release.manifest.profile_id = "three";
  for (const initial of [second, first]) {
    await estimateResources(message, {
      ...initial,
      ...initial.context,
      controlStore: store,
      controlDraw: 0,
      rpc: failedRpc(),
    });
  }
  const reopened = new FileControlStore(path);
  assert.equal(reopened.isSuspended("one"), true);
  assert.equal(reopened.isSuspended("three"), false);
  const storedDecision = JSON.parse(
    readFileSync(path, "utf8").split("\n")[0]!,
  ).payload;
  assert.throws(
    () =>
      reopened.recordDecision({
        ...storedDecision,
        observationId: "changed-policy",
        maxControlFailureStreak: 1,
      }),
    /failure_policy/,
  );
  for (let index = 0; index < 2; index++) {
    const next = await estimateResources(message, {
      ...second,
      ...second.context,
      controlStore: new FileControlStore(path),
      controlDraw: 0,
      rpc: failedRpc(),
    });
    assert.equal(next.controlSelected, true);
  }
  assert.equal(new FileControlStore(path).isSuspended("three"), true);
});

test("recovery binds the new revision's threshold without resetting prior quarantine", async () => {
  const { path, store } = newStore(),
    { event } = await selected(store);
  store.recordOutcome(event);
  const request = recovery();
  request.release.manifest.max_control_failure_streak = 1;
  store.recover(request);
  const reopened = new FileControlStore(path);
  const failed = await estimateResources(message, {
    ...request.context,
    artifact: request.artifact,
    release: request.release,
    controlStore: reopened,
    controlDraw: 0,
    rpc: failedRpc(901n),
  });
  assert.equal(failed.maxControlFailureStreak, 1);
  assert.equal(
    new FileControlStore(path).isSuspended(
      "recoverable",
      2,
      request.release.manifest.artifact_sha256,
    ),
    true,
  );
});

test("an explicit constructor policy is a compatibility pin, never a release override", async () => {
  const { path } = newStore(),
    initial = release();
  initial.release.manifest.max_control_failure_streak = 1;
  const result = await estimateResources(message, {
    ...initial,
    ...initial.context,
    controlStore: new FileControlStore(path, 3),
    controlDraw: 0,
    rpc: rpc(600n),
  });
  assert.equal(result.status, "unresolved");
  assert.equal(result.reason, "incompatible_control_failure_policy");
  assert.equal(result.limits, null);
});

test("old journals lacking release-bound thresholds fail closed and remain unchanged", async () => {
  const { path, store } = newStore();
  await selected(store);
  const source = readFileSync(path, "utf8").replaceAll(
    "cu-pilot-controls-v2",
    "cu-pilot-controls-v1",
  );
  writeFileSync(path, source);
  assert.throws(
    () => new FileControlStore(path),
    /incompatible_control_journal/,
  );
  assert.equal(readFileSync(path, "utf8"), source);
});

test("partial successful controls suspend on either known excess despite a missing counterpart", async () => {
  for (const threshold of [1, 3]) {
    for (const value of [
      { err: null, unitsConsumed: 2000n },
      { err: null, loadedAccountsDataSize: 40000 },
      { err: null, unitsConsumed: 2000n, loadedAccountsDataSize: "128" },
      { err: null, unitsConsumed: 600, loadedAccountsDataSize: 40000 },
    ]) {
      const { path, store } = newStore(),
        initial = release();
      initial.release.manifest.max_control_failure_streak = threshold;
      let event: ControlObservation | undefined;
      const result = await estimateResources(message, {
        ...initial,
        ...initial.context,
        controlStore: store,
        controlDraw: 0,
        rpc: measurementRpc(value),
        onControl: (observation) => {
          event = observation;
        },
      });
      assert.equal(result.status, "unresolved");
      assert.equal(result.reason, "missing_or_invalid_measurement");
      assert.equal(result.limits, null);
      assert.equal(result.unsignedMessage, null);
      assert.equal(event!.simulation.status, "incomplete");
      assert.equal(event!.resourceExcess, true);
      assert.equal(new FileControlStore(path).isSuspended("recoverable"), true);
      if (typeof value.unitsConsumed === "bigint") {
        assert.equal(event!.simulation.computeUnits, 2000);
        assert.equal(event!.simulation.loadedAccountsBytes, null);
      } else {
        assert.equal(event!.simulation.computeUnits, null);
        assert.equal(event!.simulation.loadedAccountsBytes, 40000);
      }
    }
  }
});

test("incomplete under-limit controls count against each released failure threshold across restart", async () => {
  for (const threshold of [1, 3]) {
    for (const value of [
      { err: null, unitsConsumed: 600n },
      { err: null, loadedAccountsDataSize: 128 },
      { err: null },
    ]) {
      const { path } = newStore(),
        initial = release();
      initial.release.manifest.max_control_failure_streak = threshold;
      for (let count = 1; count <= threshold; count++) {
        const result = await estimateResources(message, {
          ...initial,
          ...initial.context,
          controlStore: new FileControlStore(path),
          controlDraw: 0,
          rpc: measurementRpc(value),
        });
        assert.equal(result.status, "unresolved");
        assert.equal(result.simulation!.status, "incomplete");
        assert.equal(
          new FileControlStore(path).isSuspended("recoverable"),
          count === threshold,
        );
      }
    }
  }
});

test("transaction-error partial usage is never successful resource demand", async () => {
  for (const threshold of [1, 3]) {
    const { path } = newStore(),
      initial = release();
    initial.release.manifest.max_control_failure_streak = threshold;
    for (let count = 1; count <= threshold; count++) {
      let event: ControlObservation | undefined;
      const result = await estimateResources(message, {
        ...initial,
        ...initial.context,
        controlStore: new FileControlStore(path),
        controlDraw: 0,
        rpc: measurementRpc({
          err: { InstructionError: [0, "Custom"] },
          unitsConsumed: 2000n,
          loadedAccountsDataSize: 40000,
        }),
        onControl: (observation) => {
          event = observation;
        },
      });
      assert.equal(result.status, "unresolved");
      assert.equal(result.reason, "transaction_error");
      assert.equal(event!.simulation.status, "failed");
      assert.equal(event!.simulation.computeUnits, null);
      assert.equal(event!.simulation.loadedAccountsBytes, null);
      assert.equal(event!.resourceExcess, false);
      assert.equal(
        new FileControlStore(path).isSuspended("recoverable"),
        count === threshold,
      );
    }
  }
});

test("partial control validation rejects fabricated or suppressed excess before writing", async () => {
  const { path, store } = newStore(),
    initial = release();
  let event: ControlObservation | undefined;
  const proxy: ControlStore = {
    isSuspended: (...args) => store.isSuspended(...args),
    recordDecision: (decision) => store.recordDecision(decision),
    recordOutcome: (outcome) => {
      event = outcome;
    },
  };
  const result = await estimateResources(message, {
    ...initial,
    ...initial.context,
    controlStore: proxy,
    controlDraw: 0,
    rpc: measurementRpc({ err: null, unitsConsumed: 2000n }),
  });
  assert.equal(result.status, "unresolved");
  assert.ok(event);
  const original = readFileSync(path, "utf8");
  const mutations: ControlObservation[] = [
    { ...event, resourceExcess: false },
    { ...event, simulation: { ...event.simulation, slot: null } },
    { ...event, simulation: { ...event.simulation, status: "success" } },
    { ...event, simulation: { ...event.simulation, status: "failed" } },
    { ...event, simulation: { ...event.simulation, computeUnits: null } },
    { ...event, simulation: { ...event.simulation, loadedAccountsBytes: 128 } },
    { ...event, simulation: { ...event.simulation, computeUnits: -1 } },
  ];
  for (const invalid of mutations) {
    assert.throws(() => store.recordOutcome(invalid));
    assert.equal(readFileSync(path, "utf8"), original);
    assert.equal(new FileControlStore(path).isSuspended("recoverable"), false);
  }
  store.recordOutcome(event);
  assert.equal(new FileControlStore(path).isSuspended("recoverable"), true);
});
