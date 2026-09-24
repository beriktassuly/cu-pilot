// Explicit local-only loader-v3 upgrade integration. No binary fixtures or keys are written.
import assert from 'node:assert/strict';
import { createInterface } from 'node:readline';
import { createRequire } from 'node:module';
import { Surfnet } from './runtime/start.mjs';
const require = createRequire(new URL('../../typescript/package.json', import.meta.url));
const kit = await import(require.resolve('@solana/kit'));
const loader = await import(require.resolve('@solana-program/loader-v3'));
const LOADER = kit.address('BPFLoaderUpgradeab1e11111111111111111111111');
const MEMO = kit.address('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr');
const surf = Surfnet.startWithConfig({offline:true, blockProductionMode:'transaction'});
const rpc = kit.createSolanaRpc(surf.rpcUrl);
const emit = value => console.log(JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? item.toString() : item));
try {
  surf.timeTravelToSlot(100);
  // The ELF is bundled with the local runtime; this never reads a public cluster.
  const {value: installed} = await rpc.getAccountInfo(MEMO, {encoding:'base64'}).send();
  assert.ok(installed?.executable);
  const elf = Buffer.from(installed.data[0], 'base64');
  assert.equal(elf.subarray(0, 4).toString('hex'), '7f454c46');
  const program = kit.address(Surfnet.newKeypair().publicKey);
  surf.deploy({programId:program, soBytes:[...elf]});
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
  bytes[12] = 1;
  bytes.set(authorityBytes, 13);
  surf.setAccount(programData, Number(originalData.lamports), [...bytes], LOADER);
  const bufferAddress = kit.address(Surfnet.newKeypair().publicKey);
  const buffer = Buffer.alloc(37 + elf.length);
  buffer.writeUInt32LE(1, 0); buffer[4] = 1; buffer.set(authorityBytes, 5); buffer.set(elf, 37);
  surf.setAccount(bufferAddress, 1000000000, [...buffer], LOADER);
  const key = await kit.createKeyPairFromBytes(Uint8Array.from(surf.payerSecretKey));
  const signer = await kit.createSignerFromKeyPair(key);
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
  emit({rpc_url:surf.rpcUrl, program, program_data:programData, source_program:MEMO,
    runtime:await rpc.getVersion().send(), elf_bytes:elf.length, current_slot:await rpc.getSlot().send()});
  for await (const line of createInterface({input:process.stdin})) {
    const command = JSON.parse(line);
    if (command.action === 'stop') break;
    surf.timeTravelToSlot(command.slot);
    if (command.action === 'sample') {
      const message = await build({programAddress:program, data:new TextEncoder().encode('cu-pilot')});
      emit({current_slot:await rpc.getSlot().send(), serialized_base64:kit.getBase64EncodedWireTransaction(kit.compileTransaction(message))});
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
    surf.timeTravelToSlot(command.slot + 2);
    emit({signature, err:execution.meta.err, units_consumed:execution.meta.computeUnitsConsumed,
      old_deployment_slot:bytes.readBigUInt64LE(4), new_deployment_slot:upgradedBytes.readBigUInt64LE(4),
      current_slot:await rpc.getSlot().send(), instruction_sdk:'@solana-program/loader-v3@0.7.0',
      actual_loader_upgrade:true, code_payload_unchanged:true});
  }
} finally {surf.stop();}
