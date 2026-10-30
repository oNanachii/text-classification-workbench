// In-memory only. Invalidation must win over a response, even if fetch ignores abort.
export function isSecureDecisionOrigin(location, publicMode = true) {
  return location?.protocol === 'https:' || (!publicMode && location?.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]', '::1'].includes(location?.hostname))
}

export function createDecisionSession(state, { fetchImpl = fetch, operatorToken = () => '', secureTransport = () => isSecureDecisionOrigin(globalThis.location) } = {}) {
  let epoch = 0
  let controller
  function invalidate() {
    epoch += 1
    controller?.abort()
    controller = null
    state.busy = false
    state.result = null
    state.error = ''
  }
  function clear() {
    invalidate()
    state.key = ''
    state.text = ''
    state.consent = false
  }
  async function submit() {
    if (state.busy || !state.text.trim() || state.text.length > 2000) return
    const external = state.engine !== 'local'
    if (external && (!state.consent || (state.credentialSource === 'byok' && !state.key))) return
    if (external && !secureTransport()) {
      state.error = '外部凭据只能通过 HTTPS 发送；非公开本机开发可使用回环地址。'
      return
    }
    invalidate()
    const ticket = epoch
    const requestController = new AbortController()
    controller = requestController
    const signal = requestController.signal
    let credential = state.key
    const headers = { 'Content-Type': 'application/json' }
    if (external && state.credentialSource === 'byok') headers['X-TypeSafe-Key'] = credential
    if (external && state.credentialSource === 'server' && operatorToken()) headers.Authorization = 'Bearer ' + operatorToken()
    const body = JSON.stringify({ text: state.text, engine: state.engine, credential_source: state.credentialSource, external_consent: external && state.consent })
    let timedOut = false
    const timeout = setTimeout(() => { timedOut = true; requestController.abort() }, 20000)
    state.busy = true
    try {
      const response = await fetchImpl('/api/decision', { method: 'POST', headers, body, signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error' })
      const payload = await response.json()
      if (ticket !== epoch) return
      if (timedOut) throw new Error('unknown')
      if (!response.ok) {
        const messages = {
          400: '请检查凭据、外发同意和 HTTPS 连接。',
          401: '操作者认证失效，请重新载入。',
          403: '当前身份或访问来源不能使用所选凭据。',
          413: '请求超过长度限制。', 415: '请求格式不受支持。',
          422: '请检查短信长度与选项。',
          429: '请求过于频繁或服务正忙；没有自动重试。',
          503: '服务尚未开启 Jev，请使用本地模型。',
        }
        state.error = messages[response.status] || '服务没有确认结果；没有自动重试。'
        return
      }
      state.result = payload
    } catch {
      if (ticket === epoch) state.error = external
        ? '未收到可确认的结果，TypeSafe 可能已处理并计费；请先核对用量，没有自动重试。'
        : '未收到可确认的本地结果，请检查连接。'
    } finally {
      clearTimeout(timeout)
      credential = ''
      delete headers['X-TypeSafe-Key']
      delete headers.Authorization
      if (ticket === epoch) { state.busy = false; controller = null }
    }
  }
  return { submit, invalidate, clear }
}
