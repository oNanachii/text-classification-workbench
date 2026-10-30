import assert from 'node:assert/strict'
import test from 'node:test'
import { createApiClient, messageFor } from '../src/workbenchApi.js'

const reply = (status, payload) => ({ ok: status >= 200 && status < 300, status, json: async () => payload })
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

test('operator token travels only as the same-origin Authorization header', async () => {
  let seen
  const client = createApiClient({ getToken: () => 'synthetic-operator', fetchImpl: async (url, options) => { seen = { url, options }; return reply(200, { ok: true }) } })
  await client.request('/samples?limit=1')
  assert.equal(seen.url, '/api/samples?limit=1')
  assert.equal(seen.options.headers.Authorization, 'Bearer synthetic-operator')
  assert.equal(seen.options.credentials, 'same-origin')
  assert.equal(seen.options.cache, 'no-store')
  assert.ok(!seen.url.includes('synthetic-operator'))
})

test('a response that arrives after an identity change is discarded', async () => {
  const pending = deferred()
  const client = createApiClient({ fetchImpl: () => pending.promise })
  const work = client.request('/samples')
  client.invalidate()
  pending.resolve(reply(200, { items: [{ id: 'private-old' }] }))
  await assert.rejects(work, error => error.name === 'AbortError')
})

test('401 with a credential clears identity once and rejects later responses of the old identity', async () => {
  let cleared = 0
  const slow = deferred()
  let call = 0
  const client = createApiClient({ getToken: () => 'synthetic-operator', onUnauthorized: () => { cleared++ }, fetchImpl: () => ++call === 1 ? slow.promise : Promise.resolve(reply(401, { error: 'Operator authentication required' })) })
  const older = client.request('/samples')
  await assert.rejects(client.request('/dataset'), error => error.status === 401 && /认证失效/.test(error.message))
  assert.equal(cleared, 1)
  slow.resolve(reply(200, { items: [{ id: 'private' }] }))
  await assert.rejects(older, error => error.name === 'AbortError')
})

test('a write without confirmation is unknown; a read failure is not', async () => {
  const offline = createApiClient({ fetchImpl: () => Promise.reject(new TypeError('network')) })
  await assert.rejects(offline.request('/samples/x', { method: 'PATCH', body: '{}' }), error => error.unknown === true && /未知/.test(error.message))
  await assert.rejects(offline.request('/samples'), error => error.unknown === false)
  const garbled = createApiClient({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('html') } }) })
  await assert.rejects(garbled.request('/imports/confirm', { method: 'POST', body: '{}' }), error => error.unknown === true)
})

test('server errors map to fixed copy and never echo arbitrary payload text', async () => {
  assert.equal(messageFor(409, { error: 'A newer revision exists; reload before saving' }), '服务器已有更新修订，本次保存未覆盖。')
  assert.equal(messageFor(400, { error: 'Service selection revision changed' }), '服务选择已被更新，请刷新后再决定。')
  assert.equal(messageFor(403, { error: 'Public deployment is read-only' }), '公开部署只读，不能执行写入。')
  const client = createApiClient({ fetchImpl: async () => reply(500, { error: 'synthetic-key-marker private text' }) })
  await assert.rejects(client.request('/runs'), error => !error.message.includes('synthetic-key-marker') && error.status === 500)
})
