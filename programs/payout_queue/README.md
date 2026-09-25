# Local payout queue program

This application program is separate from CU Pilot's unsigned resource-planning
core. It executes one immutable ordered queue of at most 16 classic SPL Token
payments. The reference application uses a test mint; neither this program nor
its audit fields assert that the token represents dollars or a real asset.

## Supported build

The Rust interface is pinned to `solana-program = 2.3.0` and `spl-token = 8.0.0`.
The checked-in Cargo lockfile fixes the transitive dependency graph. This is a
small native Rust program: no Anchor CLI, generated IDL, or Anchor runtime is
required. Pinning these interfaces preserves the already-tested local runtime
instead of upgrading the entire repository for this application.

From the repository root in Linux or WSL Ubuntu, with Cargo and Agave's
`cargo-build-sbf 3.1.15` / platform-tools `1.52` installed:

```sh
cargo test --locked --manifest-path programs/payout_queue/Cargo.toml
cargo build-sbf --manifest-path programs/payout_queue/Cargo.toml \
  --sbf-out-dir programs/payout_queue/target/deploy -- --locked
```

Platform-tools 1.52 supplies SBF Rust 1.89. If an older distribution-packaged
`rustup` rejects the custom Solana toolchain name, after platform-tools is
installed use its compiler directly (the build still uses that pinned toolchain):

```sh
RUSTC="$HOME/.cache/solana/v1.52/platform-tools/rust/bin/rustc" \
  cargo-build-sbf --no-rustup-override \
  --manifest-path programs/payout_queue/Cargo.toml \
  --sbf-out-dir programs/payout_queue/target/deploy -- --locked
```

Use Linux Node for Surfpool on Windows; Windows Node cannot load its Linux native
module. The source has no hardcoded deployment identity: the local application
loads the built ELF under its isolated program identity, which is included in
the deployment/profile evidence. Never treat a rebuilt binary as an already
qualified deployment.

## Authority and state

The queue PDA uses seeds `[b"payout", owner_pubkey, queue_id_32_bytes]`. Its vault
is that PDA's classic SPL associated token account. Creation requires the owner
signature, an initialized classic token mint, the owner's canonical ATA, distinct
nonzero recipient addresses, nonzero integer amounts and a checked total.
It funds the vault and writes the complete immutable terms in one transaction.
The executor is a designated key stored at creation and must sign execution.

Execution requires that executor's signature and exactly the expected cursor.
Only counts 1, 2, 4 and 8 are accepted, plus the exact final remainder if at most
8. The program derives every destination ATA from the next stored recipient and
mint, creates a missing ATA through the pinned ATA program and transfers the
stored amount using classic Token `TransferChecked`. It never accepts an amount
or alternate recipient in the execute instruction. Existing destination accounts
must have the expected token-program owner, wallet authority, mint and initialized
nonfrozen state. Recipient wallets must be empty, nonexecutable System accounts;
program-owned recipient accounts are outside this MVP. The application generates
on-curve test wallets, but the program does not impose an unnecessary curve check
on immutable owner-approved recipients. Owners remain responsible for approving
an address they can use. `Pubkey::is_on_curve()` is unavailable in this pinned SBF
SDK; the initial check failed real execution and was removed before collection.

Owner and executor may themselves be approved recipients. Signature/writable
privileges for their wallet accounts can therefore be elevated by account-meta
merging. Token accounts cannot be signers. Program identities must be readonly,
executable, nonsigners. Vault authority must be the queue PDA with no delegate or
close authority. Native wrapped SOL and Token-2022 are rejected. Remaining-account
length, order, identities and writable destinations are validated explicitly.

The owner can pause/resume only an active queue. Execution rejects a paused queue
or a timestamp at/after expiry. At/after expiry the owner may refund an active
queue's remaining vault balance to the owner's canonical ATA, including any
unsolicited token deposits. Refunding enters a terminal state. Completing all
obligations also enters a terminal state. Neither terminal state permits more
payments or recreation; the queue stays allocated as a durable tombstone. Vault
and queue rent are not reclaimed. Unsolicited deposits after completion remain
locked; this bounded application has no general token-recovery instruction.

The program updates cursor, paid count and total paid only after all transfers.
Solana transaction atomicity rolls back earlier transfers and ATA creation if a
later account/transfer fails. A stale concurrent decision cannot pay again.
Decision and model digests are audit metadata, never debit authority or proof
of correct prediction. Expiry uses the on-chain Clock Unix timestamp.

Executor signature authority also permits it to spend its own SOL on ATA rent
and transaction fees. The token vault does not cap that separate spending; the
application funds a bounded test allowance and limits retries before signing.

## Wire format version 1

All integers use little-endian encoding. Public keys and IDs are raw 32 bytes.
Instruction encodings reject trailing bytes and excess accounts. Every instruction
starts with an eight-byte discriminator: its tag followed by seven zero bytes.
This keeps generic CU Pilot pattern extraction independent of queue cursor/count
and random decision IDs while exact-message binding still covers every argument.

| Instruction | Data | Ordered accounts (`s` signer, `w` writable) |
| --- | --- | --- |
| Create | `discriminator(0):8, queue_id:32, expiry:i64, executor:32, count:u8, (recipient:32, amount:u64)*count` | owner(sw), queue(w), vault(w), mint, ownerATA(w), Token, ATA, System |
| Execute | `discriminator(1):8, expected_cursor:u8, count:u8, decision_id:32, model_digest:32` | executor(sw), queue(w), vault(w), mint, Token, ATA, System, then ordered `(recipient, recipientATA(w))*count` |
| Pause/resume | `discriminator(2):8, paused:u8` (0 or 1) | owner(s), queue(w) |
| Refund | `discriminator(3):8` | owner(s), queue(w), vault(w), mint, ownerATA(w), Token |

Queue data is exactly 872 bytes:

| Offset | Type | Meaning |
| --- | --- | --- |
| 0 | 8 bytes | ASCII `CUPAY001` |
| 8 / 40 / 72 | pubkey | Owner / executor / mint |
| 104 | 32 bytes | Queue identity |
| 136 | i64 | Expiry timestamp |
| 144 / 152 | u64 | Approved total / paid total |
| 160 / 161 | u8 | Cursor / obligation count |
| 162 / 163 | u8 | Paused / status (0 active, 1 completed, 2 refunded) |
| 164 / 165 | u8 | Queue PDA bump / mint decimals |
| 166 / 167 | u8 | Paid count (equals cursor) / reserved zero |
| 168 / 200 | 32 bytes | Last decision ID / model digest |
| 232 | 16 × 40 bytes | Recipient pubkey then integer amount; unused entries zero |

The complete audit history belongs to application observation storage; the queue
holds the latest decision identifiers only. Counts and cursor are u8 because the
maximum queue length is 16; amounts and totals are checked u64 values.

Custom errors, in order starting at 1: InvalidInstruction, InvalidAccounts,
Unauthorized, InvalidQueue, InvalidToken, InvalidProgram, InvalidRecipient,
InvalidAmount, DuplicateRecipient, Overflow, InvalidCount, StaleCursor, Paused,
Expired, NotExpired, Terminal, AlreadyExists, InvalidExpiry.

The direct and transitive CPI dependency closure is payout program, classic Token,
ATA and System. A profile must invalidate when a mutable deployment in that
closure changes, even if the payout program itself is unchanged.

Official interface references checked during implementation:
[recipient verification](https://solana.com/docs/payments/send-payments/verify-address),
[classic Token instruction source](https://github.com/solana-program/token/blob/main/interface/src/instruction.rs),
and [ATA instruction source](https://github.com/solana-program/associated-token-account/blob/main/interface/src/instruction.rs).
Runtime integration tests, rather than a static linter alone, establish the
account-validation and atomic-rollback behavior of this compiled program.
