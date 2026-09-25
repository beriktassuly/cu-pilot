// Actual SBF execution in an isolated offline Surfpool. Never silently skips.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { LocalRuntime, ROOT, PROGRAM, TOKEN, ATA, SYSTEM, kit, createInstruction, executeInstruction, instructionTag, queueAddress, ata, json } from '../../apps/payout-demo/bridge.mjs';

const results=[];
const r=await LocalRuntime.start();
const hex=()=>randomBytes(32).toString('hex');
const meta=(address,role=0)=>({address:kit.address(address),role});
const clone=ix=>({...ix,data:Buffer.from(ix.data),accounts:ix.accounts.map(a=>({...a}))});
async function check(name,body){const started=performance.now();await body();results.push({name,passed:true,ms:performance.now()-started});console.log(`PASS ${name}`);}
async function transact(ix,payer=r.executor,signers=[payer]){const bound=await r.build([ix],payer);const signed=await r.sign(bound.wireBase64,signers);const result=await r.submit(signed.wire);assert.ok(result.transaction,'confirmed transaction metadata required');return {...result,wire:signed.wire};}
async function rejected(ix,queue,payer=r.executor,signers=[payer]){
  const before=queue?await r.verify(queue):undefined;
  const result=await transact(ix,payer,signers);assert.ok(result.transaction.meta.err,'malicious instruction unexpectedly succeeded');
  if(before){const after=await r.verify(queue);assert.deepEqual(after.queue.payments,before.queue.payments);assert.equal(after.queue.cursor,before.queue.cursor);assert.equal(after.queue.total_paid,before.queue.total_paid);assert.equal(after.queue.paid_count,before.queue.paid_count);assert.equal(after.vault_balance,before.vault_balance);assert.deepEqual(after.balances,before.balances);}
  return result;
}
async function execution(queue,count=1,overrides={}){const q=await r.queue(queue);return executeInstruction({executor:r.executor,queue,vault:q.vault,mint:q.mint,cursor:q.cursor,count,payments:q.payments,decision:hex(),model:hex(),...overrides});}
async function creation(payments,overrides={}){
  const id=hex(),queue=await queueAddress(r.owner,id),vault=await ata(queue,r.mint);
  return {queue,ix:createInstruction({owner:r.owner,queue,vault,mint:r.mint,source:r.source,executor:r.executor,id,expiry:Math.floor(Date.now()/1000)+86400,payments,...overrides})};
}
function pauseIx(queue,owner,paused=true){return {programAddress:PROGRAM,accounts:[meta(owner,2),meta(queue,1)],data:Buffer.concat([instructionTag(2),Buffer.from([paused?1:0])])};}
async function refundIx(queue,owner=r.owner){const q=await r.queue(queue);return {programAddress:PROGRAM,accounts:[meta(owner,2),meta(queue,1),meta(q.vault,1),meta(q.mint),meta(r.source,1),meta(TOKEN)],data:instructionTag(3)};}

try{
  const created=await r.create({length:4,existing:2});const queue=created.queue.address;
  await check('owner funds immutable terms and queue vault',async()=>{assert.ok(created.correct);assert.equal(created.queue.cursor,0);assert.equal(created.vault_balance,'10000');assert.equal(created.duplicate_count,0);});
  await check('create retry reconciles durable identity without a second debit',async()=>{
    const id=hex(),options={id,length:1};const first=await r.create(options);const sourceBefore=(await r.account(r.source)).value.data[0];const again=await r.create(options);assert.equal(again.queue.address,first.queue.address);assert.equal(again.reconciled_existing,true);assert.equal((await r.account(r.source)).value.data[0],sourceBefore);await assert.rejects(r.create({...options,payments:[{recipient:await r.newSigner(),amount:'2000'}]}),/terms_mismatch/);
  });
  await check('unauthorized executor cannot execute',async()=>{const bad=await r.newSigner();r.surf.fundSol(bad,10000000);await rejected(await execution(queue,1,{executor:bad}),queue,bad);});
  await check('executor signature is required',async()=>{const ix=await execution(queue);ix.accounts[0].role=1;await rejected(ix,queue,r.owner);});
  await check('owner signature is required at queue creation',async()=>{const p=[{recipient:await r.newSigner(),amount:'1'}];const {ix}=await creation(p);ix.accounts[0].role=1;await rejected(ix,undefined,r.executor);});
  await check('unauthorized owner cannot pause',async()=>{await rejected(pauseIx(queue,r.executor),queue);});
  await check('wrong queue, mint, vault and pinned programs rejected',async()=>{
    const another=await r.create({length:1});
    for(const [index,address] of [[1,another.queue.address],[2,r.source],[3,r.source],[4,SYSTEM],[5,TOKEN],[6,ATA]]){
      const ix=clone(await execution(queue));ix.accounts[index].address=kit.address(address);await rejected(ix,queue);
    }
  });
  await check('remaining account order, substitution and writability enforced',async()=>{
    const base=await execution(queue,2);
    const wrongRecipient=clone(base);wrongRecipient.accounts[7].address=kit.address(await r.newSigner());await rejected(wrongRecipient,queue);
    const wrongAta=clone(base);wrongAta.accounts[8].address=kit.address(r.source);await rejected(wrongAta,queue);
    const reversed=clone(base);[reversed.accounts[7],reversed.accounts[8]]=[reversed.accounts[8],reversed.accounts[7]];await rejected(reversed,queue);
    const missing=clone(base);missing.accounts.pop();await rejected(missing,queue);
    const extra=clone(base);extra.accounts.push(meta(await r.newSigner()));await rejected(extra,queue);
    const readonly=clone(base);readonly.accounts[8].role=0;await rejected(readonly,queue);
    const readonlyQueue=clone(base);readonlyQueue.accounts[1].role=0;await rejected(readonlyQueue,queue);
  });
  await check('replacement amounts and malformed audit fields rejected',async()=>{
    const inflated=clone(await execution(queue));inflated.data=Buffer.concat([inflated.data,Buffer.from('ffffffffffffffff','hex')]);await rejected(inflated,queue);
    const truncated=clone(await execution(queue));truncated.data=truncated.data.subarray(0,73);await rejected(truncated,queue);
    const changedTag=clone(await execution(queue));changedTag.data[7]=1;await rejected(changedTag,queue);
  });
  await check('recipient ATA mint, authority, owner and size are checked',async()=>{
    const target=await ata(created.queue.payments[0].recipient,r.mint),original=(await r.account(target)).value;
    const data=Buffer.from(original.data[0],'base64');
    for(const kind of ['mint','authority','owner','size']){
      const malformed=Buffer.from(data);if(kind==='mint')malformed.fill(0,0,32);if(kind==='authority')malformed.fill(0,32,64);
      r.surf.setAccount(target,Number(original.lamports),Uint8Array.from(kind==='size'?malformed.subarray(0,164):malformed),kind==='owner'?SYSTEM:TOKEN);
      await rejected(await execution(queue),queue);
      r.surf.setAccount(target,Number(original.lamports),Uint8Array.from(data),TOKEN);
    }
  });
  await check('non-menu partial counts and zero count rejected',async()=>{await rejected(await execution(queue,3),queue);await rejected(await execution(queue,0),queue);});
  await check('zero amount, duplicate beneficiaries, overflow and empty queue rejected',async()=>{
    const a=await r.newSigner(),b=await r.newSigner();
    for(const payments of [[],[{recipient:a,amount:'0'}],[{recipient:a,amount:'1'},{recipient:a,amount:'2'}],[{recipient:a,amount:'18446744073709551615'},{recipient:b,amount:'1'}]]){
      const {ix,queue:newQueue}=await creation(payments);await rejected(ix,undefined,r.owner);assert.equal((await r.account(newQueue)).value,null,'failed create left queue state');
    }
  });
  await check('on-chain pause prevents execution; resume preserves terms',async()=>{await r.pause(queue,true);await rejected(await execution(queue),queue);await r.pause(queue,false);assert.deepEqual((await r.queue(queue)).payments,created.queue.payments);});
  await check('real transfers execute both existing and missing ATAs',async()=>{
    const ix=await execution(queue,4);const result=await transact(ix);assert.equal(result.transaction.meta.err,null);const verified=await r.verify(queue);assert.ok(verified.correct);assert.equal(verified.queue.cursor,4);assert.equal(verified.queue.paid_count,4);assert.equal(verified.queue.total_paid,'10000');assert.equal(verified.vault_balance,'0');assert.equal(verified.duplicate_count,0);assert.equal(verified.queue.last_decision,Buffer.from(ix.data).subarray(10,42).toString('hex'));assert.equal(verified.queue.last_model,Buffer.from(ix.data).subarray(42,74).toString('hex'));
    results.push({name:'valid_execution_evidence',signature:result.signature,cursor:verified.queue.cursor,balances:verified.balances,compute_units:Number(result.transaction.meta.computeUnitsConsumed)});
  });
  await check('completed queue is terminal',async()=>{await rejected(await execution(queue,1,{cursor:0}),queue);await rejected(pauseIx(queue,r.owner),queue,r.owner);});
  await check('explicit three-payment tail executes',async()=>{const q=await r.create({length:3});const result=await transact(await execution(q.queue.address,3));assert.equal(result.transaction.meta.err,null);const v=await r.verify(q.queue.address);assert.ok(v.correct);assert.equal(v.queue.cursor,3);});
  await check('sixteen-payment queue and eight-payment candidates use real serialization',async()=>{
    const q=await r.create({length:16,existing:8});const counts=[1,2,4,8],calls=r.calls;
    const frozen=await r.candidates({queue:q.queue.address,counts,decisions:Object.fromEntries(counts.map(n=>[String(n),hex()])),model:hex()});
    assert.equal(r.calls-calls,3,'candidate menu must share state and blockhash reads');
    assert.equal(new Set(frozen.candidates.map(c=>c.common_snapshot_digest)).size,1);
    assert.equal(new Set(frozen.candidates.map(c=>c.slot)).size,1);
    for(const candidate of frozen.candidates){assert.ok(candidate.supported);assert.equal(candidate.existing_atas,candidate.count);assert.ok(candidate.serialized_size<=1232);assert.equal(candidate.vault_initialized,true);assert.equal(candidate.vault_frozen,false);assert.equal(candidate.mint_initialized,true);}
    await r.sendInstructions([await execution(q.queue.address,8)]);await r.sendInstructions([await execution(q.queue.address,8)]);
    const verified=await r.verify(q.queue.address);assert.ok(verified.correct);assert.equal(verified.queue.cursor,16);assert.equal(verified.duplicate_count,0);
  });
  await check('concurrent stale decisions and repeated submissions do not duplicate',async()=>{
    const q=await r.create({length:2});const a=await execution(q.queue.address),b=await execution(q.queue.address);
    const successful=await transact(a);assert.equal(successful.transaction.meta.err,null);await rejected(b,q.queue.address);
    // A duplicate RPC response may itself be malformed in the emulator/SDK.
    // Every outcome is reconciled by the original signature and chain state.
    let repeatedSendError=null;try{await r.submit(successful.wire);}catch(error){repeatedSendError=String(error);}
    const v=await r.verify(q.queue.address);assert.ok(v.correct);assert.equal(v.queue.cursor,1);assert.equal(v.queue.paid_count,1);assert.equal(v.duplicate_count,0);
    const receipt=await r.transaction(successful.signature);assert.equal(receipt.meta.err,null);assert.equal((await r.queue(q.queue.address)).cursor,1,'signature reconciliation changed progress');
    results.push({name:'repeated_send_reconciliation',signature:successful.signature,transport_error:repeatedSendError,cursor:v.queue.cursor,duplicate_count:v.duplicate_count});
  });
  await check('failure after first attempted transfer atomically rolls back',async()=>{
    const q=await r.create({length:2,existing:2}),address=q.queue.address;
    const target=await ata(q.queue.payments[1].recipient,r.mint);const account=await r.account(target);const frozen=Buffer.from(account.value.data[0],'base64');frozen[108]=2;
    // Labeled test setup: inject unsupported frozen state into the emulator.
    r.surf.setAccount(target,Number(account.value.lamports),Uint8Array.from(frozen),TOKEN);
    const failure=await rejected(await execution(address,2),address);
    assert.ok(failure.transaction.meta.logMessages.includes(`Program ${TOKEN} success`),'first Token CPI did not succeed before later failure');
    const inner=failure.transaction.meta.innerInstructions.flatMap(group=>group.instructions);assert.equal(inner.length,1);
    const transferData=Buffer.from(kit.getBase58Encoder().encode(inner[0].data));assert.equal(transferData[0],12,'first CPI must be TransferChecked');assert.equal(transferData.readBigUInt64LE(1),1000n);
    const v=await r.verify(address);assert.equal(v.queue.cursor,0);assert.equal(v.queue.total_paid,'0');assert.equal(v.vault_balance,'3000');assert.equal(v.balances[0].balance,'0');
  });
  await check('expiry, owner-only refund, terminal tombstone and identity replay',async()=>{
    const expiry=Math.floor(Date.now()/1000)+120,q=await r.create({length:2,expiry}),address=q.queue.address;
    await rejected(await refundIx(address),address,r.owner);
    r.surf.timeTravelToTimestamp((expiry+1)*1000);
    await rejected(await execution(address),address);
    await rejected(await refundIx(address,r.executor),address);
    const before=await r.account(r.source);const sourceBefore=Buffer.from(before.value.data[0],'base64').readBigUInt64LE(64);
    const result=await transact(await refundIx(address),r.owner);assert.equal(result.transaction.meta.err,null);
    const state=await r.queue(address);assert.equal(state.status,2);assert.equal(state.paused,true);assert.equal(state.cursor,0);assert.equal(state.total_paid,'0');
    const sourceAfter=Buffer.from((await r.account(r.source)).value.data[0],'base64').readBigUInt64LE(64);assert.equal(sourceAfter-sourceBefore,3000n);
    assert.equal(Buffer.from((await r.account(state.vault)).value.data[0],'base64').readBigUInt64LE(64),0n);
    await rejected(await execution(address),address);await rejected(await refundIx(address),address,r.owner);
    const replay=createInstruction({owner:r.owner,queue:address,vault:state.vault,mint:r.mint,source:r.source,executor:r.executor,id:state.queue_id,expiry:expiry+86400,payments:state.payments});await rejected(replay,address,r.owner);
    assert.equal((await r.account(address)).value.owner,PROGRAM,'refund must retain durable queue identity');
  });
  const report={passed:true,runtime:await r.call('getVersion'),elf_digest:r.elf_digest,program:PROGRAM,rpc_calls:r.calls,tests:results};
  mkdirSync(resolve(ROOT,'artifacts/payouts'),{recursive:true});writeFileSync(resolve(ROOT,'artifacts/payouts/program-tests.json'),json(report));
  console.log(`Passed ${results.filter(x=>x.passed).length} real-program security checks.`);
}finally{r.close();}
// Native runtime event workers may otherwise keep Node alive after stop().
process.exit(0);
