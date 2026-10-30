import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiError } from '../src/workbenchApi.js'
import { deferred, mountSfc, settle } from './sfcHarness.js'

const sample = id => ({ id, text: 'Authored fixture ' + id, label: 'ham', revision: 0, reviewed: false, history: [] })

async function reviewFixture(t, write) {
  const a = sample('a'), b = sample('b'), calls = []
  const mounted = await mountSfc('components/ReviewPanel.vue', {
    operator: true,
    api: async (path, options) => {
      calls.push({ path, options })
      if (options?.method === 'PATCH') return write.promise
      if (path === '/samples/a') return a
      if (path === '/samples/b') return b
      if (path === '/dataset') return { snapshots: [] }
      return { items: [a, b], total: 2, offset: 0 }
    },
  })
  t.after(mounted.unmount)
  await settle()
  await mounted.state.openSample('a')
  mounted.state.draft.value = 'spam'
  mounted.state.note.value = 'Authored reason for A'
  return { ...mounted, a, b, calls }
}

test('review keeps the saving sample selected and blocks a second record until the write is confirmed', async t => {
  const write = deferred()
  const { state, a, calls } = await reviewFixture(t, write)
  const saving = state.saveReview()
  await state.openSample('b')
  assert.equal(state.selected.value.id, 'a')
  assert.equal(state.save.value.state, 'saving')
  assert.equal(calls.filter(call => call.path === '/samples/b').length, 0)
  assert.equal(state.canSave.value, false)
  write.resolve({ sample: { ...a, label: 'spam', revision: 1, reviewed: true }, changed: true })
  await saving
  assert.equal(state.selected.value.label, 'spam')
  assert.equal(state.note.value, '')
  await state.openSample('b')
  assert.equal(state.selected.value.id, 'b')
})

test('a confirmed review cannot erase an edit made after that request was captured', async t => {
  const write = deferred()
  const { state, a } = await reviewFixture(t, write)
  const saving = state.saveReview()
  state.draft.value = 'ham'
  state.note.value = 'A newer unsent review draft'
  write.resolve({ sample: { ...a, label: 'spam', revision: 1, reviewed: true }, changed: true })
  await saving
  assert.equal(state.draft.value, 'ham')
  assert.equal(state.note.value, 'A newer unsent review draft')
})

async function importFixture(t, api) {
  const mounted = await mountSfc('components/ImportPanel.vue', { operator: false, api })
  t.after(mounted.unmount)
  mounted.state.source.value = 'Authored first batch'
  mounted.state.csv.value = 'text,label\nAuthored first,ham\n'
  mounted.state.preview.value = { preview_id: 'preview-first' }
  return mounted.state
}

test('late import confirmation preserves a newer CSV and source draft', async t => {
  const write = deferred()
  const state = await importFixture(t, async (path, options) => {
    if (options?.method === 'POST') return write.promise
    return path === '/dataset' ? { samples: 1 } : { total: 1 }
  })
  const confirming = state.confirm()
  state.source.value = 'Authored second batch'
  state.csv.value = 'text,label\nAuthored second,spam\n'
  state.changed()
  write.resolve({ created: true, count: 1 })
  await confirming
  assert.equal(state.source.value, 'Authored second batch')
  assert.equal(state.csv.value, 'text,label\nAuthored second,spam\n')
  assert.equal(state.outcome.value.kind, 'created')
})

test('unknown import keeps the original preview identity and blocks a fresh preview until reconciled', async t => {
  const write = deferred(), calls = []
  let checks = 0
  const state = await importFixture(t, async (path, options) => {
    calls.push({ path, options })
    if (path === '/imports/confirm') return ++checks === 1 ? write.promise : { created: false, count: 1 }
    if (path === '/imports/preview') return { preview_id: 'incorrect-new-preview' }
    return path === '/dataset' ? { samples: 1 } : { total: 1 }
  })
  const confirming = state.confirm()
  state.source.value = 'Authored second batch'
  state.csv.value = 'text,label\nAuthored second,spam\n'
  state.changed()
  write.reject(new ApiError('Synthetic lost confirmation', { unknown: true }))
  await confirming
  await state.runPreview()
  assert.equal(calls.filter(call => call.path === '/imports/preview').length, 0)
  assert.equal(state.outcome.value.previewId, 'preview-first')
  await state.confirm('a-different-preview')
  assert.equal(checks, 1)
  await state.confirm(state.outcome.value.previewId)
  assert.equal(JSON.parse(calls.filter(call => call.path === '/imports/confirm')[1].options.body).preview_id, 'preview-first')
  assert.equal(state.outcome.value.kind, 'existing')
  assert.equal(state.source.value, 'Authored second batch')
  assert.equal(state.csv.value, 'text,label\nAuthored second,spam\n')
})

test('an older login completion cannot clear the newer login identity', async t => {
  const originals = { fetch: globalThis.fetch, location: globalThis.location, window: globalThis.window }
  const requests = []
  globalThis.location = { hash: '#decide' }
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  globalThis.fetch = async (url, options) => {
    const credential = options.headers.Authorization
    const data = url.endsWith('/config') ? { public: false, operator_authenticated: Boolean(credential) }
      : url.endsWith('/runs') ? { runs: [] } : url.endsWith('/final-report') ? { report: null } : {}
    const response = { ok: true, status: 200, json: async () => data }
    if (!credential || url.endsWith('/runs') || url.endsWith('/final-report')) return response
    const gate = deferred()
    requests.push({ credential, resolve: () => gate.resolve(response) })
    return gate.promise
  }
  const mounted = await mountSfc('App.vue')
  t.after(() => {
    mounted.unmount()
    for (const [key, value] of Object.entries(originals)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  })
  await settle()
  const { state } = mounted
  state.tokenDraft.value = 'synthetic-A'
  const loginA = state.loadToken()
  state.tokenDraft.value = 'synthetic-B'
  const loginB = state.loadToken()
  for (const request of requests.filter(item => item.credential === 'Bearer synthetic-A')) request.resolve()
  await loginA
  assert.equal(state.token.value, 'synthetic-B')
  for (const request of requests.filter(item => item.credential === 'Bearer synthetic-B')) request.resolve()
  await loginB
  assert.equal(state.token.value, 'synthetic-B')
  assert.equal(state.operator.value, true)
  assert.equal(state.banner.value.tone, 'ok')
})
