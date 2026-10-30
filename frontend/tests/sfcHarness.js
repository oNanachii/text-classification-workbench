import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'

// Execute the real SFC setup with Vue reactivity/lifecycle and its real JS imports.
// The renderer is deliberately empty: these tests exercise component state and
// async event handlers without a DOM, browser, server or private project data.
const renderer = createRenderer({
  createComment: text => ({ text }),
  createText: text => ({ text }),
  createElement: tag => ({ tag }),
  insert() {}, remove() {}, patchProp() {}, setText() {}, setElementText() {},
  parentNode: () => null, nextSibling: () => null,
})

export async function mountSfc(relativePath, props = {}) {
  const filename = new URL(relativePath, new URL('../src/', import.meta.url))
  const { descriptor } = parse(await readFile(filename, 'utf8'), { filename: filename.pathname })
  const compiled = compileScript(descriptor, { id: relativePath })
  const source = compiled.content
    .replace(/^import (\w+) from ['"][^'"]+\.vue['"]\s*$/gm, 'const $1 = {}')
    .replace(/from (['"])([^'"]+)\1/g, (_, quote, specifier) => {
      const resolved = specifier.startsWith('.') ? new URL(specifier, filename).href : import.meta.resolve(specifier)
      return 'from ' + JSON.stringify(resolved)
    })
  const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))
  let state
  const app = renderer.createApp({
    ...component,
    setup(props, context) {
      state = component.setup(props, context)
      return state
    },
    render: () => null,
  }, props)
  app.mount({})
  return { state, unmount: () => app.unmount() }
}

export const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

export async function settle() {
  await new Promise(resolve => setImmediate(resolve))
  await nextTick()
}
