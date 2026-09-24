import { writeFileSync, mkdirSync } from "node:fs";
import {
  compressTransactionMessageUsingAddressLookupTables,
  setTransactionMessagePriorityFeeLamports,
  setTransactionMessageComputeUnitPrice,
} from "@solana/kit";
import {
  buildTransferBatch,
  FIXTURE_PAYER,
  FIXTURE_RECIPIENT,
  FIXTURE_TABLE,
} from "../src/builder.js";
import {
  bindMessage,
  compiledJson,
  canonical,
  type LookupEvidence,
  type BuilderMessage,
} from "../src/message.js";
const dir = new URL("../../tests/fixtures/kit/", import.meta.url);
mkdirSync(dir, { recursive: true });
for (const version of ["legacy", 0, 1] as const) {
  let message = buildTransferBatch({
    version,
    payer: FIXTURE_PAYER,
    destinations: [FIXTURE_RECIPIENT, FIXTURE_RECIPIENT],
    amounts: [1000000n, 2000000n],
    blockhash: "11111111111111111111111111111111",
    lastValidBlockHeight: 100n,
  });
  message =
    message.version === 1
      ? setTransactionMessagePriorityFeeLamports(9007199254740993n, message)
      : setTransactionMessageComputeUnitPrice(9007199254740993n, message);
  let lookup: LookupEvidence | undefined;
  if (message.version === 0) {
    lookup = {
      tables: { [FIXTURE_TABLE]: [FIXTURE_RECIPIENT] },
      checkedSlot: 50n,
      cluster: "fixture-local",
    };
    message = compressTransactionMessageUsingAddressLookupTables(
      message,
      lookup.tables,
    ) as BuilderMessage;
  }
  const bound = bindMessage(message, lookup);
  writeFileSync(
    new URL(`${version}.json`, dir),
    JSON.stringify(
      JSON.parse(
        canonical({
          evidence: "sdk-serialization-only",
          sdk: "@solana/kit@8.3.0",
          ...bound,
          compiled_json: compiledJson(bound),
          lookup,
        }),
      ),
      null,
      2,
    ) + "\n",
  );
}
console.log(
  "Exported real Kit legacy/v0/v1 serialization fixtures; no runtime labels invented.",
);
