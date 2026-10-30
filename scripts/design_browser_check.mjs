// Browser check of the production build against scripts/design_demo.py servers. SIMULATED DATA ONLY.
// Drives a local Chromium over the DevTools protocol (Node >= 22 global WebSocket); adds no dependencies.
// Usage: CHROME=<chromium binary> node scripts/design_browser_check.mjs --state=<demo private state dir> --profile=<scratch dir> [--out=artifacts/...]
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const args = Object.fromEntries(process.argv.slice(2).map(arg => arg.replace(/^--/, '').split(/=(.*)/s).slice(0, 2)))
const CHROME = process.env.CHROME
const OUT = resolve(args.out || 'artifacts/v2-verification/private/design-browser-check')
const STATE = args.state
const PROFILE = args.profile
const PRIVATE = 'http://127.0.0.1:8781', PUBLIC = 'http://127.0.0.1:8782', EMPTY = 'http://127.0.0.1:8783'
const TOKEN = 'demo-operator-token-not-secret'
const SAMPLE_CSV = [
  'text,label',
  'Could you pick up the blue folder from reception before the team meeting?,ham',
  'Claim your exclusive cash voucher today by replying YES to this number.,spam',
  'Lunch moved to the second floor kitchen at noon.,ham',
  'Lunch moved to the second floor kitchen at noon.,ham',
  'Parking pass renewal is due on Friday morning.,ham',
  'PARKING PASS RENEWAL IS DUE ON FRIDAY MORNING.,spam',
  'secret heldout exact content,ham',
  'This row has an unsupported label,maybe',
].join('\n') + '\n'
if (!CHROME || !STATE || !PROFILE) throw new Error('CHROME, --state and --profile are required')
mkdirSync(OUT, { recursive: true })

const checks = [], shots = []
const fail = message => { throw new Error(message) }
const ok = (condition, message) => { if (!condition) fail(message) }
async function check(id, fn) {
  try { const note = await fn(); checks.push({ id, pass: true, note: note ?? '' }); console.log('PASS', id, note ?? '') } catch (error) {
    checks.push({ id, pass: false, note: String(error.message || error) }); console.log('FAIL', id, error.message)
  }
}
const faults = value => writeFileSync(join(STATE, 'demo-faults.json'), JSON.stringify(value))
async function operatorApi(path, options = {}) {
  const response = await fetch(PRIVATE + '/api' + path, { ...options, headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN, ...options.headers } })
  return { status: response.status, body: await response.json().catch(() => null) }
}

class Page {
  constructor(ws) {
    this.ws = ws; this.seq = 0; this.calls = new Map(); this.listeners = new Set(); this.logs = []; this.requests = []
    ws.addEventListener('message', event => {
      const message = JSON.parse(event.data)
      if (message.id) {
        const call = this.calls.get(message.id); if (!call) return
        this.calls.delete(message.id); message.error ? call.reject(new Error(message.error.message)) : call.resolve(message.result)
      } else for (const listener of this.listeners) listener(message)
    })
  }
  send(method, params = {}) { const id = ++this.seq; this.ws.send(JSON.stringify({ id, method, params })); return new Promise((resolve, reject) => this.calls.set(id, { resolve, reject })) }
  once(method, timeout = 15000) {
    return new Promise((resolve, reject) => {
      const listener = message => { if (message.method === method) { clearTimeout(timer); this.listeners.delete(listener); resolve(message.params) } }
      const timer = setTimeout(() => { this.listeners.delete(listener); reject(new Error('timeout ' + method)) }, timeout)
      this.listeners.add(listener)
    })
  }
  async init() {
    for (const domain of ['Page', 'Runtime', 'Log', 'Network']) await this.send(domain + '.enable')
    await this.send('Emulation.setFocusEmulationEnabled', { enabled: true })
    await this.send('Page.addScriptToEvaluateOnNewDocument', { source: "window.__csp=[];document.addEventListener('securitypolicyviolation',e=>window.__csp.push(e.violatedDirective+' '+e.blockedURI))" })
    this.listeners.add(({ method, params }) => {
      if (method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(params.type)) this.logs.push({ kind: 'console.' + params.type, text: params.args.map(arg => arg.value ?? arg.description).join(' ').slice(0, 300) })
      if (method === 'Runtime.exceptionThrown') this.logs.push({ kind: 'exception', text: (params.exceptionDetails.exception?.description || params.exceptionDetails.text).slice(0, 300) })
      if (method === 'Log.entryAdded') this.logs.push({ kind: `log.${params.entry.source}.${params.entry.level}`, text: params.entry.text.slice(0, 300), url: params.entry.url })
      if (method === 'Network.requestWillBeSent') {
        const headers = Object.fromEntries(Object.entries(params.request.headers).map(([k, v]) => [k.toLowerCase(), v]))
        this.requests.push({ url: params.request.url, method: params.request.method, keyHeader: 'x-typesafe-key' in headers, authHeader: 'authorization' in headers, body: params.request.postData || '' })
      }
    })
  }
  async eval(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error('eval: ' + (result.exceptionDetails.exception?.description || result.exceptionDetails.text))
    return result.result.value
  }
  async waitFor(expression, timeout = 8000) {
    const end = Date.now() + timeout; let last
    while (Date.now() < end) { try { last = await this.eval(expression); if (last) return last } catch (error) { last = error.message } await sleep(100) }
    fail('waitFor timeout: ' + expression.slice(0, 160))
  }
  async goto(url) {
    // A query counter forces a full document load even when only the hash would differ.
    this.loads = (this.loads || 0) + 1
    const [base, hash = ''] = url.split('#')
    const loaded = this.once('Page.loadEventFired')
    await this.send('Page.navigate', { url: `${base}?load=${this.loads}${hash ? '#' + hash : ''}` }); await loaded
    await this.waitFor("document.querySelector('#panel-title') && !document.querySelector('.spinner-lg')")
    await sleep(150)
  }
  async hash(id) { await this.eval(`location.hash=${JSON.stringify(id)}`); await sleep(250) }
  async point(selector, text) {
    return this.eval(`(() => { const all = [...document.querySelectorAll(${JSON.stringify(selector)})]; const el = ${text ? `all.find(e => e.textContent.includes(${JSON.stringify(text)}))` : 'all[0]'}; if (!el) return null; el.scrollIntoView({ block: 'center', inline: 'nearest' }); const r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2, disabled: Boolean(el.disabled) } })()`)
  }
  async click(selector, text) {
    const at = await this.point(selector, text); if (!at) fail(`missing ${selector} ${text || ''}`)
    for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await this.send('Input.dispatchMouseEvent', { type, x: at.x, y: at.y, button: 'left', clickCount: 1 })
    await sleep(120); return at
  }
  async fill(selector, value) {
    await this.eval(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); el.focus(); el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('input', { bubbles: true })) })()`); await sleep(80)
  }
  async select(selector, value) {
    await this.eval(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('change', { bubbles: true })) })()`); await sleep(80)
  }
  async key(name, modifiers = 0) {
    const map = { Tab: [9, 'Tab'], Enter: [13, 'Enter', '\r'], Space: [32, 'Space', ' '], ArrowRight: [39, 'ArrowRight'], ArrowDown: [40, 'ArrowDown'], Escape: [27, 'Escape'] }
    const [code, codeName, text] = map[name]; const key = name === 'Space' ? ' ' : name
    await this.send('Input.dispatchKeyEvent', { type: text ? 'keyDown' : 'rawKeyDown', key, code: codeName, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, modifiers, text })
    await this.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code: codeName, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, modifiers })
    await sleep(70)
  }
  async type(text) { await this.send('Input.insertText', { text }); await sleep(80) }
  async focused() {
    return this.eval(`(() => { const e = document.activeElement; if (!e) return null; const host = e.closest('.engine-option, .choice, .file-drop, .consent') || e; const ring = [e, host].some(n => getComputedStyle(n).boxShadow !== 'none'); return { tag: e.tagName, id: e.id, name: e.name || '', value: e.value ?? '', type: e.type || '', text: (e.textContent || '').trim().slice(0, 40), ring } })()`)
  }
  async tabTo(predicate, max = 45) {
    for (let index = 0; index < max; index++) { await this.key('Tab'); if (await this.eval(`(e => Boolean(${predicate}))(document.activeElement)`)) return index + 1 }
    fail('Tab order never reached: ' + predicate)
  }
  async viewport(width, height, mobile = false) { this.view = { width, height, deviceScaleFactor: mobile ? 2 : 1, mobile }; await this.send('Emulation.setDeviceMetricsOverride', this.view); await sleep(250) }
  async overflow() { return this.eval('document.documentElement.scrollWidth - document.documentElement.clientWidth') }
  async shot(name, full = false) {
    // Full captures grow the viewport to the content height so sticky nav/results are drawn where a reader sees them.
    const view = this.view
    if (full && view) {
      const height = Math.min(5200, await this.eval('document.documentElement.scrollHeight'))
      await this.send('Emulation.setDeviceMetricsOverride', { ...view, height: Math.max(view.height, height) }); await sleep(250)
    }
    const { data } = await this.send('Page.captureScreenshot', { format: 'png' })
    if (full && view) { await this.send('Emulation.setDeviceMetricsOverride', view); await sleep(150) }
    writeFileSync(join(OUT, name + '.png'), Buffer.from(data, 'base64')); shots.push(name + '.png')
  }
  text(selector) { return this.eval(`document.querySelector(${JSON.stringify(selector)})?.textContent?.trim() ?? null`) }
}

const decisionTimes = []
async function pace() {
  // Backend limit: 6 decision requests per minute per source. Wait instead of tripping it unintentionally.
  const now = Date.now(); while (decisionTimes.length && decisionTimes[0] < now - 62000) decisionTimes.shift()
  if (decisionTimes.length >= 6) { const wait = decisionTimes[0] + 62000 - now; console.log(`… pacing ${Math.ceil(wait / 1000)} s for the per-source rate limit`); await sleep(wait); decisionTimes.shift() }
  decisionTimes.push(Date.now())
}
const results = page => page.eval(`(() => ({ loading: Boolean(document.querySelector('.engine-card.is-loading')), cards: [...document.querySelectorAll('.results .engine-card:not(.is-loading)')].map(c => ({ cls: c.className, label: (c.querySelector('.verdict-label')?.textContent || '').trim().match(/^(ham|spam)/)?.[1] || null, tag: c.querySelector('.tag')?.textContent?.trim(), text: c.textContent })), verdict: document.querySelector('.verdict')?.className || null, error: document.querySelector('.results .callout-error')?.textContent || null, notice: document.querySelector('.results .callout-info')?.textContent || null, empty: Boolean(document.querySelector('.results .empty')), text: document.querySelector('#sms-text')?.value ?? null, key: document.querySelector('#typesafe-key')?.value ?? null, consent: document.querySelector('.consent input')?.checked ?? null, blockers: document.querySelector('#submit-blockers')?.textContent || '' }))()`)
async function setup(page, { engine = 'local', text, key, consent = true }) {
  await page.click(`input[name="engine"][value="${engine}"]`)
  if (text !== undefined) await page.fill('#sms-text', text)
  if (engine !== 'local') {
    if (key !== undefined) await page.fill('#typesafe-key', key)
    if (consent && !(await page.eval("document.querySelector('.consent input').checked"))) await page.click('.consent input')
  }
}
async function submit(page, { paced = true } = {}) { if (paced) await pace(); await page.click('.composer button.btn-primary') }
async function reloadPending(page) {
  await page.click('.segmented button', '全部'); await page.waitFor("document.querySelector('.list-meta').textContent.startsWith('全部')")
  await page.click('.segmented button', '待审'); await page.waitFor("document.querySelector('.list-meta').textContent.startsWith('待审') && document.querySelectorAll('.sample-row').length > 0")
}
const settled = page => page.waitFor("!document.querySelector('.engine-card.is-loading') && (document.querySelector('.results .engine-card') || document.querySelector('.results .callout-error'))", 12000)

async function main() {
  const chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${PROFILE}`, '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-background-networking', '--disable-component-update', '--disable-sync', '--password-store=basic', '--use-mock-keychain', '--hide-scrollbars', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] })
  const endpoint = await new Promise((resolve, reject) => {
    let buffer = ''
    chrome.stderr.on('data', chunk => { buffer += chunk; const match = buffer.match(/DevTools listening on (ws:\/\/\S+)/); if (match) resolve(match[1]) })
    setTimeout(() => reject(new Error('Chromium did not start')), 20000)
  })
  const targets = await (await fetch(`http://127.0.0.1:${new URL(endpoint).port}/json/list`)).json()
  const ws = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl)
  await new Promise(resolve => ws.addEventListener('open', resolve))
  const page = new Page(ws); await page.init()
  const version = (await page.send('Browser.getVersion')).product
  try { await run(page) } finally {
    const csp = await page.eval('window.__csp || []').catch(() => [])
    const external = page.requests.filter(r => !/^(http:\/\/127\.0\.0\.1:878[123]\/|data:|about:)/.test(r.url))
    const decisions = page.requests.filter(r => r.url.endsWith('/api/decision'))
    const report = {
      title: 'Workbench design browser check — SIMULATED DATA ONLY',
      date: new Date().toISOString(), browser: version + ' (headless, isolated temporary profile)',
      data: 'Hand-authored English SMS fixtures in a scratch directory; fake operator token; in-process httpx.MockTransport instead of TypeSafe; no var/, .env, real SMS, paid request or 757-row test.',
      servers: { private: PRIVATE + ' (non-public, Jev on, fake server key)', public: PUBLIC + ' (public export bundle, BYOK only)', empty: EMPTY + ' (no model selected, Jev off)' },
      viewports: ['1280x800 desktop', '390x844 mobile (DPR 2)'],
      checks, passed: checks.filter(c => c.pass).length, failed: checks.filter(c => !c.pass).length,
      network: {
        external_requests: external.map(r => r.url),
        decision_requests: decisions.length,
        local_requests_with_key_header: decisions.filter(r => JSON.parse(r.body || '{}').engine === 'local' && r.keyHeader).length,
        key_in_url_or_body: page.requests.filter(r => /demo-(ok|flip|401|429|500|bad|nousage|slow|hang)/.test(r.url + r.body)).length,
      },
      csp_violations: csp,
      console: page.logs,
      screenshots: shots,
      not_covered: [
        'Public BYOK success over real HTTPS (demo serves loopback HTTP; covered by unit tests only)',
        'Real TypeSafe/Jev responses, latency, cost or quality (mock transport only)',
        'Safari/Firefox, physical devices, screen readers, human visual sign-off',
      ],
    }
    writeFileSync(join(OUT, 'browser-check.json'), JSON.stringify(report, null, 2) + '\n')
    console.log(`\n${report.passed} passed, ${report.failed} failed; CSP violations ${csp.length}; external requests ${external.length}`)
    ws.close(); chrome.kill()
  }
}

async function run(page) {
  faults({})
  await page.viewport(1280, 800)

  // ---------- Decision: empty, keyboard flow, local, compare, failures ----------
  await page.goto(PRIVATE + '/workbench/')
  await check('decide.empty_state', async () => {
    const r = await results(page)
    ok(r.empty && r.blockers.includes('输入一条英文短信'), 'empty state or blocker missing')
    ok(await page.eval("document.querySelector('.composer button.btn-primary').disabled"), 'submit should be disabled')
    await page.shot('01-decide-empty-desktop')
    return r.blockers
  })
  await check('decide.missing_key_blocker', async () => {
    await page.click('input[name="engine"][value="jev"]')
    const r = await results(page); ok(r.blockers.includes('填写 TypeSafe Key') && r.blockers.includes('勾选外发同意'), r.blockers)
    ok(await page.eval("document.querySelector('.transport').classList.contains('is-ok')"), 'loopback non-public should be allowed')
    await page.shot('02-decide-jev-needs-key-consent')
    await page.click('input[name="engine"][value="local"]')
    return r.blockers
  })
  await check('keyboard.decide_flow', async () => {
    await page.goto(PRIVATE + '/workbench/#decide')
    await page.eval('document.activeElement?.blur(); window.scrollTo(0,0)')
    await page.key('Tab'); const skip = await page.focused(); ok(skip.text.includes('跳到主要内容'), 'first Tab should reach skip link, got ' + skip.text)
    const steps = await page.tabTo("e.name === 'engine'"); let f = await page.focused(); ok(f.ring, 'engine radio focus ring missing')
    await page.key('ArrowRight'); ok((await page.focused()).value === 'jev', 'arrow should select jev')
    await page.tabTo("e.id === 'sms-text'"); await page.type('Please bring the blue notebook to our meeting tomorrow.')
    await page.tabTo("e.id === 'typesafe-key'"); await page.type('demo-ok'); f = await page.focused(); ok(f.ring, 'key input focus ring missing')
    await page.tabTo("e.type === 'checkbox'"); await page.key('Space')
    ok(await page.eval("document.querySelector('.consent input').checked"), 'space should check consent')
    await page.tabTo("e.classList.contains('btn-primary')"); ok(!(await page.eval('document.activeElement.disabled')), 'submit should be enabled')
    await pace(); await page.key('Enter'); await settled(page)
    const r = await results(page); ok(r.cards.length === 1 && r.cards[0].label === 'ham', 'expected Jev ham via keyboard, got ' + JSON.stringify(r.cards.map(c => c.label)))
    await page.tabTo("e.textContent.includes('清除 Key')"); await page.key('Enter')
    const cleared = await results(page); ok(!cleared.text && !cleared.key && cleared.consent === false && !cleared.cards.length, 'clear should drop text, key, consent, result')
    return `skip link first; engine radio after ${steps} Tabs; arrow/space/enter flow; clear by keyboard`
  })
  await check('decide.local_success', async () => {
    await page.click('input[name="engine"][value="local"]'); await page.click('.chip', '会议提醒'); await submit(page); await settled(page)
    const r = await results(page); ok(r.cards.length === 1 && r.cards[0].label === 'ham' && r.cards[0].text.includes('未校准'), 'local ham with uncalibrated note expected')
    ok(r.cards[0].text.includes('阈值 0.5'), 'threshold missing'); await page.shot('03-decide-local-ham', true)
    return 'ham · spam_score bar with threshold marker'
  })
  await check('decide.compare_agree', async () => {
    await setup(page, { engine: 'compare', key: 'demo-ok' }); await page.click('.chip', '奖金诱导'); await submit(page); await settled(page)
    const r = await results(page); ok(r.verdict?.includes('verdict-agree') && r.cards.length === 2, 'agree verdict expected')
    ok(r.cards[1].text.includes('不是正确率') && r.cards[1].text.includes('分布集中度'), 'confidence wording'); await page.shot('04-decide-compare-agree', true)
    return '两边标签一致 · 一致不代表正确'
  })
  await check('decide.compare_disagree', async () => {
    await page.fill('#typesafe-key', 'demo-flip'); await submit(page); await settled(page)
    const r = await results(page); ok(r.verdict?.includes('verdict-disagree'), 'disagree verdict expected'); await page.shot('05-decide-compare-disagree', true)
    return r.cards.map(c => c.label).join(' vs ')
  })
  await check('decide.jev_auth_failure', async () => {
    await page.click('input[name="engine"][value="jev"]'); await page.fill('#typesafe-key', 'demo-401'); await submit(page); await settled(page)
    const r = await results(page); ok(r.cards[0].cls.includes('is-error') && r.cards[0].text.includes('拒绝了这个 Key') && r.cards[0].text.includes('没有自动重试'), 'auth failure copy')
    return r.cards[0].tag
  })
  await check('decide.compare_one_side_unknown', async () => {
    await page.click('input[name="engine"][value="compare"]'); await page.fill('#typesafe-key', 'demo-500'); await submit(page); await settled(page)
    const r = await results(page); ok(r.verdict?.includes('verdict-incomplete'), 'incomplete verdict expected')
    ok(r.cards[0].cls.includes('is-ok') && r.cards[1].cls.includes('is-unknown') && r.cards[1].text.includes('计费'), 'local kept, Jev unknown with billing note')
    await page.shot('06-decide-compare-jev-unknown', true)
    return 'local ok kept; Jev 结果未知; 未形成有效对照'
  })
  await check('decide.invalid_provider_response', async () => {
    await page.fill('#typesafe-key', 'demo-bad'); await submit(page); await settled(page)
    const r = await results(page); ok(r.cards[1].cls.includes('is-unknown') && r.cards[1].text.includes('不符合契约'), 'invalid response copy')
    return r.cards[1].tag
  })
  await check('decide.backend_429_no_retry', async () => {
    // Submit local requests back to back until the backend's per-source limit answers 429; each click must send exactly one request.
    await page.click('input[name="engine"][value="local"]')
    for (let attempt = 1; attempt <= 8; attempt++) {
      const before = page.requests.filter(r => r.url.endsWith('/api/decision')).length
      await submit(page, { paced: false }); await settled(page); decisionTimes.push(Date.now())
      const sent = page.requests.filter(r => r.url.endsWith('/api/decision')).length - before
      ok(sent === 1, `attempt ${attempt} sent ${sent} requests`)
      const r = await results(page)
      if (r.error) { ok(r.error.includes('没有自动重试'), r.error); await page.shot('07-decide-rate-limited'); return `429 after ${attempt} rapid submits; one request each; ${r.error}` }
    }
    fail('no 429 within 8 rapid submits')
  })

  // ---------- Late responses must not come back ----------
  console.log('… waiting 62 s for the rate window before the slow-response checks'); await sleep(62000); decisionTimes.length = 0
  async function slowThen(label, act, verify) {
    await check(label, async () => {
      await page.goto(PRIVATE + '/workbench/#decide')
      await setup(page, { engine: 'jev', text: 'Your parcel is ready for pickup at the front desk.', key: 'demo-slow' }); await submit(page)
      await page.waitFor("document.querySelector('.engine-card.is-loading')", 3000)
      if (label === 'late.loading_state') await page.shot('08-decide-loading')
      await act(); await sleep(6200)
      const r = await results(page); ok(!r.cards.length, 'late result was rendered: ' + JSON.stringify(r.cards.map(c => c.label)))
      return verify ? verify(r) : 'no late result'
    })
    await sleep(1200)
  }
  await slowThen('late.clear_button', () => page.click('.composer .btn-ghost'), r => { ok(!r.text && !r.key, 'text/key should be cleared'); return 'cleared; no late result; ' + (r.notice || '').slice(0, 40) })
  await slowThen('late.edit_text', () => page.fill('#sms-text', 'Your parcel is ready for pickup at the front desk!'), r => { ok(r.notice?.includes('迟到的结果不会显示'), 'invalidation notice'); return r.notice })
  await slowThen('late.change_key', () => page.fill('#typesafe-key', 'demo-ok'))
  await slowThen('late.change_engine', () => page.click('input[name="engine"][value="local"]'))
  await slowThen('late.stop_waiting', () => page.click('.composer .btn-outline', '停止等待'), r => { ok(r.error?.includes('可能已处理并计费'), 'cancel copy'); return r.error })
  await slowThen('late.toggle_consent', () => page.click('.consent input'))
  console.log('… waiting 62 s for the rate window'); await sleep(62000); decisionTimes.length = 0
  await slowThen('late.leave_panel', async () => { await page.click('.nav-item[href="#model"]'); await sleep(300); await page.click('.nav-item[href="#decide"]') }, r => { ok(!r.text && !r.key, 'returning should not restore text or key'); return 'text/key/result gone after leaving' })
  await slowThen('late.identity_change', async () => {
    await page.click('.topbar-actions button'); await page.fill('#token-input', TOKEN); await page.key('Enter')
    await page.waitFor("document.querySelector('.topbar-actions .btn-ok')", 5000)
  }, r => { ok(!r.text, 'identity change should clear the draft'); return 'operator loaded mid-request; panel reset' })
  await slowThen('late.loading_state', () => page.click('.composer .btn-ghost'))
  await check('decide.upstream_timeout_unknown', async () => {
    await page.goto(PRIVATE + '/workbench/#decide')
    await setup(page, { engine: 'jev', text: 'Your parcel is ready for pickup at the front desk.', key: 'demo-hang' }); await submit(page); await settled(page)
    const r = await results(page); ok(r.cards[0].cls.includes('is-unknown') && r.cards[0].text.includes('超时'), 'timeout unknown copy'); return r.cards[0].tag
  })
  await check('decide.usage_not_returned', async () => {
    await page.fill('#typesafe-key', 'demo-nousage'); await submit(page); await settled(page)
    const r = await results(page); ok(r.cards[0].text.includes('服务未返回（不代表零消耗）'), 'usage null copy'); return 'usage=null shown as 未返回'
  })
  await check('storage.no_credentials_or_sms', async () => {
    const storage = await page.eval("(async () => ({ local: localStorage.length, session: sessionStorage.length, cookie: document.cookie, idb: (await indexedDB.databases()).length, caches: (await caches.keys()).length }))()")
    ok(!storage.local && !storage.session && !storage.cookie && !storage.idb && !storage.caches, JSON.stringify(storage)); return JSON.stringify(storage)
  })

  // ---------- Auth panel and identity ----------
  await check('auth.wrong_token_rejected', async () => {
    await page.goto(PRIVATE + '/workbench/#decide')
    await page.click('.topbar-actions button'); await page.fill('#token-input', 'not-the-demo-token'); await page.key('Enter')
    await page.waitFor("document.querySelector('.banner.callout-error')", 5000)
    ok((await page.text('.banner')).includes('没有通过认证'), 'rejection banner'); ok((await page.focused()).id === 'token-input', 'focus should return to token input')
    await page.key('Escape'); ok((await page.focused()).text.includes('操作者登录'), 'Escape should return focus to the auth button')
    await page.shot('09-auth-token-rejected'); return 'banner + focus management'
  })
  await check('auth.operator_login', async () => {
    await page.click('.topbar-actions button'); await page.fill('#token-input', TOKEN); await page.key('Enter')
    await page.waitFor("document.querySelector('.topbar-actions .btn-ok')", 5000)
    ok(!(await page.eval("document.querySelector('.nav-item[href=\"#review\"] .nav-lock')")), 'review nav should unlock'); return 'unlocked'
  })

  // ---------- Import ----------
  await check('import.preview_counts', async () => {
    await page.click('.nav-item[href="#import"]'); await page.waitFor("document.querySelector('#import-source')")
    ok((await page.focused()).id === 'panel-title', 'nav should move focus to the panel heading')
    await page.fill('#import-source', 'SIMULATED authored design-check rows'); await page.fill('#import-csv', SAMPLE_CSV)
    await page.click('.import-grid form .btn-primary'); await page.waitFor("document.querySelector('.stat-grid')")
    const stats = await page.eval("Object.fromEntries([...document.querySelectorAll('.stat')].map(s => [s.querySelector('span').textContent, Number(s.querySelector('b').textContent)]))")
    ok(stats['可新增'] === 3 && stats['无效'] === 1 && stats['冲突组'] === 1 && stats['留出重叠被拒'] === 1 && stats['重复'] >= 1, JSON.stringify(stats))
    await page.shot('10-import-preview', true); return JSON.stringify(stats)
  })
  await check('import.edit_invalidates_preview', async () => {
    await page.fill('#import-csv', SAMPLE_CSV + 'Bring the spare charger to the lab.,ham\n')
    ok(!(await page.eval("document.querySelector('.stat-grid')")) && (await page.text('.preview-card .callout-warn')).includes('旧预览已作废'), 'stale preview should disappear with a notice')
    await page.shot('11-import-preview-stale'); return 'stale notice; confirm unavailable'
  })
  await check('import.late_preview_ignored', async () => {
    faults({ delay: { 'POST /api/imports/preview': 2.5 } })
    await page.click('.import-grid form .btn-primary'); await sleep(400)
    await page.fill('#import-csv', SAMPLE_CSV); await sleep(3200)
    faults({})
    ok(!(await page.eval("document.querySelector('.stat-grid')")), 'late preview for an older draft must not render'); return 'older preview dropped after edit'
  })
  await check('import.empty_result_blocks_confirm', async () => {
    await page.fill('#import-csv', 'text,label\n,ham\nsecret heldout exact content,ham\n'); await page.click('.import-grid form .btn-primary')
    await page.waitFor("document.querySelector('.stat-grid')")
    ok(await page.eval("document.querySelector('.preview-card .actions .btn-primary').disabled"), 'confirm must be disabled'); return 'accepted 0 → confirm disabled'
  })
  await check('import.confirm_unknown_then_reconcile', async () => {
    await page.fill('#import-csv', SAMPLE_CSV); await page.click('.import-grid form .btn-primary'); await page.waitFor("document.querySelector('.stat-grid')")
    faults({ drop_confirm: 1 }); await page.click('.preview-card .actions .btn-primary')
    await page.waitFor("document.querySelector('.preview-card .callout-warn')", 5000)
    ok((await page.text('.preview-card .callout-warn')).includes('导入结果未知'), 'unknown import copy'); await page.shot('12-import-confirm-unknown', true)
    await page.click('.preview-card .callout-warn .btn-primary'); await page.waitFor("document.querySelector('.preview-card .callout-ok')", 5000)
    const text = await page.text('.preview-card .callout-ok'); ok(text.includes('之前已确认过') && text.includes('没有重复写入'), text)
    await page.shot('13-import-reconciled-existing'); return text.slice(0, 60)
  })

  // ---------- Review ----------
  await check('review.list_and_keyboard_confirm', async () => {
    await page.click('.callout-ok .btn-primary', '去人工审核'); await page.waitFor("document.querySelectorAll('.sample-row').length > 0", 5000)
    await page.shot('14-review-list-desktop', true)
    await page.eval("document.querySelector('.sample-row').focus()"); await page.key('Enter')
    await page.waitFor("document.activeElement?.id === 'detail-title'", 5000)
    await page.tabTo("e.name === 'review-label'"); ok((await page.focused()).ring, 'label radio focus ring')
    await page.tabTo("e.id === 'review-note'"); await page.type('Plain reminder without promotion (simulated review).')
    await page.tabTo("e.classList.contains('btn-primary') && e.closest('.review-form')"); ok((await page.text('.review-form .btn-primary')).includes('不记为纠错'), 'confirm wording'); await page.key('Enter')
    await page.waitFor("document.querySelector('.review-form .callout-ok')", 5000)
    const text = await page.text('.review-form .callout-ok'); ok(text.includes('没有新增纠错历史'), text); return text
  })
  await check('review.correction_with_history', async () => {
    await page.click('.review-form .btn-ghost', '下一条待审'); await page.waitFor("document.activeElement?.id === 'detail-title'", 5000)
    const current = await page.eval("document.querySelector('.detail-card .kv .label-badge').textContent")
    await page.click(`input[name="review-label"][value="${current === 'ham' ? 'spam' : 'ham'}"]`)
    await page.fill('#review-note', 'Simulated correction for the design check.'); await page.click('.review-form .actions .btn-primary')
    await page.waitFor("document.querySelector('.review-form .callout-ok')", 5000)
    ok((await page.eval("document.querySelectorAll('.timeline li').length")) === 1, 'history entry expected'); await page.shot('15-review-corrected-history', true)
    return (await page.text('.review-form .callout-ok')).slice(0, 40)
  })
  await check('review.conflict_409_keyboard_recovery', async () => {
    await page.click('.sample-row'); await page.waitFor("document.activeElement?.id === 'detail-title'", 5000)
    const text = await page.eval("document.querySelector('.quote').textContent")
    const listed = await operatorApi('/samples?' + new URLSearchParams({ q: text, limit: '1' }))
    const sample = listed.body.items[0]
    const other = await operatorApi('/samples/' + sample.id, { method: 'PATCH', body: JSON.stringify({ label: sample.label, note: 'simulated second reviewer', expected_revision: sample.revision }) })
    ok(other.status === 200, 'out-of-band review failed ' + other.status)
    await page.fill('#review-note', 'Draft that must survive a conflict.'); await page.click('.review-form .actions .btn-primary')
    await page.waitFor("document.querySelector('.review-form .callout-warn')", 5000)
    ok((await page.text('.review-form .callout-warn')).includes('修订冲突'), 'conflict copy'); ok(await page.eval("document.querySelector('.review-form .actions .btn-primary').disabled"), 'save blocked until reload')
    await page.shot('16-review-conflict-409', true)
    await page.eval("document.querySelector('#review-note').focus()"); await page.tabTo("e.textContent.includes('读取最新版本')"); await page.key('Enter')
    await page.waitFor("document.querySelector('.review-form .callout-info')", 5000)
    ok((await page.eval("document.querySelector('#review-note').value")) === 'Draft that must survive a conflict.', 'draft note kept')
    return (await page.text('.review-form .callout-info')).slice(0, 50)
  })
  await check('review.save_unknown_then_matches', async () => {
    await reloadPending(page)
    await page.click('.sample-row'); await page.waitFor("document.activeElement?.id === 'detail-title'", 5000)
    await page.fill('#review-note', 'Simulated save whose response is lost.'); faults({ drop_patch: 1 }); await page.click('.review-form .actions .btn-primary')
    await page.waitFor("document.querySelector('.review-form .callout-warn')", 5000)
    ok((await page.text('.review-form .callout-warn')).includes('保存结果未知'), 'unknown copy'); await page.shot('17-review-save-unknown', true)
    await page.click('.review-form .callout-warn .btn-primary'); await page.waitFor("document.querySelector('.review-form .callout-ok')", 5000)
    const text = await page.text('.review-form .callout-ok'); ok(text.includes('无需重复提交'), text); await page.shot('18-review-unknown-reconciled')
    return text.slice(0, 50)
  })
  await check('review.saved_but_list_refresh_failed', async () => {
    await reloadPending(page)
    await page.click('.sample-row'); await page.waitFor("document.activeElement?.id === 'detail-title'", 5000)
    await page.fill('#review-note', 'Simulated save with a failed list refresh.'); faults({ fail_list: 1 }); await page.click('.review-form .actions .btn-primary')
    await page.waitFor("document.querySelector('.review-form .callout-warn')", 5000)
    const text = await page.text('.review-form .callout-warn'); ok(text.includes('无需重复保存'), text); await page.shot('19-review-refresh-failed')
    faults({}); return text.slice(0, 50)
  })
  await check('review.last_page_falls_back', async () => {
    const first = await operatorApi('/samples?pending=true&limit=100')
    for (const row of first.body.items.slice(0, Math.max(0, first.body.total - 21))) await operatorApi('/samples/' + row.id, { method: 'PATCH', body: JSON.stringify({ label: row.label, note: 'simulated batch reviewer', expected_revision: row.revision }) })
    await page.click('.segmented button', '全部'); await page.click('.segmented button', '待审'); await page.waitFor("document.querySelector('.list-meta').textContent.includes('/ 21')", 5000)
    await page.click('.pagination button', '下一页'); await page.waitFor("document.querySelector('.list-meta').textContent.includes('21–21 / 21')", 5000)
    await page.click('.sample-row'); await page.waitFor("document.activeElement?.id === 'detail-title'", 5000)
    await page.fill('#review-note', 'Simulated review of the last row on the last page.'); await page.click('.review-form .actions .btn-primary')
    await page.waitFor("document.querySelector('.list-meta').textContent.includes('1–20 / 20')", 5000)
    return await page.text('.list-meta')
  })
  await check('review.snapshot', async () => {
    await page.click('.snapshot-card .btn-outline', '生成快照'); await page.waitFor("document.querySelector('.snapshot-card .callout-ok')", 5000)
    return (await page.text('.snapshot-card .callout-ok')).slice(0, 60)
  })
  await check('review.empty_pending_and_search', async () => {
    await page.fill('#sample-search', 'zzz-no-such-text'); await page.key('Enter')
    await page.waitFor("document.querySelector('.empty-mini')?.textContent.includes('当前筛选下没有样本')", 5000)
    await page.fill('#sample-search', ''); await page.key('Enter'); return 'empty filter state'
  })
  await check('layout.desktop_no_overflow', async () => {
    const pages = {}
    for (const id of ['decide', 'import', 'review', 'evidence', 'model']) { await page.hash(id); await sleep(400); pages[id] = await page.overflow() }
    ok(Object.values(pages).every(v => v <= 0), JSON.stringify(pages)); return JSON.stringify(pages)
  })

  // ---------- Model and evidence ----------
  await check('model.select_is_not_loaded', async () => {
    await page.hash('model'); await page.waitFor("document.querySelector('#candidate-run')")
    await page.select('#candidate-kind', 'mlp16'); await page.click('.panel form.card .btn-primary')
    await page.waitFor("document.querySelector('form.card .callout-ok')", 5000)
    await page.waitFor("document.querySelector('.version-card dd.diff')", 5000)
    const loaded = await page.eval("[...document.querySelectorAll('.is-loaded dd')].map(d => d.textContent).join('|')")
    ok(loaded.includes('逻辑回归') && !loaded.includes('MLP'), 'loaded must stay LR: ' + loaded)
    ok((await page.text('.context-strip')).includes('重启后才生效'), 'context strip restart hint')
    await page.shot('20-model-selected-not-loaded', true); return 'selected mlp16; loaded remains lr'
  })
  await check('model.stale_revision_rejected', async () => {
    const service = (await operatorApi('/service')).body
    await operatorApi('/service/rollback', { method: 'POST', body: JSON.stringify({ expected_revision: service.revision }) })
    await page.select('#candidate-kind', 'mlp16'); await page.click('.panel form.card .btn-primary')
    await page.waitFor("document.querySelector('form.card .callout-error')", 5000)
    const text = await page.text('form.card .callout-error'); ok(text.includes('已被更新'), text); return text
  })
  await check('model.rollback', async () => {
    await page.click('.panel .panel-head .btn-outline'); await sleep(600)
    await page.select('#candidate-kind', 'mlp16'); await page.click('.panel form.card .btn-primary'); await page.waitFor("document.querySelector('form.card .callout-ok')", 5000)
    await page.click('.panel form.card .btn-outline', '回退'); await page.waitFor("document.querySelector('form.card .callout-ok')?.textContent.includes('恢复为上一选择')", 5000)
    return 'rollback recorded; restart still required to change loaded'
  })
  await check('evidence.three_kinds', async () => {
    await page.hash('evidence'); await page.waitFor("document.querySelector('.run-card')", 5000)
    const text = await page.eval('document.querySelector(".panel").textContent')
    ok(text.includes('尚未评测') && text.includes('只评估一次') && text.includes('SYNTHETIC DESIGN DEMO'), 'evidence separation copy')
    await page.shot('21-evidence-desktop', true); return 'runs + final report (synthetic) + Jev 尚未评测'
  })

  // ---------- Identity clear ----------
  await check('identity.clear_drops_private_state', async () => {
    await page.hash('review'); await page.waitFor("document.querySelectorAll('.sample-row').length > 0", 5000)
    await page.click('.sample-row'); await page.waitFor("document.querySelector('.quote')", 5000)
    const secret = await page.eval("document.querySelector('.quote').textContent")
    await page.click('.topbar-actions button'); await page.click('#auth-panel .btn-danger')
    await page.waitFor("location.hash === '#decide' && document.querySelector('.banner')", 5000)
    const body = await page.eval('document.body.innerText')
    ok(!body.includes(secret), 'sample text still in DOM'); ok(await page.eval("Boolean(document.querySelector('.nav-item[href=\"#review\"] .nav-lock'))"), 'review should be locked')
    await page.shot('22-identity-cleared'); return 'no sample text; locked; banner shown'
  })

  // ---------- Public and empty services ----------
  await check('public.readonly_and_tls_block', async () => {
    await page.goto(PUBLIC + '/workbench/#decide')
    ok(!(await page.eval("document.querySelector('.topbar-actions button')")), 'public should not offer operator login')
    await setup(page, { engine: 'compare', text: 'Please bring the blue notebook to our meeting tomorrow.', key: 'demo-ok' })
    const r = await results(page); ok(r.blockers.includes('改用 HTTPS 连接'), r.blockers)
    ok(await page.eval("document.querySelector('.transport').classList.contains('is-bad')"), 'TLS alert'); await page.shot('23-public-tls-blocked', true)
    const writes = await page.eval(`(async () => { const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ${TOKEN}' }; const a = await fetch('/api/snapshots', { method: 'POST', headers: h }); const b = await fetch('/api/imports/preview', { method: 'POST', headers: h, body: JSON.stringify({ csv: 'text,label\\nhello there friend,ham\\n', source: 'x' }) }); const c = await fetch('/api/decision', { method: 'POST', headers: h, body: JSON.stringify({ text: 'hello', engine: 'jev', credential_source: 'server', external_consent: true }) }); const cfg = await (await fetch('/api/config', { headers: h })).json(); return [a.status, b.status, c.status, cfg.decision.server_key_available] })()`)
    ok(writes[0] === 403 && writes[1] === 403 && writes[2] >= 400 && writes[3] === false, JSON.stringify(writes))
    return `fake operator token in public: snapshot ${writes[0]}, import ${writes[1]}, server-key decision ${writes[2]} (HTTP loopback rejects before the server-key check), server_key_available=${writes[3]}`
  })
  await check('public.private_panels_locked', async () => {
    await page.hash('review'); ok((await page.text('.state-card h2')).includes('公开只读部署不提供'), 'locked copy'); await page.shot('24-public-review-locked')
    await page.hash('evidence'); await page.waitFor("document.querySelector('.run-card')", 5000); return 'review locked; evidence readable'
  })
  await check('empty.no_model_no_jev', async () => {
    await page.goto(EMPTY + '/workbench/#decide')
    ok(await page.eval("document.querySelector('input[value=\"jev\"]').disabled && document.querySelector('input[value=\"compare\"]').disabled"), 'Jev options disabled')
    await page.fill('#sms-text', 'Please bring the blue notebook to our meeting tomorrow.')
    const r = await results(page); ok(r.blockers.includes('服务尚未加载本地模型'), r.blockers); await page.shot('25-empty-no-model-jev-off')
    await page.hash('model'); ok((await page.text('.panel > .callout')).includes('尚未选择也未加载'), 'model empty copy')
    return r.blockers
  })
  await check('empty.pending_pool_empty', async () => {
    await page.click('.topbar-actions button'); await page.fill('#token-input', TOKEN); await page.key('Enter'); await page.waitFor("document.querySelector('.topbar-actions .btn-ok')", 5000)
    await page.hash('review'); await page.waitFor("document.querySelector('.empty-mini')?.textContent.includes('待审池为空')", 5000)
    await page.shot('26-empty-pending-pool'); return 'empty pending pool with link to import'
  })
  await check('empty.evidence_no_runs', async () => {
    await page.hash('evidence'); await page.waitFor("document.querySelector('.state-card.compact')", 5000); return await page.text('.block .state-card')
  })

  // ---------- Narrow screen ----------
  await page.viewport(390, 844, true)
  await check('mobile.decide_compare', async () => {
    await page.goto(PRIVATE + '/workbench/#decide')
    ok(await page.overflow() <= 0, 'empty overflow'); await page.shot('m01-decide-empty')
    await setup(page, { engine: 'compare', key: 'demo-flip' }); await page.click('.chip', '预约确认'); await submit(page); await settled(page)
    const overflow = await page.overflow(); ok(overflow <= 0, 'result overflow ' + overflow); await page.shot('m02-decide-compare-disagree', true)
    const nav = await page.eval("({ scroll: document.querySelector('.sidenav').scrollWidth, client: document.querySelector('.sidenav').clientWidth })")
    return `overflow 0; nav strip ${nav.scroll}/${nav.client}px scrolls inside itself`
  })
  await check('mobile.operator_pages', async () => {
    await page.click('.topbar-actions button'); await page.fill('#token-input', TOKEN); await page.key('Enter'); await page.waitFor("document.querySelector('.topbar-actions .btn-ok')", 5000)
    const widths = {}
    await page.hash('import'); await page.fill('#import-source', 'SIMULATED mobile check'); await page.fill('#import-csv', SAMPLE_CSV); await page.click('.import-grid form .btn-primary'); await page.waitFor("document.querySelector('.stat-grid')")
    widths.import = await page.overflow(); await page.shot('m03-import-preview', true)
    await page.hash('review'); await page.waitFor("document.querySelectorAll('.sample-row').length > 0", 5000); await page.click('.sample-row'); await page.waitFor("document.querySelector('.quote')", 5000)
    widths.review = await page.overflow(); await page.shot('m04-review-detail', true)
    await page.hash('model'); await sleep(500); widths.model = await page.overflow(); await page.shot('m05-model', true)
    await page.hash('evidence'); await page.waitFor("document.querySelector('.run-card')", 5000); widths.evidence = await page.overflow(); await page.shot('m06-evidence', true)
    const tables = await page.eval("[...document.querySelectorAll('.table-wrap')].map(t => t.scrollWidth > t.clientWidth)")
    ok(Object.values(widths).every(v => v <= 0), JSON.stringify(widths)); return `page overflow ${JSON.stringify(widths)}; tables scroll inside: ${JSON.stringify(tables)}`
  })
  await check('storage.final_empty', async () => {
    const storage = await page.eval("(async () => ({ local: localStorage.length, session: sessionStorage.length, cookie: document.cookie, idb: (await indexedDB.databases()).length }))()")
    ok(!storage.local && !storage.session && !storage.cookie && !storage.idb, JSON.stringify(storage)); return JSON.stringify(storage)
  })
}

main().catch(error => { console.error(error); process.exitCode = 1 })
