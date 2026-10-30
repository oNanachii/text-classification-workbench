import assert from 'node:assert/strict'
import test from 'node:test'
import { compareVerdict, engineMessage, engineOutcome, percent, submitBlockers } from '../src/decisionView.js'

const ok = label => ({ status: 'ok', label, probabilities: { ham: label === 'ham' ? .9 : .1, spam: label === 'ham' ? .1 : .9 } })
const failed = code => ({ status: 'error', error_code: code, outcome_unknown: false })
const unknown = code => ({ status: 'unknown', error_code: code, outcome_unknown: true })

test('only two successful engines form an agreement or disagreement', () => {
  assert.equal(compareVerdict({ engine: 'compare', local: ok('spam'), jev: ok('ham'), disagreement: true }), 'disagree')
  assert.equal(compareVerdict({ engine: 'compare', local: ok('ham'), jev: ok('ham'), disagreement: false }), 'agree')
})

test('failure or unknown on either side is never shown as a valid comparison', () => {
  // Even if a malformed payload carried disagreement=false, a failed side keeps the verdict incomplete.
  assert.equal(compareVerdict({ engine: 'compare', local: ok('ham'), jev: failed('provider_rate_limited'), disagreement: false }), 'incomplete')
  assert.equal(compareVerdict({ engine: 'compare', local: ok('ham'), jev: unknown('provider_timeout'), disagreement: null }), 'incomplete')
  assert.equal(compareVerdict({ engine: 'compare', local: failed('local_model_unavailable'), jev: ok('spam'), disagreement: null }), 'incomplete')
  assert.equal(compareVerdict({ engine: 'compare', local: ok('ham'), jev: ok('spam'), disagreement: null }), 'incomplete')
  assert.equal(compareVerdict({ engine: 'local', local: ok('ham'), jev: null, disagreement: null }), null)
  assert.equal(compareVerdict(null), null)
})

test('unknown outcomes are distinguished from definite errors', () => {
  assert.equal(engineOutcome(ok('ham')), 'ok')
  assert.equal(engineOutcome(failed('provider_authentication_failed')), 'error')
  assert.equal(engineOutcome(unknown('provider_connection_failed')), 'unknown')
  assert.equal(engineOutcome({ status: 'error', outcome_unknown: true }), 'unknown')
  assert.equal(engineOutcome(null), null)
})

test('engine errors use fixed copy, never provider text', () => {
  assert.match(engineMessage(unknown('provider_timeout')), /计费/)
  assert.equal(engineMessage({ status: 'error', error_code: 'synthetic-key-marker' }), '此引擎没有返回有效判断。')
  assert.ok(!engineMessage({ error_code: '<script>' }).includes('<script>'))
})

test('submit blockers mirror the session guards for each engine', () => {
  const ready = { enabled: true, secure: true, modelReady: true, serverAvailable: false }
  assert.deepEqual(submitBlockers({ text: '', engine: 'local' }, ready), ['输入一条英文短信'])
  assert.deepEqual(submitBlockers({ text: 'Meeting at noon.', engine: 'local' }, { ...ready, modelReady: false }), ['服务尚未加载本地模型'])
  assert.deepEqual(submitBlockers({ text: 'Meeting at noon.', engine: 'local' }, ready), [])
  assert.deepEqual(submitBlockers({ text: 'Meeting at noon.', engine: 'jev', key: '', consent: false }, ready), ['填写 TypeSafe Key', '勾选外发同意'])
  assert.deepEqual(submitBlockers({ text: 'Meeting at noon.', engine: 'compare', key: 'k', consent: true }, { ...ready, secure: false }), ['改用 HTTPS 连接'])
  assert.deepEqual(submitBlockers({ text: 'Meeting at noon.', engine: 'jev', key: 'k', consent: true }, { ...ready, enabled: false }), ['此服务未开启 Jev'])
  assert.deepEqual(submitBlockers({ text: 'Meeting at noon.', engine: 'jev', credentialSource: 'server', consent: true }, ready), ['服务器 Key 不可用'])
  assert.deepEqual(submitBlockers({ text: 'x'.repeat(2001), engine: 'local' }, ready), ['缩短到 2000 字符以内'])
  // Compare does not require a loaded local model; the local side reports its own failure.
  assert.deepEqual(submitBlockers({ text: 'Meeting at noon.', engine: 'compare', key: 'k', consent: true }, { ...ready, modelReady: false }), [])
})

test('percent formatting never invents a value', () => {
  assert.equal(percent(.125), '12.5%')
  assert.equal(percent(null), '—')
  assert.equal(percent(Number.NaN), '—')
})
