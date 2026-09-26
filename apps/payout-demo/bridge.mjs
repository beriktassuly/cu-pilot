// Application-only local keys and submission. CU Pilot's library remains unsigned.
import { createRequire } from 'node:module';
import { createHash, createPrivateKey, createPublicKey, randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Surfnet } from '../../tests/integration/runtime/start.mjs';
const require = createRequire(new URL('../../typescript/package.json', import.meta.url));
export const kit = await import(require.resolve('@solana/kit'));
const core = await import('../../typescript/dist/src/message.js');
export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
export const TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';
export const ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL';
export const SYSTEM = '11111111111111111111111111111111';
const CLOCK = 'SysvarC1ock11111111111111111111111111111111';
export const PROGRAM = kit.getAddressDecoder().decode(createHash('sha256').update('cu-pilot-payout-queue-v1').digest());
export const BUDGET = 'ComputeBudget111111111111111111111111111111';
const LOADER = 'BPFLoaderUpgradeab1e11111111111111111111111';
export const digest = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
export const elfDigest = bytes => createHash('sha256').update(bytes).digest('hex');
function fixtureRecipient(seed,index){
  const bytes=createHash('sha256').update(`${seed}:${index}`).digest();
  const key=createPrivateKey({key:Buffer.concat([Buffer.from('302e020100300506032b657004220420','hex'),bytes]),format:'der',type:'pkcs8'});
  return kit.getAddressDecoder().decode(createPublicKey(key).export({format:'der',type:'spki'}).subarray(-32));
}
export const json = value => JSON.stringify(value, (_k,v) => typeof v === 'bigint' ? Number(v) : v);
const addressBytes = value => Buffer.from(kit.getAddressEncoder().encode(kit.address(value)));
const meta = (address, role=0) => ({address:kit.address(address),role});
export const instructionTag = tag => Buffer.from([tag,0,0,0,0,0,0,0]);
const u64 = n => { const b=Buffer.alloc(8); b.writeBigUInt64LE(BigInt(n)); return b; };
const i64 = n => { const b=Buffer.alloc(8); b.writeBigInt64LE(BigInt(n)); return b; };
export function decodeQueue(data) {
  const b=Buffer.from(data);
  if(b.length!==872 || b.subarray(0,8).toString()!=='CUPAY001') throw Error('invalid_queue');
  const addr=o=>kit.getAddressDecoder().decode(b.subarray(o,o+32));
  return {owner:addr(8),executor:addr(40),mint:addr(72),queue_id:b.subarray(104,136).toString('hex'),expiry:Number(b.readBigInt64LE(136)),total:b.readBigUInt64LE(144).toString(),total_paid:b.readBigUInt64LE(152).toString(),cursor:b[160],length:b[161],paused:!!b[162],status:b[163],paid_count:b[166],last_decision:b.subarray(168,200).toString('hex'),last_model:b.subarray(200,232).toString('hex'),payments:Array.from({length:b[161]},(_,i)=>({recipient:addr(232+40*i),amount:b.readBigUInt64LE(264+40*i).toString()}))};
}
export async function ata(owner,mint) { return (await kit.getProgramDerivedAddress({programAddress:kit.address(ATA),seeds:[addressBytes(owner),addressBytes(TOKEN),addressBytes(mint)]}))[0]; }
export async function queueAddress(owner,id) { return (await kit.getProgramDerivedAddress({programAddress:PROGRAM,seeds:[Buffer.from('payout'),addressBytes(owner),Buffer.from(id,'hex')]}))[0]; }
export function createInstruction({owner,queue,vault,mint,source,executor,id,expiry,payments}) {
  return {programAddress:PROGRAM,accounts:[meta(owner,3),meta(queue,1),meta(vault,1),meta(mint),meta(source,1),meta(TOKEN),meta(ATA),meta(SYSTEM)],data:Buffer.concat([instructionTag(0),Buffer.from(id,'hex'),i64(expiry),addressBytes(executor),Buffer.from([payments.length]),...payments.flatMap(p=>[addressBytes(p.recipient),u64(p.amount)])])};
}
export async function executeInstruction({executor,queue,vault,mint,cursor,count,payments,decision,model}) {
  return {programAddress:PROGRAM,accounts:[meta(executor,3),meta(queue,1),meta(vault,1),meta(mint),meta(TOKEN),meta(ATA),meta(SYSTEM),...(await Promise.all(payments.slice(cursor,cursor+count).map(async p=>[meta(p.recipient),meta(await ata(p.recipient,mint),1)]))).flat()],data:Buffer.concat([instructionTag(1),Buffer.from([cursor,count]),Buffer.from(decision,'hex'),Buffer.from(model,'hex')])};
}
export class LocalRuntime {
  static async start(options={}) {
    const runtime=new LocalRuntime();
    runtime.allowTestMutations=options.allowTestMutations===true;
    runtime.surf=Surfnet.startWithConfig({offline:true,blockProductionMode:'transaction'});
    // Native runtime events use a bounded channel; drain even while the worker is idle.
    runtime.eventPump=setInterval(()=>runtime.surf.drainEvents(),50);
    runtime.eventPump.unref();
    runtime.surf.timeTravelToSlot(100);
    runtime.rpc=kit.createSolanaRpc(runtime.surf.rpcUrl);
    runtime.keys=new Map(); runtime.queues=new Map(); runtime.calls=0; runtime.methodCounts={}; runtime.mutations=0;
    runtime.owner=await runtime.newSigner(); runtime.executor=await runtime.newSigner(); runtime.mint=await runtime.newSigner();
    runtime.surf.fundSol(runtime.owner,20_000_000_000);
    runtime.surf.fundSol(runtime.executor,100_000_000); // Separate, bounded test SOL allowance.
    const so=options.so??resolve(ROOT,'programs/payout_queue/target/deploy/payout_queue.so');
    runtime.elf_digest=elfDigest(readFileSync(so));
    runtime.surf.deploy({programId:PROGRAM,soPath:so});
    runtime.local_dependency_installations=[];
    // Bundled programs are installed locally. Preserve code and authority while
    // replacing upstream deployment-slot headers with this local install slot.
    for(const program of [TOKEN,ATA]){
      const executable=await runtime.account(program);
      if(executable.value?.owner!==LOADER)continue;
      const programBytes=Buffer.from(executable.value.data[0],'base64');
      if(programBytes.length!==36||programBytes.readUInt32LE(0)!==2)throw Error('invalid_dependency_program');
      const programData=kit.getAddressDecoder().decode(programBytes.subarray(4,36));
      const account=await runtime.account(programData);
      if(account.value?.owner!==LOADER)throw Error('invalid_dependency_program_data');
      const bytes=Buffer.from(account.value.data[0],'base64');
      if(bytes.length<45||bytes.readUInt32LE(0)!==3)throw Error('invalid_dependency_header');
      const originalSlot=bytes.readBigUInt64LE(4);bytes.writeBigUInt64LE(100n,4);
      runtime.surf.setAccount(programData,Number(account.value.lamports),Uint8Array.from(bytes),LOADER);
      runtime.local_dependency_installations.push({program,program_data:programData,original_deployment_slot:originalSlot.toString(),local_installation_slot:100,elf_digest:elfDigest(bytes.subarray(45)),fixture:'bundled dependency local installation header'});
    }
    const rent=await runtime.call('getMinimumBalanceForRentExemption',82);
    const sys={programAddress:kit.address(SYSTEM),accounts:[meta(runtime.owner,3),meta(runtime.mint,3)],data:Buffer.concat([Buffer.alloc(4),u64(rent),u64(82),addressBytes(TOKEN)])};
    const init={programAddress:kit.address(TOKEN),accounts:[meta(runtime.mint,1)],data:Buffer.concat([Buffer.from([20,6]),addressBytes(runtime.owner),Buffer.from([0])])};
    await runtime.sendInstructions([sys,init],runtime.owner,[runtime.owner,runtime.mint]);
    runtime.source=await ata(runtime.owner,runtime.mint);
    await runtime.sendInstructions([runtime.ataInstruction(runtime.owner,runtime.owner),{programAddress:kit.address(TOKEN),accounts:[meta(runtime.mint,1),meta(runtime.source,1),meta(runtime.owner,2)],data:Buffer.concat([Buffer.from([7]),u64(1_000_000_000_000_000n)])}],runtime.owner);
    return runtime;
  }
  async call(method,...args) {this.calls++;this.methodCounts[method]=(this.methodCounts[method]??0)+1;return this.rpc[method](...args).send();}
  measurementsSince(calls,methods,started){return {bridge_rpc_calls:this.calls-calls,bridge_rpc_methods:Object.fromEntries(Object.entries(this.methodCounts).map(([method,count])=>[method,count-(methods[method]??0)]).filter(([,count])=>count>0)),bridge_ms:performance.now()-started};}
  async newSigner() { const raw=Surfnet.newKeypair();const key=await kit.createKeyPairFromBytes(Uint8Array.from(raw.secretKey));this.keys.set(raw.publicKey,key);return raw.publicKey; }
  ataInstruction(payer,recipient) {return {programAddress:kit.address(ATA),accounts:[meta(payer,3),meta(this.surf.getAta(recipient,this.mint),1),meta(recipient),meta(this.mint),meta(SYSTEM),meta(TOKEN)],data:Buffer.from([1])};}
  async build(instructions,payer=this.executor,knownLifetime) {
    const lifetime=knownLifetime??(await this.call('getLatestBlockhash',{commitment:'confirmed'})).value;
    const message=kit.pipe(kit.createTransactionMessage({version:'legacy'}),m=>kit.setTransactionMessageFeePayer(kit.address(payer),m),m=>kit.setTransactionMessageLifetimeUsingBlockhash(lifetime,m),m=>kit.appendTransactionMessageInstructions(instructions,m));
    const bound=core.bindMessage(core.prepareResources(message));if(Buffer.from(bound.wireBase64,'base64').length>1232)throw Error('serialized_size_exceeds_1232');return bound;
  }
  async sign(wire,signers=[this.executor]) { const message=core.decodeBuilder(Buffer.from(wire,'base64'));const signed=await kit.signTransaction(signers.map(s=>{const k=this.keys.get(s);if(!k)throw Error('unknown_local_signer');return k;}),kit.compileTransaction(message)); const final=kit.getBase64EncodedWireTransaction(signed);if(!Buffer.from(kit.getTransactionDecoder().decode(Buffer.from(wire,'base64')).messageBytes).equals(Buffer.from(signed.messageBytes)))throw Error('final_message_mismatch');return {wire:final,signature:kit.getSignatureFromTransaction(signed)}; }
  async submit(wire,skipPreflight=true) {
    const started=performance.now();
    const signature=await this.call('sendTransaction',wire,{encoding:'base64',skipPreflight,preflightCommitment:'confirmed',maxRetries:0n});
    const transaction=await this.transaction(signature);this.surf.drainEvents();
    return {signature,transaction,submission_confirmation_ms:performance.now()-started};
  }
  async transaction(signature) {return this.call('getTransaction',kit.signature(signature),{encoding:'base64',commitment:'confirmed',maxSupportedTransactionVersion:1});}
  async sendInstructions(instructions,payer=this.executor,signers=[payer]) {const b=await this.build(instructions,payer);const signed=await this.sign(b.wireBase64,signers);const result=await this.submit(signed.wire);if(!result.transaction||result.transaction.meta.err)throw Error('local_transaction_failed:'+json({error:result.transaction?.meta.err,logs:result.transaction?.meta.logMessages}));return result;}
  async account(address) { const {context,value}=await this.call('getAccountInfo',kit.address(address),{encoding:'base64',commitment:'confirmed'});return {slot:Number(context.slot),value}; }
  async chainTime() {const clock=await this.account(CLOCK);const bytes=Buffer.from(clock.value?.data[0]??'','base64');if(bytes.length!==40)throw Error('invalid_clock_sysvar');const timestamp=Number(bytes.readBigInt64LE(32));if(!Number.isSafeInteger(timestamp)||timestamp<=0)throw Error('invalid_clock_timestamp');return timestamp;}
  async queue(address) {const a=await this.account(address);if(a.value?.owner!==PROGRAM)throw Error('wrong_queue_owner');return {...decodeQueue(Buffer.from(a.value.data[0],'base64')),address,slot:a.slot,vault:await ata(address,this.mint)};}
  async create(options={}) {
    const length=options.length??16;if(!Number.isInteger(length)||length<1||length>16)throw Error('invalid_length');
    const id=options.id??randomBytes(32).toString('hex');
    const requestedPayments=options.payments??(options.recipient_seed===undefined?undefined:Array.from({length},(_,i)=>({recipient:fixtureRecipient(String(options.recipient_seed),i),amount:String((i+1)*1000)})));
    const queue=await queueAddress(this.owner,id),vault=await ata(queue,this.mint);
    const previous=await this.account(queue);
    if(previous.value){
      if(previous.value.owner!==PROGRAM)throw Error('queue_identity_already_in_use');
      const state=decodeQueue(Buffer.from(previous.value.data[0],'base64'));
      if(state.owner!==this.owner||state.executor!==this.executor||state.mint!==this.mint||state.length!==(requestedPayments?.length??length)||(options.expiry!==undefined&&state.expiry!==options.expiry)||(requestedPayments&&json(state.payments)!==json(requestedPayments.map(p=>({recipient:p.recipient,amount:String(p.amount)})))))throw Error('queue_identity_terms_mismatch');
      return {...await this.verify(queue),reconciled_existing:true};
    }
    const payments=requestedPayments??await Promise.all(Array.from({length},async(_,i)=>({recipient:await this.newSigner(),amount:String((i+1)*1000)})));
    const existing=options.existing??0;
    if(!Array.isArray(payments)||payments.length<1||payments.length>16||!Number.isInteger(existing)||existing<0||existing>payments.length)throw Error('invalid_payment_or_existing_account_count');
    const recipientAtas=await Promise.all(payments.map(payment=>ata(payment.recipient,this.mint)));
    const {value:recipientAccounts}=await this.call('getMultipleAccounts',recipientAtas,{encoding:'base64',commitment:'confirmed'});
    for(let i=0;i<recipientAccounts.length;i++){
      const account=recipientAccounts[i];if(!account)continue;
      const data=Buffer.from(account.data[0],'base64');
      if(account.owner===SYSTEM&&data.length===0&&!account.executable)continue;
      if(account.owner!==TOKEN||data.length!==165||account.executable||data[108]!==1||!data.subarray(0,32).equals(addressBytes(this.mint))||!data.subarray(32,64).equals(addressBytes(payments[i].recipient)))throw Error('Recipient token account is unsupported; no queue was funded.');
      if(data.readBigUInt64LE(64)!==0n)throw Error('Use a fresh recipient with a zero token balance; no queue was funded.');
    }
    for(let i=0;i<existing;i++)await this.sendInstructions([this.ataInstruction(this.owner,payments[i].recipient)],this.owner);
    const expiry=options.expiry??(await this.chainTime())+86400;
    const ix=createInstruction({owner:this.owner,queue,vault,mint:this.mint,source:this.source,executor:this.executor,id,expiry,payments});
    const result=await this.sendInstructions([ix],this.owner);
    this.queues.set(queue,{payments,created_signature:result.signature});
    return {...await this.verify(queue),created_signature:result.signature};
  }
  async snapshot(address,count) {
    const started=performance.now(),calls=this.calls;
    const q=await this.queue(address);
    if(!Number.isInteger(count)||count<1||count>8||q.cursor+count>q.length)throw Error('invalid_candidate');
    const recipients=q.payments.slice(q.cursor,q.cursor+count);
    const atas=await Promise.all(recipients.map(p=>ata(p.recipient,q.mint)));
    const {context,value}=await this.call('getMultipleAccounts',[kit.address(address),kit.address(q.vault),kit.address(q.mint),...recipients.map(p=>kit.address(p.recipient)),...atas],{encoding:'base64',commitment:'confirmed'});
    const evidence=value.map((a,i)=>({address:[address,q.vault,q.mint,...recipients.map(p=>p.recipient),...atas][i],owner:a?.owner??SYSTEM,executable:a?.executable??false,lamports:Number(a?.lamports??0),data:a?.data[0]??'',size:a?Buffer.from(a.data[0],'base64').length:0}));
    const fresh=decodeQueue(Buffer.from(evidence[0].data,'base64'));if(fresh.cursor!==q.cursor)throw Error('snapshot_cursor_changed');
    const recipientAccounts=evidence.slice(3+count);
    const vaultData=Buffer.from(evidence[1].data,'base64'),mintData=Buffer.from(evidence[2].data,'base64');
    const vault_initialized=evidence[1].owner===TOKEN&&vaultData.length===165&&vaultData.subarray(0,32).equals(addressBytes(q.mint))&&vaultData.subarray(32,64).equals(addressBytes(address))&&[1,2].includes(vaultData[108]);
    const vault_frozen=vaultData.length===165&&vaultData[108]===2;
    const mint_initialized=evidence[2].owner===TOKEN&&mintData.length===82&&mintData[45]===1;
    const vault_unencumbered=vaultData.length===165&&vaultData.readUInt32LE(72)===0&&vaultData.readUInt32LE(129)===0;
    let supported=!fresh.paused&&fresh.status===0&&vault_initialized&&!vault_frozen&&vault_unencumbered&&mint_initialized&&!evidence[1].executable&&!evidence[2].executable;
    const states=recipientAccounts.map((a,i)=>{if(a.size===0&&a.owner===SYSTEM&&!a.executable&&a.lamports===0)return 'missing';const b=Buffer.from(a.data,'base64');const valid=a.owner===TOKEN&&a.size===165&&!a.executable&&b.subarray(0,32).equals(addressBytes(q.mint))&&b.subarray(32,64).equals(addressBytes(recipients[i].recipient))&&b[108]===1;if(!valid)supported=false;return valid?'initialized':'unsupported';});
    for(const wallet of evidence.slice(3,3+count))if(wallet.owner!==SYSTEM||wallet.size!==0||wallet.executable)supported=false;
    const slot=Number(context.slot);
    return {queue:{...fresh,address,slot,vault:q.vault},count,slot,missing_atas:states.filter(s=>s==='missing').length,existing_atas:states.filter(s=>s==='initialized').length,account_sizes:evidence.map(a=>a.size),ata_states:states,vault_initialized,vault_frozen,vault_unencumbered,mint_initialized,supported,evidence,snapshot_digest:digest(evidence),state_reads:this.calls-calls,snapshot_ms:performance.now()-started};
  }
  async candidate({queue,count,decision,model}) {const snapshot=await this.snapshot(queue,count);const q=snapshot.queue;const ix=await executeInstruction({executor:this.executor,queue,vault:q.vault,mint:q.mint,cursor:q.cursor,count,payments:q.payments,decision,model});const bound=await this.build([ix]);return {...snapshot,wire:bound.wireBase64,message_identity:bound.messageIdentity,serialized_size:Buffer.from(bound.wireBase64,'base64').length};}
  async candidates({queue,counts,decisions,model}){
    if(!Array.isArray(counts)||counts.length===0||new Set(counts).size!==counts.length||counts.some(n=>!Number.isInteger(n)||n<1||n>8))throw Error('invalid_candidate_menu');
    const largest=Math.max(...counts),full=await this.snapshot(queue,largest),q=full.queue;
    const {value:lifetime}=await this.call('getLatestBlockhash',{commitment:'confirmed'});
    const candidates=[];
    for(const count of counts){
      const decision=decisions?.[String(count)];if(typeof decision!=='string'||!/^[0-9a-f]{64}$/.test(decision))throw Error('invalid_decision_identifier');
      const evidence=[...full.evidence.slice(0,3),...full.evidence.slice(3,3+count),...full.evidence.slice(3+largest,3+largest+count)];
      const states=full.ata_states.slice(0,count);
      const supported=!q.paused&&q.status===0&&full.vault_initialized&&!full.vault_frozen&&full.vault_unencumbered&&full.mint_initialized&&states.every(state=>state!=='unsupported')&&evidence.slice(3,3+count).every(a=>a.owner===SYSTEM&&a.size===0&&!a.executable);
      const ix=await executeInstruction({executor:this.executor,queue,vault:q.vault,mint:q.mint,cursor:q.cursor,count,payments:q.payments,decision,model});
      const bound=await this.build([ix],this.executor,lifetime);
      candidates.push({...full,count,evidence,supported,ata_states:states,missing_atas:states.filter(s=>s==='missing').length,existing_atas:states.filter(s=>s==='initialized').length,account_sizes:evidence.map(a=>a.size),snapshot_digest:digest(evidence),common_snapshot_digest:full.snapshot_digest,wire:bound.wireBase64,message_identity:bound.messageIdentity,serialized_size:Buffer.from(bound.wireBase64,'base64').length});
    }
    return {candidates,common_snapshot_digest:full.snapshot_digest,observation_slot:full.slot};
  }
  async verify(address) {
    const q=await this.queue(address);const atas=await Promise.all(q.payments.map(p=>ata(p.recipient,q.mint)));
    const {value}=await this.call('getMultipleAccounts',[kit.address(q.vault),...atas],{encoding:'base64',commitment:'confirmed'});
    const amounts=value.map(a=>a?Buffer.from(a.data[0],'base64').readBigUInt64LE(64).toString():'0');
    const expectedVault=q.status===2?'0':(BigInt(q.total)-BigInt(q.total_paid)).toString();
    return {queue:q,vault_balance:amounts[0],balances:q.payments.map((p,i)=>({...p,ata:atas[i],balance:amounts[i+1],ata_exists:value[i+1]!==null,paid:i<q.cursor})),correct:q.paid_count===q.cursor&&amounts[0]===expectedVault&&q.payments.every((p,i)=>amounts[i+1]===(i<q.cursor?p.amount:'0')),duplicate_count:q.payments.filter((p,i)=>BigInt(amounts[i+1])>BigInt(p.amount)).length};
  }
  async pause(queue,paused) {await this.sendInstructions([{programAddress:PROGRAM,accounts:[meta(this.owner,2),meta(queue,1)],data:Buffer.concat([instructionTag(2),Buffer.from([paused?1:0])])}],this.owner);return this.verify(queue);}
  async prepareDependencyUpgradeFixture(){
    if(!this.allowTestMutations)throw Error('test_fixture_not_enabled');
    if(this.upgradeFixture)throw Error('upgrade_fixture_already_prepared');
    const program=await this.account(TOKEN),header=Buffer.from(program.value.data[0],'base64');
    if(program.value.owner!==LOADER||header.length!==36||header.readUInt32LE(0)!==2)throw Error('invalid_token_program');
    const programData=kit.getAddressDecoder().decode(header.subarray(4,36));
    const account=await this.account(programData),bytes=Buffer.from(account.value.data[0],'base64');
    if(account.value.owner!==LOADER||bytes.readUInt32LE(0)!==3)throw Error('invalid_token_program_data');
    // A disclosed emulator setup step, performed before every training observation.
    // The subsequent deployment change is a real signed Loader-v3 instruction.
    bytes[12]=1;addressBytes(this.owner).copy(bytes,13);
    this.surf.setAccount(programData,Number(account.value.lamports),Uint8Array.from(bytes),LOADER);
    const elf=bytes.subarray(45),bufferAddress=await this.newSigner(),buffer=Buffer.alloc(37+elf.length);
    buffer.writeUInt32LE(1);buffer[4]=1;addressBytes(this.owner).copy(buffer,5);elf.copy(buffer,37);
    this.surf.setAccount(bufferAddress,1_000_000_000,Uint8Array.from(buffer),LOADER);
    this.upgradeFixture={programData,bufferAddress,elf:Buffer.from(elf),oldSlot:bytes.readBigUInt64LE(4)};
    const programDataAccounts=[];
    for(const address of [PROGRAM,TOKEN,ATA]){
      const {value}=await this.account(address);
      if(value.owner===LOADER){const data=Buffer.from(value.data[0],'base64');programDataAccounts.push(kit.getAddressDecoder().decode(data.subarray(4,36)));}
    }
    return {program_data_accounts:programDataAccounts,elf_bytes:elf.length,dependency_elf_sha256:elfDigest(elf),fixture:'local upgrade authority and buffer seeded before collection',program:PROGRAM,dependency:TOKEN,ata:ATA,system:SYSTEM,budget:BUDGET};
  }
  async upgradeDependency(){
    if(!this.allowTestMutations||!this.upgradeFixture)throw Error('test_fixture_not_enabled');
    const fixture=this.upgradeFixture,loader=await import(require.resolve('@solana-program/loader-v3'));
    const signer=await kit.createSignerFromKeyPair(this.keys.get(this.owner));
    const ix=loader.getUpgradeInstruction({programDataAccount:fixture.programData,programAccount:TOKEN,bufferAccount:fixture.bufferAddress,spillAccount:this.owner,authority:signer});
    const result=await this.sendInstructions([ix],this.owner);
    const {value}=await this.account(fixture.programData),bytes=Buffer.from(value.data[0],'base64');
    const newSlot=bytes.readBigUInt64LE(4);
    if(newSlot<=fixture.oldSlot||!bytes.subarray(45).equals(fixture.elf))throw Error('upgrade_evidence_mismatch');
    const observed=Number(await this.call('getSlot',{commitment:'confirmed'}));
    this.surf.timeTravelToSlot(observed+10);
    return {...result,old_deployment_slot:fixture.oldSlot,new_deployment_slot:newSlot,code_payload_unchanged:true,actual_loader_upgrade:true,instruction_sdk:'@solana-program/loader-v3@0.7.0',current_slot:Number(await this.call('getSlot',{commitment:'confirmed'}))};
  }
  async dispatch(command) {
    this.surf.drainEvents();
    const calls=this.calls,methods={...this.methodCounts},started=performance.now();let result;
    switch(command.action){
      case 'info': result={rpc_url:this.surf.rpcUrl,instance_id:this.surf.instanceId,owner:this.owner,executor:this.executor,mint:this.mint,program:PROGRAM,elf_digest:this.elf_digest,runtime:await this.call('getVersion'),slot:Number(await this.call('getSlot',{commitment:'confirmed'})),token_label:'CU Pilot test token',features:'Surfpool 1.5.0 default gates; offline',local_dependency_installations:this.local_dependency_installations};break;
      case 'create': result=await this.create(command);break;
      case 'candidate': result=await this.candidate(command);break;
      case 'candidates': result=await this.candidates(command);break;
      case 'snapshot': result=await this.snapshot(command.queue,command.count);break;
      case 'verify': result=await this.verify(command.queue);break;
      case 'sign': result=await this.sign(command.wire);break;
      case 'send': result=await this.submit(command.wire);break;
      case 'reconcile': result={transaction:await this.transaction(command.signature),verification:await this.verify(command.queue)};break;
      case 'pause': result=await this.pause(command.queue,command.paused);break;
      case 'create_ata': {const q=await this.queue(command.queue);await this.sendInstructions([this.ataInstruction(this.owner,q.payments[command.index].recipient)],this.owner);result=await this.verify(command.queue);break;}
      case 'balance': result={lamports:Number((await this.call('getBalance',kit.address(this.executor),{commitment:'confirmed'})).value)};break;
      case 'reset_allowance': this.surf.setAccount(this.executor,100_000_000,new Uint8Array(),SYSTEM);result={lamports:100_000_000,fixture:'isolated local test allowance reset'};break;
      case 'test_prepare_dependency_upgrade': result=await this.prepareDependencyUpgradeFixture();break;
      case 'test_upgrade_dependency': result=await this.upgradeDependency();break;
      default:throw Error('unknown_local_action');
    }
    return {...result,...this.measurementsSince(calls,methods,started)};
  }
  close(){clearInterval(this.eventPump);this.surf.stop();}
}
async function serve(){
  const runtime=await LocalRuntime.start();const token=randomBytes(32).toString('hex');
  let chain=Promise.resolve();
  const server=createServer((req,res)=>{
    if(req.method!=='POST'||req.headers.authorization!==`Bearer ${token}`){res.writeHead(403).end();return;}
    let body='';req.on('data',chunk=>{body+=chunk;if(body.length>65536)req.destroy();});
    req.on('end',()=>{chain=chain.then(async()=>{const calls=runtime.calls,methods={...runtime.methodCounts},started=performance.now();try{const result=await runtime.dispatch(JSON.parse(body));res.writeHead(200,{'Content-Type':'application/json'}).end(json(result));}catch(error){res.writeHead(400,{'Content-Type':'application/json'}).end(json({error:String(error.message).slice(0,500),...runtime.measurementsSince(calls,methods,started)}));}});});
  });
  server.listen(0,'127.0.0.1',()=>{const path=resolve(process.argv[3]??resolve(ROOT,'artifacts/payouts/runtime.json'));mkdirSync(dirname(path),{recursive:true});writeFileSync(path,json({url:`http://127.0.0.1:${server.address().port}`,token,pid:process.pid,instance_id:runtime.surf.instanceId}),{mode:0o600});console.log(`Isolated payout runtime ready. Private connection file: ${path}`);});
  const stop=()=>server.close(()=>{runtime.close();process.exit(0);});process.on('SIGTERM',stop);process.on('SIGINT',stop);
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){if(process.argv[2]!=='serve')throw Error('Usage: node apps/payout-demo/bridge.mjs serve [private-runtime.json]');await serve();}
