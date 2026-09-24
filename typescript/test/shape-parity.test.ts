import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { extractFeatures, type NormalizedTransaction } from "../src/message.js";

type MutationCase = {
  name: string;
  base: string;
  mutations: { path: (string | number)[]; value: unknown }[];
  pattern_id: string;
  risk_flags: string[];
};
const fixtures = new URL("../../tests/fixtures/", import.meta.url);
const cases: MutationCase[] = JSON.parse(
  readFileSync(new URL("shape_contract.json", fixtures), "utf8"),
);

// Structural mutations of real Kit fixtures are cross-language contract tests,
// not claims that every mutated instruction executes successfully on Solana.
for (const entry of cases) {
  test(`Python/TypeScript topology contract: ${entry.name}`, () => {
    const raw = JSON.parse(
      readFileSync(new URL(`kit/${entry.base}.json`, fixtures), "utf8"),
    ).transaction;
    for (const mutation of entry.mutations) {
      let target = raw;
      for (const key of mutation.path.slice(0, -1)) target = target[key];
      target[mutation.path.at(-1)!] = mutation.value;
    }
    if (raw.transaction_config?.priorityFee != null)
      raw.transaction_config.priorityFee = BigInt(raw.transaction_config.priorityFee);
    const features = extractFeatures(raw as NormalizedTransaction);
    assert.equal(features.pattern_id, entry.pattern_id);
    assert.deepEqual(features.risk_flags, entry.risk_flags);
  });
}
