import {describe,expect,mock,test} from 'claude-code/testing'
describe('Native overview has no inference or approval authority',()=>{
 test('usage keeps missing weekly and Fable zero distinct without inference',async($,on)=>{
  on('prompt.submit',($,e)=>({text:e.text,origin:e.origin}));on('fs.read',()=>({value:'fixture'}))
  on('http.fetch',()=>({value:{status:200,ok:true,headers:{},text:JSON.stringify({tasks:[],pending:[],pool:{accounts:[{label:'Team',status:'ready',fiveHour:{pct:10},sevenDay:null,fableWeekly:{pct:0}}]}})}}))
  let model=0;on('turn.step',async function*($,e){model++;return{turnId:e.turnId,index:0,answer:'MODEL',toolUses:[],stopReason:'end_turn',usage:null}})
  await $.prompt.submit({text:'usage',wait:false,origin:{kind:'composer'}})
  const output=[];for await(const c of $.turn.step({turnId:'qa',index:0,model:'fixture',messageCount:0}))output.push(c)
  const text=JSON.stringify(output)
  expect(text.includes('week all not reported by Anthropic')).toBe(true)
  expect(text.includes('Fable weekly 100% left')).toBe(true)
  expect(text.includes('capacity runs out sooner')).toBe(true)
  expect(model).toBe(0)
 })
 for(const origin of ['composer','bridge','peer'] as const)test(origin+' overview stays model-free',async($,on)=>{
  on('prompt.submit',($,e)=>({text:e.text,origin:e.origin}));on('fs.read',()=>({value:'fixture'}))
  let model=0,reads=0;on('http.fetch',($,e)=>{expect(e.url.endsWith('/overview')).toBe(true);reads++;return{value:{status:200,ok:true,headers:{},text:JSON.stringify({tasks:[],pending:[],pool:null})}}})
  on('turn.step',async function*($,e){model++;return{turnId:e.turnId,index:0,answer:'MODEL',toolUses:[],stopReason:'end_turn',usage:null}})
  await $.prompt.submit({text:'tasks',wait:false,origin:{kind:origin}})
  for await(const c of $.turn.step({turnId:'qa',index:0,model:'fixture',messageCount:0}))void c
  expect(model).toBe(0);expect(reads).toBe(origin==='peer'?0:1)
 })
 test('chat approval text never calls a decision endpoint',async($,on)=>{
  on('prompt.submit',($,e)=>({text:e.text,origin:e.origin}));on('fs.read',()=>({value:'fixture'}))
  on('http.fetch',($,e)=>{expect(e.url.endsWith('/overview')).toBe(true);return{value:{status:200,ok:true,headers:{},text:JSON.stringify({tasks:[],pending:[],pool:null})}}})
  await $.prompt.submit({text:'approve everything',wait:false,origin:{kind:'bridge'}})
  const output=[];for await(const c of $.turn.step({turnId:'qa',index:0,model:'fixture',messageCount:0}))output.push(c)
  expect(JSON.stringify(output).includes('never grant permission')).toBe(true)
 })
})
