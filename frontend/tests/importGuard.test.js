import test from 'node:test'
import assert from 'node:assert/strict'
import { createImportGuard, guardedPreview } from '../src/importGuard.js'
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve}}
test('late preview cannot replace a newly edited CSV/source draft',async()=>{
 const guard=createImportGuard(),pending=deferred();let draft={csv:'old body',source:'old source'},committed=null
 const ticket=guard.ticket(draft.csv,draft.source)
 const work=guardedPreview(guard,ticket,()=>pending.promise,()=>draft,p=>{committed=p})
 draft={csv:'new body',source:'new source'};guard.invalidate();pending.resolve({preview_id:'old-id'})
 assert.equal(await work,false);assert.equal(committed,null)
})
test('exact body comparison rejects drift even if an input event was missed',async()=>{
 const guard=createImportGuard(),pending=deferred();let draft={csv:'body',source:'original'},committed=false
 const work=guardedPreview(guard,guard.ticket(draft.csv,draft.source),()=>pending.promise,()=>draft,()=>{committed=true})
 draft.source='changed source';pending.resolve({preview_id:'stale'})
 assert.equal(await work,false);assert.equal(committed,false)
})
test('clearing protected state invalidates pending preview and file read tickets',async()=>{
 const guard=createImportGuard(),pending=deferred(),ticket=guard.ticket('body','source');let committed=false
 const work=guardedPreview(guard,ticket,()=>pending.promise,()=>({csv:'body',source:'source'}),()=>{committed=true})
 guard.invalidate();pending.resolve({preview_id:'private-old'})
 assert.equal(await work,false);assert.equal(committed,false);assert.equal(guard.accepts(ticket),false)
})
test('a second file selection or manual edit invalidates the first asynchronous file read',()=>{
 const guard=createImportGuard();guard.invalidate();const first=guard.ticket('original','source')
 guard.invalidate();const second=guard.ticket('original','source')
 assert.equal(guard.accepts(first),false);assert.equal(guard.accepts(second),true)
 guard.invalidate();assert.equal(guard.accepts(second),false)
})
test('unchanged current draft accepts the corresponding preview',async()=>{
 const guard=createImportGuard();let result=null
 assert.equal(await guardedPreview(guard,guard.ticket('body','source'),async()=>({preview_id:'current'}),()=>({csv:'body',source:'source'}),p=>{result=p}),true)
 assert.equal(result.preview_id,'current')
})
