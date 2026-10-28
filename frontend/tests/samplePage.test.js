import test from 'node:test'
import assert from 'node:assert/strict'
import { createSamplePageLoader } from '../src/samplePage.js'
const query = (overrides={}) => ({ q:'',pending:true,offset:0,limit:20,...overrides })
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve}}

test('same-label review removes the approved row from pending list while preserving saved detail/draft',async()=>{
 const loader=createSamplePageLoader();let visible=query(),page={items:[{id:'reviewed',reviewed:0}],total:1,offset:0}
 const selected={id:'reviewed',reviewed:1,revision:1,label:'ham'},draft='ham',note='confirmed literal reminder'
 const accepted=await loader.refresh(()=>visible,async q=>{assert.equal(q.pending,true);return {items:[],total:0}},p=>{page=p})
 assert.equal(accepted,true);assert.deepEqual(page.items,[]);assert.equal(page.total,0);assert.equal(page.offset,0)
 assert.equal(selected.revision,1);assert.equal(draft,'ham');assert.equal(note,'confirmed literal reminder')
})

test('removing the only row on last page reloads the previous valid page',async()=>{
 const loader=createSamplePageLoader();let visible=query({offset:20}),committed=null;const offsets=[]
 await loader.refresh(()=>visible,async q=>{offsets.push(q.offset);return q.offset===20?{items:[],total:20}:{items:Array.from({length:20},(_,id)=>({id})),total:20}},p=>{committed=p})
 assert.deepEqual(offsets,[20,0]);assert.equal(committed.offset,0);assert.equal(committed.items.length,20);assert.equal(committed.total,20)
})

test('confirmed import refreshes matching pending count and rows',async()=>{
 const loader=createSamplePageLoader();const current=query({q:'notebook'});let page={items:[],total:0}
 await loader.refresh(()=>current,async q=>{assert.equal(q.q,'notebook');return {items:[{id:'newly-imported',reviewed:0}],total:1}},p=>{page=p})
 assert.equal(page.total,1);assert.equal(page.items[0].id,'newly-imported')
})

test('late list response cannot replace rows for a newer search/filter draft',async()=>{
 const loader=createSamplePageLoader(),pending=deferred();let visible=query({q:'old'}),commits=0
 const work=loader.refresh(()=>visible,()=>pending.promise,()=>{commits++})
 visible=query({q:'new'});pending.resolve({items:[{id:'old-private-row'}],total:1})
 assert.equal(await work,false);assert.equal(commits,0)
})

test('a newer refresh wins even when an older identical-query response arrives last',async()=>{
 const loader=createSamplePageLoader(),older=deferred(),newer=deferred();const current=query();let page=null
 const first=loader.refresh(()=>current,()=>older.promise,p=>{page=p})
 const second=loader.refresh(()=>current,()=>newer.promise,p=>{page=p})
 newer.resolve({items:[{id:'new'}],total:1});assert.equal(await second,true)
 older.resolve({items:[{id:'old'}],total:1});assert.equal(await first,false);assert.equal(page.items[0].id,'new')
})

test('credential clear invalidates in-flight private rows including a range-reconciliation read',async()=>{
 const loader=createSamplePageLoader(),last=deferred();const current=query({offset:20});let committed=false,calls=0
 const work=loader.refresh(()=>current,async()=>{calls++;return calls===1?{items:[],total:20}:last.promise},()=>{committed=true})
 await new Promise(resolve=>setImmediate(resolve));assert.equal(calls,2);loader.invalidate();last.resolve({items:[{id:'private'}],total:20})
 assert.equal(await work,false);assert.equal(committed,false)
})
