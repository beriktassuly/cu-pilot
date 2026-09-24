// Local-only real Associated Token CPI -> Token dependency upgrade integration.
import assert from 'node:assert/strict';
import { createInterface } from 'node:readline';
import { createRequire } from 'node:module';
import { Surfnet } from './runtime/start.mjs';
const require = createRequire(new URL('../../typescript/package.json', import.meta.url));
const kit = await import(require.resolve('@solana/kit'));
const loader = await import(require.resolve('@solana-program/loader-v3'));
const LOADER = kit.address('BPFLoaderUpgradeab1e11111111111111111111111');
const TOKEN = kit.address('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const ATA = kit.address('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const SYSTEM = kit.address('11111111111111111111111111111111');
const surf = Surfnet.startWithConfig({offline:true, blockProductionMode:'transaction'});
const rpc = kit.createSolanaRpc(surf.rpcUrl);
const emit = value => console.log(JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? item.toString() : item));
try {
  surf.timeTravelToSlot(100);
  // The ELF is bundled with the local runtime; this never reads a public cluster.
  const {value: installed} = await rpc.getAccountInfo(TOKEN, {encoding:'base64'}).send();
  assert.ok(installed?.executable);
  let elf = Buffer.from(installed.data[0], 'base64');
  if (installed.owner === LOADER) {
    assert.equal(elf.readUInt32LE(0), 2);
    const installedData = kit.getAddressDecoder().decode(elf.subarray(4, 36));
    const {value: dataAccount} = await rpc.getAccountInfo(installedData, {encoding:'base64'}).send();
    assert.equal(dataAccount.owner, LOADER);
    const packed = Buffer.from(dataAccount.data[0], 'base64');
    assert.equal(packed.readUInt32LE(0), 3);
    elf = packed.subarray(45);
  }
  assert.equal(elf.subarray(0, 4).toString('hex'), '7f454c46');
  const program = TOKEN;
  // Token is already bundled as loader-v3. Keep its program address and code,
  // seeding only a local authority/upload buffer before the real Upgrade below.
  const {value: deployed} = await rpc.getAccountInfo(program, {encoding:'base64'}).send();
  assert.equal(deployed.owner, LOADER);
  const header = Buffer.from(deployed.data[0], 'base64');
  assert.equal(header.readUInt32LE(0), 2);
  const programData = kit.getAddressDecoder().decode(header.subarray(4, 36));
  const {value: originalData} = await rpc.getAccountInfo(programData, {encoding:'base64'}).send();
  const bytes = Buffer.from(originalData.data[0], 'base64');
  assert.equal(bytes.readUInt32LE(0), 3);
  const payer = kit.address(surf.payer);
  const authorityBytes = kit.getAddressEncoder().encode(payer);
  // Seed only the initial local authority and upload buffer. The subsequent
  // deployment slot/code reload is performed by the real signed Upgrade instruction.
  // Bundled Token carries its public-cluster deployment slot. Establish a local
  // fixture slot before collecting evidence; later upgrade changes are real.
  bytes.writeBigUInt64LE(100n, 4);
  bytes[12] = 1;
  bytes.set(authorityBytes, 13);
  surf.setAccount(programData, Number(originalData.lamports), [...bytes], LOADER);
  const bufferAddress = kit.address(Surfnet.newKeypair().publicKey);
  const buffer = Buffer.alloc(37 + elf.length);
  buffer.writeUInt32LE(1, 0); buffer[4] = 1; buffer.set(authorityBytes, 5); buffer.set(elf, 37);
  surf.setAccount(bufferAddress, 1000000000, [...buffer], LOADER);
  const key = await kit.createKeyPairFromBytes(Uint8Array.from(surf.payerSecretKey));
  const signer = await kit.createSignerFromKeyPair(key);
  const mint = kit.address(Surfnet.newKeypair().publicKey);
  // Official SPL Mint pack: authority36, supply8, decimals1, initialized1,
  // freeze-authority36. Initial local fixture only; ATA creation executes runtime.
  const mintData = Buffer.alloc(82); mintData[44] = 6; mintData[45] = 1;
  surf.setAccount(mint, 1000000000, [...mintData], TOKEN);
  const encoder = kit.getAddressEncoder();
  const [associatedAccount] = await kit.getProgramDerivedAddress({programAddress:ATA,
    seeds:[encoder.encode(payer), encoder.encode(TOKEN), encoder.encode(mint)]});
  // Official associated-token-account interface: CreateIdempotent discriminator1,
  // funding, ATA, wallet, mint, System, Token; compiled using Kit's real encoder.
  const createAccount = {programAddress:ATA, data:Uint8Array.of(1), accounts:[
    {address:payer, role:3}, {address:associatedAccount, role:1},
    {address:payer, role:0}, {address:mint, role:0},
    {address:SYSTEM, role:0}, {address:TOKEN, role:0}]};
  async function build(instruction) {
    const {value:lifetime} = await rpc.getLatestBlockhash({commitment:'confirmed'}).send();
    return kit.pipe(
      kit.createTransactionMessage({version:'legacy'}),
      m => kit.setTransactionMessageFeePayer(payer, m),
      m => kit.setTransactionMessageLifetimeUsingBlockhash(lifetime, m),
      m => kit.appendTransactionMessageInstruction(instruction, m),
      m => kit.setTransactionMessageComputeUnitLimit(1400000, m),
      m => kit.setTransactionMessageLoadedAccountsDataSizeLimit(67108864, m),
    );
  }
  surf.timeTravelToSlot(110);
  const probe = await build(createAccount);
  const {value:cpi} = await rpc.simulateTransaction(kit.getBase64EncodedWireTransaction(kit.compileTransaction(probe)),
    {encoding:'base64', sigVerify:false, replaceRecentBlockhash:false}).send();
  assert.equal(cpi.err, null, JSON.stringify(cpi.err));
  assert.ok(cpi.logs.some(line => line === `Program ${TOKEN} invoke [2]`), 'Token dependency must execute by CPI');
  assert.ok(cpi.logs.some(line => line === `Program ${SYSTEM} invoke [2]`), 'System dependency must execute by CPI');
  assert.ok(cpi.logs.some(line => line === `Program ${ATA} success`));
  const {value: ataProgram} = await rpc.getAccountInfo(ATA, {encoding:'base64'}).send();
  const ataHeader = Buffer.from(ataProgram.data[0], 'base64');
  const programDataAccounts = [programData];
  if (ataProgram.owner === LOADER) {
    assert.equal(ataHeader.readUInt32LE(0), 2);
    programDataAccounts.push(kit.getAddressDecoder().decode(ataHeader.subarray(4, 36)));
  }
  surf.drainEvents();
  emit({rpc_url:surf.rpcUrl, program, program_data:programData, source_program:TOKEN,
    top_level_program:ATA, dependency_program:TOKEN, system_program:SYSTEM,
    program_data_accounts:programDataAccounts,
    actual_cpi_succeeded:true, cpi_compute_units:cpi.unitsConsumed,
    runtime:await rpc.getVersion().send(), elf_bytes:elf.length, current_slot:await rpc.getSlot().send()});
  for await (const line of createInterface({input:process.stdin})) {
    const command = JSON.parse(line);
    if (command.action === 'stop') break;
    surf.timeTravelToSlot(command.slot);
    if (command.action === 'sample') {
      const message = await build(createAccount);
      emit({current_slot:await rpc.getSlot().send(), serialized_base64:kit.getBase64EncodedWireTransaction(kit.compileTransaction(message))});
      surf.drainEvents();
      continue;
    }
    if (command.action !== 'upgrade') throw new Error('Unsupported local upgrade action');
    const instruction = loader.getUpgradeInstruction({programDataAccount:programData,
      programAccount:program, bufferAccount:bufferAddress, spillAccount:payer, authority:signer});
    const message = await build(instruction);
    const signed = await kit.signTransaction([key], kit.compileTransaction(message));
    const signature = await rpc.sendTransaction(kit.getBase64EncodedWireTransaction(signed),
      {encoding:'base64', skipPreflight:false, preflightCommitment:'confirmed'}).send();
    const execution = await rpc.getTransaction(signature,
      {encoding:'json', commitment:'confirmed', maxSupportedTransactionVersion:1}).send();
    assert.equal(execution.meta.err, null);
    const {value: upgraded} = await rpc.getAccountInfo(programData, {encoding:'base64'}).send();
    const upgradedBytes = Buffer.from(upgraded.data[0], 'base64');
    assert.ok(upgradedBytes.readBigUInt64LE(4) > bytes.readBigUInt64LE(4));
    assert.ok(upgradedBytes.subarray(45).equals(elf));
    const observedAfterUpgrade = Number(await rpc.getSlot().send());
    surf.timeTravelToSlot(Math.max(command.slot + 10, observedAfterUpgrade + 10));
    emit({signature, err:execution.meta.err, units_consumed:execution.meta.computeUnitsConsumed,
      old_deployment_slot:bytes.readBigUInt64LE(4), new_deployment_slot:upgradedBytes.readBigUInt64LE(4),
      current_slot:await rpc.getSlot().send(), instruction_sdk:'@solana-program/loader-v3@0.7.0',
      actual_loader_upgrade:true, code_payload_unchanged:true});
    surf.drainEvents();
  }
} finally {surf.stop();}
