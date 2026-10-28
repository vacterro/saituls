import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
const [root, prefix = 'before-short-'] = process.argv.slice(2)
if (!root) throw Error('Usage: node native_paste_report.mjs <evidence-root> [prefix]')
const groups = new Map()
for (const dir of readdirSync(root).filter(name => name.startsWith(prefix))) {
  let result
  try { result = JSON.parse(readFileSync(join(root, dir, 'result.json'), 'utf8')) }
  catch (e) { if (e.code === 'ENOENT') continue; throw e }
  const row = result.trace.find(r => r.type === 'paste')
  const items = groups.get(result.size) ?? []
  items.push({transport: row.input_last-row.input_first,
    processing: row.render_commit-row.input_last,
    insert: row.prompt_insert_end-row.prompt_insert_begin,
    total: row.render_commit-row.input_first,
    integrity: result.integrity === true, inserts: row.inserts})
  groups.set(result.size, items)
}
const median = values => { const a = [...values].sort((a,b)=>a-b); return (a[Math.floor((a.length-1)/2)]+a[Math.floor(a.length/2)])/2 }
let previous
const rows = [...groups].sort(([a],[b])=>a-b).map(([size, samples]) => {
  const row = {size, trials: samples.length, integrity: samples.every(r=>r.integrity),
    one_insert: samples.every(r=>r.inserts===1)}
  for (const key of ['transport','processing','insert','total']) row[key+'_ms'] = median(samples.map(r=>r[key]))
  row.processing_ratio = previous ? row.processing_ms/previous.processing_ms : null
  previous = row
  return row
})
const report = {scope:'Fresh idle, real ConPTY; composer hash only, not model submission acceptance', rows}
writeFileSync(join(root, prefix+'medians.json'), JSON.stringify(report,null,2)+'\n')
console.log(JSON.stringify(report,null,2))
