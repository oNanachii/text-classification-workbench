// First-visit guide state. Only the fact that this browser closed the guide is stored, never
// text, keys or tokens. When storage is missing or throws (private windows, blocked site data),
// the guide closes for the current page and shows again on the next visit.
export const FIRST_VISIT_KEY = 'textModelLab.firstVisitGuide'

export function browserStorage() {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

export function guideClosed(storage) {
  try {
    return storage?.getItem(FIRST_VISIT_KEY) === 'closed'
  } catch {
    return false
  }
}

export function rememberGuideClosed(storage) {
  if (!storage) return false
  try {
    storage.setItem(FIRST_VISIT_KEY, 'closed')
    return true
  } catch {
    return false
  }
}
