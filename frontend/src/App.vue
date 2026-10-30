<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import DecisionPanel from './components/DecisionPanel.vue'
import ImportPanel from './components/ImportPanel.vue'
import ReviewPanel from './components/ReviewPanel.vue'
import EvidencePanel from './components/EvidencePanel.vue'
import ModelPanel from './components/ModelPanel.vue'
import Icon from './components/Icon.vue'
import { createApiClient, isAbort } from './workbenchApi.js'

const SECTIONS = [
  { id: 'decide', title: '判断体验', hint: '单条短信 · 本地 / Jev / 对照', group: 'use' },
  { id: 'import', title: '批量导入', hint: 'CSV 预览后再确认', group: 'operate', private: true },
  { id: 'review', title: '人工审核', hint: '标签、依据、修订与历史', group: 'operate', private: true },
  { id: 'evidence', title: '效果对照', hint: '验证、最终测试、未评测项', group: 'evidence' },
  { id: 'model', title: '模型与规则', hint: '实际加载、候选与任务规则', group: 'evidence' },
]
const GROUPS = [['use', '使用'], ['operate', '操作者'], ['evidence', '证据与版本']]
const KIND = { lr: '逻辑回归', mlp16: 'MLP 16' }

const fromHash = () => {
  const id = (globalThis.location?.hash || '').slice(1)
  return SECTIONS.some(section => section.id === id) ? id : 'decide'
}
const tab = ref(fromHash())
// Every identity change remounts the panels, so drafts, rows, keys and results cannot survive it.
const identity = ref(0)
const token = ref(''), tokenDraft = ref(''), authOpen = ref(false)
const config = ref(null), health = ref(null), service = ref(null), runs = ref([]), finalReport = ref(null)
const status = ref('loading'), evidenceState = ref('idle'), loadError = ref('')
const banner = ref(null)
const authButton = ref(null), tokenInput = ref(null)

const client = createApiClient({
  getToken: () => token.value,
  onUnauthorized: () => dropIdentity('操作者认证失效：令牌与页面中的私有数据已清除，请重新载入。'),
})
const api = client.request

const isPublic = computed(() => Boolean(config.value?.public))
const operator = computed(() => Boolean(config.value?.operator_authenticated) && !isPublic.value)
const loaded = computed(() => health.value?.loaded || null)
const jevEnabled = computed(() => Boolean(config.value?.decision?.enabled))
const locked = section => Boolean(section.private) && !operator.value

async function refresh() {
  const ticket = identity.value
  try {
    const [c, h, s] = await Promise.all([api('/config'), api('/health'), api('/service')])
    if (ticket !== identity.value) return
    config.value = c; health.value = h; service.value = s
    status.value = 'ready'; loadError.value = ''
  } catch (error) {
    if (isAbort(error) || ticket !== identity.value) return
    loadError.value = error.message
    status.value = config.value ? 'ready' : 'error'
    if (config.value) banner.value = { tone: 'error', text: '服务状态刷新失败：' + error.message }
    return
  }
  await loadEvidence(ticket)
}

async function loadEvidence(ticket = identity.value) {
  if (!(config.value?.public || config.value?.operator_authenticated)) {
    runs.value = []; finalReport.value = null; evidenceState.value = 'locked'
    return
  }
  evidenceState.value = 'loading'
  try {
    const [r, f] = await Promise.all([api('/runs'), api('/final-report')])
    if (ticket !== identity.value) return
    runs.value = r.runs || []; finalReport.value = f.report || null; evidenceState.value = 'ready'
  } catch (error) {
    if (isAbort(error) || ticket !== identity.value) return
    evidenceState.value = 'error'
  }
}

function resetIdentity() {
  client.invalidate()
  identity.value += 1
  runs.value = []; finalReport.value = null; evidenceState.value = 'idle'
  if (config.value) config.value = { ...config.value, operator_authenticated: false, decision: config.value.decision && { ...config.value.decision, server_key_available: false } }
}

function dropIdentity(message) {
  resetIdentity()
  token.value = ''; tokenDraft.value = ''
  if (SECTIONS.find(section => section.id === tab.value)?.private) go('decide', false)
  banner.value = message ? { tone: 'warn', text: message } : null
  refresh()
}

async function loadToken() {
  const value = tokenDraft.value.trim()
  if (!value) return
  resetIdentity()
  const ticket = identity.value
  token.value = value; tokenDraft.value = ''; banner.value = null
  await refresh()
  if (ticket !== identity.value || !token.value) return
  if (config.value && !config.value.operator_authenticated) {
    resetIdentity()
    token.value = ''
    banner.value = { tone: 'error', text: '令牌没有通过认证，私有区域保持锁定；页面没有保留这个令牌。' }
    nextTick(() => tokenInput.value?.focus())
  } else if (operator.value) {
    authOpen.value = false
    banner.value = { tone: 'ok', text: '操作者已认证。令牌只在当前页面内存中，刷新或关闭页面即失效。' }
    nextTick(() => authButton.value?.focus())
  }
}

function clearToken() {
  authOpen.value = false
  dropIdentity('已清除操作者令牌与页面中的私有数据（样本、草稿、判断文本与 Key）。进行中的请求结果不会回填。')
  nextTick(() => authButton.value?.focus())
}

function toggleAuth() {
  authOpen.value = !authOpen.value
  if (authOpen.value) nextTick(() => tokenInput.value?.focus())
}
function closeAuth() {
  authOpen.value = false
  nextTick(() => authButton.value?.focus())
}

function activate(id, focus = true) {
  if (tab.value !== id) { tab.value = id; banner.value = null }
  if (id === 'evidence' || id === 'model') refresh()
  if (focus) nextTick(() => document.getElementById('panel-title')?.focus())
}
// Activation is synchronous; the hash only mirrors it, so back/forward still work through hashchange.
function go(id, focus = true) {
  if (!SECTIONS.some(section => section.id === id)) return
  if (location.hash !== '#' + id) history.pushState(null, '', '#' + id)
  activate(id, focus)
}
function onHash() { activate(fromHash()) }
function focusMain() { document.getElementById('panel-title')?.focus() || document.getElementById('main')?.focus() }

onMounted(() => {
  window.addEventListener('hashchange', onHash)
  refresh()
})
onBeforeUnmount(() => window.removeEventListener('hashchange', onHash))
</script>

<template>
  <div class="shell">
    <a class="skip-link" href="#main" @click.prevent="focusMain">跳到主要内容</a>
    <header class="topbar">
      <a class="brand" href="#decide" @click.prevent="go('decide')">
        <span class="brand-mark" aria-hidden="true"><Icon name="split" /></span>
        <span class="brand-text"><strong>Text Model Lab</strong><small>英文短信 ham / spam 判断与人工复核</small></span>
      </a>
      <div class="topbar-actions">
        <span v-if="config" class="mode-pill" :class="isPublic ? 'is-public' : 'is-private'">{{ isPublic ? '公开只读' : '本地工作台' }}</span>
        <button v-if="config && !isPublic" ref="authButton" type="button" class="btn btn-sm" :class="operator ? 'btn-ok' : 'btn-outline'" :aria-expanded="authOpen" aria-controls="auth-panel" @click="toggleAuth">
          <Icon :name="operator ? 'shield' : 'key'" />{{ operator ? '操作者已认证' : '操作者登录' }}
        </button>
      </div>
    </header>

    <section v-if="authOpen && config && !isPublic" id="auth-panel" class="auth-panel" aria-label="操作者令牌" @keydown.esc="closeAuth">
      <form v-if="!operator" class="auth-inner" @submit.prevent="loadToken">
        <label for="token-input" class="auth-label">操作者令牌</label>
        <div class="input-row">
          <input id="token-input" ref="tokenInput" v-model="tokenDraft" type="password" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="token-hint">
          <button class="btn btn-primary" :disabled="!tokenDraft.trim()">载入</button>
          <button type="button" class="btn btn-ghost" @click="closeAuth">取消</button>
        </div>
        <p id="token-hint" class="fine">只保存在当前页面内存，不写入浏览器存储。公开部署即使带令牌也保持只读。</p>
      </form>
      <div v-else class="auth-inner">
        <p class="auth-label">操作者已认证：可导入、审核、生成快照与选择候选。</p>
        <div class="input-row">
          <button ref="tokenInput" type="button" class="btn btn-danger" @click="clearToken">清除令牌与私有数据</button>
          <button type="button" class="btn btn-ghost" @click="closeAuth">关闭</button>
        </div>
        <p class="fine">清除后页面丢弃样本、草稿、判断文本与 Key；进行中的请求结果不会回填。</p>
      </div>
    </section>

    <div v-if="config" class="context-strip" aria-label="当前服务状态">
      <span class="ctx"><span class="ctx-k">任务</span><code>{{ config.decision?.task?.id || 'english-sms-ham-spam-v1' }}</code></span>
      <span class="ctx"><span class="ctx-k">实际加载</span><template v-if="loaded"><code>{{ loaded.run_id }}</code> · {{ KIND[loaded.kind] || loaded.kind }} · 阈值 {{ loaded.threshold }}</template><template v-else>未加载本地模型</template></span>
      <span class="ctx"><span class="ctx-k">Jev</span>{{ jevEnabled ? `${config.decision.model_version} · 自带 Key、逐次同意` : '未启用' }}</span>
      <span v-if="service?.restart_required" class="ctx ctx-warn"><Icon name="clock" />已选候选，重启后才生效</span>
    </div>

    <div class="frame">
      <nav class="sidenav" aria-label="工作台功能">
        <template v-for="[group, label] in GROUPS" :key="group">
          <p class="nav-group">{{ label }}</p>
          <a v-for="section in SECTIONS.filter(item => item.group === group)" :key="section.id" :href="'#' + section.id" class="nav-item" :class="{ active: tab === section.id }" :aria-current="tab === section.id ? 'page' : undefined" @click.prevent="go(section.id)">
            <Icon :name="section.id" />
            <span class="nav-text"><span class="nav-title">{{ section.title }}</span><span class="nav-hint">{{ section.hint }}</span></span>
            <span v-if="locked(section)" class="nav-lock" :title="isPublic ? '公开部署不提供' : '需要操作者身份'"><Icon name="lock" /><span class="sr-only">{{ isPublic ? '（公开部署不提供）' : '（需要操作者身份）' }}</span></span>
          </a>
        </template>
      </nav>

      <main id="main" tabindex="-1">
        <div v-if="banner" class="callout banner" :class="'callout-' + banner.tone" :role="banner.tone === 'error' ? 'alert' : 'status'">
          <Icon :name="banner.tone === 'ok' ? 'check' : banner.tone === 'error' ? 'alert' : 'info'" />
          <span class="callout-body">{{ banner.text }}</span>
          <button type="button" class="icon-btn" aria-label="关闭提示" @click="banner = null"><Icon name="x" /></button>
        </div>
        <div v-if="config?.public" class="public-note"><Icon name="info" /><span>公开只读演示：可以试用本地判断；Jev 需你自己的 TypeSafe Key 与逐次同意，输入不保存。导入、审核与训练只在操作者本地工作台进行。</span></div>

        <div v-if="status === 'loading' && !config" class="state-card" role="status" aria-live="polite">
          <span class="spinner spinner-lg" aria-hidden="true"></span>
          <h1 id="panel-title" tabindex="-1">正在读取服务状态…</h1>
          <p>读取部署模式、实际加载模型与 Jev 能力。</p>
        </div>
        <div v-else-if="status === 'error' && !config" class="state-card state-error" role="alert">
          <span class="state-icon"><Icon name="alert" /></span>
          <h1 id="panel-title" tabindex="-1">无法读取服务状态</h1>
          <p>{{ loadError }} 页面没有发送任何短信或 Key。</p>
          <button type="button" class="btn btn-primary" @click="status = 'loading'; refresh()">重新读取</button>
        </div>
        <template v-else>
          <DecisionPanel v-if="tab === 'decide'" :key="'decide-' + identity" :config="config" :health="health" :operator-token="token" />
          <ImportPanel v-else-if="tab === 'import'" :key="'import-' + identity" :api="api" :config="config" :operator="operator" @navigate="go" />
          <ReviewPanel v-else-if="tab === 'review'" :key="'review-' + identity" :api="api" :config="config" :operator="operator" @navigate="go" />
          <EvidencePanel v-else-if="tab === 'evidence'" :key="'evidence-' + identity" :config="config" :runs="runs" :final-report="finalReport" :state="evidenceState" @refresh="refresh" />
          <ModelPanel v-else :key="'model-' + identity" :api="api" :config="config" :health="health" :service="service" :runs="runs" :operator="operator" @refresh="refresh" />
        </template>
      </main>
    </div>

    <footer class="footer">
      <span>Text Model Lab · v{{ config?.version || '2' }} · 英文短信 ham/spam</span>
      <span>模型建议不是人工真值；软件测试、模拟演示与真实效果分别记录。</span>
    </footer>
  </div>
</template>
