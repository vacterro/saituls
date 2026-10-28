// PERF-002 heap-retention probe (SRC-019).
//
// Deliberately NOT named *.test.mjs: it needs a forced GC, so it only runs
// under an explicit `node --expose-gc`. `node --test` runs without that flag,
// and a probe that silently skipped there would be worse than no probe.
//
//   node --expose-gc tests/saipatch_retention_heap_probe.mjs
//
// The audit's shape: flood 20,000 synthetic sessions, measure after GC, flood
// 20,000 more, measure again. Retention that scales with the number of
// sessions a window ever visited shows up as heap growth across that second
// flood. Pre-fix this grew +29.03 MiB over 20,000 sessions; bounded it is flat.
import { createMessageBridge, createProjection, createAmbient, MAX_TRACKED_SESSIONS } from "../Scripts/saipatch/patches/opencode-queue-mode/saipatch-native-queue-2x.js"

if (typeof global.gc !== "function") {
  console.error("this probe needs --expose-gc")
  process.exit(2)
}

const client = {
  v2: { session: { messages: async () => ({ data: { data: [], cursor: {} } }) } },
  session: { messages: async () => ({ data: [] }) },
}
const bridge = createMessageBridge({ client, projection: createProjection() })
const ambient = createAmbient()

const measure = () => { global.gc(); global.gc(); return process.memoryUsage().heapUsed }

const flood = (from, to) => {
  for (let i = from; i < to; i += 1) {
    bridge.handleLiveEvent(`session-${i}`, "session.next.prompted", {
      messageID: `m-${i}`,
      prompt: { text: "cc" },
    })
    ambient.onAdmitted(`session-${i}`, `m-${i}`)
  }
}

flood(0, 20000)
const afterFirst = measure()
flood(20000, 40000)
const afterSecond = measure()

const growthMiB = (afterSecond - afterFirst) / (1024 * 1024)
console.log(`bound=${MAX_TRACKED_SESSIONS} trackedSessions=${bridge.trackedSessions}`)
console.log(`heap after 20k sessions: ${(afterFirst / 1048576).toFixed(2)} MiB`)
console.log(`heap after 40k sessions: ${(afterSecond / 1048576).toFixed(2)} MiB`)
console.log(`growth past the bound: ${growthMiB.toFixed(2)} MiB`)
// A per-session leak of even a few hundred bytes across 20,000 extra sessions
// is well over 5 MiB, so a 2 MiB tolerance only passes when retention is flat.
const ok = bridge.trackedSessions <= MAX_TRACKED_SESSIONS && growthMiB < 2
console.log(ok ? "HEAP_FLAT=PASS" : "HEAP_FLAT=FAIL")
process.exit(ok ? 0 : 1)
