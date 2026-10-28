<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { dateTime, labelName, number, request } from '../api.js'

const pageSize = 20
const query = ref('')
const labelFilter = ref('')
const offset = ref(0)
const items = ref([])
const total = ref(0)
const listLoading = ref(false)
const listError = ref('')
const selectedId = ref(null)
const sample = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const history = ref([])
const historyLoading = ref(false)
const historyError = ref('')
const draftLabel = ref('ham')
const note = ref('')
const saving = ref(false)
const saveError = ref('')
const saveMessage = ref('')
const saveUncertain = ref(false)
let listSequence = 0
let detailSequence = 0
let listController
let detailController
let historyController
let appliedFilters = { query: '', label: '' }
const pageNumber = computed(() => Math.floor(offset.value / pageSize) + 1)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const canSave = computed(() => sample.value && draftLabel.value !== sample.value.label && note.value.length <= 500 && !saving.value && !detailLoading.value && !saveUncertain.value)

function clearSelection() {
  detailSequence += 1
  detailController?.abort()
  historyController?.abort()
  selectedId.value = null
  sample.value = null
  detailError.value = ''
  history.value = []
  historyError.value = ''
  detailLoading.value = false
  historyLoading.value = false
  saveError.value = ''
  saveMessage.value = ''
  saveUncertain.value = false
}

async function loadList(nextOffset = offset.value, { preserveSelection = false } = {}) {
  if (saving.value && !preserveSelection) return
  if (!preserveSelection) clearSelection()
  const current = ++listSequence
  listController?.abort()
  listController = new AbortController()
  offset.value = nextOffset
  listLoading.value = true
  listError.value = ''
  items.value = []
  const params = new URLSearchParams({ limit: String(pageSize), offset: String(nextOffset) })
  if (appliedFilters.query) params.set('q', appliedFilters.query)
  if (appliedFilters.label) params.set('label', appliedFilters.label)
  try {
    const response = await request(`/samples?${params}`, { signal: listController.signal })
    if (current !== listSequence) return
    items.value = response.items
    total.value = response.total
  } catch (cause) {
    if (current === listSequence && cause.name !== 'AbortError') listError.value = cause.message
  } finally {
    if (current === listSequence) listLoading.value = false
  }
}

function applyFilters() {
  if (listLoading.value || saving.value) return
  appliedFilters = { query: query.value.trim(), label: labelFilter.value }
  loadList(0)
}

function resetFilters() {
  query.value = ''
  labelFilter.value = ''
  appliedFilters = { query: '', label: '' }
  loadList(0)
}

async function selectSample(id) {
  if (saving.value) return
  clearSelection()
  const current = detailSequence
  selectedId.value = id
  detailLoading.value = true
  historyLoading.value = true
  detailController = new AbortController()
  const encoded = encodeURIComponent(id)
  const results = await Promise.allSettled([
    request(`/samples/${encoded}`, { signal: detailController.signal }),
    request(`/samples/${encoded}/corrections`, { signal: detailController.signal }),
  ])
  if (current !== detailSequence) return
  if (results[0].status === 'fulfilled') {
    sample.value = results[0].value
    draftLabel.value = sample.value.label
    note.value = ''
  } else if (results[0].reason.name !== 'AbortError') {
    detailError.value = results[0].reason.message
  }
  if (results[1].status === 'fulfilled') history.value = results[1].value
  else if (results[1].reason.name !== 'AbortError') historyError.value = results[1].reason.message
  detailLoading.value = false
  historyLoading.value = false
}

async function reloadHistory() {
  if (!selectedId.value) return
  const current = detailSequence
  const id = selectedId.value
  historyController?.abort()
  historyController = new AbortController()
  historyLoading.value = true
  historyError.value = ''
  try {
    const response = await request(`/samples/${encodeURIComponent(id)}/corrections`, { signal: historyController.signal })
    if (current === detailSequence) history.value = response
  } catch (cause) {
    if (current === detailSequence && cause.name !== 'AbortError') historyError.value = cause.message
  } finally {
    if (current === detailSequence) historyLoading.value = false
  }
}

async function saveLabel() {
  if (!canSave.value) return
  const current = detailSequence
  const id = sample.value.id
  saving.value = true
  saveError.value = ''
  saveMessage.value = ''
  try {
    const response = await request(`/samples/${encodeURIComponent(id)}/label`, {
      method: 'PATCH', body: { label: draftLabel.value, note: note.value },
    })
    if (current !== detailSequence) return
    sample.value = response.sample
    draftLabel.value = response.sample.label
    note.value = ''
    saveUncertain.value = false
    saveMessage.value = response.changed ? '标签与纠错历史已保存。' : '标签未发生变化，无需新增纠错记录。'
    let refreshList = Promise.resolve()
    if (appliedFilters.label && response.sample.label !== appliedFilters.label) {
      // Removing a matching row shifts subsequent offsets. Refill the current
      // page without closing the saved detail or losing its success message.
      total.value = Math.max(0, total.value - 1)
      const lastOffset = Math.floor(Math.max(0, total.value - 1) / pageSize) * pageSize
      refreshList = loadList(Math.min(offset.value, lastOffset), { preserveSelection: true })
    } else {
      items.value = items.value.map(item => item.id === id ? response.sample : item)
    }
    await Promise.all([refreshList, reloadHistory()])
  } catch (cause) {
    if (current === detailSequence) {
      saveError.value = cause.message
      saveUncertain.value = Boolean(cause.uncertain)
    }
  } finally {
    saving.value = false
  }
}

function draftChanged() {
  saveMessage.value = ''
  if (!saveUncertain.value) saveError.value = ''
}

onMounted(() => loadList(0))
onBeforeUnmount(() => {
  listController?.abort()
  detailController?.abort()
  historyController?.abort()
})
</script>

<template>
  <section aria-labelledby="samples-heading">
    <div class="page-heading"><div><p class="eyebrow">DATA REVIEW</p><h1 id="samples-heading">检查样本，修正标签</h1><p>读取训练样本的工作副本，保留每次标签变更的依据。</p></div><span class="quiet-tag">人工复核</span></div>
    <div class="notice info"><span class="notice-symbol" aria-hidden="true">i</span><span><strong>纠错只更新工作副本，不会自动重新训练模型。</strong> 修改标签与写入历史由同一事务保存。</span></div>

    <div :class="['samples-layout', { 'has-selection': selectedId }]">
      <section class="card samples-list" aria-labelledby="sample-list-heading">
        <div class="card-heading"><h2 id="sample-list-heading">样本列表 <span class="count-chip">{{ listLoading ? '…' : number(total) }}</span></h2><button class="text-button" type="button" :disabled="listLoading || saving" @click="loadList()">刷新</button></div>
        <form class="sample-filters" @submit.prevent="applyFilters">
          <div class="field search-field"><label for="sample-query">搜索文本</label><input id="sample-query" v-model="query" type="search" maxlength="200" placeholder="输入短信关键词" :disabled="listLoading || saving" /></div>
          <div class="field"><label for="sample-filter-label">标签</label><select id="sample-filter-label" v-model="labelFilter" :disabled="listLoading || saving"><option value="">全部标签</option><option value="ham">正常短信</option><option value="spam">垃圾短信</option></select></div>
          <button type="submit" class="button secondary filter-button" :disabled="listLoading || saving">查询</button>
        </form>

        <div v-if="listError" class="list-feedback"><div class="notice error" role="alert">{{ listError }}</div><button class="button secondary" type="button" @click="loadList()">重试加载列表</button></div>
        <div v-else-if="listLoading" class="empty-state list-loading" role="status"><span class="spinner" aria-hidden="true"></span><p>正在读取样本…</p></div>
        <div v-else-if="!items.length" class="empty-state list-empty" role="status"><h3>当前没有匹配的样本</h3><p>可以调整关键词、标签或返回上一页。</p><div class="empty-actions"><button v-if="offset > 0" class="button secondary" type="button" :disabled="saving" @click="loadList(Math.max(0, offset - pageSize))">上一页</button><button class="text-button" type="button" :disabled="saving" @click="resetFilters">清空筛选</button></div></div>
        <div v-else class="table-scroll">
          <table class="sample-table"><thead><tr><th scope="col">样本编号</th><th scope="col">短信内容</th><th scope="col">已保存标签</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead><tbody><tr v-for="item in items" :key="item.id" :class="{ selected: selectedId === item.id }"><td><span class="sample-id" :title="item.id">{{ item.id.length > 12 ? `${item.id.slice(0, 10)}…` : item.id }}</span></td><td><p class="text-preview">{{ item.text }}</p></td><td><span :class="['label-badge', item.label]">{{ labelName(item.label) }}</span></td><td><button type="button" class="row-button" :disabled="saving" :aria-label="`复核样本 ${item.id}`" @click="selectSample(item.id)">{{ selectedId === item.id ? '查看中' : '复核' }}<span aria-hidden="true">→</span></button></td></tr></tbody></table>
        </div>

        <div v-if="!listError" class="pagination"><span class="muted small">{{ total > 0 ? `${offset + 1}–${Math.min(offset + pageSize, total)} / ${number(total)} 条` : '0 条' }}</span><div><button class="button pagination-button" type="button" :disabled="listLoading || saving || offset === 0" @click="loadList(Math.max(0, offset - pageSize))" aria-label="上一页">←</button><span class="page-count">{{ pageNumber }} / {{ pageCount }}</span><button class="button pagination-button" type="button" :disabled="listLoading || saving || offset + pageSize >= total" @click="loadList(offset + pageSize)" aria-label="下一页">→</button></div></div>
      </section>

      <aside v-if="selectedId" class="card sample-detail" aria-labelledby="sample-detail-heading" :aria-busy="detailLoading || saving">
        <div class="card-heading"><h2 id="sample-detail-heading">样本复核</h2><button class="close-button" type="button" :disabled="saving" @click="clearSelection" aria-label="关闭样本详情">×</button></div>
        <div v-if="detailLoading" class="empty-state" role="status"><span class="spinner" aria-hidden="true"></span><p>正在读取详情…</p></div>
        <div v-else-if="detailError" class="detail-content"><div class="notice error" role="alert">{{ detailError }}</div><button class="button secondary" type="button" @click="selectSample(selectedId)">重试读取详情</button></div>
        <template v-else-if="sample">
          <div class="detail-content">
            <div class="detail-meta"><span class="sample-id">{{ sample.id }}</span><span class="muted small">修订 {{ sample.revision }}</span></div>
            <p class="sample-full-text">{{ sample.text }}</p>
            <dl class="saved-label-row"><dt>已保存标签</dt><dd><span :class="['label-badge', sample.label]">{{ labelName(sample.label) }}</span></dd></dl>
            <form class="correction-form" @submit.prevent="saveLabel">
              <fieldset class="label-choices" :disabled="saving"><legend>纠正为</legend><label :class="{ chosen: draftLabel === 'ham' }"><input v-model="draftLabel" type="radio" name="draft-label" value="ham" @change="draftChanged" />正常短信</label><label :class="{ chosen: draftLabel === 'spam' }"><input v-model="draftLabel" type="radio" name="draft-label" value="spam" @change="draftChanged" />垃圾短信</label></fieldset>
              <div class="field"><label for="correction-note">纠错说明 <span class="muted optional">选填</span></label><textarea id="correction-note" v-model="note" rows="3" maxlength="500" placeholder="说明这次修改的判断依据" :disabled="saving" @input="draftChanged"></textarea><div class="field-meta"><span>仅在标签变更时保存说明。</span><span>{{ note.length }} / 500</span></div></div>
              <div v-if="saveMessage" class="notice success" role="status">{{ saveMessage }}</div>
              <div v-if="saveError" class="notice error" role="alert">{{ saveError }}<button v-if="saveUncertain" class="text-button" type="button" :disabled="saving" @click="selectSample(sample.id)">重新读取记录核对</button></div>
              <button class="button primary save-button" type="submit" :disabled="!canSave"><span v-if="saving" class="spinner" aria-hidden="true"></span>{{ saving ? '正在保存…' : '保存标签与历史' }}</button>
              <p class="field-help">{{ draftLabel === sample.label ? '选择与已保存值不同的标签后，可以提交纠错。' : '当前是未保存草稿，点击保存后才会写入工作副本。' }}</p>
            </form>
          </div>
          <section class="correction-history" aria-labelledby="history-heading"><div class="history-heading"><h3 id="history-heading">纠错历史 <span class="muted small">{{ historyLoading ? '…' : history.length }}</span></h3><button class="text-button" type="button" :disabled="historyLoading || saving" @click="reloadHistory">刷新</button></div><p v-if="historyLoading" class="muted small" role="status">正在读取历史…</p><div v-else-if="historyError" class="notice error" role="alert">{{ historyError }}<button class="text-button" type="button" @click="reloadHistory">重试</button></div><p v-else-if="!history.length" class="history-empty">尚无标签变更记录。</p><ol v-else class="history-list"><li v-for="entry in [...history].reverse()" :key="entry.id"><div class="history-change"><span>{{ labelName(entry.old_label) }}</span><span class="muted" aria-hidden="true">→</span><strong>{{ labelName(entry.new_label) }}</strong><span class="revision-tag">修订 {{ entry.revision }}</span></div><p v-if="entry.note" class="history-note">{{ entry.note }}</p><time :datetime="entry.created_at">{{ dateTime(entry.created_at) }}</time></li></ol></section>
        </template>
      </aside>
    </div>
  </section>
</template>
