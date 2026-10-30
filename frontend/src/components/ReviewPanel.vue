<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Icon from './Icon.vue'
import LockedCard from './LockedCard.vue'
import { createSamplePageLoader } from '../samplePage.js'
import { reconcileUnknownSave, reviewIntent, reviewStatus } from '../reviewFlow.js'
import { isAbort } from '../workbenchApi.js'

const props = defineProps({ api: Function, config: Object, operator: Boolean })
const emit = defineEmits(['navigate'])
const LIMIT = 20
const STATUS = { original: ['原数据标签', 'tag-neutral'], reviewed: ['已审核', 'tag-ok'], pending: ['待审', 'tag-warn'] }

const search = ref(''), applied = ref(''), pending = ref(true), offset = ref(0)
const items = ref([]), total = ref(0), listState = ref('idle'), listError = ref('')
const selected = ref(null), detailState = ref('idle'), detailError = ref('')
const draft = ref('ham'), note = ref('')
// idle | saving | saved | conflict | unknown | reloaded | reconciled | error
const save = ref({ state: 'idle' })
const attempt = ref(null)
const snapshots = ref([]), snap = ref({ state: 'idle' })
const detailHeading = ref(null)
const loader = createSamplePageLoader()
let alive = true, detailTicket = 0

const query = () => ({ q: applied.value, pending: pending.value, offset: offset.value, limit: LIMIT })
const fetchPage = q => props.api('/samples?' + new URLSearchParams({ limit: String(q.limit), offset: String(q.offset), q: q.q, pending: String(q.pending) }))
const commit = page => { items.value = page.items; total.value = page.total; offset.value = page.offset; listState.value = 'ready' }
const intent = computed(() => reviewIntent(selected.value, draft.value, note.value))
const blockedUntilReload = computed(() => ['unknown', 'conflict'].includes(save.value.state))
const canSave = computed(() => detailState.value === 'ready' && intent.value.ready && save.value.state !== 'saving' && !blockedUntilReload.value)
const nextPending = computed(() => items.value.find(row => row.id !== selected.value?.id && !row.reviewed))
const rangeText = computed(() => total.value ? `${offset.value + 1}–${Math.min(offset.value + LIMIT, total.value)} / ${total.value}` : '0 / 0')
const status = row => STATUS[reviewStatus(row)]
const otherLabel = label => label === 'ham' ? 'spam' : 'ham'
const saveText = computed(() => {
  if (save.value.state === 'saving') return '正在保存…'
  if (intent.value.kind === 'correct') return `保存纠错：${selected.value.label} → ${draft.value}`
  if (intent.value.kind === 'confirm') return `确认标签 ${draft.value}（不记为纠错）`
  return '已审核，标签未变'
})

async function loadList() {
  listState.value = 'loading'; listError.value = ''
  try { await loader.refresh(query, fetchPage, commit) } catch (e) {
    if (isAbort(e) || !alive) return
    listState.value = 'error'; listError.value = e.message
  }
}
function applySearch() { applied.value = search.value.trim(); offset.value = 0; loadList() }
function setPending(value) { if (pending.value === value) return; pending.value = value; offset.value = 0; loadList() }
function turn(delta) { offset.value = Math.max(0, offset.value + delta * LIMIT); loadList() }

async function loadSnapshots() {
  try { const dataset = await props.api('/dataset'); if (alive) snapshots.value = dataset.snapshots || [] } catch (e) {
    if (!isAbort(e) && alive) snap.value = { state: 'error', message: '快照列表读取失败：' + e.message }
  }
}

async function openSample(id, { keepDraft = false, focus = true } = {}) {
  // Keep a pending write attached to its visible sample and draft until its outcome is known.
  if (save.value.state === 'saving') return null
  const ticket = ++detailTicket
  detailState.value = 'loading'; detailError.value = ''
  try {
    const sample = await props.api('/samples/' + encodeURIComponent(id))
    if (!alive || ticket !== detailTicket) return null
    selected.value = sample
    if (!keepDraft) { draft.value = sample.label; note.value = ''; save.value = { state: 'idle' }; attempt.value = null }
    detailState.value = 'ready'
    if (focus) nextTick(() => detailHeading.value?.focus())
    return sample
  } catch (e) {
    if (isAbort(e) || !alive || ticket !== detailTicket) return null
    detailState.value = 'error'; detailError.value = e.message
    return null
  }
}

async function saveReview() {
  const sample = selected.value
  if (!sample || !canSave.value) return
  const ticket = detailTicket
  const submitted = { label: draft.value, note: note.value, expected_revision: sample.revision }
  attempt.value = { id: sample.id, revision: sample.revision, label: submitted.label, note: submitted.note, from: sample.label }
  save.value = { state: 'saving' }
  let result
  try {
    result = await props.api('/samples/' + encodeURIComponent(sample.id), { method: 'PATCH', body: JSON.stringify(submitted) })
  } catch (e) {
    if (isAbort(e) || !alive || ticket !== detailTicket || selected.value?.id !== sample.id) return
    if (e.status === 409) save.value = { state: 'conflict' }
    else if (e.unknown) save.value = { state: 'unknown' }
    else save.value = { state: 'error', message: e.message }
    return
  }
  if (!alive || ticket !== detailTicket || selected.value?.id !== sample.id) return
  selected.value = result.sample
  const at = items.value.findIndex(row => row.id === sample.id)
  if (at >= 0) items.value[at] = result.sample
  if (draft.value === submitted.label && note.value === submitted.note) note.value = ''
  save.value = { state: 'saved', changed: result.changed, from: sample.label, to: result.sample.label, revision: result.sample.revision, refreshFailed: false }
  try { await loader.refresh(query, fetchPage, commit) } catch (e) {
    if (isAbort(e) || !alive || ticket !== detailTicket || selected.value?.id !== sample.id) return
    save.value = { ...save.value, refreshFailed: true }
  }
}

// After 409 or an unconfirmed save: read the server version, keep the reviewer's draft, then decide.
async function reloadKeepingDraft() {
  const previous = save.value.state
  const sample = await openSample(selected.value.id, { keepDraft: true, focus: false })
  if (!sample) return
  if (previous === 'unknown') {
    const outcome = reconcileUnknownSave(attempt.value, sample)
    save.value = { state: 'reconciled', outcome, revision: sample.revision, label: sample.label }
    if (outcome === 'matches') { draft.value = sample.label; note.value = ''; loadList() }
  } else {
    save.value = { state: 'reloaded', revision: sample.revision, label: sample.label }
  }
}

async function makeSnapshot() {
  snap.value = { state: 'saving' }
  try {
    const result = await props.api('/snapshots', { method: 'POST' })
    if (!alive) return
    snap.value = { state: 'done', id: result.snapshot_id, count: result.count, corrections: result.corrections }
    loadSnapshots()
  } catch (e) {
    if (isAbort(e) || !alive) return
    snap.value = e.unknown ? { state: 'unknown' } : { state: 'error', message: e.message }
  }
}

onMounted(() => { if (props.operator) { loadList(); loadSnapshots() } })
onBeforeUnmount(() => { alive = false; loader.invalidate() })
</script>

<template>
  <section class="panel">
    <header class="panel-head">
      <div>
        <p class="eyebrow">人工审核</p>
        <h1 id="panel-title" tabindex="-1">复核标签并记录依据</h1>
        <p class="lede">人工标签才是训练数据的来源。保存时附带修订号，别人先改过就不会被覆盖；同标签确认不计为纠错。</p>
      </div>
    </header>

    <LockedCard v-if="!operator" :public-mode="Boolean(config?.public)" what="人工审核" />
    <template v-else>
      <div class="review-grid">
        <section class="card list-card" aria-labelledby="list-title">
          <div class="list-head">
            <h2 id="list-title" class="card-title">样本</h2>
            <div class="segmented" role="group" aria-label="审核状态筛选">
              <button type="button" :aria-pressed="pending" @click="setPending(true)">待审</button>
              <button type="button" :aria-pressed="!pending" @click="setPending(false)">全部</button>
            </div>
          </div>
          <form class="search-row" role="search" @submit.prevent="applySearch">
            <label for="sample-search" class="sr-only">搜索样本文本</label>
            <input id="sample-search" v-model="search" type="search" maxlength="200" placeholder="搜索文本">
            <button class="btn btn-outline">搜索</button>
          </form>
          <p class="fine list-meta" role="status">{{ listState === 'loading' ? '正在读取…' : `${pending ? '待审' : '全部'}${applied ? ` · 包含“${applied}”` : ''} · ${rangeText}` }}</p>

          <div v-if="listState === 'error'" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">{{ listError }} <button type="button" class="link-btn" @click="loadList">重新读取</button></span></div>
          <div v-if="listState === 'loading' && !items.length" class="skeleton-block" aria-hidden="true"><div class="sk sk-row"></div><div class="sk sk-row"></div><div class="sk sk-row"></div></div>
          <div v-else-if="listState === 'ready' && !items.length" class="empty-mini">
            <template v-if="pending && !applied">待审池为空。新样本需要先 <button type="button" class="link-btn" @click="emit('navigate', 'import')">批量导入</button>。</template>
            <template v-else>当前筛选下没有样本。</template>
          </div>
          <ul v-else class="sample-list" :class="{ dim: listState === 'loading' }">
            <li v-for="row in items" :key="row.id">
              <button type="button" class="sample-row" :disabled="save.state === 'saving'" :class="{ current: selected?.id === row.id }" :aria-current="selected?.id === row.id ? 'true' : undefined" @click="openSample(row.id)">
                <span class="sample-text">{{ row.text }}</span>
                <span class="sample-meta"><span class="label-badge" :class="row.label">{{ row.label }}</span><span class="tag" :class="status(row)[1]">{{ status(row)[0] }}</span><span class="rev">v{{ row.revision }}</span></span>
              </button>
            </li>
          </ul>
          <div class="pagination">
            <button type="button" class="btn btn-sm btn-outline" :disabled="listState === 'loading' || offset === 0" @click="turn(-1)">上一页</button>
            <span class="fine">{{ rangeText }}</span>
            <button type="button" class="btn btn-sm btn-outline" :disabled="listState === 'loading' || offset + LIMIT >= total" @click="turn(1)">下一页</button>
          </div>
        </section>

        <section class="card detail-card" aria-labelledby="detail-title">
          <template v-if="!selected">
            <h2 id="detail-title" class="card-title">审核详情</h2>
            <div v-if="detailState === 'loading'" class="skeleton-block" aria-hidden="true"><div class="sk sk-w70"></div><div class="sk sk-bar"></div></div>
            <div v-else-if="detailState === 'error'" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">{{ detailError }}</span></div>
            <p v-else class="empty-mini">从左侧选择一条样本，查看原文、当前标签与历史。</p>
          </template>
          <template v-else>
            <header class="detail-head">
              <h2 id="detail-title" ref="detailHeading" class="card-title" tabindex="-1">样本 · 修订 v{{ selected.revision }}</h2>
              <span class="tag" :class="status(selected)[1]">{{ status(selected)[0] }}</span>
            </header>
            <blockquote class="quote" lang="en">{{ selected.text }}</blockquote>
            <dl class="kv kv-compact">
              <dt>当前标签</dt><dd><span class="label-badge" :class="selected.label">{{ selected.label }}</span></dd>
              <dt>来源</dt><dd>{{ selected.source }}</dd>
              <dt>样本 ID</dt><dd><code>{{ selected.id.slice(0, 16) }}…</code></dd>
            </dl>
            <div v-if="detailState === 'error'" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">读取失败：{{ detailError }}。草稿仍保留。</span></div>

            <form class="review-form" @submit.prevent="saveReview">
              <fieldset class="label-choice" :disabled="save.state === 'saving' || detailState === 'loading'">
                <legend>复核标签</legend>
                <label v-for="label in ['ham', 'spam']" :key="label" class="choice" :class="[label, { selected: draft === label }]">
                  <input v-model="draft" type="radio" name="review-label" :value="label">
                  <span><b>{{ label }}</b> {{ label === 'ham' ? '正常短信' : '垃圾短信' }}</span>
                  <span v-if="label === selected.label" class="choice-note">当前</span>
                </label>
              </fieldset>
              <p class="intent" :class="'intent-' + intent.kind">
                <template v-if="intent.kind === 'correct'">纠错：{{ selected.label }} → {{ draft }}。标签与纠错历史在同一事务写入，需要填写依据。</template>
                <template v-else-if="intent.kind === 'confirm'">确认原标签 {{ draft }}：样本变为已审核，修订号加一，但不产生纠错历史。需要填写依据。</template>
                <template v-else>这条样本已审核且标签未变，无需保存。改选 {{ otherLabel(selected.label) }} 才会形成纠错。</template>
              </p>
              <div class="field">
                <label for="review-note">复核依据 <span v-if="intent.needsNote" class="req">必填</span></label>
                <textarea id="review-note" v-model="note" rows="3" maxlength="500" :disabled="save.state === 'saving' || detailState === 'loading'" placeholder="例如：明确的会议提醒，无推广内容"></textarea>
                <p class="field-note">{{ note.length }} / 500</p>
              </div>

              <div v-if="save.state === 'saved'" class="callout" :class="save.refreshFailed ? 'callout-warn' : 'callout-ok'" role="status">
                <Icon :name="save.refreshFailed ? 'info' : 'check'" />
                <span class="callout-body">
                  <strong v-if="save.changed">已保存纠错 {{ save.from }} → {{ save.to }}（v{{ save.revision }}）。</strong>
                  <strong v-else>已确认标签 {{ save.to }}（v{{ save.revision }}），没有新增纠错历史。</strong>
                  <template v-if="save.refreshFailed">列表刷新没有完成，请重新读取列表；<b>无需重复保存</b>。<button type="button" class="link-btn" @click="loadList">重新读取列表</button></template>
                </span>
              </div>
              <div v-else-if="save.state === 'conflict'" class="callout callout-warn" role="alert">
                <Icon name="alert" />
                <span class="callout-body"><strong>修订冲突：服务器已有更新版本，这次保存没有覆盖它。</strong>你的草稿（标签 {{ draft }}{{ note.trim() ? '、依据' : '' }}）已保留。读取最新版本后再决定是否保存。
                  <span class="callout-actions"><button type="button" class="btn btn-sm btn-primary" @click="reloadKeepingDraft">读取最新版本（保留草稿）</button></span></span>
              </div>
              <div v-else-if="save.state === 'unknown'" class="callout callout-warn" role="alert">
                <Icon name="alert" />
                <span class="callout-body"><strong>保存结果未知：请求可能已经写入。</strong>先重新读取这条样本核对，再决定是否保存；草稿已保留。
                  <span class="callout-actions"><button type="button" class="btn btn-sm btn-primary" @click="reloadKeepingDraft">重新读取核对</button></span></span>
              </div>
              <div v-else-if="save.state === 'reloaded'" class="callout callout-info" role="status">
                <Icon name="info" />
                <span class="callout-body">已读取最新版本 v{{ save.revision }}（当前标签 {{ save.label }}）。草稿保留为 {{ draft }}；确认无误后再保存。</span>
              </div>
              <div v-else-if="save.state === 'reconciled'" class="callout" :class="save.outcome === 'matches' ? 'callout-ok' : 'callout-info'" role="status">
                <Icon :name="save.outcome === 'matches' ? 'check' : 'info'" />
                <span class="callout-body">
                  <template v-if="save.outcome === 'matches'"><strong>服务器已是 v{{ save.revision }}，标签 {{ save.label }} 与这次草稿一致。</strong>按已保存处理，无需重复提交；可在下方历史核对。</template>
                  <template v-else-if="save.outcome === 'not_saved'"><strong>服务器仍是 v{{ save.revision }}：这次保存没有生效。</strong>草稿已保留，可以再次保存。</template>
                  <template v-else><strong>服务器已是 v{{ save.revision }}，标签 {{ save.label }}，与草稿不同。</strong>可能有其他修改；草稿已保留，请核对后再决定。</template>
                </span>
              </div>
              <div v-else-if="save.state === 'error'" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">{{ save.message }}</span></div>

              <div class="actions">
                <button class="btn btn-primary" :disabled="!canSave"><span v-if="save.state === 'saving'" class="spinner" aria-hidden="true"></span>{{ saveText }}</button>
                <button type="button" class="btn btn-outline" :disabled="save.state === 'saving' || detailState === 'loading'" @click="blockedUntilReload ? reloadKeepingDraft() : openSample(selected.id, { keepDraft: true, focus: false })">重新读取服务器版本</button>
                <button v-if="save.state === 'saved' && nextPending" type="button" class="btn btn-ghost" @click="openSample(nextPending.id)">下一条待审<Icon name="arrow" /></button>
              </div>
              <p v-if="save.state === 'saving'" class="fine" role="status">正在确认这条样本的保存结果，暂时不能切换样本或编辑。若结果未知，会保留草稿并提供重新读取核对。</p>
              <p v-if="intent.needsNote && !note.trim() && !blockedUntilReload" class="blockers">还需要：填写复核依据</p>
            </form>

            <h3 class="sub-title">标签历史</h3>
            <p v-if="!selected.history.length" class="fine">暂无纠错历史。同标签确认不会增加纠错记录。</p>
            <ol v-else class="timeline">
              <li v-for="item in selected.history" :key="item.id || item.revision"><span class="timeline-rev">v{{ item.revision }}</span><span><span class="label-badge" :class="item.old_label">{{ item.old_label }}</span> → <span class="label-badge" :class="item.new_label">{{ item.new_label }}</span></span><span class="timeline-note">{{ item.note }}</span><span class="fine">{{ item.created_at }}</span></li>
            </ol>
          </template>
        </section>
      </div>

      <section class="card snapshot-card" aria-labelledby="snapshot-title">
        <div class="list-head">
          <div><h2 id="snapshot-title" class="card-title">已审核数据快照</h2><p class="fine">快照冻结当前全部已审核样本（内容寻址）；不会自动训练。训练在本地 CLI 执行：<code>workbench.py train --snapshot 快照ID --run 新运行名</code></p></div>
          <button type="button" class="btn btn-outline" :disabled="snap.state === 'saving'" @click="makeSnapshot"><span v-if="snap.state === 'saving'" class="spinner" aria-hidden="true"></span>生成快照</button>
        </div>
        <div v-if="snap.state === 'done'" class="callout callout-ok" role="status"><Icon name="check" /><span class="callout-body">快照 <code>{{ snap.id.slice(0, 24) }}…</code> · {{ snap.count }} 条已审核 · {{ snap.corrections }} 次纠错。相同内容总得到同一 ID。</span></div>
        <div v-else-if="snap.state === 'unknown'" class="callout callout-warn" role="alert"><Icon name="alert" /><span class="callout-body">快照结果未知。快照按内容寻址，重新生成不会产生重复；可先刷新列表核对。<button type="button" class="link-btn" @click="loadSnapshots">刷新列表</button></span></div>
        <div v-else-if="snap.state === 'error'" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">{{ snap.message }}</span></div>
        <div v-if="snapshots.length" class="table-wrap">
          <table class="table">
            <thead><tr><th scope="col">快照</th><th scope="col">已审核条数</th><th scope="col">纠错次数</th><th scope="col">数据版本</th></tr></thead>
            <tbody><tr v-for="item in snapshots" :key="item.snapshot_id"><td><code>{{ item.snapshot_id.slice(0, 22) }}…</code></td><td>{{ item.count }}</td><td>{{ item.corrections }}</td><td><code>{{ item.dataset_id }}</code></td></tr></tbody>
          </table>
        </div>
        <p v-else class="empty-mini">还没有快照。</p>
      </section>
    </template>
  </section>
</template>
