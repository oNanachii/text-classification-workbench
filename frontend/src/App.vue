<script setup>
import { onMounted, ref } from 'vue'
import LossChart from './components/LossChart.vue'
import { createImportGuard, guardedPreview } from './importGuard.js'
import { createSamplePageLoader } from './samplePage.js'
const tab=ref('predict'), config=ref(null), health=ref(null), service=ref(null), token=ref(''), tokenDraft=ref(''), showAuth=ref(false)
const error=ref(''), notice=ref(''), busy=ref(false), input=ref('Free entry in our weekly prize draw. Claim your reward now.'), result=ref(null)
const finalReport=ref(null)
const runs=ref([]), dataset=ref(null), samples=ref([]), total=ref(0), offset=ref(0), search=ref(''), pending=ref(false), selected=ref(null), draft=ref('ham'), note=ref('')
const csv=ref('text,label\n'), source=ref(''), preview=ref(null), candidate=ref(''), kind=ref('lr'), threshold=ref(0.5)
let authEpoch=0
const importGuard=createImportGuard()
const samplePageLoader=createSamplePageLoader()
function importChanged(){importGuard.invalidate();preview.value=null}
const n=v=>Number.isFinite(v)?v.toLocaleString():'—'
const pct=v=>Number.isFinite(v)?(v*100).toFixed(2)+'%':'—'
const short=v=>v?String(v).slice(0,22)+'…':'—'
const chartHistory=history=>(history||[]).map(r=>({...r,train_loss:r.train_loss??r.training_loss}))
function clearPrivate(){importChanged();samplePageLoader.invalidate();finalReport.value=null;config.value=config.value?{...config.value,operator_authenticated:false}:null;dataset.value=null;samples.value=[];total.value=0;selected.value=null;preview.value=null;runs.value=[];csv.value='text,label\n';source.value='';note.value='';candidate.value=''}
async function api(path,options={}){
 const epoch=authEpoch, credential=token.value
 let r
 try { r=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json',...(credential?{Authorization:'Bearer '+credential}:{}),...options.headers}}) } catch(e) {
  if(epoch!==authEpoch)throw new DOMException('Credential changed','AbortError')
  throw new Error(options.method&&options.method!=='GET'&&path!=='/predict'?'操作结果未知，请重新读取样本、导入或服务状态核对；不要据此认定未提交。':'连接失败，请稍后重新读取。')
 }
 let data;try{data=await r.json()}catch{throw new Error('响应未确认，请重新读取状态；不要据此认定保存未提交。')}
 if(epoch!==authEpoch)throw new DOMException('Credential changed','AbortError')
 if(!r.ok){if(r.status===401&&credential){token.value='';authEpoch++;clearPrivate()}const e=new Error(data.error||data.detail||'请求失败');e.status=r.status;throw e}
 return data
}
async function act(fn){if(busy.value)return;busy.value=true;error.value='';notice.value='';try{await fn()}catch(e){if(e.name!=='AbortError')error.value=e.message}finally{busy.value=false}}
async function refresh(){config.value=await api('/config');health.value=await api('/health');service.value=await api('/service');if(config.value.public||config.value.operator_authenticated){runs.value=(await api('/runs')).runs;finalReport.value=(await api('/final-report')).report;if(!candidate.value)candidate.value=runs.value.find(r=>r.state==='completed')?.run_id||''}}
async function saveToken(){authEpoch++;token.value=tokenDraft.value;tokenDraft.value='';clearPrivate();showAuth.value=false;busy.value=false;await act(refresh)}
async function clearToken(){authEpoch++;token.value='';tokenDraft.value='';clearPrivate();tab.value='predict';busy.value=false;await act(refresh)}
async function predict(){await act(async()=>{result.value=await api('/predict',{method:'POST',body:JSON.stringify({text:input.value})})})}
function sampleQuery(){return {limit:20,offset:offset.value,q:search.value,pending:pending.value}}
async function refreshSampleList(){
 return samplePageLoader.refresh(sampleQuery,query=>api('/samples?'+new URLSearchParams({limit:String(query.limit),offset:String(query.offset),q:query.q,pending:String(query.pending)})),page=>{samples.value=page.items;total.value=page.total;offset.value=page.offset})
}
async function loadSamples(){await act(async()=>{dataset.value=await api('/dataset');await refreshSampleList()})}
async function openSample(id){await act(async()=>{selected.value=await api('/samples/'+id);draft.value=selected.value.label;note.value=''})}
async function saveReview(){await act(async()=>{
 const id=selected.value.id;let d
 try{d=await api('/samples/'+id,{method:'PATCH',body:JSON.stringify({label:draft.value,note:note.value,expected_revision:selected.value.revision})})}
 catch(e){if(e.status===409)error.value='已有更新，请重新读取样本后再保存。';throw e}
 selected.value=d.sample
 const at=samples.value.findIndex(r=>r.id===id);if(at>=0)samples.value[at]=d.sample
 notice.value=d.changed?'标签与历史已在同一事务中保存。':'审核已确认，标签未变，不增加纠错历史。'
 try{await refreshSampleList()}
 catch(e){if(e.name==='AbortError')throw e;throw new Error('审核已保存，但列表刷新未完成，请重新查询；无需重复保存。')}
})}
async function chooseFile(event){
 const file=event.target.files?.[0];if(!file)return
 importChanged()
 if(file.size>5*1024*1024){error.value='文件超过5 MiB';return}
 const epoch=authEpoch,ticket=importGuard.ticket(csv.value,source.value)
 const content=await file.text()
 if(epoch!==authEpoch||!importGuard.accepts(ticket))return
 csv.value=content;if(!source.value)source.value=file.name
 importChanged()
}
async function previewImport(){
 const body={csv:csv.value,source:source.value}
 importChanged();const ticket=importGuard.ticket(body.csv,body.source)
 await act(()=>guardedPreview(importGuard,ticket,()=>api('/imports/preview',{method:'POST',body:JSON.stringify(body)}),()=>({csv:csv.value,source:source.value}),value=>{preview.value=value}))
}
async function confirmImport(){await act(async()=>{
 const d=await api('/imports/confirm',{method:'POST',body:JSON.stringify({preview_id:preview.value.preview_id})})
 notice.value=`已导入 ${d.count} 条，进入待审核池；尚未进入训练快照。`;preview.value=null
 try{dataset.value=await api('/dataset');await refreshSampleList()}
 catch(e){if(e.name==='AbortError')throw e;throw new Error('导入已确认，但列表刷新未完成，请重新查询；如需核对请沿用原预览记录。')}
})}
async function snapshot(){await act(async()=>{const s=await api('/snapshots',{method:'POST'});notice.value='快照已生成：'+s.snapshot_id;dataset.value=await api('/dataset')})}
async function select(){await act(async()=>{await api('/service/select',{method:'POST',body:JSON.stringify({run_id:candidate.value,kind:kind.value,threshold:Number(threshold.value),expected_revision:service.value.revision})});await refresh();notice.value='已选择候选。需重启服务才生效，页面继续显示当前实际加载版本。'})}
async function rollback(){await act(async()=>{await api('/service/rollback',{method:'POST',body:JSON.stringify({expected_revision:service.value.revision})});await refresh();notice.value='已恢复上一选择。需重启服务，并核对实际加载版本。'})}
async function switchTab(value){tab.value=value;error.value='';notice.value='';if(value==='samples'&&config.value?.operator_authenticated&&!config.value?.public)await loadSamples();else await act(refresh)}
onMounted(()=>act(refresh))
</script>

<template>
 <div class="app-shell v2-shell">
  <header class="app-header"><a class="brand" href="/workbench/"><span class="brand-mark" aria-hidden="true">T</span><span><strong>文本分类与数据纠错</strong><span class="brand-subtitle">TEXT MODEL LAB · V2</span></span></a><button v-if="!config?.public" class="button secondary" @click="showAuth=!showAuth">{{ token?'操作令牌已载入':'载入操作令牌' }}</button></header>
  <section v-if="showAuth&&!config?.public" class="card v2-auth" aria-label="操作令牌"><label>操作令牌<input v-model="tokenDraft" type="password" autocomplete="off" aria-label="操作令牌输入"></label><button class="button primary" :disabled="busy" @click="saveToken">载入</button><button class="button secondary" @click="clearToken">清除令牌与私有数据</button><p>仅保存在当前页面内存。公网服务只读，令牌不会开启公网训练。</p></section>
  <nav class="tab-navigation" aria-label="工作台功能"><button v-for="[id,name] in [['predict','文本预测'],['samples','数据审核'],['experiments','实验对比'],['service','服务版本']]" :key="id" class="tab-button" :class="{active:tab===id}" :aria-current="tab===id?'page':undefined" @click="switchTab(id)">{{ name }}</button></nav>
  <main>
   <div v-if="error" class="notice error" role="alert">{{ error }}</div><div v-if="notice" class="notice info" role="status">{{ notice }}</div>
   <div v-if="config?.public" class="notice info">公开只读演示 · 英文短信正常／垃圾二分类。导入、审核和CPU训练在操作者本地工作台进行；预测输入默认不保存。</div>
   <section v-if="tab==='predict'">
    <div class="page-heading"><div><p class="eyebrow">PREDICT</p><h1>使用明确版本的模型预测</h1><p>从数据快照到训练记录，再到当前实际加载的服务版本。</p></div></div>
    <div class="prediction-layout"><form class="card prediction-form" @submit.prevent="predict"><div class="card-heading"><h2>英文短信</h2><span>{{ input.length }} / 2000</span></div><label class="field"><span class="sr-only">待分类文本</span><textarea v-model="input" maxlength="2000" rows="8" aria-label="待分类英文短信"></textarea></label><p>首版只支持英文短信场景；分数不是经过校准的真实概率。</p><div class="form-actions"><button class="button primary" :disabled="busy||!input.trim()||!health?.model_ready">{{ busy?'正在读取…':'开始预测' }}</button><button type="button" class="button secondary" @click="input='Are we still meeting for lunch tomorrow?'">填入正常短信示例</button></div></form>
     <section class="card prediction-result" aria-live="polite"><p class="eyebrow">RESULT</p><template v-if="result"><h2>{{ result.label==='spam'?'预测为垃圾短信':'预测为正常短信' }}</h2><p class="v2-score">{{ pct(result.spam_score) }}</p><p>垃圾分类分数 · 阈值 {{ result.threshold }}</p><dl><dt>实际模型</dt><dd>{{ result.model }} / {{ result.model_version }}</dd><dt>训练快照</dt><dd>{{ result.snapshot_id }}</dd><dt>数据版本</dt><dd>{{ result.dataset_id }}</dd></dl></template><template v-else><h2>{{ health?.model_ready?'等待预测':'尚未加载模型' }}</h2><p>当前版本：{{ health?.loaded?.run_id||'未选择' }} / {{ health?.loaded?.kind||'—' }}</p></template></section>
    </div>
   </section>
   <section v-else-if="tab==='samples'">
    <div class="page-heading"><div><p class="eyebrow">REVIEW</p><h1>导入、复核与训练快照</h1><p>新数据先审核；标签保存不会自动改变旧模型。</p></div></div>
    <div v-if="!config?.operator_authenticated||config?.public" class="card empty-state"><h2>私有操作区</h2><p>请在本地工作台载入操作令牌。公开部署不展示私有样本、导入文件或纠错历史。</p></div>
    <template v-else>
     <section class="card v2-pad"><h2>导入同格式CSV</h2><p>UTF-8，列名 text,label；标签 ham / spam。最多5 MiB、10000行，单文本2000字符。</p><label class="field">来源说明<input v-model="source" maxlength="200" @input="importChanged" placeholder="例如：本人编写的明确标注样本"></label><label class="field">选择CSV<input type="file" accept=".csv,text/csv" @change="chooseFile"></label><label class="field">CSV内容<textarea v-model="csv" rows="5" aria-label="CSV内容" @input="importChanged"></textarea></label><button class="button secondary" :disabled="busy||!source.trim()" @click="previewImport">预览与隔离检查</button><div v-if="preview" class="v2-preview"><h3>预览结果</h3><p>输入 {{ preview.summary.input_rows }} · 可新增 {{ preview.summary.accepted_rows }} · 无效 {{ preview.summary.invalid_rows }} · 重复 {{ preview.summary.duplicate_rows }} · 冲突 {{ preview.summary.conflict_groups }} · 与留出集重叠被拒 {{ preview.summary.heldout_overlap_rejected }}</p><p v-for="r in preview.sample_preview" :key="r.id">{{ r.label }} — {{ r.text }}</p><button class="button primary" :disabled="busy||!preview.summary.accepted_rows" @click="confirmImport">确认导入待审核池</button></div></section>
     <section class="card v2-pad"><div class="v2-row"><h2>样本审核</h2><span>{{ total }} 条</span><button class="button secondary" :disabled="busy" @click="snapshot">导出已审核训练快照</button></div><form class="v2-row" @submit.prevent="offset=0;loadSamples()"><input v-model="search" aria-label="搜索训练样本" placeholder="搜索样本"><label><input v-model="pending" type="checkbox">仅待审核</label><button class="button secondary" :disabled="busy">查询</button></form><div class="table-scroll"><table><thead><tr><th>文本</th><th>标签</th><th>修订</th><th>审核</th><th>操作</th></tr></thead><tbody><tr v-for="r in samples" :key="r.id"><td class="v2-text">{{ r.text }}</td><td>{{ r.label }}</td><td>v{{ r.revision }}</td><td>{{ r.source.startsWith('uci:')&&r.revision===0?'原数据标签':r.reviewed?'已审核':'待审核' }}</td><td><button class="text-button" @click="openSample(r.id)">审阅</button></td></tr></tbody></table></div><p v-if="!samples.length" role="status">当前筛选下暂无样本。</p><div class="v2-row"><button class="button secondary" :disabled="busy||offset===0" @click="offset=Math.max(0,offset-20);loadSamples()">上一页</button><span>{{ total?offset+1:0 }}–{{ Math.min(offset+20,total) }} / {{ total }}</span><button class="button secondary" :disabled="busy||offset+20>=total" @click="offset+=20;loadSamples()">下一页</button></div></section>
     <section v-if="selected" class="card v2-pad"><h2>复核样本 · 修订 v{{ selected.revision }}</h2><p class="v2-raw">{{ selected.text }}</p><form @submit.prevent="saveReview"><label class="field">复核标签<select v-model="draft"><option value="ham">ham 正常短信</option><option value="spam">spam 垃圾短信</option></select></label><label class="field">复核依据<textarea v-model="note" maxlength="500" rows="3"></textarea></label><div class="v2-row"><button class="button primary" :disabled="busy||(!note.trim()&&(!selected.reviewed||draft!==selected.label))">保存审核</button><button type="button" class="button secondary" @click="openSample(selected.id)">重新读取服务器版本</button></div></form><h3>标签历史</h3><p v-if="!selected.history.length">暂无标签修改；同标签审核不增加纠错历史。</p><p v-for="r in selected.history" :key="r.id">v{{ r.revision }}：{{ r.old_label }} → {{ r.new_label }} · {{ r.note }}</p></section>
     <section class="card v2-pad"><h2>不可变训练快照</h2><p v-for="s in dataset?.snapshots" :key="s.snapshot_id" class="v2-break">{{ s.snapshot_id }} · {{ s.count }} 条 · {{ s.corrections }} 次纠错</p><p>训练在本地CLI执行：workbench.py train --snapshot 快照ID --run 新运行名。网页不执行训练命令。</p></section>
    </template>
   </section>
   <section v-else-if="tab==='experiments'">
    <div class="page-heading"><div><p class="eyebrow">EXPERIMENTS</p><h1>按数据版本核对实验</h1><p>以下来自实际训练报告；验证结果不代表最终测试或线上效果。</p></div><button class="button secondary" :disabled="busy" @click="act(refresh)">刷新记录</button></div>
    <p v-if="!runs.length" class="card empty-state">{{ config?.public?'没有公开实验记录。':'载入操作令牌后读取本地实验。' }}</p>
    <section v-if="finalReport" class="card v2-pad"><h2>独立最终测试报告</h2><p>冻结候选 {{ finalReport.candidate.run_id }} / {{ finalReport.candidate.kind }} · 阈值 {{ finalReport.candidate.threshold }}。只评估一次，不用于继续选择模型。</p><p>测试样本 {{ finalReport.metrics.count }} · 查准率 {{ pct(finalReport.metrics.precision) }} · 召回率 {{ pct(finalReport.metrics.recall) }} · macro-F1 {{ pct(finalReport.metrics.macro_f1) }}</p><p>{{ finalReport.scope }}</p></section>
    <article v-for="run in runs" :key="run.run_id" class="card v2-pad"><div class="v2-row"><h2>{{ run.run_id }}</h2><span>{{ run.state }}</span></div><p class="v2-break">数据 {{ run.dataset_id }} · 快照 {{ run.snapshot_id }}</p><p>训练 {{ n(run.training_count) }} 条 · 新增 {{ n(run.imported_count) }} 条 · 纠错 {{ n(run.correction_count) }} 次 · 验证 {{ n(run.validation_count) }} 条 · {{ run.test_set_evaluated?'本报告包含最终测试':'本训练报告仅含验证结果；最终测试另记' }}</p><p v-if="run.data_audit">数据规则：{{ run.data_audit.rule }}。{{ run.data_audit.near_duplicate_scope }}</p><div class="table-scroll"><table><thead><tr><th>模型</th><th>查准率</th><th>召回率</th><th>macro-F1</th><th>误判正常</th><th>漏判垃圾</th><th>检查点</th></tr></thead><tbody><tr v-for="m in run.models" :key="m.kind"><td>{{ m.name }}</td><td>{{ pct(m.metrics.precision) }}</td><td>{{ pct(m.metrics.recall) }}</td><td>{{ pct(m.metrics.macro_f1) }}</td><td>{{ m.metrics.false_positive }}</td><td>{{ m.metrics.false_negative }}</td><td>{{ m.checkpoint_epoch||'—' }}</td></tr></tbody></table></div><template v-for="m in run.models" :key="m.kind"><LossChart v-if="m.history?.length" :name="m.name" :history="chartHistory(m.history)" :checkpoint="m.checkpoint_epoch" /></template><p>固定阈值 {{ run.config.threshold }} · seed {{ run.config.seed }} · 安全数值产物保存加载一致：{{ run.safe_numeric_reload_matches?'通过':'未确认' }}</p><details><summary>查看错误样例计数与来源</summary><p v-for="m in run.models" :key="m.kind">{{ m.kind }}：FP {{ m.metrics.false_positive }}，FN {{ m.metrics.false_negative }}；报告保留样例ID、实际标签、预测和分数。未公开原始私有文本。</p><p class="v2-break">代码摘要 {{ run.code_sha256 }}</p></details></article>
   </section>
   <section v-else>
    <div class="page-heading"><div><p class="eyebrow">SERVING</p><h1>区分候选与实际服务版本</h1><p>选择写入注册清单，校验后重启生效；预测始终报告已加载版本。</p></div></div><section class="card v2-pad"><h2>实际已加载</h2><pre>{{ service?.loaded||'尚未加载' }}</pre><h2>注册清单中的选择</h2><pre>{{ service?.selected||'尚未选择' }}</pre><p :class="['notice',service?.restart_required?'info':'success']">{{ service?.restart_required?'选择尚未生效，需要重启并重新核对。':'注册选择与实际加载版本一致。' }}</p></section>
    <form v-if="config?.operator_authenticated&&!config?.public" class="card v2-pad" @submit.prevent="select"><h2>明确选择模型</h2><label class="field">已完成运行<select v-model="candidate"><option v-for="r in runs.filter(r=>r.state==='completed')" :key="r.run_id" :value="r.run_id">{{ r.run_id }}</option></select></label><div class="form-grid"><label class="field">模型<select v-model="kind"><option value="lr">逻辑回归</option><option value="mlp16">小型MLP 16</option></select></label><label class="field">阈值<input v-model="threshold" type="number" min="0" max="1" step="0.05"></label></div><div class="v2-row"><button class="button primary" :disabled="busy||!candidate">选择候选，重启后生效</button><button type="button" class="button secondary" :disabled="busy||!service?.history?.length" @click="rollback">回退到上次选择</button></div></form>
   </section>
  </main><footer class="app-footer"><span>Text Model Lab · 英文短信示例 · AI辅助研发</span><span>训练版本、软件验证与本人掌握分别记录</span></footer>
 </div>
</template>
