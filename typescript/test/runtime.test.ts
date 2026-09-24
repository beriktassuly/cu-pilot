import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FileControlStore } from "../src/control-store.js";
import {
  appendTransactionMessageInstruction,
  address,
  setTransactionMessageLifetimeUsingDurableNonce,
  type Nonce,
  type Instruction,
} from "@solana/kit";
import {
  buildTransferBatch,
  FIXTURE_PAYER,
  FIXTURE_RECIPIENT,
} from "../src/builder.js";
import {
  estimateResources,
  releaseRisk,
  type EstimateOptions,
  type ReleaseSnapshot,
} from "../src/runtime.js";
import {
  bindMessage,
  canonical,
  sha256,
  BUDGET,
  MAX_DATA,
  MAX_CU,
} from "../src/message.js";
import { loadArtifact, roundedLimit } from "../src/policy.js";
import type { ControlStore } from "../src/control-store.js";
const message = buildTransferBatch({
  version: "legacy",
  payer: FIXTURE_PAYER,
  destinations: [FIXTURE_RECIPIENT],
  amounts: [1n],
  blockhash: "11111111111111111111111111111111",
  lastValidBlockHeight: 999n,
});
const bound = bindMessage(message);
const context = {
  currentSlot: 400n,
  context: "synthetic:paired",
  cluster: "test",
  runtime: "test",
  workload: "batch",
  budgetIndependent: true,
};
function setup() {
  const f = JSON.parse(
    readFileSync("../tests/fixtures/resource_contract.json", "utf8"),
  );
  const a = f.artifact;
  a.source = "simulation";
  a.label_source = "simulation";
  a.evidence_origin = "local-runtime";
  const s = a.patterns[Object.keys(a.patterns)[0]!];
  a.patterns = { [bound.features.pattern_id]: s };
  s.program_ids = bound.features.program_ids;
  s.instruction_data_length_ranges =
    bound.features.instruction_data_lengths.map((n) => ({
      minimum: n,
      maximum: n,
      missing_seen: false,
    }));
  for (const [key, r] of Object.entries(s.numerical_ranges)) {
    const n = bound.features[key as keyof typeof bound.features];
    Object.assign(r as object, {
      minimum: n,
      maximum: n,
      missing_seen: n === null,
    });
  }
  const artifact = loadArtifact(a);
  const now = Date.now() / 1000;
  const programs = [...new Set(bound.features.program_ids)];
  const release: ReleaseSnapshot = {
    schema_version: "cu-pilot-release-snapshot-v1",
    exported_at: now,
    state: "active",
    quarantine: null,
    force_simulation: false,
    watcher_failed: false,
    artifact_canonical_json: canonical(artifact),
    manifest: {
      schema_version: "cu-pilot-lifecycle-v1",
      profile_id: "test-profile",
      revision: 1,
      artifact_sha256: sha256(canonical(artifact)),
      context: context.context,
      cluster_identity: "test",
      runtime_identity: "test",
      workload_allowlist: ["batch"],
      deployment_bindings: Object.fromEntries(
        programs.map((p) => [p, "fixture"]),
      ),
      dependencies: Object.fromEntries(programs.map((p) => [p, []])),
      dependency_closure_verified: true,
      budget_independent: true,
      evidence_min_slot: "0",
      evidence_max_slot: "399",
      max_observation_age_slots: 1000,
      max_deployment_age_slots: 100,
      max_deployment_age_seconds: 60,
      control_probability: 0,
    },
    deployments: programs.map((p) => ({
      program_id: p,
      fingerprint: "fixture",
      owner: "fixture",
      deployment_slot: null,
      observed_slot: "399",
      checked_at: now,
      cluster_identity: "test",
      runtime_identity: "test",
    })),
  };
  return {
    artifact,
    release,
    controlStore: new FileControlStore(
      join(mkdtempSync(join(tmpdir(), "cu-pilot-controls-")), "events.jsonl"),
    ),
  };
}
function rpc(values: unknown[]) {
  let calls = 0;
  const client = {
    simulateTransaction: () => ({
      send: async () => {
        const value = values[Math.min(calls++, values.length - 1)];
        if (value instanceof Error) throw value;
        return value;
      },
    }),
  } as unknown as EstimateOptions["rpc"];
  return { client, calls: () => calls };
}
const measured = (
  cu = 600n,
  data: number | undefined = 36,
  err: unknown = null,
) => ({
  context: { slot: 401n },
  value: { err, unitsConsumed: cu, loadedAccountsDataSize: data },
});
test("eligible local snapshot skips sizing RPC and force/shadow preserve prediction separately", async () => {
  const { artifact, release, controlStore } = setup(),
    fake = rpc([measured()]);
  const accepted = await estimateResources(message, {
    ...context,
    artifact,
    release,
    controlStore,
    rpc: fake.client,
  });
  assert.equal(accepted.status, "prediction", canonical(accepted));
  assert.equal(fake.calls(), 0);
  const shadow = await estimateResources(message, {
    ...context,
    artifact,
    release,
    controlStore,
    rpc: fake.client,
    shadow: true,
  });
  assert.equal(shadow.status, "simulation");
  assert.equal(shadow.prediction!.simulation_recommended, false);
  assert.equal(fake.calls(), 1);
  const forced = await estimateResources(message, {
    ...context,
    artifact,
    release,
    controlStore,
    rpc: fake.client,
    forceSimulation: true,
  });
  assert.equal(forced.reason, "force_simulation");
});
test("stale watcher/dependency/release gates fail closed without changing artifact", () => {
  const { artifact, release } = setup();
  assert.equal(releaseRisk(release, artifact, bound, context), null);
  for (const key of ["watcher_failed", "force_simulation"] as const) {
    const r = structuredClone(release);
    r[key] = true;
    assert.notEqual(releaseRisk(r, artifact, bound, context), null);
  }
  const stale = structuredClone(release);
  stale.exported_at -= 61;
  assert.equal(
    releaseRisk(stale, artifact, bound, context),
    "stale_release_snapshot",
  );
  const changed = structuredClone(release);
  changed.deployments[0]!.fingerprint = "changed";
  assert.equal(
    releaseRisk(changed, artifact, bound, context),
    "deployment_changed",
  );
  const missing = structuredClone(release);
  delete missing.manifest.dependencies[bound.features.program_ids[0]!];
  assert.equal(
    releaseRisk(missing, artifact, bound, context),
    "untracked_dependency",
  );
  const bad = structuredClone(release);
  bad.manifest.max_deployment_age_seconds = NaN;
  assert.equal(
    releaseRisk(bad, artifact, bound, context),
    "invalid_release_policy",
  );
});
test("missing labels, deterministic failures and cap excess stay unresolved", async () => {
  for (const [response, reason] of [
    [
      { context: { slot: 401n }, value: { err: null, unitsConsumed: 1n } },
      "missing_or_invalid_measurement",
    ],
    [
      measured(100n, 36, { InstructionError: [0, "Custom"] }),
      "transaction_error",
    ],
    [measured(BigInt(MAX_CU), MAX_DATA), "required_limit_exceeds_cap"],
  ] as const) {
    const fake = rpc([response]);
    const result = await estimateResources(message, {
      ...context,
      rpc: fake.client,
    });
    assert.equal(result.status, "unresolved");
    assert.equal(result.reason, reason);
    assert.equal(result.limits, null);
    assert.equal(fake.calls(), 1);
  }
});
test("429 retries bounded; deterministic RPC failures redact URLs; cancellation avoids calls", async () => {
  const rate = Object.assign(
    new Error("https://secret:password@provider.invalid/token"),
    { context: { __code: 8100002, statusCode: 429 } },
  );
  const flaky = rpc([rate, measured()]);
  const result = await estimateResources(message, {
    ...context,
    rpc: flaky.client,
  });
  assert.equal(result.status, "simulation");
  assert.equal(flaky.calls(), 2);
  const broken = rpc([rate]);
  const failed = await estimateResources(message, {
    ...context,
    rpc: broken.client,
    maxAttempts: 2,
  });
  assert.equal(failed.status, "unresolved");
  assert.equal(broken.calls(), 2);
  assert.ok(!canonical(failed).includes("password"));
  const stop = new AbortController();
  stop.abort();
  const cancelled = rpc([measured()]);
  assert.equal(
    (
      await estimateResources(message, {
        ...context,
        rpc: cancelled.client,
        abortSignal: stop.signal,
      })
    ).reason,
    "cancelled",
  );
  assert.equal(cancelled.calls(), 0);
});
test("sample selection precedes labels; control failures and excess reach separate audit sink", async () => {
  const observations: unknown[] = [];
  for (const response of [
    measured(50000n, 40000),
    measured(1n, 0, { InstructionError: [0, "Custom"] }),
  ]) {
    const { artifact, release, controlStore } = setup();
    release.manifest.control_probability = 1;
    const fake = rpc([response]);
    const result = await estimateResources(message, {
      ...context,
      artifact,
      release,
      controlStore,
      rpc: fake.client,
      controlDraw: 0,
      onControl: (event) => {
        observations.push(event);
      },
    });
    assert.equal(result.controlSelected, true);
  }
  assert.equal(observations.length, 2);
  assert.equal(
    (observations[0] as { resourceExcess: boolean }).resourceExcess,
    true,
  );
  assert.equal(
    (observations[1] as { simulation: { status: string } }).simulation.status,
    "failed",
  );
});
test("duplicate budget is unresolved before RPC, and integer rounding never clamps", async () => {
  const duplicate = appendTransactionMessageInstruction(
    { programAddress: address(BUDGET), data: Uint8Array.from([2, 1, 0, 0, 0]) },
    message,
  ) as typeof message;
  const fake = rpc([measured()]);
  const result = await estimateResources(duplicate, {
    ...context,
    rpc: fake.client,
  });
  assert.equal(result.reason, "invalid_resource_configuration");
  assert.equal(fake.calls(), 0);
  assert.ok(roundedLimit(MAX_CU, 1000, 100) > MAX_CU);
});

test("asynchronous caller mutation cannot change the final instruction bytes", async () => {
  const instructions: readonly Instruction[] = message.instructions;
  const mutable = {
    ...message,
    instructions: instructions.map((ix) => ({
      ...ix,
      data: ix.data ? new Uint8Array(ix.data) : undefined,
    })),
  } as typeof message;
  const original = bindMessage(mutable).transaction.instructions[0]!.data_hex;
  const fake = {
    simulateTransaction: () => ({
      send: async () => {
        (mutable.instructions[0]!.data as Uint8Array)[4] = 250;
        await Promise.resolve();
        return measured();
      },
    }),
  } as unknown as EstimateOptions["rpc"];
  const result = await estimateResources(mutable, { ...context, rpc: fake });
  assert.equal(result.status, "simulation");
  assert.equal(result.final!.transaction.instructions[0]!.data_hex, original);
  assert.notEqual(
    bindMessage(mutable).transaction.instructions[0]!.data_hex,
    original,
  );
  assert.deepEqual(
    result.unsignedMessage!.lifetimeConstraint,
    message.lifetimeConstraint,
  );
});

test("asynchronous audit persistence rechecks expiry and quarantine before acceptance", async () => {
  for (const scenario of ["expiry", "quarantine"] as const) {
    const { artifact, release } = setup();
    let suspended = false;
    const store: ControlStore = {
      isSuspended: () => suspended,
      recordOutcome: () => {},
      recordDecision: async (decision) => {
        // Mutating the sink's copy must not mutate the pending result.
        decision.prediction!.compute_unit_limit = 1;
        if (scenario === "expiry") release.exported_at -= 120;
        else suspended = true;
        await Promise.resolve();
      },
    };
    const fake = rpc([measured()]);
    const result = await estimateResources(message, {
      ...context,
      artifact,
      release,
      controlStore: store,
      rpc: fake.client,
    });
    assert.equal(result.status, "simulation");
    assert.equal(fake.calls(), 1);
    assert.notEqual(result.prediction!.compute_unit_limit, 1);
    assert.equal(
      result.reason,
      scenario === "expiry"
        ? "release_changed_during_preparation"
        : "profile_quarantined",
    );
  }
});

test("simulation context requires an exact SDK u64 slot", async () => {
  for (const invalid of [true, 401, "401", -1n, 1n << 64n]) {
    const fake = rpc([{ ...measured(), context: { slot: invalid } }]);
    const result = await estimateResources(message, {
      ...context,
      currentSlot: 0n,
      rpc: fake.client,
    });
    assert.equal(result.status, "unresolved");
    assert.equal(result.reason, "invalid_simulation_context");
    assert.equal(result.limits, null);
    assert.equal(fake.calls(), 1);
  }
});

test("resource rebuilding preserves durable nonce instruction ordering and lifetime", async () => {
  for (const version of ["legacy", 0, 1] as const) {
    const base = buildTransferBatch({
      version,
      payer: FIXTURE_PAYER,
      destinations: [FIXTURE_RECIPIENT],
      amounts: [1n],
      blockhash: "11111111111111111111111111111111",
      lastValidBlockHeight: 999n,
    });
    const durable = setTransactionMessageLifetimeUsingDurableNonce(
      {
        nonce: "11111111111111111111111111111111" as Nonce,
        nonceAccountAddress: address("11111111111111111111111111111114"),
        nonceAuthorityAddress: FIXTURE_PAYER,
      },
      base,
    );
    const original = bindMessage(durable),
      fake = rpc([measured()]);
    const result = await estimateResources(durable, {
      ...context,
      rpc: fake.client,
    });
    assert.equal(result.status, "simulation");
    assert.deepEqual(
      result.unsignedMessage!.lifetimeConstraint,
      durable.lifetimeConstraint,
    );
    assert.deepEqual(
      result.final!.transaction.instructions[0],
      original.transaction.instructions[0],
    );
    assert.equal(
      result.final!.transaction.instructions[0]!.data_hex,
      "04000000",
    );
    assert.deepEqual(
      result.final!.transaction.instructions.map((ix) => ix.program_id),
      original.transaction.instructions.map((ix) => ix.program_id),
    );
  }
});
