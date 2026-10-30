import assert from 'node:assert/strict'
import test from 'node:test'
import { createDecisionSession, isSecureDecisionOrigin } from '../src/decisionSession.js'
const initial = () => ({ text: 'An authored normal meeting reminder.', engine: 'compare', credentialSource: 'byok', key: 'synthetic-key-marker', consent: true, busy: false, result: null, error: '' })
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }

test('BYOK goes to the fixed same-origin header, never the request body', async () => {
  const state = initial(); let count = 0
  const session = createDecisionSession(state, { secureTransport: () => true, fetchImpl: async (url, options) => {
    count++; assert.equal(url, '/api/decision'); assert.equal(options.headers['X-TypeSafe-Key'], 'synthetic-key-marker')
    assert.equal(options.headers.Authorization, undefined); assert.ok(!options.body.includes('synthetic-key-marker'))
    assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error')
    return { ok: true, json: async () => ({ disagreement: true }) }
  } })
  await session.submit(); assert.equal(count, 1); assert.equal(state.result.disagreement, true)
})

test('missing consent and missing BYOK produce zero requests', async () => {
  const state = initial(); let count = 0
  const session = createDecisionSession(state, { secureTransport: () => true, fetchImpl: () => { count++ } })
  state.consent = false; await session.submit(); state.consent = true; state.key = ''; await session.submit(); assert.equal(count, 0)
})

test('clear removes key and text, aborts, and discards a late successful response', async () => {
  const state = initial(), pending = deferred(); let signal
  const session = createDecisionSession(state, { secureTransport: () => true, fetchImpl: (_, options) => { signal = options.signal; return pending.promise } })
  const task = session.submit(); session.clear()
  assert.equal(state.key, ''); assert.equal(state.text, ''); assert.equal(state.consent, false); assert.equal(signal.aborted, true)
  pending.resolve({ ok: true, json: async () => ({ stale: true }) }); await task
  assert.equal(state.result, null); assert.equal(state.error, ''); assert.equal(state.busy, false)
})

test('editing input invalidates old response and cannot replace new result', async () => {
  const state = initial(), old = deferred(); let count = 0
  const session = createDecisionSession(state, { secureTransport: () => true, fetchImpl: () => ++count === 1 ? old.promise : Promise.resolve({ ok: true, json: async () => ({ current: true }) }) })
  const task = session.submit(); state.text = 'New authored reminder.'; session.invalidate(); await session.submit()
  old.resolve({ ok: true, json: async () => ({ old: true }) }); await task
  assert.deepEqual(state.result, { current: true }); assert.equal(count, 2)
})

test('unknown and rejected responses are not retried or copied into errors', async () => {
  for (const response of [Promise.reject(new Error('synthetic-key-marker')), Promise.resolve({ ok: false, status: 429, json: async () => ({ detail: 'synthetic-key-marker' }) }), Promise.resolve({ ok: true, json: async () => { throw new Error('synthetic-key-marker') } })]) {
    const state = initial(); let count = 0
    const session = createDecisionSession(state, { secureTransport: () => true, fetchImpl: () => { count++; return response } })
    await session.submit(); assert.equal(count, 1); assert.equal(state.result, null); assert.ok(!state.error.includes('synthetic-key-marker')); assert.ok(state.error.includes('自动重试'))
  }
})

test('explicit operator server credential never includes the BYOK value', async () => {
  const state = initial(); state.credentialSource = 'server'
  const session = createDecisionSession(state, { secureTransport: () => true, operatorToken: () => 'synthetic-operator', fetchImpl: async (_, options) => {
    assert.equal(options.headers['X-TypeSafe-Key'], undefined); assert.equal(options.headers.Authorization, 'Bearer synthetic-operator')
    return { ok: true, json: async () => ({}) }
  } }); await session.submit()
})

test('local requests do not send either credential or external consent', async () => {
  const state = initial(); state.engine = 'local'
  const session = createDecisionSession(state, { secureTransport: () => true, operatorToken: () => 'synthetic-operator', fetchImpl: async (_, options) => {
    assert.equal(options.headers['X-TypeSafe-Key'], undefined); assert.equal(options.headers.Authorization, undefined)
    assert.equal(JSON.parse(options.body).external_consent, false)
    return { ok: true, json: async () => ({}) }
  } }); await session.submit()
})


test('remote HTTP and public loopback cannot transmit a key even before backend validation', async () => {
  assert.equal(isSecureDecisionOrigin({ protocol: 'https:', hostname: 'example.test' }), true)
  assert.equal(isSecureDecisionOrigin({ protocol: 'http:', hostname: 'example.test' }, false), false)
  assert.equal(isSecureDecisionOrigin({ protocol: 'http:', hostname: '127.0.0.1' }, true), false)
  assert.equal(isSecureDecisionOrigin({ protocol: 'http:', hostname: '127.0.0.1' }, false), true)
  assert.equal(isSecureDecisionOrigin(undefined), false)
  const state = initial(); let count = 0
  const session = createDecisionSession(state, { secureTransport: () => false, fetchImpl: () => { count++ } })
  await session.submit(); assert.equal(count, 0); assert.ok(state.error.includes('HTTPS')); assert.equal(state.key, 'synthetic-key-marker')
})

test('stopping the wait keeps no late result and never claims the call was free', async () => {
  const state = initial(), pending = deferred(); let signal, count = 0
  const session = createDecisionSession(state, { secureTransport: () => true, fetchImpl: (_, options) => { count++; signal = options.signal; return pending.promise } })
  const task = session.submit(); assert.equal(state.busy, true)
  session.cancel()
  assert.equal(signal.aborted, true); assert.equal(state.busy, false); assert.match(state.error, /可能已处理并计费/)
  assert.equal(state.key, 'synthetic-key-marker'); assert.equal(state.text, 'An authored normal meeting reminder.')
  pending.resolve({ ok: true, json: async () => ({ late: true }) }); await task
  assert.equal(state.result, null); assert.equal(count, 1)
  session.cancel(); assert.match(state.error, /计费/)
})
