const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
const context={localStorage:{getItem(){return null}},TextEncoder,TextDecoder,crypto:webcrypto};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/campaigns.js','utf8')+'\nglobalThis.parseItems=campaignFileItems;globalThis.makeId=stableTargetId;',context);
function file(name,text,chunk=7){const bytes=new TextEncoder().encode(text);let offset=0;return {name,stream(){return new ReadableStream({pull(controller){if(offset>=bytes.length){controller.close();return;}controller.enqueue(bytes.slice(offset,offset+=chunk));}});}};}
async function collect(value){const result=[];for await(const item of context.parseItems(value))result.push(JSON.parse(JSON.stringify(item)));return result;}
(async()=>{
  const items=await collect(file('screen.fasta','>目标 α\r\nACDE\r\nFGH\r\n>目标 α\nACDEFGH\n',1));
  assert.equal(items.length,2);assert.equal(items[0].name,'目标 α');assert.equal(items[0].chains[0].sequence,'ACDEFGH');assert.notEqual(items[0].targetId,items[1].targetId);
  assert.equal(items[0].targetId,await context.makeId(1,'目标 α'));
  const replay=await collect(file('screen.fasta','>目标 α\nACDEFGH\n>目标 α\nACDEFGH',23));assert.equal(items[0].targetId,replay[0].targetId);
  const complex=await collect(file('complex.jsonl','{"targetId":"ab-1","chains":[{"id":"A","sequence":"ACDE"},{"id":"B","sequence":"FGHI"}]}\n'));
  assert.equal(complex[0].targetId,'ab-1');assert.equal(complex[0].chains.length,2);
  await assert.rejects(()=>collect(file('empty.fa','')),/没有有效目标/);
  await assert.rejects(()=>collect(file('bad.fa','ACDE')),/以 >标题/);
  await assert.rejects(()=>collect(file('empty-record.fa','>one\n>two\nACDE')),/缺少序列/);
  await assert.rejects(()=>collect(file('bad.jsonl','{"chains":[]}\n[1]')),/chains 数组/);
  console.log('PASS: streaming Unicode/CRLF FASTA, stable target IDs, duplicate candidates, multichain JSONL and invalid input (8 checks)');
})().catch(error=>{console.error(error);process.exitCode=1});
