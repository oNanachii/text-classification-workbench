export class ApiError extends Error {
  constructor(message, { status = null, uncertain = false } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.uncertain = uncertain
  }
}

const fieldNames = {
  text: '短信文本', label: '标签', note: '纠错说明', threshold: '分类阈值',
  model: '模型', q: '搜索内容', limit: '每页条数', offset: '分页位置',
}

function errorDetail(detail) {
  if (Array.isArray(detail)) {
    const fields = [...new Set(detail.map(item => fieldNames[item.loc?.at(-1)]).filter(Boolean))]
    return fields.length ? `请检查${fields.join('、')}的格式和取值。` : '提交的数据不符合要求，请检查后重试。'
  }
  const known = {
    'Sample not found': '找不到这条样本，请刷新列表。',
    'Prediction failed': '预测暂时失败，请稍后重试。',
    'Service not ready': '模型正在准备中，请稍后重试。',
    'Storage operation failed': '数据操作失败，请重新读取记录核对。',
  }
  return typeof detail === 'string' ? known[detail] : null
}

// All requests go to the same backend. A rejected HTTP response is never success.
export async function request(path, { method = 'GET', body, signal } = {}) {
  const mutating = ['PATCH', 'PUT', 'DELETE'].includes(method)
  let response
  try {
    response = await fetch(path, {
      method,
      signal,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (error) {
    if (error.name === 'AbortError') throw error
    throw new ApiError(mutating
      ? '连接中断，保存结果尚未确认。请重新读取记录核对后再操作。'
      : '暂时无法连接服务，请检查连接后重试。', { uncertain: mutating })
  }

  let payload
  try {
    payload = await response.json()
  } catch {
    throw new ApiError(mutating
      ? '未收到可确认的保存结果，请重新读取记录核对。'
      : '服务返回了无法读取的数据，请重试。', { status: response.status, uncertain: mutating })
  }
  if (!response.ok) {
    const fallback = response.status === 422
      ? '提交的数据不符合要求，请检查后重试。'
      : `请求未完成（${response.status}），请重试。`
    throw new ApiError(errorDetail(payload.detail) || fallback, { status: response.status })
  }
  return payload
}

export function labelName(label) {
  return label === 'spam' ? '垃圾短信' : label === 'ham' ? '正常短信' : '—'
}

export function percentage(value, digits = 1) {
  return Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : '—'
}

export function number(value) {
  return Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—'
}

export function dateTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}
