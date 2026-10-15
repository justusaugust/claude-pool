import { describe, expect, mock, test } from 'claude-code/testing'

const SESSION='11223344-1122-4334-8122-112233445566', GEN='22334455-1122-4334-8122-112233445566'
const START={cwd:'/public-qa',surface:'terminal',isInteractive:true} as const

describe('private session controls',()=>{
 test('registers the actual Native task without changing inference or permissions',async($,on)=>{
  mock.env(on,{});mock.clock(on);on('session.id',()=>({value:SESSION}));on('session.start',()=>({cwd:START.cwd}))
  on('fs.read',()=>({value:'fixture-worker-token'}));on('command.register',()=>({value:{command:'phone'}}))
  on('process.run',($,e)=>{expect(e.argv[2]).toBe('register');expect(JSON.parse(e.init?.stdin||'{}').session).toBe(SESSION);return{value:{exitCode:0,stdout:JSON.stringify({task:SESSION,session:SESSION,generation:GEN}),stderr:'',isStdoutTruncated:false,isStderrTruncated:false}}})
  const writes:string[]=[];on('env.set',($,e)=>{writes.push(e.name);return{value:undefined}})
  await $.session.start(START)
  expect(writes).toEqual(['CLAUDE_POOL_CONFIG','COCKPIT_CONTROL_TASK','COCKPIT_CONTROL_GENERATION','COCKPIT_CONTROL_SESSION'])
 })
 test('a registration outage does not stop the ordinary Native task',async($,on)=>{
  mock.env(on,{});mock.clock(on);on('session.id',()=>({value:SESSION}));on('session.start',()=>({cwd:START.cwd}))
  on('fs.read',()=>{throw Error('offline')});on('command.register',()=>({value:{command:'phone'}}))
  let calls=0;on('turn.step',async function*($,e){calls++;return{turnId:e.turnId,index:e.index,answer:'ordinary Native',toolUses:[],stopReason:'end_turn',usage:null}})
  await $.session.start(START)
  for await(const c of $.turn.step({turnId:'qa',index:0,model:'fixture',messageCount:0}))void c
  expect(calls).toBe(1)
 })
})
