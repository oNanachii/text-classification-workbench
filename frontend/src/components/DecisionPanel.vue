<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { createDecisionSession, isSecureDecisionOrigin } from '../decisionSession.js'
import { ENGINES, compareVerdict, engineMessage, engineOutcome, percent, submitBlockers } from '../decisionView.js'

const props = defineProps({ config: Object, health: Object, operatorToken: { type: String, default: '' } })
const state = reactive({ text: '', engine: 'local', credentialSource: 'byok', key: '', consent: false, busy: false, result: null, error: '' })
const notice = ref('')
const secureTransport = () => isSecureDecisionOrigin(globalThis.location, props.config?.public !== false)
const session = createDecisionSession(state, { operatorToken: () => props.operatorToken, secureTransport })

// AI-authored smoke examples (examples/jev_authored_sms.json); not real messages or evaluation data.
const EXAMPLES = [
  ['会议提醒', 'Please bring the blue notebook to our meeting tomorrow.'],
  ['预约确认', 'The dentist appointment you booked is confirmed for Monday at 10 AM.'],
  ['奖金诱导', 'You have won an unclaimed cash prize! Send a processing payment now to claim it.'],
  ['投资推广', 'Exclusive unsolicited offer: buy our miracle investment package today for guaranteed instant riches!'],
]
const KIND = { lr: '逻辑回归', mlp16: 'MLP 16' }

const external = computed(() => state.engine !== 'local')
const enabled = computed(() => Boolean(props.config?.decision?.enabled))
const serverAvailable = computed(() => Boolean(props.config?.decision?.server_key_available))
const modelReady = computed(() => Boolean(props.health?.model_ready))
const modelVersion = computed(() => props.config?.decision?.model_version || 'jev-1.14.0')
const secure = computed(() => secureTransport())
const httpsPage = computed(() => globalThis.location?.protocol === 'https:')
const blockers = computed(() => submitBlockers(state, { enabled: enabled.value, secure: secure.value, modelReady: modelReady.value, serverAvailable: serverAvailable.value }))
const canSubmit = computed(() => !state.busy && blockers.value.length === 0)
const verdict = computed(() => compareVerdict(state.result))
const waitingFor = computed(() => ENGINES[state.engine] || [])
const parts = computed(() => state.result ? (ENGINES[state.result.engine] || []).filter(name => state.result[name]).map(name => [name, state.result[name]]) : [])

const engineOptions = computed(() => [
  { id: 'local', name: '本地模型', desc: '不外发文本 · 未校准分数', badge: modelReady.value ? '' : '未加载', tone: 'warn', disabled: false },
  { id: 'jev', name: 'Jev', desc: `外发 TypeSafe · ${modelVersion.value}`, badge: enabled.value ? '外发' : '未启用', tone: enabled.value ? 'accent' : 'neutral', disabled: !enabled.value },
  { id: 'compare', name: '并排对照', desc: '同一文本，本地与 Jev 各判一次', badge: enabled.value ? '外发' : '未启用', tone: enabled.value ? 'accent' : 'neutral', disabled: !enabled.value },
])
const submitText = computed(() => state.busy ? '正在判断…' : { local: '本地判断', jev: '发送给 Jev 判断', compare: '本地 + Jev 对照判断' }[state.engine])
const liveText = computed(() => {
  if (state.busy) return '正在等待判断结果。'
  if (!state.result) return ''
  const sides = parts.value.map(([name, part]) => `${name === 'local' ? '本地' : 'Jev'}：${engineOutcome(part) === 'ok' ? part.label : engineOutcome(part) === 'unknown' ? '结果未知' : '失败'}`)
  const extra = { disagree: '两边不一致，请人工复核。', agree: '两边一致，但一致不代表正确。', incomplete: '未形成有效对照。' }[verdict.value] || ''
  return `判断完成。${sides.join('；')}。${extra}`
})

function outcomeTag(part) {
  return { ok: ['有效判断', 'tag-ok'], unknown: ['结果未知', 'tag-warn'], error: ['未得到判断', 'tag-error'] }[engineOutcome(part)]
}
const fixed = (value, digits) => Number.isFinite(value) ? value.toFixed(digits) : '—'
const short = value => value ? (String(value).length > 26 ? String(value).slice(0, 24) + '…' : String(value)) : '—'
const ms = value => !Number.isFinite(value) ? '—' : value < 1 ? '不足 1 ms' : `${Math.round(value)} ms`
const relation = part => part.probabilities.spam >= part.threshold ? '≥' : '<'

// Any change to text, engine, credential source, key or consent invalidates the pending or shown result.
watch(() => [state.text, state.engine, state.credentialSource, state.key, state.consent], () => {
  if (state.busy) notice.value = '输入或外发设置已变化：等待中的请求已作废，迟到的结果不会显示。若已外发，TypeSafe 仍可能处理并计费。'
  else if (state.result || state.error) notice.value = '输入或设置已变化，之前的结果已移除。'
  session.invalidate()
}, { flush: 'sync' })
watch(() => props.operatorToken, () => { session.clear(); state.credentialSource = 'byok'; notice.value = '' })
watch(serverAvailable, value => { if (!value) state.credentialSource = 'byok' })
watch(enabled, value => { if (!value && state.engine !== 'local') state.engine = 'local' })
onBeforeUnmount(session.clear)

function submit() {
  if (!canSubmit.value) return
  notice.value = ''
  session.submit()
}
function cancel() {
  notice.value = ''
  session.cancel()
}
function clearAll() {
  const sentOut = state.busy && state.engine !== 'local'
  session.clear()
  state.credentialSource = 'byok'
  notice.value = sentOut
    ? '已清除 Key、文本、同意项与结果。已发出的外发请求无法撤回，TypeSafe 仍可能处理并计费。'
    : '已清除 Key、文本、同意项与结果。'
}
</script>

<template>
  <section class="panel">
    <header class="panel-head">
      <div>
        <p class="eyebrow">判断体验</p>
        <h1 id="panel-title" tabindex="-1">判断一条英文短信</h1>
        <p class="lede">同一条文本、同一组 <b>ham / spam</b> 标签。结果只是供人工复核的建议，不会写入审核标签或训练快照。</p>
      </div>
    </header>

    <div class="decide-grid">
      <form class="card composer" novalidate @submit.prevent="submit">
        <fieldset class="step">
          <legend class="step-label"><span class="step-no">1</span>选择引擎</legend>
          <div class="engine-options">
            <label v-for="option in engineOptions" :key="option.id" class="engine-option" :class="{ selected: state.engine === option.id, disabled: option.disabled }">
              <input v-model="state.engine" type="radio" name="engine" :value="option.id" :disabled="option.disabled">
              <span class="engine-body"><span class="engine-name">{{ option.name }}<span v-if="option.badge" class="tag" :class="'tag-' + option.tone">{{ option.badge }}</span></span><span class="engine-desc">{{ option.desc }}</span></span>
            </label>
          </div>
          <p v-if="!enabled" class="field-note">此服务没有开启 Jev，只能使用本地模型。</p>
          <p v-else-if="!modelReady" class="field-note">服务没有加载本地模型：本地判断不可用，对照中的本地一侧会显示失败。</p>
        </fieldset>

        <div class="step">
          <div class="step-label-row">
            <label for="sms-text" class="step-label"><span class="step-no">2</span>输入英文短信</label>
            <span class="counter" :class="{ over: state.text.length > 2000 }">{{ state.text.length }} / 2000</span>
          </div>
          <textarea id="sms-text" v-model="state.text" rows="5" maxlength="2000" spellcheck="false" placeholder="例如：Your parcel is ready for pickup at the front desk." aria-describedby="label-rules"></textarea>
          <div class="examples" role="group" aria-label="自编示例">
            <span class="examples-label">自编示例</span>
            <button v-for="[name, text] in EXAMPLES" :key="name" type="button" class="chip" @click="state.text = text">{{ name }}</button>
          </div>
          <p id="label-rules" class="label-rules"><span><span class="label-badge ham">ham</span>正常个人、事务或明确订阅的消息</span><span><span class="label-badge spam">spam</span>未经请求的推广、奖品诱导或诈骗</span></p>
        </div>

        <fieldset v-if="external && enabled" class="step outbound" :class="{ blocked: !secure }">
          <legend class="step-label"><span class="step-no">3</span>外发前确认</legend>
          <p class="transport" :class="secure ? 'is-ok' : 'is-bad'" :role="secure ? undefined : 'alert'">
            <Icon :name="secure ? 'shield' : 'alert'" />
            <span v-if="secure">{{ httpsPage ? '连接使用 HTTPS，可以发送 Key。' : '本机回环开发连接（非公开模式），允许发送 Key。' }}</span>
            <span v-else>当前连接不是 HTTPS：页面不会发送任何 Key。请改用 HTTPS 访问；公开部署不接受本机 HTTP。</span>
          </p>
          <fieldset v-if="serverAvailable" class="sub-fieldset">
            <legend>凭据来源</legend>
            <label class="radio"><input v-model="state.credentialSource" type="radio" name="credential" value="byok">使用自己的 TypeSafe Key</label>
            <label class="radio"><input v-model="state.credentialSource" type="radio" name="credential" value="server">明确使用操作者配置的服务器 Key</label>
          </fieldset>
          <div v-if="state.credentialSource === 'byok'" class="field">
            <label for="typesafe-key">TypeSafe API Key</label>
            <input id="typesafe-key" v-model="state.key" type="password" maxlength="512" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="key-note">
            <p id="key-note" class="field-note">只在当前页面内存中；不写入浏览器存储、数据库或日志。以请求头经同源连接交给后端，由后端调用一次 TypeSafe。清除、换身份或离开本页即丢弃。</p>
          </div>
          <p v-else class="field-note">服务器 Key 由操作者在后端配置，页面看不到它的值；用量记在该 Key 名下。</p>
          <label class="consent"><input v-model="state.consent" type="checkbox" aria-describedby="consent-note"><span>我同意把这条文本发送给 TypeSafe，并使用所选 Key 的额度。</span></label>
          <ul id="consent-note" class="fine-list">
            <li>每次提交至多一次上游请求，不会自动重试。</li>
            <li>超时、取消或断线都不能证明没有被处理或没有计费。</li>
            <li>不要输入无权外发的内容。</li>
          </ul>
        </fieldset>

        <div class="actions">
          <button class="btn btn-primary btn-lg" :disabled="!canSubmit" :aria-describedby="blockers.length && !state.busy ? 'submit-blockers' : undefined">
            <span v-if="state.busy" class="spinner" aria-hidden="true"></span>{{ submitText }}
          </button>
          <button v-if="state.busy" type="button" class="btn btn-outline" @click="cancel">停止等待</button>
          <button type="button" class="btn btn-ghost" @click="clearAll">清除 Key、文本与结果</button>
        </div>
        <p v-if="blockers.length && !state.busy" id="submit-blockers" class="blockers">还需要：{{ blockers.join(' · ') }}</p>
      </form>

      <section class="results" aria-labelledby="results-title" :aria-busy="state.busy ? 'true' : 'false'">
        <div class="results-head">
          <h2 id="results-title">判断结果</h2>
          <span class="fine">{{ state.busy ? '等待中' : state.result ? `用时 ${ms(state.result.elapsed_ms)}` : '' }}</span>
        </div>
        <p class="sr-only" role="status" aria-live="polite">{{ liveText }}</p>

        <div v-if="notice" class="callout callout-info" role="status"><Icon name="info" /><span class="callout-body">{{ notice }}</span></div>
        <div v-if="state.error" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body"><strong>没有可显示的判断。</strong>{{ state.error }}</span></div>

        <template v-if="state.busy">
          <article v-for="name in waitingFor" :key="name" class="engine-card is-loading">
            <header class="engine-card-head"><div><h3>{{ name === 'local' ? '本地模型' : 'Jev · TypeSafe' }}</h3><p class="mono-sub">{{ name === 'local' ? '本地计算中' : '等待 TypeSafe 返回' }}</p></div><span class="spinner" aria-hidden="true"></span></header>
            <div class="sk sk-w40"></div><div class="sk sk-bar"></div><div class="sk sk-w70"></div>
            <p v-if="name === 'jev'" class="fine">不会自动重试。可以停止等待，但已发出的请求仍可能被处理并计费。</p>
          </article>
        </template>

        <template v-else-if="state.result">
          <div v-if="verdict" class="verdict" :class="'verdict-' + verdict" role="status">
            <Icon :name="verdict === 'disagree' ? 'alert' : verdict === 'agree' ? 'split' : 'info'" />
            <div>
              <strong v-if="verdict === 'disagree'">两边判断不一致 · 请人工复核</strong>
              <strong v-else-if="verdict === 'agree'">两边标签一致 · 一致不代表正确</strong>
              <strong v-else>未形成有效对照</strong>
              <p v-if="verdict === 'disagree'">本地判为 {{ state.result.local.label }}，Jev 判为 {{ state.result.jev.label }}。分歧只是需要人工关注的信号，不说明哪一边正确。</p>
              <p v-else-if="verdict === 'agree'">双方都判为 {{ state.result.local.label }}。两个模型可能同时出错，仍以人工判断为准。</p>
              <p v-else>至少一边失败或结果未知。只展示成功的一侧，不计为一致或分歧。</p>
            </div>
          </div>

          <article v-for="[name, part] in parts" :key="name" class="engine-card" :class="'is-' + engineOutcome(part)">
            <header class="engine-card-head">
              <div>
                <h3>{{ name === 'local' ? '本地模型' : 'Jev · TypeSafe' }}</h3>
                <p class="mono-sub">{{ name === 'local' ? (KIND[part.model] || part.model || '未加载') : part.model }} · {{ part.model_version || '—' }}</p>
              </div>
              <span class="tag" :class="outcomeTag(part)[1]">{{ outcomeTag(part)[0] }}</span>
            </header>

            <template v-if="engineOutcome(part) === 'ok'">
              <p class="verdict-label" :class="part.label"><span class="verdict-dot" aria-hidden="true"></span>{{ part.label }}<small>{{ part.label === 'spam' ? '垃圾短信' : '正常短信' }}</small><span class="suggest">模型建议</span></p>
              <template v-if="name === 'local'">
                <div class="score-track" role="img" :aria-label="`spam_score ${fixed(part.probabilities.spam, 3)}，阈值 ${part.threshold}`">
                  <span class="score-fill" :class="part.label" :style="{ width: part.probabilities.spam * 100 + '%' }"></span>
                  <span class="score-threshold" :style="{ left: part.threshold * 100 + '%' }"></span>
                </div>
                <div class="score-axis"><span>0 偏 ham</span><span>阈值 {{ part.threshold }}</span><span>1 偏 spam</span></div>
                <p class="explain">未校准模型分数 spam_score <b>{{ fixed(part.probabilities.spam, 3) }}</b> {{ relation(part) }} 阈值 {{ part.threshold }}，因此判为 {{ part.label }}。这个分数不是“判断正确的概率”。</p>
              </template>
              <template v-else>
                <div class="dist" role="img" :aria-label="`ham ${percent(part.probabilities.ham)}，spam ${percent(part.probabilities.spam)}`">
                  <span class="dist-seg ham" :style="{ width: part.probabilities.ham * 100 + '%' }"></span>
                  <span class="dist-seg spam" :style="{ width: part.probabilities.spam * 100 + '%' }"></span>
                </div>
                <div class="score-axis"><span>ham {{ percent(part.probabilities.ham) }}</span><span>spam {{ percent(part.probabilities.spam) }}</span></div>
                <p class="explain">TypeSafe 报告的选项分布，未经本地校准。分布集中度（confidence）<b>{{ fixed(part.confidence, 2) }}</b> 只表示分布有多集中，不是正确率。</p>
              </template>
              <dl class="kv kv-compact">
                <template v-if="name === 'local'"><dt>阈值</dt><dd>{{ part.threshold }}</dd><dt>训练快照</dt><dd><code>{{ short(part.snapshot_id) }}</code></dd><dt>数据版本</dt><dd><code>{{ short(part.dataset_id) }}</code></dd></template>
                <template v-else><dt>输入 / 输出 token</dt><dd>{{ part.usage ? `${part.usage.input_tokens} / ${part.usage.output_tokens}` : '服务未返回（不代表零消耗）' }}</dd></template>
                <dt>耗时</dt><dd>{{ ms(part.elapsed_ms) }}</dd>
              </dl>
            </template>

            <template v-else>
              <p class="engine-error">{{ engineMessage(part) }}</p>
              <p v-if="engineOutcome(part) === 'unknown'" class="fine">结果未知：上游可能已处理并计费。没有自动重试；如需再判断，请先核对用量再手动提交。</p>
              <p v-else-if="name === 'jev'" class="fine">没有自动重试，也没有改用其他凭据。</p>
              <dl class="kv kv-compact"><dt>状态码</dt><dd><code>{{ part.error_code || part.status }}</code></dd><dt>耗时</dt><dd>{{ ms(part.elapsed_ms) }}</dd></dl>
            </template>
          </article>

          <p class="result-foot"><Icon name="info" /><span>仅供人工判断参考（human_review_required）。这次结果不会写入审核标签、训练库或快照。请求 <code>{{ short(state.result.request_id) }}</code></span></p>
        </template>

        <div v-else-if="!state.error" class="empty">
          <div class="empty-art" aria-hidden="true"><span class="label-badge ham">ham</span><span class="empty-or">或</span><span class="label-badge spam">spam</span></div>
          <h3>还没有判断结果</h3>
          <p>输入或选用一条自编示例，选择引擎后提交。本地模式不外发文本，也不发送任何 Key。</p>
          <ul class="fine-list">
            <li>本地分数未经校准，不是正确率。</li>
            <li>Jev confidence 表示分布集中度，不是正确率。</li>
            <li>模型建议不会成为人工标签或训练数据。</li>
          </ul>
        </div>
      </section>
    </div>
  </section>
</template>
