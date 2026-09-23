// Disposable instrumentation only. Never installed by the production manifest.
import { appendFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
export default { id: 'saipatch-paste-trace', async tui(api) {
  const output = process.env.SAIPATCH_PASTE_TRACE
  if (!output) return
  const r = api.renderer
  api.kv.set('paste_summary_enabled', process.env.SAIPATCH_PASTE_SUMMARY !== 'false')
  const clock = () => performance.now()
  const write = record => appendFileSync(output, JSON.stringify(record) + '\n')
  let pending, inputFirst, inputLast, ready = false
  const parser = r.stdinParser
  if (!parser || typeof r.handleStdinEvent !== 'function') throw Error('Unknown paste trace boundary')
  const push = parser.push
  parser.push = function(bytes) {
    inputFirst ??= clock()
    inputLast = clock()
    return push.call(this, bytes)
  }
  const dispatch = r.handleStdinEvent
  r.handleStdinEvent = function(event) {
    if (event.type !== 'paste') return dispatch.call(this, event)
    const editor = r.currentFocusedRenderable
    const row = pending = {type:'paste', bytes:event.bytes.length, input_first:inputFirst,
      input_last:inputLast, paste_event_begin:clock(), inserts:0, frames:0}
    inputFirst = inputLast = undefined
    const original = editor?.onPaste
    if (typeof original !== 'function') throw Error('Focused composer has no paste handler')
    const insert = editor.insertText
    editor.insertText = function(text) {
      row.inserts++
      row.prompt_insert_begin = clock()
      const result = insert.call(this, text)
      row.prompt_insert_end = clock()
      return result
    }
    editor.onPaste = async function(e) {
      row.handler_begin = clock()
      try { return await original.call(this, e) }
      finally {
        row.composer_complete = clock()
        editor.onPaste = original
        editor.insertText = insert
        // Hash expanded synthetic composer, never persist its content.
        const text = editor.getClipboardText(editor.plainText)
        row.chars = text.length
        row.newlines = (text.match(/\n/g) || []).length
        row.sha256 = createHash('sha256').update(text).digest('hex')
        row.composer_chars = editor.plainText.length
      }
    }
    return dispatch.call(this, event)
  }
  const frame = () => {
    if (!ready && typeof r.currentFocusedRenderable?.onPaste === 'function') {
      ready = true
      write({type:'ready', version:api.app?.version, time:clock()})
    }
    if (!pending) {
      if (!parser.paste) inputFirst = inputLast = undefined
      return
    }
    pending.frames++
    if (!pending.composer_complete) return
    pending.render_commit = clock()
    write(pending)
    pending = undefined
  }
  r.on('frame', frame)
  api.lifecycle.onDispose(() => {
    parser.push = push
    r.handleStdinEvent = dispatch
    r.off('frame', frame)
  })
  write({type:'loaded', version:api.app?.version, time:clock()})
} }
