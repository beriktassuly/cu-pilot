import {
  address,
  type Address,
  type Rpc,
  type SolanaRpcApi,
} from "@solana/kit";
import { getAddressLookupTableDecoder } from "@solana-program/address-lookup-table";
import type { LookupEvidence } from "./message.js";

/** One bounded RPC batch, owner/layout checked, inactive/unwarmed entries excluded. */
export async function resolveLookupTables(
  rpc: Pick<Rpc<SolanaRpcApi>, "getMultipleAccounts">,
  tables: readonly Address[],
  options: {
    currentSlot: bigint;
    cluster: string;
    commitment?: "confirmed" | "finalized";
    abortSignal?: AbortSignal;
  },
): Promise<LookupEvidence> {
  if (
    tables.length < 1 ||
    tables.length > 32 ||
    new Set(tables).size !== tables.length
  )
    throw new Error("invalid_lookup_batch");
  if (
    typeof options.currentSlot !== "bigint" ||
    options.currentSlot < 0n ||
    options.currentSlot >= 2n ** 64n
  )
    throw new Error("invalid_current_slot");
  const result = await rpc
    .getMultipleAccounts(tables, {
      encoding: "base64",
      commitment: options.commitment ?? "confirmed",
      minContextSlot: options.currentSlot,
    })
    .send({ abortSignal: options.abortSignal });
  if (
    typeof result.context.slot !== "bigint" ||
    result.context.slot >= 2n ** 64n ||
    result.context.slot < options.currentSlot ||
    result.value.length !== tables.length
  )
    throw new Error("stale_lookup_evidence");
  const resolved: Record<Address, Address[]> = {};
  for (let index = 0; index < tables.length; index++) {
    const account = result.value[index];
    if (
      !account ||
      account.owner !==
        address("AddressLookupTab1e1111111111111111111111111") ||
      account.executable ||
      account.data[1] !== "base64"
    )
      throw new Error("invalid_lookup_account");
    const bytes = Buffer.from(account.data[0], "base64");
    if (bytes.length < 56 || (bytes.length - 56) % 32 !== 0)
      throw new Error("invalid_lookup_layout");
    const table = getAddressLookupTableDecoder().decode(bytes);
    if (
      table.discriminator !== 1 ||
      table.deactivationSlot !== 2n ** 64n - 1n ||
      table.lastExtendedSlot > result.context.slot ||
      table.lastExtendedSlotStartIndex > table.addresses.length ||
      table.addresses.length > 256
    )
      throw new Error("inactive_lookup_table");
    resolved[tables[index]!] =
      table.lastExtendedSlot === result.context.slot
        ? table.addresses.slice(0, table.lastExtendedSlotStartIndex)
        : table.addresses;
  }
  return {
    tables: resolved,
    checkedSlot: result.context.slot,
    cluster: options.cluster,
  };
}
