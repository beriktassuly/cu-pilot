import { createHash } from "node:crypto";
import {
  compileTransaction,
  compileTransactionMessage,
  decompileTransactionMessage,
  getCompiledTransactionMessageDecoder,
  getCompiledTransactionMessageEncoder,
  getTransactionDecoder,
  getTransactionEncoder,
  getBase58Decoder,
  setTransactionMessageComputeUnitLimit,
  setTransactionMessageLoadedAccountsDataSizeLimit,
  type TransactionMessage,
  type TransactionMessageWithFeePayer,
  type TransactionMessageWithLifetime,
  type AddressesByLookupTableAddress,
} from "@solana/kit";

export const MAX_CU = 1_400_000;
export const MAX_DATA = 67_108_864;
export const SYSTEM = "11111111111111111111111111111111";
export const BUDGET = "ComputeBudget111111111111111111111111111111";
const TOKENS = new Set([
  "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
]);
export type BuilderMessage = TransactionMessage &
  TransactionMessageWithFeePayer &
  TransactionMessageWithLifetime;
export type Account = {
  pubkey: string;
  signer: boolean;
  writable: boolean;
  source: "static" | "lookup";
};
export type NormalizedTransaction = {
  version: "legacy" | 0 | 1;
  accounts: Account[];
  instructions: { program_id: string; accounts: number[]; data_hex: string }[];
  signature_count: number;
  lookup_table_count: number;
  lookup_writable_count: number;
  lookup_readonly_count: number;
  serialized_size: number;
  transaction_config: Record<string, number | bigint | null> | null;
};
export type Features = ReturnType<typeof extractFeatures>;
export type LookupEvidence = {
  tables: AddressesByLookupTableAddress;
  checkedSlot: bigint;
  cluster: string;
};

export function canonical(value: unknown): string {
  if (typeof value === "bigint") return JSON.stringify(value.toString());
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object")
    return (
      "{" +
      Object.keys(value)
        .filter((k) => (value as Record<string, unknown>)[k] !== undefined)
        .sort()
        .map(
          (k) =>
            JSON.stringify(k) +
            ":" +
            canonical((value as Record<string, unknown>)[k]),
        )
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
export function sha256(bytes: Uint8Array | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

/** Decode the authoritative wire input. Supplied features are never accepted. */
export function normalizeWire(
  wire: Uint8Array,
  lookup?: LookupEvidence,
): NormalizedTransaction {
  const tx = getTransactionDecoder().decode(wire);
  if (
    !Buffer.from(getTransactionEncoder().encode(tx)).equals(Buffer.from(wire))
  )
    throw new Error("noncanonical_transaction");
  const m = getCompiledTransactionMessageDecoder().decode(tx.messageBytes);
  if (
    !Buffer.from(getCompiledTransactionMessageEncoder().encode(m)).equals(
      Buffer.from(tx.messageBytes),
    )
  )
    throw new Error("noncanonical_message");
  const h = m.header;
  if (
    h.numSignerAccounts < 1 ||
    h.numReadonlySignerAccounts >= h.numSignerAccounts ||
    h.numSignerAccounts > m.staticAccounts.length ||
    h.numReadonlyNonSignerAccounts >
      m.staticAccounts.length - h.numSignerAccounts
  )
    throw new Error("invalid_message_header");
  const accounts: Account[] = m.staticAccounts.map((pubkey, i) => ({
    pubkey,
    signer: i < h.numSignerAccounts,
    writable:
      i < h.numSignerAccounts
        ? i < h.numSignerAccounts - h.numReadonlySignerAccounts
        : i < m.staticAccounts.length - h.numReadonlyNonSignerAccounts,
    source: "static",
  }));
  const lookups = m.version === 0 ? (m.addressTableLookups ?? []) : [];
  let writable = 0,
    readonly = 0;
  for (const role of ["writable", "readonly"] as const)
    for (const l of lookups) {
      const table = lookup?.tables[l.lookupTableAddress];
      if (!table) throw new Error("missing_lookup_evidence");
      const indices =
        role === "writable" ? l.writableIndexes : l.readonlyIndexes;
      if (
        new Set([...l.writableIndexes, ...l.readonlyIndexes]).size !==
        l.writableIndexes.length + l.readonlyIndexes.length
      )
        throw new Error("duplicate_lookup_index");
      for (const i of indices) {
        const pubkey = table[i];
        if (!pubkey) throw new Error("invalid_lookup_index");
        accounts.push({
          pubkey,
          signer: false,
          writable: role === "writable",
          source: "lookup",
        });
        if (role === "writable") writable++;
        else readonly++;
      }
    }
  if (
    new Set(accounts.map((a) => a.pubkey)).size !== accounts.length ||
    accounts.length > (m.version === 1 ? 64 : 256)
  )
    throw new Error("invalid_accounts");
  const instructions =
    m.version === 1
      ? m.instructionHeaders.map((ix, i) => ({
          programAddressIndex: ix.programAccountIndex,
          accountIndices: m.instructionPayloads[i]!.instructionAccountIndices,
          data: m.instructionPayloads[i]!.instructionData,
        }))
      : m.instructions;
  const normalized = instructions.map((ix) => {
    const program = accounts[ix.programAddressIndex];
    if (!program) throw new Error("invalid_program_index");
    const indices = [...(ix.accountIndices ?? [])];
    if (indices.some((i) => !accounts[i]))
      throw new Error("invalid_account_index");
    return {
      program_id: program.pubkey,
      accounts: indices,
      data_hex: Buffer.from(ix.data ?? []).toString("hex"),
    };
  });
  let config: NormalizedTransaction["transaction_config"] = null;
  if (m.version === 1) {
    if (
      (m.configMask & ~31) !== 0 ||
      (m.configMask & 3) === 1 ||
      (m.configMask & 3) === 2
    )
      throw new Error("invalid_config_mask");
    let i = 0;
    config = {
      priorityFee: m.configMask & 3 ? m.configValues[i++]!.value : null,
      computeUnitLimit: m.configMask & 4 ? m.configValues[i++]!.value : null,
      loadedAccountsDataSizeLimit:
        m.configMask & 8 ? m.configValues[i++]!.value : null,
      heapSize: m.configMask & 16 ? m.configValues[i++]!.value : null,
    };
  }
  return {
    version: m.version,
    accounts,
    instructions: normalized,
    signature_count: h.numSignerAccounts,
    lookup_table_count: lookups.length,
    lookup_writable_count: writable,
    lookup_readonly_count: readonly,
    serialized_size: wire.length,
    transaction_config: config,
  };
}

export function extractFeatures(tx: NormalizedTransaction) {
  const risks = new Set<string>();
  const budget: {
    requested_compute_units: number | null;
    requested_loaded_accounts_bytes: number | null;
    requested_heap_bytes: number | null;
    requested_micro_lamports: bigint | null;
    requested_priority_fee_lamports: bigint | null;
  } = {
    requested_compute_units: null,
    requested_loaded_accounts_bytes: null,
    requested_heap_bytes: null,
    requested_micro_lamports: null,
    requested_priority_fee_lamports: null,
  };
  const raw = tx.instructions.map((ix) => Buffer.from(ix.data_hex, "hex"));
  if (tx.version === 1) {
    const c = tx.transaction_config!;
    budget.requested_compute_units = Number(c.computeUnitLimit ?? 0);
    budget.requested_loaded_accounts_bytes = Number(
      c.loadedAccountsDataSizeLimit ?? 0,
    );
    budget.requested_heap_bytes =
      c.heapSize === null ? null : Number(c.heapSize);
    budget.requested_priority_fee_lamports = BigInt(c.priorityFee ?? 0);
    if (!budget.requested_compute_units) risks.add("v1_zero_compute_limit");
    if (!budget.requested_loaded_accounts_bytes)
      risks.add("v1_zero_loaded_accounts_limit");
    if (tx.instructions.some((ix) => ix.program_id === BUDGET))
      risks.add("v1_compute_budget_noop");
  } else {
    const seen = new Set<number>();
    tx.instructions.forEach((ix, i) => {
      if (ix.program_id !== BUDGET) return;
      const d = raw[i]!;
      const tag = d[0];
      if (tag === undefined || ![1, 2, 3, 4].includes(tag)) {
        risks.add("invalid_compute_budget_instruction");
        return;
      }
      if (seen.has(tag)) risks.add("duplicate_compute_budget_instruction");
      seen.add(tag);
      if (d.length !== (tag === 3 ? 9 : 5)) {
        risks.add("invalid_compute_budget_instruction");
        return;
      }
      if (tag === 3) budget.requested_micro_lamports = d.readBigUInt64LE(1);
      else {
        const n = d.readUInt32LE(1);
        if (tag === 1) budget.requested_heap_bytes = n;
        if (tag === 2) budget.requested_compute_units = n;
        if (tag === 4) budget.requested_loaded_accounts_bytes = n;
      }
    });
    if (budget.requested_compute_units === 0) risks.add("zero_compute_limit");
    if (budget.requested_loaded_accounts_bytes === 0)
      risks.add("zero_loaded_accounts_limit");
  }
  const heap = budget.requested_heap_bytes;
  if (heap !== null && (heap < 32768 || heap > 262144 || heap % 1024 !== 0))
    risks.add("invalid_heap_size");
  if (!tx.instructions.length) risks.add("empty_instruction_list");
  tx.instructions.forEach((ix, i) => {
    if (
      (ix.program_id === SYSTEM && raw[i]!.length < 4) ||
      (TOKENS.has(ix.program_id) && raw[i]!.length === 0)
    )
      risks.add("truncated_instruction_discriminator");
  });
  const max = tx.version === 1 ? 4096 : 1232;
  if (tx.serialized_size > max) risks.add("transaction_size_exceeds_limit");
  if (raw.some((d) => d.length > max))
    risks.add("instruction_data_exceeds_transaction_limit");
  const labels = new Map<number, number>([[0, 0]]);
  const label = (i: number) => {
    if (!labels.has(i)) labels.set(i, labels.size);
    return labels.get(i)!;
  };
  const instructions = tx.instructions.map((ix, i) => ({
    program: ix.program_id,
    program_account: label(
      tx.accounts.findIndex((a) => a.pubkey === ix.program_id),
    ),
    accounts: ix.accounts.map(label),
    discriminator: raw[i]!.subarray(
      0,
      ix.program_id === SYSTEM
        ? 4
        : TOKENS.has(ix.program_id) || ix.program_id === BUDGET
          ? 1
          : 8,
    ).toString("hex"),
    data_length: raw[i]!.length,
  }));
  const role = (a: Account) => [a.signer, a.writable, a.source];
  const unused = tx.accounts
    .filter((_, i) => !labels.has(i))
    .map(role)
    .sort((a, b) => String(a).localeCompare(String(b)));
  const payload = {
    schema: "shape-v1",
    version: tx.version,
    signature_count: tx.signature_count,
    roles: [...labels.keys()].map((i) => role(tx.accounts[i]!)),
    unused_roles: unused,
    instructions,
    lookups: [
      tx.lookup_table_count,
      tx.lookup_writable_count,
      tx.lookup_readonly_count,
    ],
    heap_bytes: heap ?? 32768,
  };
  return {
    schema_version: "shape-v1",
    pattern_id: "shape-v1:" + sha256(canonical(payload)),
    version: tx.version,
    signature_count: tx.signature_count,
    account_count: tx.accounts.length,
    signer_count: tx.accounts.filter((a) => a.signer).length,
    writable_count: tx.accounts.filter((a) => a.writable).length,
    instruction_count: instructions.length,
    program_ids: tx.instructions.map((ix) => ix.program_id),
    instruction_data_lengths: raw.map((d) => d.length),
    total_instruction_data_bytes: raw.reduce((n, d) => n + d.length, 0),
    lookup_table_count: tx.lookup_table_count,
    lookup_writable_count: tx.lookup_writable_count,
    lookup_readonly_count: tx.lookup_readonly_count,
    serialized_size: tx.serialized_size,
    risk_flags: [...risks].sort(),
    ...budget,
  };
}

export function encodeUnsigned(message: BuilderMessage): Uint8Array {
  return new Uint8Array(
    getTransactionEncoder().encode(compileTransaction(message)),
  );
}
/** Existing resource fields are replaced in place; absent fields append before binding. */
export function prepareResources(
  message: BuilderMessage,
  cu = MAX_CU,
  data = MAX_DATA,
): BuilderMessage {
  const initial = extractFeatures(
    normalizeWire(encodeUnsigned(message), lookupFromBuilder(message)),
  );
  if (
    initial.risk_flags.some(
      (r) =>
        !["v1_zero_compute_limit", "v1_zero_loaded_accounts_limit"].includes(r),
    )
  )
    throw new Error("invalid_resource_configuration");
  return setTransactionMessageLoadedAccountsDataSizeLimit(
    data,
    setTransactionMessageComputeUnitLimit(cu, message),
  );
}
/** Builder lookup metas are checked against separately fetched lookup evidence in bindMessage. */
function lookupFromBuilder(
  message: BuilderMessage,
): LookupEvidence | undefined {
  const tables: Record<string, string[]> = {};
  for (const ix of message.instructions)
    for (const a of ix.accounts ?? [])
      if ("lookupTableAddress" in a) {
        const table = (tables[a.lookupTableAddress] ??= []);
        table[a.addressIndex] = a.address;
      }
  return {
    tables: tables as AddressesByLookupTableAddress,
    checkedSlot: 0n,
    cluster: "",
  };
}
export type BoundMessage = Readonly<{
  wireBase64: string;
  messageIdentity: string;
  transaction: NormalizedTransaction;
  features: Features;
}>;
export function bindMessage(
  message: BuilderMessage,
  lookup?: LookupEvidence,
  expectedWire?: Uint8Array,
): BoundMessage {
  for (const ix of message.instructions)
    for (const account of ix.accounts ?? [])
      if (
        "lookupTableAddress" in account &&
        lookup?.tables[account.lookupTableAddress]?.[account.addressIndex] !==
          account.address
      )
        throw new Error("lookup_builder_mismatch");
  const wire = encodeUnsigned(message);
  if (expectedWire && !Buffer.from(wire).equals(Buffer.from(expectedWire)))
    throw new Error("message_wire_mismatch");
  const transaction = normalizeWire(wire, lookup);
  const compiled = compileTransactionMessage(message);
  // Compilation may normalize peer ordering; identity commits to the compiled message, not signatures.
  const identity =
    "message-v1:" +
    sha256(
      new Uint8Array(getCompiledTransactionMessageEncoder().encode(compiled)),
    );
  return {
    wireBase64: Buffer.from(wire).toString("base64"),
    messageIdentity: identity,
    transaction,
    features: extractFeatures(transaction),
  };
}
export function verifyBoundMessage(
  bound: BoundMessage,
  message: BuilderMessage,
  lookup?: LookupEvidence,
): boolean {
  return bindMessage(message, lookup).messageIdentity === bound.messageIdentity;
}
export function decodeBuilder(
  wire: Uint8Array,
  lookup?: LookupEvidence,
): BuilderMessage {
  const tx = getTransactionDecoder().decode(wire);
  normalizeWire(wire, lookup);
  return decompileTransactionMessage(
    getCompiledTransactionMessageDecoder().decode(tx.messageBytes),
    { addressesByLookupTableAddress: lookup?.tables },
  );
}
export function compiledJson(bound: BoundMessage) {
  const wire = Buffer.from(bound.wireBase64, "base64");
  const m = getCompiledTransactionMessageDecoder().decode(
    getTransactionDecoder().decode(wire).messageBytes,
  );
  const t = bound.transaction;
  const n = m.staticAccounts.length;
  const config = t.transaction_config
    ? Object.fromEntries(
        Object.entries(t.transaction_config).map(([k, v]) => [
          k,
          typeof v === "bigint" ? v.toString() : v,
        ]),
      )
    : undefined;
  return {
    version: t.version,
    message: {
      header: {
        numRequiredSignatures: m.header.numSignerAccounts,
        numReadonlySignedAccounts: m.header.numReadonlySignerAccounts,
        numReadonlyUnsignedAccounts: m.header.numReadonlyNonSignerAccounts,
      },
      accountKeys: m.staticAccounts,
      recentBlockhash: m.lifetimeToken,
      instructions: t.instructions.map((ix) => ({
        programIdIndex: t.accounts.findIndex((a) => a.pubkey === ix.program_id),
        accounts: ix.accounts,
        data: getBase58Decoder().decode(Buffer.from(ix.data_hex, "hex")),
      })),
      ...(config ? { transactionConfig: config } : {}),
      ...(m.version === 0
        ? {
            addressTableLookups: (m.addressTableLookups ?? []).map((l) => ({
              accountKey: l.lookupTableAddress,
              writableIndexes: l.writableIndexes,
              readonlyIndexes: l.readonlyIndexes,
            })),
          }
        : {}),
    },
    meta: {
      loadedAddresses: {
        writable: t.accounts
          .slice(n)
          .filter((a) => a.writable)
          .map((a) => a.pubkey),
        readonly: t.accounts
          .slice(n)
          .filter((a) => !a.writable)
          .map((a) => a.pubkey),
      },
    },
  };
}
