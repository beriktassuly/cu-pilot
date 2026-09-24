# Local program upgrade integration

The explicit upgrade check executes a real signed loader-v3 `Upgrade` instruction
inside offline Surfpool 1.5.0, observes the changed ProgramData deployment slot,
suspends the old resource profile, and verifies that the next bound estimate actually
simulates. No public cluster, paid provider, committed key or binary fixture is used.

Install the locked JavaScript dependencies, then run:

```sh
npm --prefix typescript ci
npm --prefix tests/integration/runtime ci
uv sync --locked --group dev
uv run python tests/integration/program_upgrade.py
```

Surfpool's native package requires Linux x64 or supported macOS. On Windows use WSL
with Node 24 or later. Set `CU_PILOT_UPGRADE_NODE` to a JSON array containing the entire
Linux launch command, including the script path. For example, adapt these paths to
the checkout and installed Linux Node:

```powershell
$env:CU_PILOT_UPGRADE_NODE='["wsl","-d","Ubuntu","--cd","/mnt/c/Users/berek/cu-pilot","--exec","/mnt/c/Users/berek/cu-pilot/work/node-linux/bin/node","tests/integration/upgrade.mjs"]'
uv run python tests/integration/program_upgrade.py
```

The Python runner fails with `INCOMPLETE INTEGRATION` if the child runtime cannot
start or respond. It does not convert unavailable prerequisites into passing skips.

## What runs

1. Start an offline Surfpool process with its **default feature configuration**.
2. Read the ELF from the Memo program bundled in that local runtime. Deploy a copy
   to a fresh local program address with the runtime's deployment fixture helper.
3. Seed the initial upgrade authority and upload buffer using local account setup.
   All signing keys exist only in the local test process.
4. Collect six real simulations of a message invoking the copied program, including
   CU and loaded-account measurements. Explicitly release a local test profile and
   verify an accepted prediction with no additional RPC call.
5. Build the official SDK's loader-v3 `Upgrade` instruction, sign with the ephemeral
   test authority, submit it to the local runtime with preflight enabled, and check
   successful execution metadata and the changed deployment slot.
6. Read the deployed Program and ProgramData accounts from the runtime. Run the
   production deployment decoder and profile policy, verify suspension, then verify
   an actual fallback simulation through `estimate_resources`.

The instruction is built with pinned `@solana-program/loader-v3@0.7.0` and Kit 8.3.0.
The [official loader client](https://github.com/solana-program/loader-v3) defines the
SDK instruction, and the [loader-v3 state source](https://docs.rs/crate/solana-loader-v3-interface/latest/source/src/state.rs)
defines the initial ProgramData and Buffer metadata layouts used during local setup.

The test reloads the **same executable payload** through an actual Upgrade
transaction. It verifies deployment-change detection and conservative invalidation,
not a measured resource increase from new program logic. Changing the authority or
account bytes during initial fixture setup is not described as the upgrade; the
later signed loader instruction is the event under test.

The six-simulation artifact uses `min_samples=2`, `min_calibration_samples=2`, and
`max_joint_underestimation_rate=0.8` solely to exercise lifecycle transitions in a
short integration test. These explicitly weak settings are not evidence of
production qualification. Observations remain labeled `local-runtime`; no synthetic
measurements are used to release the test artifact.

## Snapshot and runtime boundaries

The fixture knows its ProgramData address from the initial Program account. It
fetches the Program, ProgramData and relevant native accounts in **one actual
`getMultipleAccounts` response**. A small reader adapter supplies subsets of that
same atomic response to `refresh_deployments`; the production decoder still checks
the ProgramData pointer, owner, loader state, deployment slot and fingerprint. This
is a projection of real runtime evidence, not handcrafted RPC account data.

This avoids a Surfpool context quirk: `getSlot` can lag an account response context
by one slot, and separate batches can cross bank contexts. The test uses the newest
actually observed account context when checking freshness. The general production
watcher remains strict and rejects inconsistent two-batch snapshots.

The loader-upgrade test uses default runtime features. With `allFeatures: true`,
Surfpool 1.5.0 rejected redeployment of its bundled Memo ELF with the runtime log
`Detected sbpf_version required by the executable which are not enabled`. The
separate legacy/v0/v1 transaction suite uses all features to exercise v1. These are
different explicit runtime configurations; one passing matrix is not evidence for
the other. The upgrade manifest's runtime identity includes the default-feature
configuration, in addition to the reported runtime build identity.

A successful observed run reported Surfpool 1.5.0, `solana-core: 4.1.2`, feature-set
3345198602, a 2,304-byte bundled Memo ELF, and 2,670 CUs for the actual upgrade.
Deployment slot changed from 101 to 2000; the watcher returned `deployment_changed`,
and the following estimate returned `simulation_success` with `profile_suspended`
and one resource simulation. Slot values can vary with local runtime startup.
The program prints its own runtime and outcome report on every run.

## Known CPI dependency upgrade

A separate check covers a changed dependency while its caller remains unchanged:

```sh
uv run python tests/integration/cpi_program_upgrade.py
```

On Windows, use the same launcher configuration described above, named
`CU_PILOT_CPI_UPGRADE_NODE`, with `tests/integration/cpi_upgrade.mjs` as the script.
It uses the same locked dependencies and fails explicitly when prerequisites are
missing. No additional compiler, program binary or public-network access is needed.

The controlled builder prepares Associated Token Program `CreateIdempotent` for a
local mint. A real runtime simulation must succeed and contain Token and System
`invoke [2]` logs, establishing actual cross-program execution. The six collected
resource labels then exercise the same explicitly weak local qualification settings
described above. The manifest records Associated Token -> Token/System dependencies
and includes deployment fingerprints for the complete declared closure.

The Token program is already bundled as loader-v3. Initial fixture setup supplies
an ephemeral authority, a buffer containing its bundled 100,312-byte ELF, and a
local deployment slot of 100. This replaces the public-cluster deployment slot in
the bundled account, which would otherwise be in the future relative to the local
bank. These setup changes happen before any observations; they are not the event
asserted as an upgrade. The later signed SDK `Upgrade` actually executes through
the loader and changes the deployment slot to 2000.

The test verifies that **only Token's fingerprint changes**. The Associated Token,
System and Compute Budget identities stay unchanged, yet the production registry
returns `deployment_changed`, suspends the profile and forces a successful actual
resource simulation. The successful observed run reported 2,670 CUs for the loader
upgrade and 13,724 CUs for the initial ATA simulation; PDA derivation and local
keys can change the latter between runs. The Token executable payload is unchanged,
so this verifies dependency invalidation rather than a resource increase caused by
different program logic.

The instruction account order and discriminator follow the
[official Associated Token interface](https://github.com/solana-program/associated-token-account/blob/main/interface/src/instruction.rs),
and the mint fixture follows the
[official SPL Token state layout](https://github.com/solana-program/token/blob/main/interface/src/state.rs).
