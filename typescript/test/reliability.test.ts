import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { Features } from "../src/message.js";
import {
  loadArtifact,
  predictResources,
  roundedLimit,
  slot,
  wilsonUpper,
} from "../src/policy.js";

const fixture = JSON.parse(
  readFileSync("../tests/fixtures/resource_contract.json", "utf8"),
);
const firstPattern = Object.keys(fixture.artifact.patterns)[0]!;
const rawArtifact = () => structuredClone(fixture.artifact);

test("nonfinite or coerced calibration bounds can never qualify", () => {
  for (const bound of [
    NaN,
    Infinity,
    -Infinity,
    "not-a-number",
    "0.022",
    true,
    [],
    {},
    null,
  ]) {
    const raw = rawArtifact();
    raw.patterns[firstPattern].calibration_upper_bound = bound;
    assert.throws(() => loadArtifact(raw), /invalid_joint_bound/);
  }
  const raw = rawArtifact();
  raw.patterns[firstPattern].calibration_upper_bound += 1e-13;
  assert.throws(() => loadArtifact(raw), /invalid_joint_bound/);
});

test("diagnostic and range contracts match strict portable Python artifacts", () => {
  for (const diagnostics of [
    {},
    [],
    null,
    { ...fixture.artifact.diagnostics, unknown: 1 },
  ]) {
    const raw = rawArtifact();
    raw.diagnostics = diagnostics;
    assert.throws(() => loadArtifact(raw));
  }
  for (const maximum of [true, "256", NaN, 2 ** 32]) {
    const raw = rawArtifact();
    raw.patterns[firstPattern].numerical_ranges.account_count.maximum = maximum;
    assert.throws(() => loadArtifact(raw), /invalid_integer/);
  }
  const malformed = rawArtifact();
  malformed.patterns = [];
  assert.throws(() => loadArtifact(malformed), /invalid_artifact_object/);
});

test("provenance cannot silently combine historical and simulation labels", () => {
  const mislabeled = rawArtifact();
  mislabeled.source = "simulation";
  mislabeled.label_source = "historical";
  assert.throws(() => loadArtifact(mislabeled), /invalid_provenance/);
  mislabeled.label_source = "simulation";
  mislabeled.evidence_origin = "synthetic";
  assert.throws(() => loadArtifact(mislabeled), /invalid_provenance/);
});

test("open probability boundary permits every finite number strictly below one", () => {
  const raw = rawArtifact();
  raw.policy.max_joint_underestimation_rate = 1 - Number.EPSILON / 2;
  assert.doesNotThrow(() => loadArtifact(raw));
  raw.policy.max_joint_underestimation_rate = 1;
  assert.throws(() => loadArtifact(raw), /invalid_probability/);
});

test("rounded limits remain exact and the joint bound rejects invalid denominators", () => {
  assert.equal(roundedLimit(1001, 1000, 100), 1200);
  assert.throws(
    () => roundedLimit(Number.MAX_SAFE_INTEGER, 1000, 100),
    /inexact_resource_limit/,
  );
  for (const count of [0, NaN, Infinity, -1, 1.5]) {
    assert.throws(() => wilsonUpper(0, count));
  }
  assert.throws(() => wilsonUpper(2, 1), /invalid_joint_counts/);
});

test("coerced budgets follow the Python incomplete-feature guard", () => {
  const artifact = loadArtifact(rawArtifact());
  const example = fixture.cases[0];
  for (const key of [
    "requested_compute_units",
    "requested_loaded_accounts_bytes",
  ]) {
    for (const value of [true, "1000", -1, 1.5, NaN]) {
      const features = { ...example.features, [key]: value } as Features;
      assert.equal(
        predictResources(
          artifact,
          features,
          example.context,
          BigInt(example.current_slot),
        ).reason,
        "incomplete_features",
      );
    }
  }
});

test("JavaScript prototype names are unknown patterns, never implicit profiles", () => {
  const artifact = loadArtifact(rawArtifact());
  const example = fixture.cases[0];
  for (const pattern_id of ["toString", "__proto__", "constructor"]) {
    const result = predictResources(
      artifact,
      { ...example.features, pattern_id },
      example.context,
      BigInt(example.current_slot),
    );
    assert.equal(result.reason, "unknown_pattern");
  }
});

test("exact slot types and evidence window boundaries stay strict", () => {
  for (const value of [
    400,
    400n,
    true,
    "0400",
    "+400",
    "4e2",
    "-1",
    (2n ** 64n).toString(),
  ]) {
    assert.throws(() => slot(value), /invalid_exact_slot/);
  }
  assert.equal(slot("9007199254740993"), 9007199254740993n);
  const raw = rawArtifact();
  raw.policy.independence_window_slots = 100;
  assert.throws(() => loadArtifact(raw), /invalid_profile_chronology/);
});
