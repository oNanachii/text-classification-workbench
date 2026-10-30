<script setup>
import { computed, onBeforeUnmount, reactive, watch } from 'vue'
import { createDecisionSession, isSecureDecisionOrigin } from '../decisionSession.js'
const props = defineProps({ config: Object, health: Object, operatorToken: { type: String, default: '' } })
const state = reactive({ text: 'Please bring the blue notebook to our meeting tomorrow.', engine: 'local', credentialSource: 'byok', key: '', consent: false, busy: false, result: null, error: '' })
const secureTransport = () => isSecureDecisionOrigin(globalThis.location, props.config?.public !== false)
const session = createDecisionSession(state, { operatorToken: () => props.operatorToken, secureTransport })
const external = computed(() => state.engine !== 'local')
const enabled = computed(() => Boolean(props.config?.decision?.enabled))
const canSubmit = computed(() => !state.busy && state.text.trim() && state.text.length <= 2000 && (!external.value || (enabled.value && secureTransport() && state.consent && (state.credentialSource === 'server' || state.key))) && (state.engine !== 'local' || props.health?.model_ready))
const results = computed(() => [['local', '本地英文短信模型'], ['jev', 'Jev / TypeSafe']].filter(([name]) => state.result?.[name]))
const pct = value => Number.isFinite(value) ? (100 * value).toFixed(2) + '%' : '—'
const errors = {
  provider_authentication_failed: 'TypeSafe 拒绝凭据，请核对 Key。',
  provider_rate_limited: 'TypeSafe 限流，没有自动重试。',
  provider_unavailable: 'TypeSafe 服务不可用，处理和计费结果未确认。',
  provider_request_rejected: 'TypeSafe 拒绝此请求。',
  provider_timeout: 'TypeSafe 响应超时，可能已处理并计费。',
  provider_connection_failed: '连接中断，可能已处理并计费。',
  invalid_provider_response: 'TypeSafe 返回不符合契约，未采用该判断。',
  provider_response_too_large: 'TypeSafe 响应超过安全上限，未采用该判断。',
  local_model_unavailable: '当前没有加载本地模型。', local_prediction_failed: '本地模型无法判断此文本；仅限有已知词汇的英文短信。',
}
watch(() => [state.text, state.engine, state.credentialSource, state.key, state.consent], session.invalidate, { flush: 'sync' })
watch(() => props.operatorToken, () => { session.clear(); state.credentialSource = 'byok' })
watch(() => props.config?.decision?.server_key_available, value => { if (!value) state.credentialSource = 'byok' })
onBeforeUnmount(session.clear)
</script>

<template>
 <section>
  <div class="page-heading"><div><p class="eyebrow">DECISION</p><h1>单条判断与并排对照</h1><p>同一条英文短信，同一组 ham / spam 标签；模型建议交由人工复核。</p></div></div>
  <form class="card v2-pad" @submit.prevent="session.submit">
   <label class="field">英文短信<textarea v-model="state.text" maxlength="2000" rows="5" aria-label="待判断英文短信"></textarea></label><p>{{ state.text.length }} / 2000 · ham：正常个人、事务或订阅消息；spam：未经请求的推广、奖品诱导或诈骗。</p>
   <label class="field">判断引擎<select v-model="state.engine" aria-label="判断引擎"><option value="local">本地英文短信模型</option><option value="jev" :disabled="!enabled">Jev · {{ props.config?.decision?.model_version || 'jev-1.14.0' }}</option><option value="compare" :disabled="!enabled">本地与 Jev 并排对照</option></select></label>
   <p v-if="!enabled">此服务尚未开启 Jev；本地判断可继续使用。</p>
   <template v-if="external">
    <p v-if="!secureTransport()" class="notice error" role="alert">此连接不允许发送 Key。请使用 HTTPS；非公开本机开发可使用回环地址。</p>
    <label class="field">凭据来源<select v-model="state.credentialSource"><option value="byok">使用自己的 TypeSafe Key</option><option v-if="props.config?.decision?.server_key_available" value="server">明确使用操作者已配置的服务器 Key</option></select></label>
    <label v-if="state.credentialSource==='byok'" class="field">TypeSafe API Key<input v-model="state.key" type="password" maxlength="512" autocomplete="off" spellcheck="false" autocapitalize="off" aria-label="TypeSafe API Key"></label>
    <p>Key 仅存当前页面内存。公网通过同源 HTTPS 送到后端，再由后端向 TypeSafe 发起一次请求；不保存 Key 和短信。离开本面板即清除。</p>
    <label class="field"><span><input v-model="state.consent" type="checkbox">我同意将这条文本发送给 TypeSafe，并使用所选 Key 的额度。</span></label>
    <p>超时或取消仍可能消耗额度，不自动重试。清除凭据无法撤回已经发出的请求。不要输入无权外发的内容。</p>
   </template>
   <p>本地模型仅有英文短信范围的历史证据；中文或其他标签未验证。分数、概率与 confidence 均不等于正确率。</p>
   <div v-if="state.error" class="notice error" role="alert">{{ state.error }}</div>
   <div class="form-actions"><button class="button primary" :disabled="!canSubmit">{{ state.busy?'正在判断…':external?'明确调用并判断':'本地判断' }}</button><button type="button" class="button secondary" @click="session.clear">清除 Key、文本与结果</button></div>
  </form>
  <div v-if="state.result" class="prediction-layout" aria-live="polite">
   <section v-for="[name,title] in results" :key="name" class="card v2-pad"><h2>{{ title }}</h2><template v-if="state.result[name].status==='ok'"><h3>{{ state.result[name].label==='spam'?'spam · 垃圾短信':'ham · 正常短信' }}</h3><p>正常 {{ pct(state.result[name].probabilities.ham) }} · 垃圾 {{ pct(state.result[name].probabilities.spam) }}</p><p v-if="name==='local'">本地未校准模型分数，阈值 {{ state.result[name].threshold }}。</p><p v-else>TypeSafe 返回的概率分布；confidence {{ pct(state.result[name].confidence) }} 表示分布集中程度，非正确率。</p></template><p v-else role="status">{{ errors[state.result[name].error_code] || '此引擎未返回有效判断。' }}</p><dl><dt>模型版本</dt><dd>{{ state.result[name].model }} / {{ state.result[name].model_version||'未加载' }}</dd><dt>耗时</dt><dd>{{ state.result[name].elapsed_ms }} ms</dd><template v-if="name==='local'"><dt>训练快照</dt><dd class="v2-break">{{ state.result[name].snapshot_id||'—' }}</dd><dt>数据版本</dt><dd class="v2-break">{{ state.result[name].dataset_id||'—' }}</dd></template><template v-if="name==='jev'"><dt>输入 / 输出 token</dt><dd>{{ state.result[name].usage?.input_tokens??'未返回' }} / {{ state.result[name].usage?.output_tokens??'未返回' }}</dd></template></dl></section>
  </div>
  <p v-if="state.result" class="notice info"><template v-if="state.result.engine==='compare'">{{ state.result.disagreement===true?'两边判断不一致，请人工复核。':state.result.disagreement===false?'两边标签一致，但一致不代表正确。':'未形成有效的双模型对照。' }} </template>此次结果不会修改人工标签或进入训练快照。</p>
 </section>
</template>
