// Read-only inventory of the exact compiled paste boundary. No user input is read.
import { readFileSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { resolve } from 'node:path'

const [exe, output] = process.argv.slice(2)
if (!exe || !output) throw new Error('Usage: node probe_paste_host.mjs <exe> <output.json>')
const bytes = readFileSync(exe)
const sha256 = createHash('sha256').update(bytes).digest('hex')
const expected = '88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5'
const markers = {
  collector: 'consumePasteBytes($){',
  append: 'pushPasteBytes($){',
  summary: 'paste_summary_enabled',
  pasteListener: 'set onPaste(',
  renderer: 'getLifecyclePasses()',
}
const seams = Object.fromEntries(Object.entries(markers).map(([name, marker]) => {
  const needle = Buffer.from(marker)
  const offsets = []
  for (let at = bytes.indexOf(needle); at !== -1; at = bytes.indexOf(needle, at + needle.length)) offsets.push(at)
  return [name, { marker, offsets }]
}))
const report = {
  schema: 1, executable: resolve(exe), sha256,
  verdict: sha256 === expected && Object.values(seams).every(x => x.offsets.length > 0)
    ? 'KNOWN_PASTE_BOUNDARY' : 'UNKNOWN',
  seams,
  limitation: 'Inventory only. Does not prove performance, input equivalence, or successful rendering.',
}
writeFileSync(output, JSON.stringify(report, null, 2) + '\n')
console.log(JSON.stringify(report))
if (report.verdict === 'UNKNOWN') process.exitCode = 2
