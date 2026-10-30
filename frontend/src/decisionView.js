// Pure interpretation of /api/decision results. No result is upgraded beyond what the contract states.
export const ENGINE_ERRORS = {
  provider_authentication_failed: 'TypeSafe 拒绝了这个 Key，请核对后手动重新提交。',
  provider_rate_limited: 'TypeSafe 限流；没有自动重试。',
  provider_unavailable: 'TypeSafe 服务不可用；是否已处理、是否计费都未确认。',
  provider_request_rejected: 'TypeSafe 拒绝了此请求。',
  provider_timeout: 'TypeSafe 响应超时；可能已处理并计费。',
  provider_connection_failed: '与 TypeSafe 的连接中断；可能已处理并计费。',
  invalid_provider_response: 'TypeSafe 返回不符合契约，未采用该判断。',
  provider_response_too_large: 'TypeSafe 响应超过安全上限，未采用该判断。',
  local_model_unavailable: '服务当前没有加载本地模型。',
  local_prediction_failed: '本地模型无法判断此文本：仅支持含已知词汇的英文短信。',
}

export const ENGINES = {
  local: ['local'],
  jev: ['jev'],
  compare: ['local', 'jev'],
}

export function engineOutcome(part) {
  if (!part) return null
  if (part.status === 'ok') return 'ok'
  if (part.status === 'unknown' || part.outcome_unknown) return 'unknown'
  return 'error'
}

export function engineMessage(part) {
  return ENGINE_ERRORS[part?.error_code] || '此引擎没有返回有效判断。'
}

// Only two successful engines form a comparison. Failure or unknown never reads as agreement.
export function compareVerdict(result) {
  if (!result || result.engine !== 'compare') return null
  if (engineOutcome(result.local) !== 'ok' || engineOutcome(result.jev) !== 'ok') return 'incomplete'
  if (result.disagreement === true) return 'disagree'
  if (result.disagreement === false) return 'agree'
  return 'incomplete'
}

export function percent(value, digits = 1) {
  return Number.isFinite(value) ? (100 * value).toFixed(digits) + '%' : '—'
}

// Human-readable reasons why the submit button is disabled; mirrors the session guards.
export function submitBlockers({ text = '', engine = 'local', credentialSource = 'byok', key = '', consent = false }, { enabled = false, secure = false, modelReady = false, serverAvailable = false } = {}) {
  const reasons = []
  const external = engine !== 'local'
  if (!text.trim()) reasons.push('输入一条英文短信')
  if (text.length > 2000) reasons.push('缩短到 2000 字符以内')
  if (engine === 'local' && !modelReady) reasons.push('服务尚未加载本地模型')
  if (external) {
    if (!enabled) reasons.push('此服务未开启 Jev')
    else {
      if (!secure) reasons.push('改用 HTTPS 连接')
      if (credentialSource === 'byok' && !key) reasons.push('填写 TypeSafe Key')
      if (credentialSource === 'server' && !serverAvailable) reasons.push('服务器 Key 不可用')
      if (!consent) reasons.push('勾选外发同意')
    }
  }
  return reasons
}
