import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { canonical, sha256, type BoundMessage } from "../src/message.js";
import { loadArtifact } from "../src/policy.js";
import { releaseRisk, type ReleaseSnapshot } from "../src/runtime.js";

test("new deployment cannot adopt calibration from before its deployment", () => {
  const fixture = JSON.parse(
    readFileSync("../tests/fixtures/resource_contract.json", "utf8"),
  );
  fixture.artifact.source = "simulation";
  fixture.artifact.label_source = "simulation";
  fixture.artifact.evidence_origin = "local-runtime";
  const artifact = loadArtifact(fixture.artifact);
  const program = fixture.cases[0].features.program_ids[0];
  const fingerprint = "0".repeat(64);
  // A contract-level unit fixture. Runtime upgrade behavior has a separate real suite.
  const bound = { features: fixture.cases[0].features } as BoundMessage;
  const release: ReleaseSnapshot = {
    schema_version: "cu-pilot-release-snapshot-v1",
    exported_at: 1000,
    state: "active",
    quarantine: null,
    force_simulation: false,
    watcher_failed: false,
    artifact_canonical_json: canonical(artifact),
    manifest: {
      schema_version: "cu-pilot-lifecycle-v1",
      profile_id: "chronology",
      revision: 1,
      artifact_sha256: sha256(canonical(artifact)),
      context: artifact.context,
      cluster_identity: "local",
      runtime_identity: "local-test",
      workload_allowlist: ["batch"],
      deployment_bindings: { [program]: fingerprint },
      dependencies: { [program]: [] },
      dependency_closure_verified: true,
      budget_independent: true,
      evidence_min_slot: "0",
      evidence_max_slot: "399",
      max_observation_age_slots: 1000,
      max_deployment_age_slots: 100,
      max_deployment_age_seconds: 60,
      control_probability: 0,
    },
    deployments: [
      {
        program_id: program,
        fingerprint,
        owner: "BPFLoaderUpgradeab1e11111111111111111111111",
        deployment_slot: "450",
        observed_slot: "500",
        checked_at: 1000,
        cluster_identity: "local",
        runtime_identity: "local-test",
      },
    ],
  };
  const context = {
    context: artifact.context,
    cluster: "local",
    runtime: "local-test",
    workload: "batch",
    currentSlot: 500n,
    budgetIndependent: true,
  };
  for (const deployment of ["450", "280"]) {
    release.deployments[0]!.deployment_slot = deployment;
    assert.equal(
      releaseRisk(release, artifact, bound, context, 1000),
      "deployment_not_covered_by_calibration",
    );
  }
  release.deployments[0]!.deployment_slot = "279";
  assert.equal(releaseRisk(release, artifact, bound, context, 1000), null);
});
