import { createInterface } from 'node:readline';
import { Surfnet } from '@solana/surfpool';
import { createRequire } from 'node:module';
const require=createRequire(new URL('../../../typescript/package.json',import.meta.url));
const kit=await import(require.resolve('@solana/kit'));
const {buildTransferBatch,FIXTURE_RECIPIENT}=await import('../../../typescript/dist/src/builder.js');
const {bindMessage,decodeBuilder,canonical}=await import('../../../typescript/dist/src/message.js');
const surf=Surfnet.startWithConfig({offline:true,allFeatures:true,blockProductionMode:'transaction'});
const rpc=kit.createSolanaRpc(surf.rpcUrl);
try {
  surf.timeTravelToSlot(100);surf.fundSol(FIXTURE_RECIPIENT,1000000);
  const {value:life}=await rpc.getLatestBlockhash().send();const currentSlot=await rpc.getSlot().send();
  const message=buildTransferBatch({version:'legacy',payer:kit.address(surf.payer),destinations:[FIXTURE_RECIPIENT,FIXTURE_RECIPIENT],amounts:[1000000n,2000000n],blockhash:life.blockhash,lastValidBlockHeight:life.lastValidBlockHeight});
  const bound=bindMessage(message);
  console.log(canonical({rpc_url:surf.rpcUrl,current_slot:currentSlot.toString(),serialized_base64:bound.wireBase64,payer:surf.payer,runtime:await rpc.getVersion().send()}));
  const key=await kit.createKeyPairFromBytes(Uint8Array.from(surf.payerSecretKey));
  for await(const line of createInterface({input:process.stdin})){
    const command=JSON.parse(line);if(command.action==='stop')break;
    if(command.action==='next'){
      surf.timeTravelToSlot(command.slot);
      const {value:lifetime}=await rpc.getLatestBlockhash().send();const slot=await rpc.getSlot().send();
      const next=buildTransferBatch({version:command.version??'legacy',payer:kit.address(surf.payer),destinations:[FIXTURE_RECIPIENT,FIXTURE_RECIPIENT],amounts:[1000000n,2000000n],blockhash:lifetime.blockhash,lastValidBlockHeight:lifetime.lastValidBlockHeight});
      console.log(canonical({current_slot:slot.toString(),serialized_base64:bindMessage(next).wireBase64}));continue;
    }
    if(command.action==='grow'){
      surf.setAccount(FIXTURE_RECIPIENT,1000000000,Array(command.bytes).fill(0),'11111111111111111111111111111111');
      console.log(JSON.stringify({account:FIXTURE_RECIPIENT,bytes:command.bytes}));continue;
    }
    if(command.action!=='execute')throw new Error('Unsupported local-test action');
    const final=decodeBuilder(Buffer.from(command.serialized_base64,'base64'));
    const signed=await kit.signTransaction([key],kit.compileTransaction(final));
    const wire=kit.getBase64EncodedWireTransaction(signed);
    const signature=await rpc.sendTransaction(wire,{encoding:'base64',skipPreflight:false,preflightCommitment:'confirmed'}).send();
    const transaction=await rpc.getTransaction(signature,{encoding:'base64',maxSupportedTransactionVersion:1,commitment:'confirmed'}).send();
    console.log(canonical({signature,serialized_base64:wire,transaction}));
  }
}finally{surf.stop();}
