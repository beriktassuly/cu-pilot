import {
  address,
  blockhash,
  createTransactionMessage,
  setTransactionMessageFeePayer,
  setTransactionMessageLifetimeUsingBlockhash,
  appendTransactionMessageInstructions,
  createNoopSigner,
  type Address,
} from "@solana/kit";
import { getTransferSolInstruction } from "@solana-program/system";
import { prepareResources, type BuilderMessage } from "./message.js";

/** Controlled workload: an ordered batch of System transfers with fixed account roles. */
export function buildTransferBatch(options: {
  version: "legacy" | 0 | 1;
  payer: Address;
  destinations: readonly Address[];
  amounts: readonly bigint[];
  blockhash: string;
  lastValidBlockHeight: bigint;
}): BuilderMessage {
  if (
    options.destinations.length !== options.amounts.length ||
    !options.destinations.length
  )
    throw new Error("invalid_batch");
  const base = setTransactionMessageLifetimeUsingBlockhash(
    {
      blockhash: blockhash(options.blockhash),
      lastValidBlockHeight: options.lastValidBlockHeight,
    },
    setTransactionMessageFeePayer(
      options.payer,
      createTransactionMessage({ version: options.version }),
    ),
  );
  const message = appendTransactionMessageInstructions(
    options.destinations.map((destination, i) =>
      getTransferSolInstruction({
        source: createNoopSigner(options.payer),
        destination,
        amount: options.amounts[i]!,
      }),
    ),
    base,
  );
  return prepareResources(message as BuilderMessage);
}

export const FIXTURE_PAYER = address("11111111111111111111111111111112");
export const FIXTURE_RECIPIENT = address("11111111111111111111111111111113");
export const FIXTURE_TABLE = address("11111111111111111111111111111114");
