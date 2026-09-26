// Isolated fixture. It never connects to or mutates the interactive demo runtime.
import {createInterface} from 'node:readline';
import {LocalRuntime,json} from '../../apps/payout-demo/bridge.mjs';
const runtime=await LocalRuntime.start({allowTestMutations:true});
const emit=value=>process.stdout.write(json(value)+'\n');
try{
  const fixture=await runtime.prepareDependencyUpgradeFixture();
  emit({...await runtime.dispatch({action:'info'}),...fixture});
  for await(const line of createInterface({input:process.stdin})){
    const command=JSON.parse(line);
    if(command.action==='stop')break;
    try{
      if(command.action==='advance'){
        runtime.surf.timeTravelToSlot(command.slot);
        emit(await runtime.dispatch({action:'info'}));
      }else emit(await runtime.dispatch(command));
    }catch(error){emit({fixture_error:String(error.stack)});}
  }
}finally{runtime.close();}
process.exit(0);
