import test from "node:test";
import assert from "node:assert/strict";
import {
  address,
  compressTransactionMessageUsingAddressLookupTables,
} from "@solana/kit";
import { getAddressLookupTableEncoder } from "@solana-program/address-lookup-table";
import { resolveLookupTables } from "../src/lookups.js";
import {
  buildTransferBatch,
  FIXTURE_PAYER,
  FIXTURE_RECIPIENT,
  FIXTURE_TABLE,
} from "../src/builder.js";
import { bindMessage, type BuilderMessage } from "../src/message.js";
const ALT = "AddressLookupTab1e1111111111111111111111111";
function rpc({
  owner = ALT,
  extended = 99n,
  deactivated = 2n ** 64n - 1n,
  executable = false,
} = {}) {
  const bytes = getAddressLookupTableEncoder().encode({
    deactivationSlot: deactivated,
    lastExtendedSlot: extended,
    lastExtendedSlotStartIndex: 0,
    authority: null,
    addresses: [FIXTURE_RECIPIENT],
  });
  return {
    getMultipleAccounts: () => ({
      send: async () => ({
        context: { slot: 100n },
        value: [
          {
            owner,
            data: [Buffer.from(bytes).toString("base64"), "base64"],
            executable,
          },
        ],
      }),
    }),
  } as unknown as Parameters<typeof resolveLookupTables>[0];
}
test("lookup resolver validates owner, activation and same-slot warmup before binding", async () => {
  const options = { currentSlot: 100n, cluster: "local" };
  const evidence = await resolveLookupTables(rpc(), [FIXTURE_TABLE], options);
  assert.deepEqual(evidence.tables[FIXTURE_TABLE], [FIXTURE_RECIPIENT]);
  const warm = await resolveLookupTables(
    rpc({ extended: 100n }),
    [FIXTURE_TABLE],
    options,
  );
  assert.deepEqual(warm.tables[FIXTURE_TABLE], []);
  await assert.rejects(
    resolveLookupTables(rpc({ deactivated: 100n }), [FIXTURE_TABLE], options),
    /inactive_lookup/,
  );
  await assert.rejects(
    resolveLookupTables(
      rpc({ owner: FIXTURE_PAYER }),
      [FIXTURE_TABLE],
      options,
    ),
    /invalid_lookup_account/,
  );
  await assert.rejects(
    resolveLookupTables(rpc({ executable: true }), [FIXTURE_TABLE], options),
    /invalid_lookup_account/,
  );
  await assert.rejects(
    resolveLookupTables(rpc(), [FIXTURE_TABLE, FIXTURE_TABLE], options),
    /invalid_lookup_batch/,
  );
  const message = buildTransferBatch({
    version: 0,
    payer: FIXTURE_PAYER,
    destinations: [FIXTURE_RECIPIENT],
    amounts: [1n],
    blockhash: "11111111111111111111111111111111",
    lastValidBlockHeight: 999n,
  });
  assert.equal(message.version, 0);
  const compressed = compressTransactionMessageUsingAddressLookupTables(
    message as Extract<BuilderMessage, { version: 0 }>,
    evidence.tables,
  ) as BuilderMessage;
  assert.doesNotThrow(() => bindMessage(compressed, evidence));
  assert.throws(
    () =>
      bindMessage(compressed, {
        ...evidence,
        tables: {
          [FIXTURE_TABLE]: [address("11111111111111111111111111111115")],
        },
      }),
    /lookup_builder_mismatch/,
  );
});
