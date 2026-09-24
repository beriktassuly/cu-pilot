import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  setTransactionMessageComputeUnitLimit,
  appendTransactionMessageInstruction,
  address,
} from "@solana/kit";
import {
  normalizeWire,
  extractFeatures,
  canonical,
  decodeBuilder,
  bindMessage,
  verifyBoundMessage,
  type Features,
  type LookupEvidence,
} from "../src/message.js";
import { loadArtifact, predictResources, roundedLimit } from "../src/policy.js";
const fixtures = new URL(
  "../../tests/fixtures/",
  new URL("../../", import.meta.url),
);
// Resolve in both source (tsx) and compiled (dist) layouts.
const root = process.cwd().endsWith("typescript")
  ? new URL(
      "../tests/fixtures/",
      `file://${process.cwd().replaceAll("\\", "/")}/`,
    )
  : fixtures;
for (const version of ["legacy", "0", "1"])
  test(`Kit ${version}: authoritative decode, shape and exact fee contract`, () => {
    const f = JSON.parse(
      readFileSync(new URL(`kit/${version}.json`, root), "utf8"),
    );
    const lookup = f.lookup
      ? ({
          ...f.lookup,
          checkedSlot: BigInt(f.lookup.checkedSlot),
        } as LookupEvidence)
      : undefined;
    const wire = Buffer.from(f.wireBase64, "base64");
    const tx = normalizeWire(wire, lookup);
    assert.equal(canonical(tx), canonical(f.transaction));
    assert.equal(canonical(extractFeatures(tx)), canonical(f.features));
    const message = decodeBuilder(wire, lookup),
      bound = bindMessage(message, lookup);
    assert.equal(bound.messageIdentity, f.messageIdentity);
    assert.equal(verifyBoundMessage(bound, message, lookup), true);
    assert.equal(
      verifyBoundMessage(
        bound,
        setTransactionMessageComputeUnitLimit(10000, message),
        lookup,
      ),
      false,
    );
    assert.equal(
      verifyBoundMessage(
        bound,
        appendTransactionMessageInstruction(
          {
            programAddress: address("11111111111111111111111111111111"),
            data: new Uint8Array([2, 0, 0, 0]),
          },
          message,
        ) as typeof message,
        lookup,
      ),
      false,
    );
    const fees = extractFeatures(tx);
    assert.equal(
      fees.requested_micro_lamports ?? fees.requested_priority_fee_lamports,
      9007199254740993n,
    );
    assert.throws(
      () => bindMessage(message, lookup, new Uint8Array([1, 2, 3])),
      /message_wire_mismatch/,
    );
  });
test("portable Python decisions including nulls, rounding, slot and policy boundaries", () => {
  const fixture = JSON.parse(
    readFileSync(new URL("resource_contract.json", root), "utf8"),
  );
  const a = loadArtifact(fixture.artifact);
  for (const c of fixture.cases) {
    const got = predictResources(
      a,
      c.features as Features,
      c.context,
      BigInt(c.current_slot),
    );
    const { explanation, ...expected } = c.expected;
    assert.deepEqual(got, expected, c.name);
  }
  assert.equal(roundedLimit(1001, 1000, 100), 1200);
  assert.equal(roundedLimit(0, 1000, 1024), 1024);
  assert.throws(
    () =>
      loadArtifact({ ...fixture.artifact, artifact_version: "cu-pilot-v1" }),
    /incompatible/,
  );
  assert.throws(
    () => loadArtifact({ ...fixture.artifact, max_slot: 399 }),
    /invalid_exact_slot/,
  );
  const bad = structuredClone(fixture.artifact);
  Object.values(bad.patterns).forEach((p: any) => {
    p.calibration_upper_bound = 0;
  });
  assert.throws(() => loadArtifact(bad), /invalid_joint_bound/);
});
