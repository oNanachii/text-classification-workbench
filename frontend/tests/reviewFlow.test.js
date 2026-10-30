import assert from 'node:assert/strict'
import test from 'node:test'
import { reconcileUnknownSave, reviewIntent, reviewStatus } from '../src/reviewFlow.js'

const pending = { id: 'a', label: 'ham', reviewed: 0, revision: 0, source: 'authored fixture' }
const reviewed = { id: 'b', label: 'spam', reviewed: 1, revision: 1, source: 'authored fixture' }

test('same-label approval is a confirmation, never a correction', () => {
  assert.deepEqual(reviewIntent(pending, 'ham', ''), { kind: 'confirm', needsNote: true, ready: false })
  assert.deepEqual(reviewIntent(pending, 'ham', 'literal reminder'), { kind: 'confirm', needsNote: true, ready: true })
})

test('a label change is a correction and requires a reason', () => {
  assert.deepEqual(reviewIntent(pending, 'spam', '  '), { kind: 'correct', needsNote: true, ready: false })
  assert.deepEqual(reviewIntent(reviewed, 'ham', 'not promotional'), { kind: 'correct', needsNote: true, ready: true })
})

test('an already reviewed sample with an unchanged label has nothing to save', () => {
  assert.equal(reviewIntent(reviewed, 'spam', 'any note').kind, 'noop')
  assert.equal(reviewIntent(reviewed, 'spam', 'any note').ready, false)
  assert.equal(reviewIntent(null, 'ham', '').ready, false)
  assert.equal(reviewIntent(pending, 'unknown', 'note').ready, false)
})

test('unknown save is reconciled from a fresh server read, not assumed', () => {
  const attempt = { revision: 0, label: 'spam' }
  assert.equal(reconcileUnknownSave(attempt, { revision: 0, label: 'ham', reviewed: 0 }), 'not_saved')
  assert.equal(reconcileUnknownSave(attempt, { revision: 1, label: 'spam', reviewed: 1 }), 'matches')
  assert.equal(reconcileUnknownSave(attempt, { revision: 1, label: 'ham', reviewed: 1 }), 'superseded')
  assert.equal(reconcileUnknownSave(attempt, { revision: 2, label: 'spam', reviewed: 0 }), 'superseded')
  assert.equal(reconcileUnknownSave(null, { revision: 1 }), 'superseded')
})

test('review status keeps original corpus labels apart from human review', () => {
  assert.equal(reviewStatus({ source: 'uci:fixture', revision: 0, reviewed: 1 }), 'original')
  assert.equal(reviewStatus({ source: 'uci:fixture', revision: 1, reviewed: 1 }), 'reviewed')
  assert.equal(reviewStatus(pending), 'pending')
})
