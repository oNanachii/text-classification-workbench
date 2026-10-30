// Same-origin JSON client for operator/public reads and writes.
// An identity change wins over any response; a write without confirmation is reported as unknown.
export class ApiError extends Error {
  constructor(message, { status = 0, unknown = false, code = '' } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.unknown = unknown
    this.code = code
  }
}

// Only messages raised by this project's own backend are translated; anything else stays generic.
const KNOWN = {
  'CSV exceeds 5 MiB': 'CSV 超过 5 MiB 上限。',
  'A source description is required': '请填写来源说明。',
  'CSV columns must be exactly text,label': '列名必须恰好是 text,label。',
  'Malformed CSV': 'CSV 格式无法解析，请检查引号与分隔符。',
  'CSV must contain 1..10000 rows': 'CSV 需包含 1–10000 行数据。',
  'No accepted new rows': '这次预览没有可新增的行，不能确认。',
  'Sample appeared after preview; preview again': '预览之后数据已变化，请重新预览。',
  'Preview checksum mismatch': '预览记录校验失败，请重新预览。',
  'Dataset changed': '数据集版本已变化，请重新预览。',
  'Review pool exceeds 20000 rows; use a separate data version': '审核池将超过 20000 行，请另立数据版本。',
  'A newer revision exists; reload before saving': '服务器已有更新修订，本次保存未覆盖。',
  'Label corrections require a reason': '修改标签需要填写复核依据。',
  'Review confirmation requires a reason': '确认待审样本需要填写复核依据。',
  'Reviewed training data requires both classes': '已审核数据需同时包含 ham 与 spam 才能生成快照。',
  'Service selection revision changed': '服务选择已被更新，请刷新后再决定。',
  'No previous selection': '没有可回退的上一选择。',
  'Candidate is incomplete or invalid': '候选运行未完成或参数无效。',
  'Public deployment is read-only': '公开部署只读，不能执行写入。',
  'Prepare the local dataset first': '本地数据集尚未准备。',
  'Operator token is not configured': '服务没有配置操作者令牌。',
  'Fields do not match the request contract': '请求字段不符合接口契约。',
  'Request too large': '请求超过大小上限。',
  'Record not found': '记录不存在或已被移除。',
}
const BY_STATUS = {
  401: '操作者认证失效，私有数据已从页面清除；请重新载入令牌。',
  403: '当前身份不能执行此操作。',
  404: '记录不存在或已被移除。',
  409: '服务器已有更新版本，本次写入未覆盖。',
  413: '请求超过大小上限。',
  422: '请求字段不符合接口契约。',
  429: '请求过于频繁或服务正忙；没有自动重试。',
  503: '服务暂不可用。',
}

export function messageFor(status, payload) {
  const raw = typeof payload?.error === 'string' ? payload.error : typeof payload?.detail === 'string' ? payload.detail : ''
  if (status === 401) return BY_STATUS[401]
  return KNOWN[raw] || BY_STATUS[status] || `请求未完成（HTTP ${status}）。`
}

export function createApiClient({ fetchImpl = (...args) => fetch(...args), getToken = () => '', onUnauthorized = () => {} } = {}) {
  let epoch = 0
  const stale = () => new DOMException('Identity changed', 'AbortError')
  async function request(path, options = {}) {
    const ticket = epoch
    const credential = getToken()
    const write = Boolean(options.method && options.method !== 'GET')
    let response
    try {
      response = await fetchImpl('/api' + path, {
        ...options,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', ...(credential ? { Authorization: 'Bearer ' + credential } : {}), ...options.headers },
      })
    } catch {
      if (ticket !== epoch) throw stale()
      throw write
        ? new ApiError('操作结果未知：请求可能已到达服务器。请重新读取核对，不要据此认定未提交。', { unknown: true })
        : new ApiError('连接失败，请稍后重新读取。')
    }
    let data
    try {
      data = await response.json()
    } catch {
      if (ticket !== epoch) throw stale()
      throw write
        ? new ApiError('响应未确认：写入可能已完成。请重新读取核对，不要据此认定未提交。', { status: response.status, unknown: true })
        : new ApiError('响应无法读取，请重新读取。', { status: response.status })
    }
    if (ticket !== epoch) throw stale()
    if (!response.ok) {
      if (response.status === 401 && credential) {
        epoch += 1
        onUnauthorized()
      }
      throw new ApiError(messageFor(response.status, data), { status: response.status, code: typeof data?.error === 'string' ? data.error : '' })
    }
    return data
  }
  return {
    request,
    invalidate() { epoch += 1 },
  }
}

export const isAbort = error => error?.name === 'AbortError'
