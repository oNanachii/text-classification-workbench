<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Icon from './Icon.vue'
import LockedCard from './LockedCard.vue'
import { createImportGuard, guardedPreview } from '../importGuard.js'
import { isAbort } from '../workbenchApi.js'

const props = defineProps({ api: Function, config: Object, operator: Boolean })
const emit = defineEmits(['navigate'])
const HEADER = 'text,label\n'
const source = ref(''), csv = ref(HEADER), fileName = ref('')
const preview = ref(null), stale = ref(false), busy = ref(''), error = ref('')
// created | existing | unknown; unknown keeps the preview id so the idempotent confirm can be re-checked.
const outcome = ref(null)
const pool = ref(null), poolError = ref(false)
const guard = createImportGuard()
let alive = true

const rows = computed(() => Math.max(0, csv.value.replace(/\s+$/, '').split('\n').length - 1))
const summary = computed(() => preview.value?.summary)
const confirmationUnknown = computed(() => outcome.value?.kind === 'unknown')
const STEPS = ['来源说明', 'CSV 内容', '预览检查', '确认导入']
const step = computed(() => {
  if (outcome.value && outcome.value.kind !== 'unknown') return 5
  if (preview.value || outcome.value) return 4
  if (source.value.trim() && rows.value) return 3
  if (source.value.trim()) return 2
  return 1
})
const short = value => value ? String(value).slice(0, 18) + '…' : '—'

async function loadPool() {
  try {
    const [dataset, pending] = await Promise.all([props.api('/dataset'), props.api('/samples?limit=1&pending=true')])
    if (!alive) return
    pool.value = { total: dataset.samples, pending: pending.total }; poolError.value = false
  } catch (e) {
    if (!isAbort(e) && alive) poolError.value = true
  }
}

function changed() {
  guard.invalidate()
  if (preview.value) stale.value = true
  preview.value = null
  error.value = ''
  if (outcome.value?.kind !== 'unknown') outcome.value = null
}

async function chooseFile(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  changed()
  if (file.size > 5 * 1024 * 1024) { error.value = '文件超过 5 MiB 上限，没有读取。'; return }
  const ticket = guard.ticket(csv.value, source.value)
  let content
  try { content = await file.text() } catch {
    if (alive && guard.accepts(ticket, csv.value, source.value)) error.value = '文件读取失败，请重新选择。'
    return
  }
  // A newer edit, file choice or identity change wins over this read.
  if (!alive || !guard.accepts(ticket, csv.value, source.value)) return
  csv.value = content
  fileName.value = file.name
  if (!source.value.trim()) source.value = file.name
  changed()
}

async function runPreview() {
  if (busy.value || confirmationUnknown.value || !source.value.trim() || !rows.value) return
  const body = { csv: csv.value, source: source.value }
  guard.invalidate()
  preview.value = null; stale.value = false; error.value = ''; outcome.value = null
  const ticket = guard.ticket(body.csv, body.source)
  busy.value = 'preview'
  try {
    await guardedPreview(guard, ticket, () => props.api('/imports/preview', { method: 'POST', body: JSON.stringify(body) }), () => ({ csv: csv.value, source: source.value }), value => { preview.value = value })
  } catch (e) {
    if (!isAbort(e) && alive && guard.accepts(ticket, csv.value, source.value)) error.value = e.unknown ? '预览没有确认结果。预览本身不写入样本，可以直接重新预览。' : e.message
  } finally {
    if (alive) busy.value = ''
  }
}

async function confirm(previewId = preview.value?.preview_id) {
  if (busy.value || !previewId) return
  // An unknown confirmation must keep both its original id and original draft binding.
  // A newer draft can be edited, but cannot create a replacement preview until this is reconciled.
  if (confirmationUnknown.value && previewId !== outcome.value.previewId) return
  const draftTicket = confirmationUnknown.value ? outcome.value.draftTicket : guard.ticket(csv.value, source.value)
  busy.value = 'confirm'; error.value = ''
  try {
    const done = await props.api('/imports/confirm', { method: 'POST', body: JSON.stringify({ preview_id: previewId }) })
    if (!alive) return
    const retainedDraft = !guard.accepts(draftTicket, csv.value, source.value)
    if (!retainedDraft) {
      guard.invalidate()
      preview.value = null; stale.value = false
      csv.value = HEADER; source.value = ''; fileName.value = ''
    }
    outcome.value = { kind: done.created ? 'created' : 'existing', count: done.count, retainedDraft }
    await loadPool()
  } catch (e) {
    if (isAbort(e) || !alive) return
    if (e.unknown) outcome.value = { kind: 'unknown', previewId, draftTicket }
    else {
      error.value = e.message
      if (e.status === 409) { preview.value = null; outcome.value = null }
    }
  } finally {
    if (alive) busy.value = ''
  }
}

function discard() {
  guard.invalidate()
  preview.value = null; stale.value = false; error.value = ''
}

onMounted(() => { if (props.operator) loadPool() })
onBeforeUnmount(() => { alive = false; guard.invalidate() })
</script>

<template>
  <section class="panel">
    <header class="panel-head">
      <div>
        <p class="eyebrow">批量导入</p>
        <h1 id="panel-title" tabindex="-1">导入同格式 CSV</h1>
        <p class="lede">先预览并检查隔离规则，再明确确认。确认后的样本进入<b>待审池</b>：不会自动审核、不会进入训练快照，也不会调用 Jev。</p>
      </div>
      <dl v-if="operator && pool" class="head-stats">
        <div><dt>审核池</dt><dd>{{ pool.total.toLocaleString() }}</dd></div>
        <div><dt>待审</dt><dd>{{ pool.pending.toLocaleString() }}</dd></div>
      </dl>
    </header>

    <LockedCard v-if="!operator" :public-mode="Boolean(config?.public)" what="批量导入" />
    <template v-else>
      <ol class="stepper" aria-label="导入步骤">
        <li v-for="(name, index) in STEPS" :key="name" :class="{ done: step > index + 1, current: step === index + 1 }" :aria-current="step === index + 1 ? 'step' : undefined">
          <span class="stepper-no"><Icon v-if="step > index + 1" name="check" /><template v-else>{{ index + 1 }}</template></span>{{ name }}
        </li>
      </ol>
      <p v-if="poolError" class="callout callout-warn" role="status"><Icon name="info" /><span class="callout-body">审核池统计读取失败；不影响预览与确认。</span></p>

      <div class="import-grid">
        <form class="card" @submit.prevent="runPreview">
          <div class="field">
            <label for="import-source">来源说明 <span class="req">必填</span></label>
            <input id="import-source" v-model="source" maxlength="200" placeholder="例如：本人编写并逐条核对的英文短信" @input="changed">
            <p class="field-note">写入导入记录，方便日后追溯。</p>
          </div>
          <div class="field">
            <span class="field-label">CSV 文件</span>
            <label class="file-drop">
              <input type="file" accept=".csv,text/csv" class="sr-only" @change="chooseFile">
              <Icon name="file" />
              <span class="file-name">{{ fileName || '选择 .csv 文件' }}</span>
              <small>UTF-8 · 不超过 5 MiB · 10000 行</small>
            </label>
          </div>
          <div class="field">
            <label for="import-csv">CSV 内容 <span class="muted">约 {{ rows }} 行数据</span></label>
            <textarea id="import-csv" v-model="csv" rows="9" class="mono" spellcheck="false" aria-describedby="csv-rules" @input="changed"></textarea>
            <p id="csv-rules" class="field-note">列名恰好为 <code>text,label</code>；标签只接受 <code>ham</code> / <code>spam</code>；单条不超过 2000 字符。</p>
          </div>
          <div class="actions">
            <button class="btn btn-primary" :disabled="Boolean(busy) || confirmationUnknown || !source.trim() || !rows"><span v-if="busy === 'preview'" class="spinner" aria-hidden="true"></span>{{ busy === 'preview' ? '正在预览…' : '预览与隔离检查' }}</button>
            <span class="fine">{{ confirmationUnknown ? '先核对未确认的导入，再预览下一批。当前草稿会保留。' : '预览不写入样本。' }}</span>
          </div>
        </form>

        <section class="card preview-card" aria-labelledby="preview-title" :aria-busy="busy === 'preview' ? 'true' : 'false'">
          <h2 id="preview-title" class="card-title">预览结果</h2>
          <div v-if="error" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">{{ error }}</span></div>
          <div v-if="stale" class="callout callout-warn" role="status"><Icon name="info" /><span class="callout-body">CSV 或来源说明已修改，旧预览已作废。请重新预览后再确认。</span></div>

          <div v-if="outcome?.kind === 'created' || outcome?.kind === 'existing'" class="callout callout-ok" role="status">
            <Icon name="check" />
            <span class="callout-body">
              <strong v-if="outcome.kind === 'created'">已导入 {{ outcome.count }} 条，进入待审池。</strong>
              <strong v-else>这份预览之前已确认过：沿用原导入记录（{{ outcome.count }} 条），没有重复写入。</strong>
              尚未审核，不进入训练快照，也不会调用 Jev。
              <span v-if="outcome.retainedDraft">确认期间编辑的新草稿已保留，尚未预览或导入。</span>
              <span class="callout-actions"><button type="button" class="btn btn-sm btn-primary" @click="emit('navigate', 'review')">去人工审核<Icon name="arrow" /></button></span>
            </span>
          </div>
          <div v-if="outcome?.kind === 'unknown'" class="callout callout-warn" role="alert">
            <Icon name="alert" />
            <span class="callout-body">
              <strong>导入结果未知：请求可能已经写入。</strong>可以用同一份预览再次确认来核对——重复确认会沿用原导入记录，不会重复写入。
              <span class="callout-actions">
                <button type="button" class="btn btn-sm btn-primary" :disabled="Boolean(busy)" @click="confirm(outcome.previewId)">{{ busy === 'confirm' ? '正在核对…' : '用同一预览核对' }}</button>
                <button type="button" class="btn btn-sm btn-ghost" @click="emit('navigate', 'review')">去待审池查看</button>
              </span>
            </span>
          </div>

          <div v-if="busy === 'preview'" class="skeleton-block" aria-hidden="true"><div class="sk sk-w40"></div><div class="sk sk-grid"></div><div class="sk sk-w70"></div></div>
          <template v-else-if="preview">
            <div class="stat-grid">
              <div class="stat"><span>输入行</span><b>{{ summary.input_rows }}</b></div>
              <div class="stat stat-accent"><span>可新增</span><b>{{ summary.accepted_rows }}</b></div>
              <div class="stat" :class="{ 'stat-warn': summary.invalid_rows }"><span>无效</span><b>{{ summary.invalid_rows }}</b></div>
              <div class="stat"><span>重复</span><b>{{ summary.duplicate_rows }}</b></div>
              <div class="stat" :class="{ 'stat-warn': summary.conflict_groups }"><span>冲突组</span><b>{{ summary.conflict_groups }}</b></div>
              <div class="stat" :class="{ 'stat-warn': summary.heldout_overlap_rejected }"><span>留出重叠被拒</span><b>{{ summary.heldout_overlap_rejected }}</b></div>
            </div>
            <p v-if="summary.invalid_line_numbers?.length" class="fine">无效行号：{{ summary.invalid_line_numbers.join('、') }}{{ summary.invalid_rows > summary.invalid_line_numbers.length ? ' …' : '' }}</p>
            <p class="fine">同一文本标签矛盾的冲突组整组拒绝；与留出集相同、同模板或近重复的行被隔离，不进入审核池。</p>
            <div v-if="preview.sample_preview.length" class="table-wrap">
              <table class="table">
                <caption class="sr-only">可新增样本（最多显示前 10 条）</caption>
                <thead><tr><th scope="col">标签</th><th scope="col">文本（可新增，前 10 条）</th></tr></thead>
                <tbody><tr v-for="row in preview.sample_preview" :key="row.id"><td><span class="label-badge" :class="row.label">{{ row.label }}</span></td><td class="cell-text">{{ row.text }}</td></tr></tbody>
              </table>
            </div>
            <p v-else class="empty-mini">没有可新增的行：全部无效、重复、冲突或与留出集重叠，不能确认。</p>
            <div class="actions">
              <button type="button" class="btn btn-primary" :disabled="Boolean(busy) || !summary.accepted_rows" @click="confirm()"><span v-if="busy === 'confirm'" class="spinner" aria-hidden="true"></span>{{ busy === 'confirm' ? '正在确认…' : `确认导入 ${summary.accepted_rows} 条到待审池` }}</button>
              <button type="button" class="btn btn-ghost" :disabled="Boolean(busy)" @click="discard">放弃预览</button>
            </div>
            <p class="fine">来源：{{ summary.source }} · 预览 <code>{{ short(preview.preview_id) }}</code></p>
          </template>
          <p v-else-if="!outcome && !error && !stale" class="empty-mini">填写来源说明，选择或粘贴 CSV，然后点“预览与隔离检查”。</p>
        </section>
      </div>
    </template>
  </section>
</template>
