import assert from 'node:assert/strict'
import test from 'node:test'
import { FIRST_VISIT_KEY, browserStorage, guideClosed, rememberGuideClosed } from '../src/firstVisit.js'
import { mountSfc } from './sfcHarness.js'

// Storage doubles only: no browser, server or project data.
const memoryStorage = () => {
  const map = new Map()
  return { map, getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, String(value)) }
}
const blocked = { getItem() { throw new Error('SecurityError') }, setItem() { throw new Error('QuotaExceededError') } }

function stubLocalStorage(t, descriptor) {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, ...descriptor })
  t.after(() => original ? Object.defineProperty(globalThis, 'localStorage', original) : delete globalThis.localStorage)
}

test('the guide shows until this browser closes it, and only that choice is stored', () => {
  const storage = memoryStorage()
  assert.equal(guideClosed(storage), false)
  assert.equal(rememberGuideClosed(storage), true)
  assert.equal(guideClosed(storage), true)
  assert.deepEqual([...storage.map], [[FIRST_VISIT_KEY, 'closed']])
})

test('blocked or missing storage never throws and keeps showing the guide', t => {
  assert.equal(guideClosed(blocked), false)
  assert.equal(rememberGuideClosed(blocked), false)
  assert.equal(guideClosed(null), false)
  assert.equal(rememberGuideClosed(null), false)
  stubLocalStorage(t, { get() { throw new Error('SecurityError') } })
  assert.equal(browserStorage(), null)
})

test('the guide component reads the stored choice and remembers closing', async t => {
  const storage = memoryStorage()
  stubLocalStorage(t, { value: storage })
  let closed = 0
  const first = await mountSfc('components/FirstVisitGuide.vue', { sections: [], onClose: () => { closed += 1 } })
  t.after(first.unmount)
  assert.equal(first.state.open.value, true)
  first.state.close()
  assert.equal(first.state.open.value, false)
  assert.equal(closed, 1)
  assert.equal(storage.getItem(FIRST_VISIT_KEY), 'closed')
  const again = await mountSfc('components/FirstVisitGuide.vue', { sections: [] })
  t.after(again.unmount)
  assert.equal(again.state.open.value, false)
})

test('the guide still closes for this page when the browser refuses to store it', async t => {
  stubLocalStorage(t, { value: blocked })
  const mounted = await mountSfc('components/FirstVisitGuide.vue', { sections: [] })
  t.after(mounted.unmount)
  assert.equal(mounted.state.open.value, true)
  mounted.state.close()
  assert.equal(mounted.state.open.value, false)
})
