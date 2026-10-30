<script setup>
import { computed, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { isAbort } from '../workbenchApi.js'

const props = defineProps({ api: Function, config: Object, health: Object, service: Object, runs: { type: Array, default: () => [] }, operator: Boolean })
const emit = defineEmits(['refresh'])
const KIND = { lr: '逻辑回归', mlp16: 'MLP 16' }
const FIELDS = [['run_id', '运行'], ['kind', '模型'], ['threshold', '阈值'], ['snapshot_id', '训练快照'], ['dataset_id', '数据版本'], ['weights_sha256', '权重摘要']]
const FALLBACK_TASK = { id: 'english-sms-ham-spam-v1', language: 'English', labels: {} }

const candidate = ref(''), kind = ref('lr'), threshold = ref(0.5)
const action = ref({ state: 'idle' })
const completed = computed(() => props.runs.filter(run => run.state === 'completed'))
watch(completed, list => { if (!list.some(run => run.run_id === candidate.value)) candidate.value = list.at(-1)?.run_id || '' }, { immediate: true })

const loaded = computed(() => props.service?.loaded ?? props.health?.loaded ?? null)
const selected = computed(() => props.service?.selected ?? null)
const restart = computed(() => Boolean(props.service?.restart_required))
const task = computed(() => props.config?.decision?.task || FALLBACK_TASK)
const limits = computed(() => props.config?.decision?.limits || {})
const thresholdValid = computed(() => threshold.value !== '' && Number(threshold.value) >= 0 && Number(threshold.value) <= 1)
const differs = key => Boolean(loaded.value && selected.value && loaded.value[key] !== selected.value[key])
const show = (key, value) => {
  if (value === undefined || value === null) return '—'
  if (key === 'kind') return KIND[value] || value
  const text = String(value)
  return text.length > 30 ? text.slice(0, 28) + '…' : text
}

async function write(path, body, done) {
  action.value = { state: 'saving' }
  try {
    const result = await props.api(path, { method: 'POST', body: JSON.stringify(body) })
    action.value = { state: 'done', text: done(result) }
    emit('refresh')
  } catch (e) {
    if (isAbort(e)) return
    action.value = e.unknown ? { state: 'unknown' } : { state: 'error', text: e.message }
  }
}
function selectCandidate() {
  if (!candidate.value || !thresholdValid.value || action.value.state === 'saving') return
  const before = props.service?.revision
  write('/service/select', { run_id: candidate.value, kind: kind.value, threshold: Number(threshold.value), expected_revision: before }, result => result.revision === before
    ? '与当前注册选择相同，没有变化。'
    : `已写入注册选择（修订 r${result.revision}）。尚未生效：预测仍使用“实际加载”版本，需要操作者重启服务后再核对。`)
}
function rollback() {
  if (action.value.state === 'saving') return
  write('/service/rollback', { expected_revision: props.service?.revision }, result => `已把注册选择恢复为上一选择（修订 r${result.revision}）。同样需要重启服务后核对实际加载版本。`)
}
</script>

<template>
  <section class="panel">
    <header class="panel-head">
      <div>
        <p class="eyebrow">模型与规则</p>
        <h1 id="panel-title" tabindex="-1">实际加载与已选候选</h1>
        <p class="lede">判断始终使用<b>实际加载</b>的版本。选择候选只写入注册清单，重启服务并核对后才算生效。</p>
      </div>
      <button type="button" class="btn btn-outline" @click="emit('refresh')"><Icon name="refresh" />重新读取</button>
    </header>

    <div class="version-compare">
      <article class="card version-card is-loaded" aria-labelledby="loaded-title">
        <header><span class="tag tag-ok">实际加载</span><h2 id="loaded-title">判断正在使用</h2></header>
        <dl v-if="loaded" class="kv">
          <template v-for="[key, label] in FIELDS" :key="key"><dt>{{ label }}</dt><dd :class="{ diff: differs(key) }"><code>{{ show(key, loaded[key]) }}</code></dd></template>
        </dl>
        <p v-else class="empty-mini">服务没有加载本地模型；本地判断不可用。</p>
      </article>
      <div class="version-arrow" aria-hidden="true"><Icon name="arrow" /><span>重启后</span></div>
      <article class="card version-card is-selected" aria-labelledby="selected-title">
        <header><span class="tag tag-accent">注册清单选择</span><h2 id="selected-title">重启后才会加载</h2></header>
        <dl v-if="selected" class="kv">
          <template v-for="[key, label] in FIELDS" :key="key"><dt>{{ label }}</dt><dd :class="{ diff: differs(key) }"><code>{{ show(key, selected[key]) }}</code></dd></template>
        </dl>
        <p v-else class="empty-mini">注册清单里还没有选择。</p>
      </article>
    </div>
    <div class="callout" :class="restart ? 'callout-warn' : 'callout-ok'" role="status">
      <Icon :name="restart ? 'clock' : 'check'" />
      <span class="callout-body">
        <template v-if="restart"><strong>已选候选尚未生效。</strong>判断仍使用左侧实际加载的版本；高亮字段是两者差异。需要操作者重启服务后重新核对，本页面不执行重启。</template>
        <template v-else-if="loaded">注册选择与实际加载一致（注册修订 r{{ service?.revision ?? '—' }}）。</template>
        <template v-else>尚未选择也未加载任何本地模型。</template>
      </span>
    </div>

    <form v-if="operator" class="card" @submit.prevent="selectCandidate">
      <h2 class="card-title">选择候选</h2>
      <p class="fine">提交时附带注册修订号 r{{ service?.revision ?? '—' }}；如果期间已被更新，服务器会拒绝，不会覆盖。</p>
      <div class="form-grid">
        <div class="field"><label for="candidate-run">已完成运行</label><select id="candidate-run" v-model="candidate"><option v-if="!completed.length" value="" disabled>没有已完成的运行</option><option v-for="run in completed" :key="run.run_id" :value="run.run_id">{{ run.run_id }}</option></select></div>
        <div class="field"><label for="candidate-kind">模型</label><select id="candidate-kind" v-model="kind"><option value="lr">逻辑回归</option><option value="mlp16">MLP 16</option></select></div>
        <div class="field"><label for="candidate-threshold">阈值</label><input id="candidate-threshold" v-model="threshold" type="number" min="0" max="1" step="0.05" :aria-invalid="!thresholdValid"><p v-if="!thresholdValid" class="field-note is-error">阈值需在 0 到 1 之间。</p></div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" :disabled="action.state === 'saving' || !candidate || !thresholdValid">写入选择（重启后生效）</button>
        <button type="button" class="btn btn-outline" :disabled="action.state === 'saving' || !service?.history?.length" @click="rollback">回退到上一选择</button>
      </div>
      <div v-if="action.state === 'done'" class="callout callout-ok" role="status"><Icon name="check" /><span class="callout-body">{{ action.text }}</span></div>
      <div v-else-if="action.state === 'unknown'" class="callout callout-warn" role="alert"><Icon name="alert" /><span class="callout-body"><strong>写入结果未知。</strong>请先重新读取服务状态核对修订号，不要直接重复提交。<button type="button" class="link-btn" @click="emit('refresh')">重新读取</button></span></div>
      <div v-else-if="action.state === 'error'" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">{{ action.text }}</span></div>
    </form>
    <div v-else class="state-card compact"><span class="state-icon"><Icon name="lock" /></span><p>{{ config?.public ? '公开部署只读：不能选择或回退模型。' : '载入操作令牌后可选择候选或回退。' }}</p></div>

    <section v-if="operator && service?.history?.length" class="card">
      <h2 class="card-title">注册选择历史</h2>
      <ol class="timeline">
        <li v-for="item in [...service.history].reverse()" :key="item.revision"><span class="timeline-rev">r{{ item.revision }}</span><span><code>{{ item.selected?.run_id }}</code> · {{ KIND[item.selected?.kind] || item.selected?.kind }} · 阈值 {{ item.selected?.threshold }}</span><span class="fine">被替换于 {{ item.replaced_at }}</span></li>
      </ol>
    </section>

    <div class="rules-grid">
      <section class="card" aria-labelledby="rules-title">
        <h2 id="rules-title" class="card-title">任务与标签规则</h2>
        <p><code>{{ task.id }}</code> · {{ task.language === 'English' ? '英文' : task.language }}单条短信</p>
        <dl class="label-defs">
          <div><dt><span class="label-badge ham">ham</span></dt><dd>正常个人、事务或明确订阅的英文短信<span v-if="task.labels.ham" class="en" lang="en">{{ task.labels.ham }}</span></dd></div>
          <div><dt><span class="label-badge spam">spam</span></dt><dd>未经请求的推广、奖品诱导或诈骗英文短信<span v-if="task.labels.spam" class="en" lang="en">{{ task.labels.spam }}</span></dd></div>
        </dl>
        <ul class="rule-list">
          <li>只支持英文短信 ham / spam；中文或其他标签没有验证，也没有支持。</li>
          <li>模型建议（本地或 Jev）不会成为人工标签，也不进入训练快照。</li>
          <li>本地分数未经校准；Jev confidence 是分布集中度。二者都不是正确率。</li>
          <li>双方一致不证明正确；失败或结果未知不计入对照。</li>
        </ul>
      </section>
      <section class="card" aria-labelledby="caps-title">
        <h2 id="caps-title" class="card-title">能力与限制</h2>
        <table class="kv-table">
          <tbody>
            <tr><th scope="row">部署模式</th><td>{{ config?.public ? '公开只读' : '本地工作台' }}</td></tr>
            <tr><th scope="row">操作者</th><td>{{ operator ? '已认证（令牌仅在页面内存）' : config?.public ? '公开部署不接受写入' : '未认证' }}</td></tr>
            <tr><th scope="row">Jev 外部判断</th><td>{{ config?.decision?.enabled ? `已开启 · ${config.decision.model_version}` : '未开启' }}</td></tr>
            <tr><th scope="row">服务器 Key</th><td>{{ config?.decision?.server_key_available ? '可由已认证操作者明确选用' : '不可用' }}</td></tr>
            <tr><th scope="row">本地推断</th><td>{{ health?.model_ready ? '数值格式，无 Torch / pickle' : '未加载模型' }}</td></tr>
            <tr><th scope="row">单条长度</th><td>{{ limits.text_chars ?? 2000 }} 字符</td></tr>
            <tr><th scope="row">判断频率</th><td>每来源 {{ limits.requests_per_minute_per_source ?? '—' }} 次/分钟 · 全局 {{ limits.requests_per_minute_global ?? '—' }} 次/分钟 · 并发 {{ limits.concurrent ?? '—' }}（本地与 Jev 判断共用；超限返回 429，不自动重试）</td></tr>
            <tr><th scope="row">上游截止</th><td>{{ limits.deadline_seconds ?? '—' }} 秒；不自动重试</td></tr>
          </tbody>
        </table>
      </section>
    </div>
  </section>
</template>
