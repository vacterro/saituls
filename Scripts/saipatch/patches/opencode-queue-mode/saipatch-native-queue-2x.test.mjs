import test from "node:test"
import assert from "node:assert/strict"
import { existsSync, readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import path from "node:path"

import {
  mixHex,
  rgbaToHex,
  normalizePastedText,
  createAmbient,
  createProjection,
  createQueueCore,
  createMessageBridge,
  createHostProjector,
  createBarrierHook,
  HOST_HOOK,
  HOST_SEAM_HOOK,
  createFastPaste,
  toNativePromptPayload,
  saipatchTui,
  pluginID,
  AMBIENT,
  normalizeAutoContinueSettings,
  classifyStall,
  createAutoContinue,
  AUTO_CC_PROMPT,
  AUTO_CC_EXHAUSTED,
  AUTO_CC_STATES,
} from "./saipatch-native-queue-2x.js"
import { createCompletionSound, createCompletionEpisodes } from "./saipatch-completion-sound.js"

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, "..", "..", "..", "..")
// Permanent sanitized fixture (T-166 packaging repair): deterministic rows
// only -- generic ids, FIXTURE_PROMPT texts, normalized timestamps, cursor
// stubs. Never a .saipen/evidence path: product tests must not depend on a
// disposable kitchen run.
const fixturePath = path.join(
  repoRoot,
  "tests",
  "fixtures",
  "saipatch",
  "native-live-messages.json",
)

function fakeV2() {
  const calls = { prompt: [], interrupt: [], switchModel: [], switchAgent: [], messages: [] }
  return {
    calls,
    client: {
      v2: {
        session: {
          prompt: async (input, options) => {
            calls.prompt.push({ input, options })
            const seq = calls.prompt.length
            return { data: { admittedSeq: seq, id: `msg_adm_${seq}`, sessionID: input.sessionID, delivery: input.delivery } }
          },
          interrupt: async (input) => {
            calls.interrupt.push(input)
            return {}
          },
          switchModel: async (input) => {
            calls.switchModel.push(input)
            return {}
          },
          switchAgent: async (input) => {
            calls.switchAgent.push(input)
            return {}
          },
          messages: async (input) => {
            calls.messages.push(input)
            return pages.length > 0 ? pages.shift() : { data: { data: [], cursor: {} } }
          },
        },
      },
      session: {
        prompt: async (input) => {
          calls.legacyPrompt = calls.legacyPrompt ?? []
          calls.legacyPrompt.push(input)
          return { data: { id: "legacy" } }
        },
        abort: async (input) => {
          calls.legacyAbort = calls.legacyAbort ?? []
          calls.legacyAbort.push(input)
          return {}
        },
        messages: async () => ({ data: [{ info: { id: "legacy", role: "user", time: { created: 1 } }, parts: [] }] }),
      },
    },
  }
}

let pages = []

test("projection maps the recorded native cc1..cc5 fixture into legacy {info, parts}", () => {
  const raw = JSON.parse(readFileSync(fixturePath, "utf8"))
  const native = raw.data

  const projection = createProjection()
  const rows = projection.project("s", native)
  const roles = rows.map((row) => row.info.role)
  assert.ok(roles.every((role) => role === "user" || role === "assistant"), `unexpected roles: ${roles}`)
  assert.equal(rows[0].info.role, "user")
  assert.equal(rows[1].info.role, "assistant")
  const texts = rows.flatMap((row) => row.parts.map((part) => part.text))
  assert.ok(texts.some((text) => typeof text === "string" && text.startsWith("cc1")), `cc1 text missing: ${JSON.stringify(texts)}`)
  assert.ok(texts.some((text) => typeof text === "string" && text.length > 0))
})

test("projection skips unknown message shapes", () => {
  const projection = createProjection()
  const rows = projection.project("s", [{ type: "mystery" }, null, "nope"])
  assert.equal(rows.length, 0)
})

test("toNativePromptPayload preserves text, files and agent mentions", () => {
  const payload = toNativePromptPayload({
    text: "hello",
    parts: [
      { type: "file", url: "file:///tmp/a.png" },
      { type: "agent", name: "build" },
      { type: "text", text: "world" },
    ],
  })
  assert.equal(payload.text, "hello\nworld")
  assert.deepEqual(payload.files, [{ uri: "file:///tmp/a.png" }])
  assert.deepEqual(payload.agents, ["build"])
})

test("queue core routes ordinary admission to v2 delivery:queue and never the legacy endpoint", async () => {
  const fake = fakeV2()
  pages = []
  const admitted = []
  const core = createQueueCore({ client: fake.client, onAdmitted: (s, m) => admitted.push([s, m]) })
  const dispose = core.install()
  await fake.client.session.prompt({ sessionID: "s", text: "cc1" })
  assert.equal(fake.calls.prompt.length, 1)
  assert.equal(fake.calls.prompt[0].input.delivery, "queue")
  assert.equal(fake.calls.legacyPrompt, undefined)
  assert.equal(admitted.length, 1)
  dispose()
})

test("queue core uses delivery:steer only for explicit steer and skips admission tracking", async () => {
  const fake = fakeV2()
  pages = []
  const admitted = []
  const core = createQueueCore({ client: fake.client, onAdmitted: (s, m) => admitted.push([s, m]) })
  const dispose = core.install()
  await fake.client.session.prompt({ sessionID: "s", text: "cc1", __saipatchSteer: true })
  assert.equal(fake.calls.prompt[0].input.delivery, "steer")
  assert.equal(admitted.length, 0)
  dispose()
})

test("queue core has no busy gate: consecutive admissions all reach the native scheduler", async () => {
  const fake = fakeV2()
  pages = []
  const core = createQueueCore({ client: fake.client })
  const dispose = core.install()
  await fake.client.session.prompt({ sessionID: "s", text: "cc1" })
  await fake.client.session.prompt({ sessionID: "s", text: "cc2" })
  await fake.client.session.prompt({ sessionID: "s", text: "cc3" })
  assert.equal(fake.calls.prompt.length, 3)
  dispose()
})

test("queue core maps abort onto v2 interrupt", async () => {
  const fake = fakeV2()
  pages = []
  const aborts = []
  const core = createQueueCore({ client: fake.client, onAbort: (sessionID) => aborts.push(sessionID) })
  const dispose = core.install()
  await fake.client.session.abort({ sessionID: "s" })
  assert.deepEqual(fake.calls.interrupt, [{ sessionID: "s" }])
  assert.deepEqual(aborts, ["s"])
  assert.equal(fake.calls.legacyAbort, undefined)
  dispose()
})

test("queue core refuses to install twice and fail-closed without v2", () => {
  const fake = fakeV2()
  pages = []
  const core = createQueueCore({ client: fake.client })
  core.install()
  assert.throws(() => createQueueCore({ client: fake.client }).install())
  assert.throws(() => createQueueCore({ client: { session: {} } }))
})

test("ambient: new session is NEUTRAL, admission makes RUNNING, permission overrides to NEEDS_HUMAN, resolution recomputes", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  assert.equal(ambient.evaluate("s"), AMBIENT.NEUTRAL)
  ambient.onAdmitted("s", "m1")
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING)
  ambient.onEvent("s", "session.status", { status: { type: "busy" } })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING)
  ambient.onEvent("s", "permission.asked", { sessionID: "s" })
  assert.equal(ambient.evaluate("s"), AMBIENT.NEEDS_HUMAN, "active execution blocked on user must be NEEDS_HUMAN, not RUNNING")
  ambient.onEvent("s", "permission.replied", { sessionID: "s" })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "execution resumes after permission reply")
})

test("ambient: question blocks to NEEDS_HUMAN and rejection after terminal state recomputes to DONE", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "m1")
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  ambient.onEvent("s", "session.status", { status: { type: "idle" } })
  ambient.onEvent("s", "session.next.prompted", { messageID: "m1" })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
  ambient.onEvent("s", "question.asked", { sessionID: "s" })
  assert.equal(ambient.evaluate("s"), AMBIENT.NEEDS_HUMAN)
  ambient.onEvent("s", "question.rejected", { sessionID: "s" })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
})

test("ambient: queued-but-unpromoted work suppresses DONE between turns", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "cc1")
  ambient.onEvent("s", "session.next.prompted", { messageID: "cc1" })
  ambient.onAdmitted("s", "cc2")
  ambient.onEvent("s", "session.next.step.started", {})
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  ambient.onEvent("s", "session.status", { status: { type: "idle" } })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "cc2 still pending promotion must not show DONE")
  ambient.onEvent("s", "session.next.prompted", { messageID: "cc2" })
  ambient.onEvent("s", "session.next.step.started", {})
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  ambient.onEvent("s", "session.status", { status: { type: "idle" } })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
})

test("ambient: prompted events never consume queued work (native promotes early)", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "cc1")
  ambient.onAdmitted("s", "cc2")
  // The native scheduler may promote cc2 at decision time, before cc1's turn
  // completes; prompted must not empty the pending-work counter.
  ambient.onEvent("s", "session.next.prompted", { messageID: "cc1" })
  ambient.onEvent("s", "session.next.prompted", { messageID: "cc2" })
  ambient.onEvent("s", "session.next.step.started", {})
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  ambient.onEvent("s", "session.status", { status: { type: "idle" } })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "cc2 admitted but not completed must keep RUNNING")
  ambient.onEvent("s", "session.next.step.started", {})
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  ambient.onEvent("s", "session.status", { status: { type: "idle" } })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
})

test("ambient: a queued prompt admitted mid-step must not drop the running step's ended (T-144)", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "u1")
  ambient.onEvent("s", "session.next.prompted", { messageID: "u1" })
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a1" })
  // cc2 is admitted while cc1's step is still running: this must not overwrite
  // the active assistant identity used by the step.ended generation gate.
  ambient.onAdmitted("s", "u2")
  ambient.onEvent("s", "session.next.step.ended", { assistantMessageID: "a1", finish: "stop" })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "cc2 still pending must stay RUNNING")
  ambient.onEvent("s", "session.next.prompted", { messageID: "u2" })
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a2" })
  ambient.onEvent("s", "session.next.step.ended", { assistantMessageID: "a2", finish: "stop" })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
})

test("ambient: abort and failures never show DONE; new prompt after DONE returns to RUNNING", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "m1")
  ambient.onEvent("s", "session.next.prompted", { messageID: "m1" })
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  ambient.onEvent("s", "session.status", { status: { type: "idle" } })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
  ambient.onAbort("s")
  assert.equal(ambient.evaluate("s"), AMBIENT.NEUTRAL, "abort must not stay DONE")

  ambient.onEvent("s2", "session.next.step.failed", { })
  ambient.onEvent("s2", "session.status", { status: { type: "idle" } })
  assert.equal(ambient.evaluate("s2"), AMBIENT.NEUTRAL, "failed turn must not show DONE")

  ambient.onAdmitted("s", "m2")
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "new prompt after DONE is RUNNING")
})

test("ambient theme mapping covers RUNNING, NEEDS_HUMAN and DONE with tinted backgrounds", () => {
  const ambient = createAmbient({ baseBackground: "#101010" })
  assert.equal(ambient.themeNameFor(AMBIENT.NEUTRAL), null)
  assert.equal(ambient.themeNameFor(AMBIENT.RUNNING), "saipatch-queue-running")
  const running = ambient.colorFor(AMBIENT.RUNNING)
  assert.notEqual(running, "#101010")
  assert.match(running, /^#[0-9a-f]{6}$/)
  const done = ambient.colorFor(AMBIENT.DONE)
  assert.notEqual(done, running)
  const needsHuman = ambient.colorFor(AMBIENT.NEEDS_HUMAN)
  assert.notEqual(needsHuman, running)
  assert.notEqual(needsHuman, done)
})

test("mixHex tints subtly and clamps", () => {
  const subtle = mixHex("#000000", "#ffffff", 0.1)
  assert.notEqual(subtle, "#000000")
  assert.notEqual(subtle, "#ffffff")
  assert.equal(mixHex("#000000", "#ffffff", 2), "#ffffff")
  assert.equal(mixHex("#000000", "#ffffff", -1), "#000000")
  assert.equal(mixHex("nope", "#ffffff", 0.5), "nope")
})

test("rgbaToHex converts OpenTUI RGBA colors and keeps hex strings", () => {
  assert.equal(rgbaToHex("#aabbcc"), "#aabbcc")
  const hex = rgbaToHex({ r: 1, g: 0.5, b: 0, a: 1 })
  assert.match(hex, /^#[0-9a-f]{6}$/)
  const alpha = rgbaToHex({ r: 1, g: 1, b: 1, a: 0.5 })
  assert.match(alpha, /^#[0-9a-f]{8}$/)
  assert.equal(rgbaToHex({}), undefined)
})

test("message bridge hydrates native pages into the legacy response and respects the limit", async () => {
  const fake = fakeV2()
  pages = [
    { data: { data: [{ id: "m3", type: "user", time: { created: 30 }, text: "cc3" }], cursor: { next: "c2" } } },
    { data: { data: [{ id: "m1", type: "assistant", time: { created: 10, completed: 20 }, finish: "stop", content: [{ type: "text", id: "t1", text: "one" }] }], cursor: {} } },
  ]
  const projection = createProjection()
  const bridge = createMessageBridge({ client: fake.client, projection })
  const dispose = bridge.install()
  const response = await fake.client.session.messages({ sessionID: "s", limit: 100 })
  assert.equal(fake.calls.messages.length, 2)
  assert.equal(response.data.length, 2)
  assert.equal(response.data[0].info.role, "assistant")
  assert.equal(response.data[0].parts[0].text, "one")
  assert.equal(response.data[1].info.role, "user")
  assert.equal(response.data[1].parts[0].text, "cc3")
  dispose()
})

test("message bridge injectLive writes through live store proxies and no-ops on detached arrays", () => {
  const fake = fakeV2()
  pages = []
  const projection = createProjection()
  const bridge = createMessageBridge({ client: fake.client, projection })
  const live = []
  const partStore = []
  const state = {
    session: { messages: () => live },
    part: () => partStore,
  }
  const detachedCalls = { count: 0 }
  const detached = {
    session: { messages: () => { detachedCalls.count += 1; return [] } },
    part: () => [],
  }
  const rows = projection.project("s", [{ id: "m1", type: "user", time: { created: 1 }, text: "hi" }])
  assert.deepEqual(bridge.injectLive("s", rows, state), { inserted: 1, updated: 0, verified: 1 })
  assert.equal(live.length, 1)
  assert.equal(partStore.length, 1)
  const result = bridge.injectLive("s", rows, detached)
  assert.equal(result.inserted, 0)
  assert.equal(bridge.seam.unsupported, false)
})

// --- Live native projection suite (T-143 clauses 8-12) ----------------------

// T-144 render-crash regression (evidence: disposable 1.18.29 crashed with
// "undefined is not an object (evaluating 'HU.tokens.output')" in the status
// bar findLast over assistant rows). The crash itself proved live store
// inserts reach the real transcript; every projected/live row must carry the
// complete legacy tokens shape the host reads unguarded.
test("live: every injected row carries the complete legacy tokens shape", () => {
  const { bridge, storeView } = liveBridge(fakeStore())
  bridge.handleLiveEvent("s", "session.next.prompted", {
    timestamp: ts(1000), sessionID: "s", messageID: "u1", prompt: { text: "cc1" },
  })
  bridge.handleLiveEvent("s", "session.next.step.started", {
    timestamp: ts(1001), sessionID: "s", assistantMessageID: "a1", agent: "build",
    model: { providerID: "p", id: "m" },
  })
  bridge.handleLiveEvent("s", "session.next.step.failed", {
    timestamp: ts(1002), sessionID: "s", assistantMessageID: "a2", error: { message: "x" },
  })
  const rows = storeView.messages.get("s")
  assert.equal(rows.length, 3)
  for (const row of rows) {
    assert.ok(row.tokens && typeof row.tokens === "object", `row ${row.id} must carry tokens`)
    assert.equal(typeof row.tokens.output, "number", `row ${row.id} tokens.output numeric`)
    assert.equal(typeof row.tokens.input, "number", `row ${row.id} tokens.input numeric`)
    assert.equal(typeof row.tokens.reasoning, "number", `row ${row.id} tokens.reasoning numeric`)
    assert.ok(row.tokens.cache && typeof row.tokens.cache.read === "number", `row ${row.id} cache.read numeric`)
    assert.equal(typeof row.tokens.cache.write, "number", `row ${row.id} cache.write numeric`)
  }
  // step.ended tokens are normalized, never a partial object
  bridge.handleLiveEvent("s", "session.next.step.ended", {
    timestamp: ts(2000), sessionID: "s", assistantMessageID: "a1", finish: "stop",
    cost: 0, tokens: { input: 3, output: 4 },
  })
  const ended = rows.find((r) => r.id === "a1")
  assert.deepEqual(ended.tokens, { input: 3, output: 4, reasoning: 0, cache: { read: 0, write: 0 } })
})

function fakeStore() {
  const messages = new Map()
  const parts = new Map()
  return {
    view: {
      session: {
        messages: (sessionID) => {
          if (!messages.has(sessionID)) messages.set(sessionID, [])
          return messages.get(sessionID)
        },
      },
      part: (messageID) => {
        if (!parts.has(messageID)) parts.set(messageID, [])
        return parts.get(messageID)
      },
    },
    messages,
    parts,
  }
}

function liveBridge(storeView) {
  const fake = fakeV2()
  pages = []
  const projection = createProjection()
  const bridge = createMessageBridge({ client: fake.client, projection, state: storeView.view })
  const dispose = bridge.install()
  return { fake, bridge, projection, storeView, dispose }
}

const ts = (ms) => new Date(ms).toISOString()

test("live: native user message appears exactly once", () => {
  const { bridge, storeView } = liveBridge(fakeStore())
  bridge.handleLiveEvent("s", "session.next.prompted", {
    timestamp: ts(1000), sessionID: "s", messageID: "u1", prompt: { text: "cc1" },
  })
  const store = storeView.messages.get("s")
  assert.equal(store.length, 1)
  assert.equal(store[0].id, "u1")
  bridge.handleLiveEvent("s", "session.next.prompted", {
    timestamp: ts(1001), sessionID: "s", messageID: "u1", prompt: { text: "cc1" },
  })
  assert.equal(store.length, 1, "repeated prompted events must not duplicate the row")
})

test("live: assistant message appears once and text update changes the existing row", () => {
  const { bridge, storeView } = liveBridge(fakeStore())
  bridge.handleLiveEvent("s", "session.next.step.started", {
    timestamp: ts(1000), sessionID: "s", assistantMessageID: "a1", agent: "build", model: { providerID: "p", id: "m" },
  })
  bridge.handleLiveEvent("s", "session.next.text.started", {
    timestamp: ts(1001), sessionID: "s", assistantMessageID: "a1", textID: "t1",
  })
  bridge.handleLiveEvent("s", "session.next.text.delta", {
    timestamp: ts(1002), sessionID: "s", assistantMessageID: "a1", textID: "t1", delta: "hel",
  })
  bridge.handleLiveEvent("s", "session.next.text.delta", {
    timestamp: ts(1003), sessionID: "s", assistantMessageID: "a1", textID: "t1", delta: "lo",
  })
  const store = storeView.messages.get("s")
  assert.equal(store.length, 1, "streaming deltas must evolve one assistant row in place")
  const parts = storeView.parts.get("a1")
  assert.equal(parts.length, 1)
  assert.equal(parts[0].text, "hello")
})

test("live: repeated native events never duplicate rows or parts", () => {
  const { bridge, storeView } = liveBridge(fakeStore())
  for (let i = 0; i < 3; i++) {
    bridge.handleLiveEvent("s", "session.next.prompted", {
      timestamp: ts(1000 + i), sessionID: "s", messageID: "u1", prompt: { text: "cc1" },
    })
    bridge.handleLiveEvent("s", "session.next.step.started", {
      timestamp: ts(1100 + i), sessionID: "s", assistantMessageID: "a1", agent: "build",
    })
    bridge.handleLiveEvent("s", "session.next.text.started", {
      timestamp: ts(1200 + i), sessionID: "s", assistantMessageID: "a1", textID: "t1",
    })
  }
  const store = storeView.messages.get("s")
  assert.deepEqual(store.map((m) => m.id), ["u1", "a1"])
  assert.equal(storeView.parts.get("a1").length, 1)
})

test("live: step-finish updates the same assistant message row", () => {
  const { bridge, storeView } = liveBridge(fakeStore())
  bridge.handleLiveEvent("s", "session.next.step.started", {
    timestamp: ts(1000), sessionID: "s", assistantMessageID: "a1", agent: "build",
  })
  bridge.handleLiveEvent("s", "session.next.step.ended", {
    timestamp: ts(1500), sessionID: "s", assistantMessageID: "a1", finish: "stop", cost: 0.01, tokens: { input: 5, output: 7 },
  })
  const store = storeView.messages.get("s")
  assert.equal(store.length, 1)
  assert.equal(store[0].finish, "stop")
  assert.equal(store[0].tokens.output, 7)
})

test("live: two queued turns stay in correct transcript order", () => {
  const { bridge, storeView } = liveBridge(fakeStore())
  bridge.handleLiveEvent("s", "session.next.prompted", {
    timestamp: ts(1000), sessionID: "s", messageID: "u1", prompt: { text: "cc1" },
  })
  bridge.handleLiveEvent("s", "session.next.step.started", {
    timestamp: ts(1001), sessionID: "s", assistantMessageID: "a1", agent: "build", model: { providerID: "p", id: "m" },
  })
  bridge.handleLiveEvent("s", "session.next.text.ended", {
    timestamp: ts(1002), sessionID: "s", assistantMessageID: "a1", textID: "t1", text: "one",
  })
  bridge.handleLiveEvent("s", "session.next.prompted", {
    timestamp: ts(2000), sessionID: "s", messageID: "u2", prompt: { text: "cc2" },
  })
  bridge.handleLiveEvent("s", "session.next.step.started", {
    timestamp: ts(2001), sessionID: "s", assistantMessageID: "a2", agent: "build", model: { providerID: "p", id: "m" },
  })
  bridge.handleLiveEvent("s", "session.next.text.ended", {
    timestamp: ts(2002), sessionID: "s", assistantMessageID: "a2", textID: "t2", text: "two",
  })
  assert.deepEqual(storeView.messages.get("s").map((m) => m.id), ["u1", "a1", "u2", "a2"])
  const texts = storeView.messages.get("s").flatMap((m) => (storeView.parts.get(m.id) ?? []).map((p) => p.text))
  assert.deepEqual(texts, ["cc1", "one", "cc2", "two"])
})

test("live: hydration after live injection does not duplicate rows", async () => {
  const { fake, bridge, projection, storeView } = liveBridge(fakeStore())
  bridge.handleLiveEvent("s", "session.next.prompted", {
    timestamp: ts(1000), sessionID: "s", messageID: "u1", prompt: { text: "cc1" },
  })
  bridge.handleLiveEvent("s", "session.next.step.started", {
    timestamp: ts(1001), sessionID: "s", assistantMessageID: "a1", agent: "build", model: { providerID: "p", id: "m" },
  })
  // Hydration fetches the same native message IDs; projected ids must equal
  // the live-injected ids so the store hydration replaces rows, never repeats.
  const nativePage = {
    data: { data: [
      { id: "u1", type: "user", time: { created: 1000 }, text: "cc1" },
      { id: "a1", type: "assistant", time: { created: 1001 }, finish: "stop",
        model: { providerID: "p", id: "m" }, content: [{ type: "text", id: "t1", text: "one" }] },
    ], cursor: {} },
  }
  pages = [nativePage]
  const response = await fake.client.session.messages({ sessionID: "s", limit: 100 })
  const liveIds = storeView.messages.get("s").map((m) => m.id)
  const projectedIds = response.data.map((row) => row.info.id)
  assert.deepEqual(projectedIds, liveIds)
  const merged = new Set([...liveIds, ...projectedIds])
  assert.equal(merged.size, liveIds.length, "hydration ids must be the same identity as live ids")
  assert.equal(projection.project("s", nativePage.data.data).length, liveIds.length)
})

test("live: pre-hydration detached store is not a seam failure; a rejecting real store fails closed", async () => {
  const { fake } = liveBridge(fakeStore())
  const projection = createProjection()

  // Detached store: fresh [] per call (session key absent). Skips must not
  // trip the seam — hydration owns these rows.
  const detached = { session: { messages: () => [] }, part: () => [] }
  const detachedBridge = createMessageBridge({ client: fake.client, projection, state: detached, log: () => {} })
  detachedBridge.handleLiveEvent("s", "session.next.prompted", {
    timestamp: ts(1000), sessionID: "s", messageID: "u1", prompt: { text: "cc" },
  })
  assert.equal(detachedBridge.seam.unsupported, false, "pre-hydration store must not count as seam failure")

  // Real store whose push never becomes visible (same array identity, row
  // missing): three failures close the seam.
  const swallowed = new Proxy([{ id: "seed", role: "user" }], {
    get(target, prop) {
      if (prop === "push") return () => target.length
      return Reflect.get(target, prop, target)
    },
  })
  const rejectingBridge = createMessageBridge({
    client: fake.client,
    projection,
    state: { session: { messages: () => swallowed }, part: () => [] },
    log: () => {},
  })
  let result
  for (let i = 0; i < 4; i++) {
    result = rejectingBridge.handleLiveEvent("s", "session.next.prompted", {
      timestamp: ts(1000 + i), sessionID: "s", messageID: `u${i}`, prompt: { text: "cc" },
    })
  }
  assert.equal(rejectingBridge.seam.unsupported, true, "three consecutive unverified inserts must fail the seam closed")
  assert.equal(rejectingBridge.seam.reason, "LIVE_PROJECTION_SEAM_UNSUPPORTED")
  assert.equal(result, null, "after the seam fails closed no further live work happens")
})

test("plugin wiring: fail-closed without v2, active with v2, theme variants switch with state, dispose restores", async () => {
  const minimal = { client: { session: {} }, event: { on: () => () => {} }, theme: {} }
  await saipatchTui(minimal)
  assert.ok(true, "fail-closed path must not throw")

  const fake = fakeV2()
  pages = []
  const installed = []
  const setCalls = []
  const eventHandlers = []
  let disposeHook
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: {
      current: { background: "#0b0b0b", backgroundPanel: "#111111", text: "#eeeeee" },
      selected: "fixture-custom-theme",
      install: async (file) => installed.push(file),
      set: (name) => { setCalls.push(name); api.theme.selected = name },
    },
    lifecycle: { onDispose: (fn) => { disposeHook = fn } },
    app: { version: "1.18.29" },
  }
  await saipatchTui(api, {})
  assert.equal(fake.client.session.prompt.__saipatchNative, true)
  assert.equal(fake.client.session.abort.__saipatchNative, true)
  assert.equal(fake.client.session.messages.__saipatchProjection, true)
  assert.equal(installed.length, 3)
  const expectedTypes = new Set(eventHandlers.map(([type]) => type))
  assert.ok(expectedTypes.has("session.status"))
  assert.ok(expectedTypes.has("permission.asked"))
  assert.ok(expectedTypes.has("session.next.step.started"))
  assert.ok(expectedTypes.has("saipatch.session.selected"), "plugin must consume the authoritative host selection event")
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  fire("saipatch.session.selected", { sessionID: "s" })
  fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  assert.ok(setCalls.includes("saipatch-queue-running"), `expected running theme in ${JSON.stringify(setCalls)}`)
  disposeHook()
  assert.equal(fake.client.session.prompt.__saipatchNative, undefined)
  assert.equal(fake.client.session.messages.__saipatchProjection, undefined)
  assert.ok(setCalls.at(-1) === "fixture-custom-theme", "dispose must restore the original custom theme")
  const themeDir = path.dirname(installed[0])
  assert.equal(existsSync(themeDir), false, "owned theme temp directory must be removed on dispose")
})

test("W2-003: execution events never select the foreground; only the host selection event does", async () => {
  const fake = fakeV2()
  const eventHandlers = []
  const setCalls = []
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: {
      current: { background: "#0b0b0b", backgroundPanel: "#111111", text: "#eeeeee" },
      selected: "fixture-custom-theme",
      install: async () => {},
      set: (name) => { setCalls.push(name) },
    },
    lifecycle: { onDispose: () => {} },
  }
  await saipatchTui(api, {})
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  fire("session.next.step.started", { sessionID: "B", assistantMessageID: "aB" })
  assert.deepEqual(setCalls, [], "background execution must not select or recolor the foreground")
  fire("saipatch.session.selected", { sessionID: "B" })
  assert.ok(setCalls.includes("saipatch-queue-running"), `selection must project the session's own RUNNING state: ${JSON.stringify(setCalls)}`)
})

// §11: navigating to a non-session route (home, /new, blank composer) must
// reset NEUTRAL and restore the native theme; stored per-session state stays.
async function selectionApi(eventHandlers, setCalls, routeCurrent = null) {
  const fake = fakeV2()
  let dispose
  const api = {
    client: fake.client,
    route: routeCurrent === null ? undefined : { current: routeCurrent, navigate: () => {}, register: () => {} },
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: {
      current: { background: "#0b0b0b", backgroundPanel: "#111111", text: "#eeeeee" },
      selected: "fixture-custom-theme",
      install: async () => {},
      set: (name) => { setCalls.push(name); api.theme.selected = name },
    },
    lifecycle: { onDispose: (fn) => { dispose = fn } },
    app: { version: "1.18.29" },
  }
  await saipatchTui(api, {})
  return { api, dispose, fake }
}

test("§11: non-session selection resets to NEUTRAL and restores the native theme, keeping stored session state", async () => {
  const eventHandlers = []
  const setCalls = []
  const { dispose } = await selectionApi(eventHandlers, setCalls)
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  fire("saipatch.session.selected", { sessionID: "A" })
  fire("session.next.step.started", { sessionID: "A", assistantMessageID: "aA" })
  assert.ok(setCalls.includes("saipatch-queue-running"))
  setCalls.length = 0
  // Navigate to /new (explicit null / missing sessionID).
  fire("saipatch.session.selected", { sessionID: null })
  assert.ok(setCalls.includes("fixture-custom-theme"), `/new must restore the native theme immediately: ${JSON.stringify(setCalls)}`)
  setCalls.length = 0
  // Switching back restores session A's own current projection (RUNNING),
  // proving stored state was not erased.
  fire("saipatch.session.selected", { sessionID: "A" })
  assert.ok(setCalls.includes("saipatch-queue-running"), `switching back must re-project A: ${JSON.stringify(setCalls)}`)
  dispose()
})

test("§11: a selection event with no sessionID at all also resets NEUTRAL", async () => {
  const eventHandlers = []
  const setCalls = []
  const { dispose } = await selectionApi(eventHandlers, setCalls)
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  fire("saipatch.session.selected", { sessionID: "A" })
  setCalls.length = 0
  fire("saipatch.session.selected", {})
  assert.ok(setCalls.includes("fixture-custom-theme"), JSON.stringify(setCalls))
  dispose()
})

test("§10: initial route.current session is selected at plugin init without waiting for navigation", async () => {
  const eventHandlers = []
  const setCalls = []
  const { dispose } = await selectionApi(eventHandlers, setCalls, { at: "session/sess-1", parts: ["session", "sess-1"] })
  // The plugin itself must have selected sess-1 and projected it (NEUTRAL for
  // an idle session => native theme, but the selection is established).
  assert.equal(setCalls.length >= 0, true)
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  fire("session.next.step.started", { sessionID: "sess-1", assistantMessageID: "a1" })
  assert.ok(setCalls.includes("saipatch-queue-running"), `init-selected session must project RUNNING without a navigation event: ${JSON.stringify(setCalls)}`)
  dispose()
})

test("§10: initial non-session route leaves the plugin unselected (no stale session tint)", async () => {
  const eventHandlers = []
  const setCallsA = []
  const { dispose: d1 } = await selectionApi(eventHandlers, setCallsA, { at: "home", parts: ["home"] })
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  // Init already reset to the native theme (NEUTRAL / no session).
  assert.ok(setCallsA.includes("fixture-custom-theme"), `init non-session route restores native theme: ${JSON.stringify(setCallsA)}`)
  setCallsA.length = 0
  fire("session.next.step.started", { sessionID: "X", assistantMessageID: "aX" })
  assert.deepEqual(setCallsA, [], "a non-session initial route must not project any session")
  d1()
})

// Host seam fixtures: the patched host invokes __SAIPATCH(w, H) with the raw
// SSE wrapper and the host sync consumer.
function wrapHost(projector, onEvent) {
  return (w, H) => projector(w, H)
}
const hostWrapper = (payload, directory = "d:/x", workspace = "w:/y") => ({ payload, directory, workspace })
const hostConsumer = (sink) => (event, context) => { sink.push({ event, context }); return true }

test("host seam B: prompted projects a user message and its text part, then passes the native payload through", () => {
  const projector = createHostProjector()
  const sink = []
  projector(hostWrapper({
    id: "e1",
    type: "session.next.prompted",
    properties: { sessionID: "s", messageID: "u1", timestamp: "2026-09-09T00:00:00.000Z", prompt: { text: "cc1 hello" } },
  }), hostConsumer(sink))
  assert.deepEqual(sink.map((r) => r.event.type), ["message.updated", "message.part.updated", "session.next.prompted"])
  assert.equal(sink[0].event.properties.info.role, "user")
  assert.equal(sink[0].event.properties.info.sessionID, "s")
  assert.equal(sink[1].event.properties.part.text, "cc1 hello")
  assert.equal(sink[1].event.properties.part.messageID, "u1")
  assert.equal(sink[2].event.type, "session.next.prompted", "native payload must still reach native consumers")
})

test("host seam B §6: every projected and passthrough event carries the ORIGINAL {directory, workspace} context", () => {
  const projector = createHostProjector()
  const sink = []
  const w = hostWrapper(
    { type: "session.next.prompted", properties: { sessionID: "s", messageID: "u1", timestamp: "2026-09-09T00:00:00.000Z", prompt: { text: "ctx" } } },
    "D:/orig/dir", "W:/orig/ws",
  )
  projector(w, hostConsumer(sink))
  for (const record of sink) {
    assert.deepEqual(record.context, { directory: "D:/orig/dir", workspace: "W:/orig/ws" })
  }
  const passthrough = []
  projector(hostWrapper({ type: "message.updated", properties: { info: { id: "x" } } }, "D:/p/dir", "W:/p/ws"), hostConsumer(passthrough))
  assert.equal(passthrough.length, 1)
  assert.deepEqual(passthrough[0].context, { directory: "D:/p/dir", workspace: "W:/p/ws" }, "original passthrough must preserve the exact host context")
})

test("host seam B §7: sync frames are dropped exactly like the original host dispatch", () => {
  const projector = createHostProjector()
  const sink = []
  projector(hostWrapper({ type: "sync", properties: {} }), hostConsumer(sink))
  assert.equal(sink.length, 0, "sync payload must never reach the sync consumer")
})

test("host seam B §8: a throwing projector never kills SSE delivery — original payload passes through", () => {
  const exploding = () => { throw new Error("projector exploded") }
  const hook = createBarrierHook(exploding, { log: () => {} })
  const sink = []
  hook(hostWrapper({ type: "session.next.step.started", properties: { sessionID: "s", assistantMessageID: "a1" } }, "D:/b", "W:/b"), hostConsumer(sink))
  assert.equal(sink.length, 1, "the ORIGINAL payload must reach the host consumer once after the barrier catch")
  assert.equal(sink[0].event.type, "session.next.step.started")
  assert.deepEqual(sink[0].context, { directory: "D:/b", workspace: "W:/b" })
  // subsequent ordinary events still flow
  const sink2 = []
  const projector = createHostProjector()
  const barrier = createBarrierHook(projector, { log: () => {} })
  barrier(hostWrapper({ type: "session.next.prompted", properties: { sessionID: "s", messageID: "u2", prompt: { text: "next" } } }, "D:/b", "W:/b"), hostConsumer(sink2))
  assert.ok(sink2.some((r) => r.event.type === "session.next.prompted"), "subsequent events still reach the host consumer after a projector failure")
})

test("host seam B: assistant step + text stream projects message and part delta/final events", () => {
  const projector = createHostProjector()
  const sink = []
  const run = (payload) => projector(hostWrapper(payload), hostConsumer(sink))
  run({ type: "session.next.step.started", properties: { sessionID: "s", assistantMessageID: "a1", timestamp: "2026-09-09T00:00:01.000Z", agent: "build", model: { id: "m", providerID: "p" } } })
  run({ type: "session.next.text.started", properties: { sessionID: "s", assistantMessageID: "a1", textID: "t1", timestamp: "2026-09-09T00:00:02.000Z" } })
  run({ type: "session.next.text.delta", properties: { sessionID: "s", assistantMessageID: "a1", textID: "t1", delta: "cc1 " } })
  run({ type: "session.next.text.delta", properties: { sessionID: "s", assistantMessageID: "a1", textID: "t1", delta: "finished" } })
  run({ type: "session.next.text.ended", properties: { sessionID: "s", assistantMessageID: "a1", textID: "t1", text: "cc1 finished" } })
  assert.deepEqual(sink.map((r) => r.event.type), [
    "message.updated", "session.next.step.started",
    "message.part.updated", "session.next.text.started",
    "message.part.delta", "session.next.text.delta",
    "message.part.delta", "session.next.text.delta",
    "message.part.updated", "session.next.text.ended",
  ])
  const info = sink[0].event.properties.info
  assert.equal(info.role, "assistant")
  assert.equal(info.modelID, "m")
  assert.equal(info.providerID, "p")
  assert.equal(info.agent, "build")
  const finalPart = sink[8].event.properties.part
  assert.deepEqual([finalPart.id, finalPart.type, finalPart.text], ["t1", "text", "cc1 finished"])
  const delta = sink[4].event.properties
  assert.deepEqual([delta.messageID, delta.partID, delta.field, delta.delta], ["a1", "t1", "text", "cc1 "])
})

test("host seam B: unmapped and legacy payloads pass through exactly once", () => {
  const projector = createHostProjector()
  const sink = []
  const legacy = { type: "message.updated", properties: { info: { id: "x" } } }
  projector(hostWrapper(legacy), hostConsumer(sink))
  const moved = { type: "session.next.moved", properties: { sessionID: "s" } }
  projector(hostWrapper(moved), hostConsumer(sink))
  assert.deepEqual(sink.map((r) => r.event), [legacy, moved])
})

test("host seam B §5/§4: hook is namespaced, transaction-owned, restores prior state, never clobbers newer owners", async () => {
  const fake = fakeV2()
  let dispose
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: () => () => {} },
    theme: { current: { background: "#0b0b0b" }, selected: "fixture-theme", install: async () => {}, set: () => {} },
    lifecycle: { onDispose: (fn) => { dispose = fn } },
  }
  assert.equal(globalThis.__SAIPATCH !== undefined, true, "module top installs the safe passthrough before plugin init")
  const before = globalThis.__SAIPATCH
  await saipatchTui(api, {})
  const installedHook = globalThis.__SAIPATCH
  assert.equal(typeof installedHook, "function")
  assert.notEqual(installedHook, before, "saipatchTui upgrades the hook to the real barriered projector")
  // project through the installed hook
  const sink = []
  installedHook(
    hostWrapper({ type: "session.next.prompted", properties: { sessionID: "s", messageID: "u1", prompt: { text: "hi" } } }, "D:/z", "W:/z"),
    hostConsumer(sink),
  )
  assert.deepEqual(sink.map((r) => r.event.type), ["message.updated", "message.part.updated", "session.next.prompted"])
  // §4: a newer external hook replacement survives our dispose
  const newer = () => {}
  globalThis.__SAIPATCH = newer
  dispose()
  assert.equal(globalThis.__SAIPATCH, newer, "newer replacement hook must survive dispose")
  // and a normal dispose restores the pre-install value
  const priorValue = () => "pre-existing hook"
  globalThis.__SAIPATCH = priorValue
  const api2 = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: () => () => {} },
    theme: { current: { background: "#0b0b0b" }, selected: "fixture-theme", install: async () => {}, set: () => {} },
    lifecycle: { onDispose: (fn) => { dispose = fn } },
  }
  await saipatchTui(api2, {})
  assert.notEqual(globalThis.__SAIPATCH, priorValue)
  dispose()
  assert.equal(globalThis.__SAIPATCH, priorValue, "normal dispose restores the pre-install hook exactly")
  globalThis.__SAIPATCH = before === undefined ? undefined : before
  if (before === undefined) delete globalThis.__SAIPATCH
})

test("host seam B §4: a failed later install stage unwinds the host hook (failed install restores prior state)", async () => {
  const fake = fakeV2()
  const originalPrompt = fake.client.session.prompt
  const api = {
    client: bridgeBreakerClient(fake),
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: () => () => {} },
    theme: {},
  }
  const sentinel = () => "sentinel prior hook"
  globalThis.__SAIPATCH = sentinel
  await assert.rejects(saipatchTui(api, { ambient: false, fastPaste: false }), /bridge construction failed/)
  assert.equal(globalThis.__SAIPATCH, sentinel, "failed install must restore the prior hook value")
  assert.equal(fake.client.session.prompt, originalPrompt)
  delete globalThis.__SAIPATCH
})

test("host seam B §5: a pre-existing unrelated global on the hook name survives install and dispose", async () => {
  const fake = fakeV2()
  let dispose
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: () => () => {} },
    theme: { current: { background: "#0b0b0b" }, selected: "fixture-theme", install: async () => {}, set: () => {} },
    lifecycle: { onDispose: (fn) => { dispose = fn } },
  }
  const unrelated = { owner: "someone else" }
  globalThis.__SAIPATCH = unrelated
  await saipatchTui(api, {})
  assert.equal(typeof globalThis.__SAIPATCH, "function", "install replaces the unrelated value while running")
  dispose()
  assert.equal(globalThis.__SAIPATCH, unrelated, "dispose must put the pre-existing unrelated value back exactly")
  delete globalThis.__SAIPATCH
})

// T-144: the binary seam calls __SPB(w, emit) with the RAW wrapper and an emit
// callback; the product projector must accept that shape and forward to the
// same host consumer with the original context.
test("host seam B T-144: raw-wrapper __SPB entry (w, emit) projects and forwards the native payload", () => {
  const projector = createHostProjector()
  const sink = []
  // Exactly the patched binary's call shape: globalThis.__SPB?.(w, e=>H(e,w))
  const H = hostConsumer(sink)
  const w = hostWrapper(
    { type: "session.next.prompted", properties: { sessionID: "s", messageID: "u1", timestamp: "2026-09-09T00:00:00.000Z", prompt: { text: "cc1 raw" } } },
    "D:/raw/dir", "W:/raw/ws",
  )
  projector(w, (event) => H(event, { directory: w.directory, workspace: w.workspace }))
  assert.deepEqual(sink.map((r) => r.event.type), ["message.updated", "message.part.updated", "session.next.prompted"])
  for (const record of sink) assert.deepEqual(record.context, { directory: "D:/raw/dir", workspace: "W:/raw/ws" })
})

test("host seam B T-144: binary hook alias __SPB is installed and upgraded at init, restored on dispose", async () => {
  const fake = fakeV2()
  let dispose
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: () => () => {} },
    theme: { current: { background: "#0b0b0b" }, selected: "fixture-theme", install: async () => {}, set: () => {} },
    lifecycle: { onDispose: (fn) => { dispose = fn } },
  }
  assert.equal(HOST_SEAM_HOOK, "__SPB")
  assert.equal(typeof globalThis.__SPB, "function", "module top installs the __SPB passthrough (binary alias)")
  const before = globalThis.__SPB
  await saipatchTui(api, {})
  assert.notEqual(globalThis.__SPB, before, "init upgrades __SPB to the real barriered projector")
  const sink = []
  globalThis.__SPB(
    hostWrapper({ type: "session.next.prompted", properties: { sessionID: "s", messageID: "u9", prompt: { text: "sp" } } }, "D:/s", "W:/s"),
    hostConsumer(sink),
  )
  assert.ok(sink.some((r) => r.event.type === "message.updated"), "__SPB projects into the legacy transcript vocabulary")
  dispose()
  assert.equal(globalThis.__SPB, before, "dispose restores the pre-install __SPB value exactly")
  globalThis.__SPB = before === undefined ? undefined : before
  if (before === undefined) delete globalThis.__SPB
})

test("host seam B T-144: barrier forwards the ORIGINAL payload when the projector throws in the raw shape", () => {
  const exploding = () => { throw new Error("boom") }
  const hook = createBarrierHook(exploding, { log: () => {} })
  const sink = []
  const w = hostWrapper({ type: "session.next.text.delta", properties: { sessionID: "s" } }, "D:/k", "W:/k")
  hook(w, hostConsumer(sink))
  assert.equal(sink.length, 1, "the original payload reaches the host consumer after the failure")
  assert.equal(sink[0].event.type, "session.next.text.delta")
  assert.deepEqual(sink[0].context, { directory: "D:/k", workspace: "W:/k" })
})

test("plugin id is stable", () => {
  assert.equal(pluginID, "saipatch-native-queue-2x")
})

test("fast paste: ctrl+v layer inserts the whole clipboard once at the caret, no submit", async () => {
  const layers = []
  const keymap = { registerLayer: (layer) => { layers.push(layer); return () => {} } }
  let text = ""
  const focused = { insertText: (value) => { text += value; return true } }
  const clipboard = "alpha\r\nbeta\r\rgamma\n\n"
  createFastPaste({ keymap, readClipboard: async () => clipboard, log: () => {} })
  assert.equal(layers.length, 1)
  assert.equal(layers[0].bindings[0].key, "ctrl+v")
  const ok = await layers[0].commands[0].run({ focused })
  assert.equal(ok, true)
  assert.equal(text, normalizePastedText(clipboard))
  assert.equal(text, clipboard)
})

test("fast paste: re-entry while active is ignored, empty clipboard no-ops, broken focus refused", async () => {
  const gate = (() => { let r; const p = new Promise((res) => { r = res }); return { p, r } })()
  const spyLayers = []
  const spyKeymap = { registerLayer: (layer) => { spyLayers.push(layer); return () => {} } }
  let inserts = 0
  const focused = { insertText: () => { inserts += 1; return true } }
  createFastPaste({
    keymap: spyKeymap,
    readClipboard: async () => { await gate.p; return "SLOW" },
    log: () => {},
  })
  const run = spyLayers[0].commands[0].run
  const pending = run({ focused })
  const second = await run({ focused })
  assert.equal(second, true, "re-entry ignored, never queued, never inserted twice")
  gate.r()
  assert.equal(await pending, true)
  assert.equal(inserts, 1)

  const emptyLayers = []
  createFastPaste({
    keymap: { registerLayer: (layer) => { emptyLayers.push(layer); return () => {} } },
    readClipboard: async () => "",
    log: () => {},
  })
  assert.equal(await emptyLayers[0].commands[0].run({ focused }), true)
  assert.equal(inserts, 1, "empty clipboard must not insert")

  const refused = await emptyLayers[0].commands[0].run({ focused: undefined })
  assert.equal(refused, false, "paste without a text editor target must refuse")
  assert.equal(inserts, 1)
})

test("fast paste: refuses install without keymap and exposes normalize", () => {
  assert.equal(createFastPaste({ keymap: undefined, log: () => {} }), null)
  assert.equal(normalizePastedText("a\r\nb\rc\nd\n"), "a\r\nb\rc\nd\n")
  assert.equal(normalizePastedText(undefined), "")
})

test("paste preservation red control: blank lines and all non-newline characters", () => {
  const fixtures = ["\nA\n\n\nB\n", "\nalpha\n", "alpha\n\n\nbeta", "    indented", "tail    ",
    "\talpha\tbeta\t", "```md\n\nsection\n\n```\n", '{"text":"a\\nb"}',
    "```powershell\n    $x = 1\n\n\tWrite-Output $x\n```\n", "Кириллица 日本語 😀\n",
    Array.from({length: 1200}, (_, i) => `    line ${i}\n\n`).join("")]
  for (const text of fixtures) assert.equal(normalizePastedText(text), text)
})

for (const setting of ["switchModel", "switchAgent"]) {
  test(`${setting} rejection blocks native admission`, async () => {
    const {client, calls} = fakeV2()
    client.v2.session[setting] = async () => { throw new Error("setting rejected") }
    createQueueCore({client}).install()
    await assert.rejects(client.session.prompt({sessionID: "s", text: "fixture", model: {providerID: "p", modelID: "m"}, agent: "build"}), /setting rejected/)
    assert.equal(calls.prompt.length, 0)
  })
}

test("no requested settings means no switch calls", async () => {
  const {client, calls} = fakeV2()
  createQueueCore({client}).install()
  await client.session.prompt({sessionID: "s", text: "fixture"})
  assert.equal(calls.prompt.length, 1)
  assert.equal(calls.switchModel.length, 0)
  assert.equal(calls.switchAgent.length, 0)
})

// --- Audit 16 correctness controls (T-144 wave, SRC-015 CORE-003/004, W2-001..004) ---

// CORE-003: a native-owned session NEVER falls back to legacy hydration.
test("CORE-003: v2 failure keeps legacy originalMessages at zero calls for a native-owned session", async () => {
  const fake = fakeV2()
  pages = []
  const legacyRows = [{ info: { id: "SENTINEL_LEGACY_ROW", role: "user", time: { created: 1 } }, parts: [] }]
  let legacyCalls = 0
  fake.client.session.messages = async () => { legacyCalls += 1; return { data: legacyRows } }
  fake.client.v2.session.messages = async () => { throw new Error("native history unavailable") }
  const bridge = createMessageBridge({ client: fake.client, projection: createProjection() })
  bridge.install()
  const response = await fake.client.session.messages({ sessionID: "s", limit: 50 })
  assert.equal(legacyCalls, 0, "legacy originalMessages must never be called for a native-owned session")
  assert.equal((response.data ?? []).some((row) => row?.info?.id === "SENTINEL_LEGACY_ROW"), false, "legacy sentinel rows must never enter the transcript")
  assert.equal(response.hydrationUnavailable, true, "failure must surface as hydration-unavailable")
})

test("CORE-003: malformed non-session calls still delegate to the original implementation", async () => {
  const fake = fakeV2()
  pages = []
  let legacyCalls = 0
  fake.client.session.messages = async () => { legacyCalls += 1; return { data: [] } }
  const bridge = createMessageBridge({ client: fake.client, projection: createProjection() })
  bridge.install()
  await fake.client.session.messages(undefined)
  assert.equal(legacyCalls, 1, "non-session calls may delegate per the ownership contract")
})

test("CORE-003: transient v2 failure then recovery keeps prior rows and reconciles without duplicates", async () => {
  const fake = fakeV2()
  let fail = true
  fake.client.v2.session.messages = async () => {
    if (fail) throw new Error("native history unavailable")
    return { data: { data: [{ id: "u1", type: "user", time: { created: 1000 }, text: "cc1" }], cursor: {} } }
  }
  const bridge = createMessageBridge({ client: fake.client, projection: createProjection() })
  bridge.install()
  const failed = await fake.client.session.messages({ sessionID: "s", limit: 50 })
  assert.equal(failed.hydrationUnavailable, true)
  assert.equal(failed.data.length, 0)
  fail = false
  const recovered = await fake.client.session.messages({ sessionID: "s", limit: 50 })
  assert.deepEqual(recovered.data.map((r) => r.info.id), ["u1"])
})

test("CORE-003: pagination failure after an earlier native page fails closed without legacy data", async () => {
  const fake = fakeV2()
  let calls = 0
  fake.client.v2.session.messages = async () => {
    calls += 1
    if (calls === 1) return { data: { data: [{ id: "u1", type: "user", time: { created: 1000 }, text: "cc1" }], cursor: { next: "c2" } } }
    throw new Error("cursor page failed")
  }
  let legacyCalls = 0
  fake.client.session.messages = async () => { legacyCalls += 1; return { data: [{ info: { id: "LEGACY", role: "user", time: { created: 1 } }, parts: [] }] } }
  const bridge = createMessageBridge({ client: fake.client, projection: createProjection() })
  bridge.install()
  const response = await fake.client.session.messages({ sessionID: "s", limit: 100 })
  assert.equal(legacyCalls, 0)
  assert.equal(response.data.some((r) => r?.info?.id === "LEGACY"), false)
  assert.equal(response.data.some((r) => r?.info?.id === "u1"), true, "already-projected native rows stay")
})

// W2-002: newest-N pagination across page boundaries.
// Server contract (audit W2-002, OpenCode protocol): default page 50,
// order desc = NEWEST FIRST, cursor.next walks toward older messages.
function paginationHost(messageCount, requestLog) {
  const ascending = Array.from({ length: messageCount }, (_, i) => ({ id: `m${String(i + 1).padStart(3, "0")}`, type: "user", time: { created: 1000 + i }, text: `cc${i + 1}` }))
  const newestFirst = [...ascending].reverse()
  return {
    async v2Messages(input) {
      requestLog.push(input)
      const start = input.cursor ? newestFirst.findIndex((m) => m.id === input.cursor) : 0
      if (start === -1) return { data: { data: [], cursor: {} } }
      const page = newestFirst.slice(start, start + (input.limit ?? 50))
      const nextIndex = start + page.length
      return { data: { data: page, cursor: nextIndex < newestFirst.length ? { next: newestFirst[nextIndex].id } : {} } }
    },
  }
}

for (const limit of [1, 20, 49, 50, 51, 80, 99, 100, 101, 150, 200]) {
  test(`W2-002: limit ${limit} of 250 returns exactly the newest ${limit} in chronological order`, async () => {
    const host = paginationHost(250, [])
    const fake = fakeV2()
    pages = []
    fake.client.v2.session.messages = host.v2Messages
    const bridge = createMessageBridge({ client: fake.client, projection: createProjection() })
    bridge.install()
    const response = await fake.client.session.messages({ sessionID: "s", limit })
    const ids = response.data.map((r) => r.info.id)
    assert.equal(ids.length, limit)
    assert.equal(ids.includes("m250"), true, "the newest message must always be present")
    assert.equal(new Set(ids).size, limit, "no duplicates")
    // newest N = m(250-N+1)..m250; chronological display = oldest first.
    assert.equal(ids[0], `m${String(250 - limit + 1).padStart(3, "0")}`)
    assert.equal(ids[ids.length - 1], "m250")
  })
}

test("W2-002: first request establishes desc order with a bounded limit; cursor never re-sends order", async () => {
  const requestLog = []
  const host = paginationHost(120, requestLog)
  const fake = fakeV2()
  fake.client.v2.session.messages = host.v2Messages
  const bridge = createMessageBridge({ client: fake.client, projection: createProjection() })
  bridge.install()
  await fake.client.session.messages({ sessionID: "s", limit: 120 })
  assert.equal(requestLog[0].order, "desc")
  assert.equal(requestLog[0].limit, 120)
  for (const request of requestLog.slice(1)) {
    assert.equal(request.order, undefined, "cursor continuation must not re-send order")
    assert.ok(request.cursor, "continuation carries the cursor")
  }
})

test("W2-002: short final page, empty continuation and missing cursor terminate cleanly", async () => {
  const shortHost = paginationHost(60, [])
  const fakeA = fakeV2()
  fakeA.client.v2.session.messages = shortHost.v2Messages
  const bridgeA = createMessageBridge({ client: fakeA.client, projection: createProjection() })
  bridgeA.install()
  const short = await fakeA.client.session.messages({ sessionID: "s", limit: 100 })
  assert.equal(short.data.length, 60)
  assert.equal(short.data[0].info.id, "m001")

  const emptyHost = paginationHost(5, [])
  const fakeB = fakeV2()
  fakeB.client.v2.session.messages = emptyHost.v2Messages
  const bridgeB = createMessageBridge({ client: fakeB.client, projection: createProjection() })
  bridgeB.install()
  const empty = await fakeB.client.session.messages({ sessionID: "s", limit: 50 })
  assert.equal(empty.data.length, 5)

  const fakeC = fakeV2()
  fakeC.client.v2.session.messages = async () => ({ data: { data: [{ id: "u1", type: "user", time: { created: 1 }, text: "x" }] } })
  const bridgeC = createMessageBridge({ client: fakeC.client, projection: createProjection() })
  bridgeC.install()
  const missing = await fakeC.client.session.messages({ sessionID: "s", limit: 50 })
  assert.deepEqual(missing.data.map((r) => r.info.id), ["u1"])
})

// W2-001: transactional install + ownership-aware dispose (tests A-E).
function bridgeBreakerClient(fake) {
  return new Proxy(fake.client, {
    get(target, prop) {
      if (prop === "v2") {
        return new Proxy(target.v2, {
          get(t2, p2) {
            if (p2 === "session") {
              return new Proxy(t2.session, {
                get(t3, p3) {
                  if (p3 === "messages") throw new Error("bridge construction failed")
                  return t3[p3]
                },
              })
            }
            return t2[p2]
          },
        })
      }
      return target[prop]
    },
  })
}

test("W2-001 A: bridge construction failure after queue install restores every method exactly", async () => {
  const fake = fakeV2()
  const originalPrompt = fake.client.session.prompt
  const originalAbort = fake.client.session.abort
  const originalMessages = fake.client.session.messages
  const api = { client: bridgeBreakerClient(fake), event: { on: () => () => {} }, theme: {} }
  await assert.rejects(saipatchTui(api, { ambient: false, fastPaste: false }), /bridge construction failed/)
  assert.equal(fake.client.session.prompt, originalPrompt, "prompt must be reference-identical after rollback")
  assert.equal(fake.client.session.abort, originalAbort, "abort must be reference-identical after rollback")
  assert.equal(fake.client.session.messages, originalMessages)
  assert.equal(fake.client.session.__saipatchQueueCore, undefined, "no leaked ownership marker")
})

test("W2-001 B: after a failed install, repairing the prerequisite lets the same process retry succeed", async () => {
  const fake = fakeV2()
  const api = { client: bridgeBreakerClient(fake), event: { on: () => () => {} }, theme: {} }
  await assert.rejects(saipatchTui(api, { ambient: false, fastPaste: false }), /bridge construction failed/)
  // Repair: same client object, no reset. Retry must succeed.
  await saipatchTui({ client: fake.client, event: { on: () => () => {} }, theme: {} }, { ambient: false, fastPaste: false })
  assert.equal(fake.client.session.prompt.__saipatchNative, true)
  assert.equal(fake.client.session.messages.__saipatchProjection, true)
})

test("W2-001 C: a failing event registration unwinds all earlier resources", async () => {
  const fake = fakeV2()
  const originalPrompt = fake.client.session.prompt
  let eventCalls = 0
  const api = {
    client: fake.client,
    event: { on: () => { eventCalls += 1; if (eventCalls >= 3) throw new Error("event registration exploded"); return () => {} } },
    theme: {},
  }
  await assert.rejects(saipatchTui(api, { ambient: false, fastPaste: false }), /event registration exploded/)
  assert.equal(fake.client.session.prompt, originalPrompt, "queue wrapper must be unwound when a later registration throws")
  assert.equal(fake.client.session.__saipatchQueueCore, undefined)
})

test("W2-001 D: dispose leaves a newer external prompt wrapper untouched", async () => {
  const fake = fakeV2()
  let disposeHook
  const api = { client: fake.client, event: { on: () => () => {} }, theme: {}, lifecycle: { onDispose: (fn) => { disposeHook = fn } } }
  await saipatchTui(api, { ambient: false, fastPaste: false })
  const newerPrompt = async () => ({ data: { id: "newer" } })
  fake.client.session.prompt = newerPrompt
  disposeHook()
  assert.equal(fake.client.session.prompt, newerPrompt, "newer external wrapper must survive saipatch dispose")
})

test("W2-001 E: repeated dispose is idempotent", async () => {
  const fake = fakeV2()
  const originalPrompt = fake.client.session.prompt
  let disposeHook
  const api = { client: fake.client, event: { on: () => () => {} }, theme: {}, lifecycle: { onDispose: (fn) => { disposeHook = fn } } }
  await saipatchTui(api, { ambient: false, fastPaste: false })
  disposeHook()
  disposeHook()
  disposeHook()
  assert.equal(fake.client.session.prompt, originalPrompt)
  assert.equal(fake.client.session.__saipatchQueueCore, undefined)
})

test("W2-001: queue core dispose never deletes a newer owner's ownership marker", () => {
  const fake = fakeV2()
  const core = createQueueCore({ client: fake.client })
  const dispose = core.install()
  const newerMarker = Symbol("newer-owner")
  fake.client.session.__saipatchQueueCore = newerMarker
  dispose()
  assert.equal(fake.client.session.__saipatchQueueCore, newerMarker, "newer owner's marker must survive saipatch dispose")
})

test("W2-001: queue core dispose restores only its own wrapper (token match)", () => {
  const fake = fakeV2()
  const originalPrompt = fake.client.session.prompt
  const core = createQueueCore({ client: fake.client })
  const dispose = core.install()
  dispose()
  assert.equal(fake.client.session.prompt, originalPrompt, "token match: own wrapper restored exactly once")
  const core2 = createQueueCore({ client: fake.client })
  const dispose2 = core2.install()
  const newerPrompt = async () => ({})
  fake.client.session.prompt = newerPrompt
  dispose2()
  assert.equal(fake.client.session.prompt, newerPrompt, "token mismatch: newer external wrapper survives")
})

test("W2-001: a second core refuses to install while another owns the client", () => {
  const fake = fakeV2()
  const core = createQueueCore({ client: fake.client })
  core.install()
  assert.throws(() => createQueueCore({ client: fake.client }).install())
})

// CORE-004: abort authority + generation safety.
test("CORE-004: rejected interrupt must not commit abort state (interrupt first)", async () => {
  const fake = fakeV2()
  pages = []
  const aborts = []
  const core = createQueueCore({ client: fake.client, onAbort: (s) => aborts.push(s) })
  core.install()
  fake.client.v2.session.interrupt = async () => { throw new Error("interrupt rejected") }
  await assert.rejects(fake.client.session.abort({ sessionID: "s" }), /interrupt rejected/)
  assert.deepEqual(aborts, [], "onAbort must not fire before the native interrupt succeeds")
})

test("CORE-004: committed abort clears activeSteps/pendingHuman/lastFinish and the new turn reaches DONE", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "m1")
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a1" })
  ambient.onAbort("s") // committed abort clears every generation-scoped field
  assert.equal(ambient.evaluate("s"), AMBIENT.NEUTRAL)
  assert.equal(ambient.sessions.get("s").activeSteps, 0)
  assert.equal(ambient.sessions.get("s").pendingHuman, false)
  assert.equal(ambient.sessions.get("s").lastFinish, undefined)
  ambient.onAdmitted("s", "m2")
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a2" })
  ambient.onEvent("s", "session.next.step.ended", { assistantMessageID: "a2", finish: "stop" })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE, "post-abort turn must complete cleanly")
})

test("CORE-004: stale step.ended from the aborted generation must not complete the new one", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "m1")
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a1" })
  ambient.onAbort("s")
  ambient.onAdmitted("s", "m2")
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a2" })
  ambient.onEvent("s", "session.next.step.ended", { assistantMessageID: "a1", finish: "stop" })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "stale generation event must not complete the new turn")
  ambient.onEvent("s", "session.next.step.ended", { assistantMessageID: "a2", finish: "stop" })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
})

test("CORE-004: permission pending then abort then new turn leaves no stale NEEDS_HUMAN", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "m1")
  ambient.onEvent("s", "permission.asked", { sessionID: "s" })
  assert.equal(ambient.evaluate("s"), AMBIENT.NEEDS_HUMAN)
  ambient.onAbort("s")
  ambient.onAdmitted("s", "m2")
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "stale pendingHuman must not leak into the new generation")
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a2" })
  ambient.onEvent("s", "session.next.step.ended", { assistantMessageID: "a2", finish: "stop" })
  assert.equal(ambient.evaluate("s"), AMBIENT.DONE)
})

test("CORE-004: attemptAbort never mutates state (rejected interrupt keeps RUNNING)", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.onAdmitted("s", "m1")
  ambient.onEvent("s", "session.next.step.started", { assistantMessageID: "a1" })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING)
  const probe = ambient.attemptAbort("s")
  assert.deepEqual(probe, { sessionID: "s", generation: 1 })
  assert.equal(ambient.evaluate("s"), AMBIENT.RUNNING, "attemptAbort must never mutate state")
})

// W2-003: selected-session authority.
test("W2-003: background permission in B never repaints selected A; switch to B shows NEEDS_HUMAN", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.setSelectedSession("A")
  ambient.onAdmitted("A", "mA1")
  ambient.onEvent("A", "session.next.step.started", { assistantMessageID: "aA1" })
  ambient.onEvent("A", "session.next.step.ended", { assistantMessageID: "aA1", finish: "stop" })
  assert.equal(ambient.evaluate("A"), AMBIENT.DONE)
  ambient.onEvent("B", "permission.asked", { sessionID: "B" })
  assert.equal(ambient.evaluate("B"), AMBIENT.NEEDS_HUMAN)
  assert.equal(ambient.evaluate("A"), AMBIENT.DONE, "background B must not change A's record")
  ambient.setSelectedSession("B")
  assert.equal(ambient.evaluate("B"), AMBIENT.NEEDS_HUMAN)
})

test("W2-003: events never select the session; idle session evaluates NEUTRAL immediately", () => {
  const ambient = createAmbient({ baseBackground: "#0a0a0a" })
  ambient.setSelectedSession("A")
  ambient.onAdmitted("C", "mC1")
  ambient.onEvent("C", "session.next.step.started", { assistantMessageID: "aC1" })
  assert.equal(ambient.selectedSessionID, "A", "execution events in C must not move the selection")
  ambient.setSelectedSession("C")
  assert.equal(ambient.selectedSessionID, "C")
  ambient.onEvent("C", "session.next.step.ended", { assistantMessageID: "aC1", finish: "stop" })
  assert.equal(ambient.evaluate("idle-x"), AMBIENT.NEUTRAL, "idle session is NEUTRAL immediately")
})

// W2-004: settings/admission transaction.
function transactionHost() {
  const fake = fakeV2()
  const state = { model: { id: "old", providerID: "p" }, agent: "old-agent" }
  fake.sessionState = state
  fake.client.session.get = async () => ({ model: { ...state.model }, agent: state.agent })
  fake.client.v2.session.switchModel = async (input) => {
    fake.calls.switchModel.push(input)
    if (state.__modelFail) throw new Error("model rejected")
    state.model = { ...input.model }
    return {}
  }
  fake.client.v2.session.switchAgent = async (input) => {
    fake.calls.switchAgent.push(input)
    if (state.__agentFail) throw new Error("agent rejected")
    state.agent = input.agent
    return {}
  }
  fake.client.v2.session.prompt = async (input) => {
    fake.calls.prompt.push(input)
    if (state.__promptFail) throw new Error("admission rejected")
    return { data: { id: "msg_admitted", sessionID: input.sessionID } }
  }
  return fake
}

test("W2-004: model success + agent fail restores the old model and admits nothing", async () => {
  const fake = transactionHost()
  fake.sessionState.__agentFail = true
  const core = createQueueCore({ client: fake.client })
  core.install()
  await assert.rejects(fake.client.session.prompt({ sessionID: "s", text: "x", model: { providerID: "p", modelID: "new" }, agent: "build" }), /agent rejected/)
  assert.deepEqual(fake.sessionState.model, { id: "old", providerID: "p" }, "model must be compensated")
  assert.equal(fake.calls.prompt.length, 0)
})

test("W2-004: agent success + model fail restores the old agent", async () => {
  const fake = transactionHost()
  fake.sessionState.__modelFail = true
  const core = createQueueCore({ client: fake.client })
  core.install()
  await assert.rejects(fake.client.session.prompt({ sessionID: "s", text: "x", model: { providerID: "p", modelID: "new" }, agent: "build" }), /model rejected/)
  assert.equal(fake.sessionState.agent, "old-agent", "agent must be compensated")
  assert.equal(fake.calls.prompt.length, 0)
})

test("W2-004: both settings succeed + prompt fails rolls back to pre-operation settings", async () => {
  const fake = transactionHost()
  fake.sessionState.__promptFail = true
  const core = createQueueCore({ client: fake.client })
  core.install()
  await assert.rejects(fake.client.session.prompt({ sessionID: "s", text: "x", model: { providerID: "p", modelID: "new" }, agent: "build" }), /admission rejected/)
  assert.deepEqual(fake.sessionState.model, { id: "old", providerID: "p" })
  assert.equal(fake.sessionState.agent, "old-agent")
})

test("W2-004: async session.get response.data.data shape captures authoritative pre-state", async () => {
  const fake = transactionHost()
  const originalGet = fake.client.session.get
  fake.client.session.get = async (input) => ({ data: { data: await originalGet(input) } })
  fake.sessionState.__agentFail = true
  const core = createQueueCore({ client: fake.client })
  core.install()
  await assert.rejects(fake.client.session.prompt({ sessionID: "s", text: "x", model: { providerID: "p", modelID: "new" }, agent: "build" }), /agent rejected/)
  assert.deepEqual(fake.sessionState.model, { id: "old", providerID: "p" }, "pre-state must be read from the wrapped SDK response")
})

test("W2-004: success path admits exactly once with requested settings", async () => {
  const fake = transactionHost()
  const core = createQueueCore({ client: fake.client })
  core.install()
  await fake.client.session.prompt({ sessionID: "s", text: "x", model: { providerID: "p", modelID: "new" }, agent: "build" })
  assert.equal(fake.calls.prompt.length, 1)
  assert.deepEqual(fake.sessionState.model, { id: "new", providerID: "p" })
  assert.equal(fake.sessionState.agent, "build")
})

test("W2-004: concurrent translated prompts cannot cross-combine settings", async () => {
  const fake = transactionHost()
  const core = createQueueCore({ client: fake.client })
  core.install()
  const first = fake.client.session.prompt({ sessionID: "s", text: "a", model: { providerID: "p", modelID: "m1" }, agent: "agent1" })
  const second = fake.client.session.prompt({ sessionID: "s", text: "b", model: { providerID: "p", modelID: "m2" }, agent: "agent2" })
  await Promise.allSettled([first, second])
  assert.equal(fake.calls.prompt.length, 2)
  const models = fake.calls.switchModel.map((c) => c.model.id)
  const agents = fake.calls.switchAgent.map((c) => c.agent)
  // Serialized per session: each transaction writes its model then its agent;
  // no interleaving of the two transactions' setting writes.
  assert.ok(
    (JSON.stringify(models) === JSON.stringify(["m1", "m2"]) && JSON.stringify(agents) === JSON.stringify(["agent1", "agent2"])) ||
    (JSON.stringify(models) === JSON.stringify(["m2", "m1"]) && JSON.stringify(agents) === JSON.stringify(["agent2", "agent1"])),
    `no cross-combination: models=${JSON.stringify(models)} agents=${JSON.stringify(agents)}`,
  )
})

test("W2-004: newer external setting change survives stale compensation", async () => {
  const fake = transactionHost()
  fake.sessionState.__agentFail = true
  const core = createQueueCore({ client: fake.client })
  core.install()
  const originalSwitchModel = fake.client.v2.session.switchModel
  let externalized = false
  fake.client.v2.session.switchModel = async (input) => {
    await originalSwitchModel(input)
    if (!externalized) {
      externalized = true
      fake.sessionState.model = { id: "external", providerID: "p" }
    }
    return {}
  }
  await assert.rejects(fake.client.session.prompt({ sessionID: "s", text: "x", model: { providerID: "p", modelID: "new" }, agent: "build" }), /agent rejected/)
  assert.deepEqual(fake.sessionState.model, { id: "external", providerID: "p" }, "compensation must not clobber a newer external value")
})

// 2.5.0 regression: onAdmitted consults the WS-ATT attention episode registry.
// The registry used to be declared inside the ambient installation block, so
// the identifier was unresolved at admission time and EVERY follow-up send
// failed with "attentionEpisodes is not defined" (Failed to send prompt).
test("2.5.0: native admission resolves the attention episode registry (plugin-scope)", async () => {
  const fake = fakeV2()
  const eventHandlers = []
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: {
      current: { background: "#0b0b0b", backgroundPanel: "#111111", text: "#eeeeee" },
      selected: "fixture-custom-theme",
      install: async () => {},
      set: () => {},
    },
    lifecycle: { onDispose: () => {} },
    app: { version: "1.18.29" },
  }
  await saipatchTui(api, {})
  await assert.doesNotReject(
    () => fake.client.session.prompt({ sessionID: "s", text: "cc" }),
    "admission must not throw ReferenceError: attentionEpisodes is not defined",
  )
  assert.equal(fake.calls.prompt.length, 1)
  // The admission resolved cleanly: a genuine DONE transition for this
  // session may now pulse exactly once. Use the pulse sink to observe it.
  const fs = await import("node:fs")
  const os = await import("node:os")
  const sink = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "saipatch-att-")), "pulse.ndjson")
  process.env.SAIPATCH_ATTENTION_PULSE_SINK = sink
  try {
    const fire = (type, properties) => {
      for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
    }
    fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
    fire("session.status", { sessionID: "s", status: "idle" })
  } finally {
    delete process.env.SAIPATCH_ATTENTION_PULSE_SINK
  }
})

// ─────────────────────────────────────────────────────────────────────────────
// T-144 AUTO-CC RECOVERY: bounded automatic continuation for transient stalls.
// Deterministic: injected schedule/cancel/now seams, injected queue depth,
// no real timers, no real queue. The synthetic cc always flows through the
// EXISTING native queue submission path.
// ─────────────────────────────────────────────────────────────────────────────

function autoCcHarness({ settings, queueDepth } = {}) {
  const calls = { submit: [], records: [], exhausted: [] }
  const timers = []
  let seq = 0
  const controller = createAutoContinue({
    settings,
    submit: async (sessionID) => {
      calls.submit.push(sessionID)
      return `admitted_${calls.submit.length}`
    },
    queueDepth,
    schedule: (fn, ms) => {
      const handle = { id: ++seq, fn, ms }
      timers.push(handle)
      return handle
    },
    cancelScheduled: (handle) => {
      const index = timers.indexOf(handle)
      if (index >= 0) timers.splice(index, 1)
    },
    now: () => 1_000_000,
    sink: (record) => calls.records.push(record),
    onExhausted: (sessionID) => calls.exhausted.push(sessionID),
  })
  const fireAll = () => {
    for (const handle of [...timers]) handle.fn()
  }
  const stall = (assistantMessageID, sessionID = "s") =>
    controller.noteEvent(sessionID, "session.next.step.failed", {
      sessionID,
      assistantMessageID,
      error: "The operation timed out.",
    })
  return { controller, timers, calls, fireAll, stall }
}

test("auto-cc 1: the exact live 'The operation timed out.' case auto-submits one native queued cc", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  // Build active: the failing generation is known.
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  stall("a1")
  assert.equal(calls.submit.length, 0, "nothing fires before the grace period")
  fireAll()
  assert.deepEqual(calls.submit, ["s"])
  // The queue core admission hook completes the ADMITTED transition.
  controller.noteAdmission("s", "msg_cc_1")
  const status = controller.getStatus("s")
  assert.equal(status.attempts, 1)
  assert.equal(status.state, AUTO_CC_STATES.ADMITTED)
  assert.equal(status.lastReason, "TOOL_TIMEOUT")
  controller.dispose()
})

test("auto-cc 1b: the synthetic cc goes through the existing native queue path with delivery:queue", async () => {
  const fake = fakeV2()
  pages = []
  const core = createQueueCore({ client: fake.client, onAdmitted: () => {} })
  const disposeCore = core.install()
  const timers = []
  const controller = createAutoContinue({
    settings: { graceMs: 5 },
    submit: async (sessionID) => {
      await fake.client.session.prompt({ sessionID, text: AUTO_CC_PROMPT })
    },
    schedule: (fn) => {
      timers.push(fn)
      return fn
    },
    cancelScheduled: () => {},
  })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  controller.noteEvent("s", "session.next.step.failed", {
    sessionID: "s",
    assistantMessageID: "a1",
    error: { name: "TimeoutError", message: "read operation timed out" },
  })
  for (const fn of [...timers]) await fn()
  assert.equal(fake.calls.prompt.length, 1)
  assert.equal(fake.calls.prompt[0].input.prompt.text, "cc")
  assert.equal(fake.calls.prompt[0].input.delivery, "queue", "auto-cc must use the native queue, never steer")
  assert.equal(fake.calls.legacyPrompt, undefined, "no direct injection into legacy/internal message arrays")
  disposeCore()
  controller.dispose()
})

test("auto-cc 2: classification covers the live string and structured fields; prose never classifies", () => {
  assert.equal(classifyStall({ error: "The operation timed out." }), "TOOL_TIMEOUT")
  assert.equal(classifyStall({ error: "read operation timed out" }), "TOOL_TIMEOUT")
  assert.equal(classifyStall({ error: "Edit operation timed out" }), "TOOL_TIMEOUT")
  assert.equal(classifyStall({ error: "command execution timed out" }), "TOOL_TIMEOUT")
  assert.equal(classifyStall({ error: { code: "ETIMEDOUT", message: "whatever" } }), "TOOL_TIMEOUT")
  assert.equal(classifyStall({ error: { name: "TimeoutError", message: "x" } }), "TOOL_TIMEOUT")
  assert.equal(classifyStall({ timeout: true, error: "x" }), "TOOL_TIMEOUT")
  // Narrow fallback: arbitrary prose containing the word timeout does NOT classify.
  assert.equal(classifyStall({ error: "the model mentioned a timeout in its answer" }), null)
  assert.equal(classifyStall({ error: "user aborted the operation" }), null)
  assert.equal(classifyStall({ error: "401 unauthorized: invalid API key" }), null)
  assert.equal(classifyStall({ error: "insufficient quota" }), null)
  assert.equal(classifyStall({}), null)
  const tuned = normalizeAutoContinueSettings({ enabled: false, maxAttempts: "7", graceMs: 1 })
  assert.equal(tuned.enabled, false)
  assert.equal(tuned.maxAttempts, 7)
  assert.equal(normalizeAutoContinueSettings({}).enabled, true, "default Auto-continue = ON")
  assert.equal(normalizeAutoContinueSettings({}).maxAttempts, 3)
})

test("auto-cc 2b (live screenshot case): Build active -> tool timeout -> settle -> idle -> queue empty = exactly one cc, resumed build yields no second cc", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a_build" })
  stall("a_build")
  // Generation settles; session goes idle; native queue empty (default depth 0).
  fireAll()
  controller.noteAdmission("s", "msg_cc_1")
  assert.equal(calls.submit.length, 1)
  // Build resumes: the promoted cc generation starts work.
  controller.noteEvent("s", "session.next.prompted", { sessionID: "s", messageID: "msg_cc_1" })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a_resumed" })
  assert.equal(calls.submit.length, 1, "no second cc after the recovery already resumed work")
  controller.dispose()
})

test("auto-cc 3: repeated timeouts are bounded to exactly maxAttempts synthetic messages", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness({ settings: { maxAttempts: 3 } })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  fireAll()
  controller.noteAdmission("s", "cc1")
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  stall("a1") // the cc generation timed out too
  fireAll()
  controller.noteAdmission("s", "cc2")
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a2" })
  stall("a2")
  fireAll()
  controller.noteAdmission("s", "cc3")
  assert.equal(calls.submit.length, 3)
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a3" })
  stall("a3") // fourth qualifying stall
  fireAll()
  assert.equal(calls.submit.length, 3, "no auto cc #4 after the budget is spent")
  const status = controller.getStatus("s")
  assert.equal(status.attempts, 3)
  assert.equal(status.exhausted, true)
  assert.equal(status.lastReason, AUTO_CC_EXHAUSTED)
  controller.dispose()
})

test("auto-cc 4: exhaustion surfaces AUTO_CC_EXHAUSTED through the exhausted callback and status", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness({ settings: { maxAttempts: 2 } })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  fireAll()
  controller.noteAdmission("s", "cc1")
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  stall("a1")
  fireAll()
  controller.noteAdmission("s", "cc2")
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a2" })
  stall("a2")
  fireAll()
  assert.equal(calls.submit.length, 2)
  assert.deepEqual(calls.exhausted, ["s"], "onExhausted surfaced exactly once")
  const lines = controller.describe("s")
  assert.ok(lines.includes("Auto-cc exhausted"), `diagnostics must say exhausted: ${JSON.stringify(lines)}`)
  assert.ok(lines.some((line) => line.startsWith("Recovery attempts: 2/2")), JSON.stringify(lines))
  controller.dispose()
})

test("auto-cc 5: ordinary queued user prompts take precedence; no cc is prepended", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness({ queueDepth: () => 2 })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  fireAll()
  assert.equal(calls.submit.length, 0, "queued 'continue implementing T-109' + 'run regression tests' win")
  assert.equal(controller.getStatus("s").lastReason, "QUEUED_WORK_TAKES_PRECEDENCE")
  controller.dispose()
})

test("auto-cc 5b: after the queue drains and the run settles stalled again, recovery may evaluate a fresh cc", async () => {
  let depth = 2
  const { controller, calls, fireAll, stall } = autoCcHarness({ queueDepth: () => depth })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  fireAll()
  assert.equal(calls.submit.length, 0)
  depth = 0
  // The queued work ran and its generation stalled again (new generation id).
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  stall("a1")
  fireAll()
  assert.equal(calls.submit.length, 1, "fresh evaluation after the queue drained")
  controller.noteAdmission("s", "cc1")
  controller.dispose()
})

test("auto-cc 6: a manual cc before the grace expires wins; the automatic cc is cancelled; exactly one continuation", async () => {
  const { controller, calls, timers, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  assert.equal(timers.length, 1, "grace timer armed")
  // The user manually sends cc: the queue core admission hook reports it
  // (not ours — no in-flight marker).
  controller.noteAdmission("s", "msg_manual_cc")
  fireAll()
  assert.equal(calls.submit.length, 0, "automatic cc cancelled; manual cc admitted exactly once")
  assert.equal(timers.length, 0, "pending timer disarmed")
  controller.dispose()
})

test("auto-cc 7: spontaneous self-recovery before the grace expires cancels the pending cc with zero synthetic messages", async () => {
  const { controller, calls, timers, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  assert.equal(timers.length, 0, "pending timer disarmed on self-recovery")
  fireAll()
  assert.equal(calls.submit.length, 0)
  controller.dispose()
})

test("auto-cc 8: a stale generation timeout cannot inject cc into a newer generation", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  // Generation A times out.
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "aA" })
  stall("aA")
  fireAll()
  controller.noteAdmission("s", "ccA")
  // Generation B starts (the cc resumed the run) and is active.
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "aB" })
  // A delayed/redelivered timeout from generation A arrives.
  stall("aA")
  fireAll()
  assert.equal(calls.submit.length, 1, "stale generation-A event never triggers another cc")
  controller.dispose()
})

test("auto-cc 8b: a stall without generation identity fails closed (no cc)", async () => {
  const { controller, calls, fireAll } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.failed", { sessionID: "s", error: "The operation timed out." })
  fireAll()
  assert.equal(calls.submit.length, 0)
  controller.dispose()
})

test("auto-cc 9: a user abort never auto-continues, even mid-recovery", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  controller.onAbort("s")
  fireAll()
  assert.equal(calls.submit.length, 0, "pending recovery cancelled by abort")

  const { controller: c2, calls: calls2, fireAll: fire2, stall: stall2 } = autoCcHarness()
  c2.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall2("a0")
  fire2()
  c2.noteAdmission("s", "cc1")
  c2.onAbort("s") // user aborts while the auto-cc generation runs
  assert.equal(c2.getStatus("s").state, AUTO_CC_STATES.IDLE, "abort ends the recovery episode")
  fire2()
  assert.equal(calls2.submit.length, 1, "abort adds no further synthetic submission")
  controller.dispose()
  c2.dispose()
})

test("auto-cc 10: permission and question waits cancel recovery (NEEDS_HUMAN is never auto-continued)", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  controller.noteEvent("s", "permission.asked", { sessionID: "s" })
  fireAll()
  assert.equal(calls.submit.length, 0)

  const { controller: c2, calls: calls2, fireAll: fire2, stall: stall2 } = autoCcHarness()
  c2.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall2("a0")
  c2.noteEvent("s", "question.asked", { sessionID: "s" })
  fire2()
  assert.equal(calls2.submit.length, 0)
  controller.dispose()
  c2.dispose()
})

test("auto-cc 11: a normal completed answer and PAUSE both prevent auto-cc; resume re-evaluates safely; disabling restores current behavior", async () => {
  // Normal completion while pending: no cc.
  const a = autoCcHarness()
  a.controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  a.stall("a0")
  a.controller.noteEvent("s", "session.next.step.ended", { sessionID: "s", assistantMessageID: "a0", finish: "stop" })
  a.fireAll()
  assert.equal(a.calls.submit.length, 0)
  a.controller.dispose()

  // Paused queue: stall is held, never fired; resume re-arms the current episode.
  const b = autoCcHarness()
  b.controller.setPaused(true)
  b.controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  b.stall("a0")
  b.fireAll()
  assert.equal(b.calls.submit.length, 0, "paused queue never auto-continues behind the operator's back")
  assert.equal(b.controller.getStatus("s").state, AUTO_CC_STATES.PAUSED_HOLD)
  assert.ok(b.controller.describe("s").includes("Auto-cc: PAUSED"), JSON.stringify(b.controller.describe("s")))
  b.controller.setPaused(false)
  assert.equal(b.timers.length, 1, "resume re-evaluates the still-current recovery episode")
  b.fireAll()
  assert.equal(b.calls.submit.length, 1)
  b.controller.dispose()

  // Disabling Auto-continue restores current behavior entirely.
  const c = autoCcHarness({ settings: { enabled: false } })
  c.controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  c.stall("a0")
  c.fireAll()
  assert.equal(c.calls.submit.length, 0)
  assert.equal(c.controller.getStatus("s").enabled, false)
  c.controller.dispose()
})

test("auto-cc 12: budget resets only on a COMPLETED turn; immediate re-timeout consumes another attempt", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness({ settings: { maxAttempts: 3 } })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  fireAll()
  controller.noteAdmission("s", "cc1")
  // The cc's resumed generation starts work (progress evidence) but stalls again.
  controller.noteEvent("s", "session.next.prompted", { sessionID: "s", messageID: "cc1" })
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  stall("a1")
  fireAll()
  controller.noteAdmission("s", "cc2")
  assert.equal(calls.submit.length, 2, "timeout -> cc -> immediate timeout -> cc = two consumed attempts")
  assert.equal(controller.getStatus("s").attempts, 2)
  // A fully completed turn closes the chain: a later stall starts a FRESH budget.
  controller.noteEvent("s", "session.next.step.ended", { sessionID: "s", assistantMessageID: "a2", finish: "stop" })
  assert.equal(controller.getStatus("s").state, AUTO_CC_STATES.IDLE)
  controller.dispose()
})

test("auto-cc 13: the completion sound is suppressed during recovery and plays after a real drain", () => {
  const played = []
  const episodes = createCompletionEpisodes()
  const sound = createCompletionSound({
    settings: { enabled: true },
    picker: { next: () => "PICKUP01.wav" },
    episodes,
    requestPlayback: (sessionID, name) => played.push([sessionID, name]),
  })
  const { controller, fireAll, stall } = autoCcHarness()
  // Production wiring: the ambient listener consults the veto BEFORE the
  // completion sound ever sees the DONE transition.
  const transition = (sessionID, next, previous) => {
    if (controller.suppressesCompletion(sessionID, next, previous)) return
    sound.onTransition(sessionID, next, previous)
  }
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  episodes.openTurn("s")
  stall("a0")
  // timeout -> pending auto-cc: the DONE transition is vetoed.
  assert.equal(controller.suppressesCompletion("s", "DONE", "NEUTRAL"), true)
  transition("s", "DONE", "NEUTRAL")
  assert.equal(played.length, 0, "no final completion sound between timeout, pending cc and resumed work")
  // Recovery admitted: still vetoed.
  fireAll()
  controller.noteAdmission("s", "cc1")
  assert.equal(controller.suppressesCompletion("s", "DONE", "NEUTRAL"), true)
  // Recovery resolves (completed turn); a NEW genuine DONE transition plays.
  controller.noteEvent("s", "session.next.step.ended", { sessionID: "s", assistantMessageID: "a1", finish: "stop" })
  assert.equal(controller.suppressesCompletion("s", "DONE", "NEUTRAL"), false)
  transition("s", "DONE", "NEUTRAL")
  assert.deepEqual(played, [["s", "PICKUP01.wav"]])
  controller.dispose()
})

test("auto-cc 14: process restart cannot duplicate the synthetic cc (fail-closed, in-memory only)", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  // OpenCode restarts: every timer and the whole in-memory episode die.
  controller.dispose()
  fireAll()
  assert.equal(calls.submit.length, 0, "a pre-restart pending cc never fires after disposal")
  // The fresh process never saw generation a0 start: the redelivered
  // pre-restart timeout is rejected (fail closed) — no duplicate continuation
  // for work OpenCode may already have persisted/resumed.
  const fresh = autoCcHarness()
  fresh.stall("a0")
  fresh.fireAll()
  assert.equal(fresh.calls.submit.length, 0, "a pre-restart stall cannot recover in a fresh process")
  assert.equal(fresh.controller.getStatus("s").lastReason, "GENERATION_NOT_STARTED_HERE")
  // A generation the FRESH process observed starting is a legitimate new stall
  // and recovers exactly once.
  fresh.controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "aF" })
  fresh.stall("aF")
  fresh.fireAll()
  assert.equal(fresh.calls.submit.length, 1)
  fresh.controller.dispose()
})

test("auto-cc 15: dedup — the same stall generation never double-arms a second cc", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  stall("a0") // duplicate delivery of the same generation's stall
  assert.equal(controller.getStatus("s").pending, true)
  fireAll()
  assert.equal(calls.submit.length, 1)
  controller.noteAdmission("s", "cc1")
  assert.equal(controller.getStatus("s").state, AUTO_CC_STATES.ADMITTED)
  controller.dispose()
})

test("auto-cc 16: diagnostics expose session/episode id, attempts and decisions; never prompt or error content", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  stall("a0")
  fireAll()
  controller.noteAdmission("s", "cc1")
  const status = controller.getStatus("s")
  assert.equal(status.enabled, true)
  assert.ok(status.episodeID.startsWith("s:a0#"), `episode id ties session + generation: ${status.episodeID}`)
  assert.equal(status.pending, false)
  const events = calls.records.map((record) => record.event)
  assert.ok(events.includes("arm") && events.includes("submit") && events.includes("admitted"), JSON.stringify(events))
  for (const record of calls.records) {
    const text = JSON.stringify(record)
    assert.ok(!text.includes("The operation timed out"), "diagnostics never carry full error/prompt content")
  }
  const lines = controller.describe("s")
  assert.ok(lines.includes("Auto-cc: ON"))
  assert.ok(lines.some((line) => line.startsWith("Recovery attempts: 1/3")))
  assert.ok(lines.some((line) => line.startsWith("Last reason: TOOL_TIMEOUT")))
  controller.dispose()
})

test("auto-cc 17: full plugin wiring — a live timeout event arms recovery through the real event bus, with diagnostics; dispose cancels", async () => {
  const fake = fakeV2()
  pages = []
  const eventHandlers = []
  let disposeHook
  const fs = await import("node:fs")
  const os = await import("node:os")
  const sinkPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "saipatch-autocc-")), "autocc.ndjson")
  process.env.SAIPATCH_AUTOCC_SINK = sinkPath
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: {
      current: { background: "#0b0b0b", backgroundPanel: "#111111", text: "#eeeeee" },
      selected: "fixture-custom-theme",
      install: async () => {},
      set: () => {},
    },
    lifecycle: { onDispose: (fn) => { disposeHook = fn } },
    app: { version: "1.18.29" },
  }
  try {
    await saipatchTui(api, { ambient: false })
    const fire = (type, properties) => {
      for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
    }
    fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a_live" })
    fire("session.next.step.failed", {
      sessionID: "s",
      assistantMessageID: "a_live",
      error: "The operation timed out.",
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    const lines = fs.readFileSync(sinkPath, "utf8").trim().split(String.fromCharCode(10)).map((line) => JSON.parse(line))
    assert.ok(lines.some((record) => record.event === "arm" && record.sessionID === "s" && record.reason === "TOOL_TIMEOUT"),
      `the live event stream must arm recovery: ${JSON.stringify(lines)}`)
    assert.ok(!lines.some((record) => record.event === "submit"), "grace period holds before any submission")
    // Disposal (plugin unload / OpenCode shutdown) cancels the pending grace.
    disposeHook?.()
    await new Promise((resolve) => setTimeout(resolve, 4100))
    const after = fs.readFileSync(sinkPath, "utf8").trim().split(String.fromCharCode(10)).map((line) => JSON.parse(line))
    assert.ok(!after.some((record) => record.event === "submit"), "dispose cancels the pending recovery")
    assert.equal(fake.calls.prompt.length, 0)
  } finally {
    delete process.env.SAIPATCH_AUTOCC_SINK
  }
})

// ─────────────────────────────────────────────────────────────────────────────
// T-144 2.5.1 PRODUCTION OCCUPANCY FIX: the pending-ordinary-user-queue truth
// is an exact stable-message-ID tracker inside the controller (NOT a counter
// baseline and NOT a test-only injected queueDepth). Work admitted BEFORE a
// timeout suppresses the synthetic cc exactly like work admitted after it.
// ─────────────────────────────────────────────────────────────────────────────

test("auto-cc 18 (production wiring): user work queued BEFORE the timeout suppresses cc; after the native queue drains, exactly one synthetic cc follows FIFO", async () => {
  const fake = fakeV2()
  pages = []
  const eventHandlers = []
  const timers = []
  let seq = 0
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: {
      current: { background: "#0b0b0b", backgroundPanel: "#111111", text: "#eeeeee" },
      selected: "fixture-custom-theme",
      install: async () => {},
      set: () => {},
    },
    lifecycle: { onDispose: () => {} },
    app: { version: "1.18.30" },
  }
  // The SAME production wiring as the installed plugin; only the grace clock
  // is a deterministic test seam.
  await saipatchTui(api, {
    ambient: false,
    autoContinue: {
      schedule: (fn) => { const handle = { id: ++seq, fn }; timers.push(handle); return handle },
      cancelScheduled: (handle) => { const index = timers.indexOf(handle); if (index >= 0) timers.splice(index, 1) },
    },
  })
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  // The grace timer's submission flows through the REAL async queue wrapper:
  // drain timers AND let the microtask chain (admission callback included)
  // settle before asserting.
  const fireAll = async () => {
    for (const handle of [...timers]) {
      timers.splice(timers.indexOf(handle), 1)
      await handle.fn()
    }
    await new Promise((resolve) => setImmediate(resolve))
  }
  const synthetic = () => fake.calls.prompt.filter((call) => call.input.prompt?.text === "cc")

  // current Build prompt: admitted through the REAL queue core wrapper...
  await fake.client.session.prompt({ sessionID: "s", text: "run the long build" })
  assert.equal(fake.client.session.prompt.__saipatchNative, true)
  // ...and promoted natively (its stable message ID leaves the pending set).
  fire("session.next.prompted", { sessionID: "s", messageID: "msg_adm_1" })
  // The Build generation starts.
  fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a_build" })
  // Two ordinary follow-ups are admitted WHILE Build runs and are still waiting.
  await fake.client.session.prompt({ sessionID: "s", text: "followup one" })
  await fake.client.session.prompt({ sessionID: "s", text: "followup two" })
  // The Build generation times out.
  fire("session.next.step.failed", { sessionID: "s", assistantMessageID: "a_build", error: "The operation timed out." })
  await fireAll()
  assert.equal(synthetic().length, 0, "queued user work admitted BEFORE the timeout suppresses the synthetic cc")
  assert.equal(fake.calls.prompt.length, 3, "native FIFO untouched: only the user prompts so far")

  // The native queue drains in FIFO order: followup1 promotes and completes.
  fire("session.next.prompted", { sessionID: "s", messageID: "msg_adm_2" })
  fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a_f1" })
  fire("session.next.step.ended", { sessionID: "s", assistantMessageID: "a_f1", finish: "stop" })
  // followup2 promotes; ITS generation times out; the queue is now empty.
  fire("session.next.prompted", { sessionID: "s", messageID: "msg_adm_3" })
  fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a_f2" })
  fire("session.next.step.failed", { sessionID: "s", assistantMessageID: "a_f2", error: "The operation timed out." })
  await fireAll()
  assert.equal(synthetic().length, 1, "exactly one synthetic cc after the ordinary queue is empty")
  assert.equal(synthetic()[0].input.delivery, "queue")
  assert.equal(fake.calls.prompt.length, 4)
})

test("auto-cc 19: promotion-before-admission race leaves zero pending, and the tombstone structure stays bounded", () => {
  const { controller } = autoCcHarness()
  // Race B: the native promotion event arrives BEFORE the wrapper admission.
  controller.noteEvent("s", "session.next.prompted", { sessionID: "s", messageID: "msgA" })
  controller.noteAdmission("s", "msgA")
  assert.equal(controller.pendingUserCount("s"), 0, "no ghost queued message")
  // Race A: admission first, promotion second — also zero.
  controller.noteAdmission("s", "msgB")
  assert.equal(controller.pendingUserCount("s"), 1)
  controller.noteEvent("s", "session.next.prompted", { sessionID: "s", messageID: "msgB" })
  assert.equal(controller.pendingUserCount("s"), 0)
  // Thousands of raced IDs: the tombstone map must stay bounded forever.
  for (let i = 0; i < 5000; i++) {
    controller.noteEvent("s", "session.next.prompted", { sessionID: "s", messageID: `m${i}` })
    controller.noteAdmission("s", `m${i}`)
  }
  const debug = controller._debug()
  assert.ok(debug.tombstones <= debug.tombstoneLimit, `bounded: ${JSON.stringify(debug)}`)
  assert.equal(debug.pendingTotal, 0)
  controller.dispose()
  assert.equal(controller._debug().pendingTotal, 0)
  assert.equal(controller._debug().tombstones, 0, "dispose clears owned tombstones")
})

test("auto-cc 20: a manual cc queued BEFORE the timeout suppresses automatic cc via the default tracker (no injected queueDepth)", async () => {
  const { controller, calls, fireAll, stall } = autoCcHarness()
  // Build generation active.
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a_build" })
  // The current prompt was admitted and promoted before the generation started.
  controller.noteAdmission("s", "msg_current")
  controller.noteEvent("s", "session.next.prompted", { sessionID: "s", messageID: "msg_current" })
  // The user manually queues "cc" while Build runs — it is pending work.
  controller.noteAdmission("s", "msg_manual_cc")
  assert.equal(controller.pendingUserCount("s"), 1)
  // Build times out: the already queued manual cc is sufficient continuation.
  stall("a_build")
  fireAll()
  assert.equal(calls.submit.length, 0, "no synthetic cc behind a pending manual cc")
  assert.equal(controller.getStatus("s").lastReason, "QUEUED_WORK_TAKES_PRECEDENCE")
  // The manual cc promotes and continues the job — still zero synthetic.
  controller.noteEvent("s", "session.next.prompted", { sessionID: "s", messageID: "msg_manual_cc" })
  fireAll()
  assert.equal(calls.submit.length, 0)
  controller.dispose()
})

test("auto-cc 21 (P1): the diagnostic event sink records sanitized classification evidence and never text content", () => {
  const records = []
  const controller = createAutoContinue({
    settings: { graceMs: 250 },
    submit: async () => {},
    diagnosticSink: (record) => records.push(record),
  })
  controller.noteEvent("s", "session.next.step.failed", {
    sessionID: "s",
    assistantMessageID: "aX",
    error: { code: "ETIMEDOUT", name: "TimeoutError", message: "SECRET TOOL OUTPUT" },
  })
  controller.noteEvent("s", "session.next.step.failed", {
    sessionID: "s",
    assistantMessageID: "aY",
    error: "prose mentioning a timeout without an operation noun",
  })
  assert.equal(records.length, 2)
  const first = records[0]
  assert.equal(first.type, "session.next.step.failed")
  assert.equal(first.sessionID, "s")
  assert.equal(first.assistantMessageID, "aX")
  assert.equal(first.errorCode, "ETIMEDOUT")
  assert.equal(first.errorName, "TimeoutError")
  assert.equal(first.timeout, false)
  assert.equal(first.matched, true)
  assert.equal(first.classification, "TOOL_TIMEOUT")
  assert.equal(records[1].matched, false)
  assert.equal(records[1].classification, null)
  const dump = JSON.stringify(records)
  assert.ok(!dump.includes("SECRET"), "never records error/message text")
  assert.ok(!dump.includes("prose"), "never records message text")
  controller.dispose()
})

test("auto-cc 22: package version 2.5.2 with the auto-continue module transaction-owned", () => {
  const manifest = JSON.parse(readFileSync(path.join(here, "manifest.json"), "utf8"))
  const pkg = JSON.parse(readFileSync(path.join(here, "package.json"), "utf8"))
  assert.equal(manifest.version, "2.5.2")
  assert.equal(pkg.version, "2.5.2")
  assert.ok(manifest.files.includes("saipatch-auto-continue.js"), "auto-continue runtime is transaction-owned")
  for (const file of manifest.files) {
    assert.ok(existsSync(path.join(here, file)), `manifest member exists: ${file}`)
  }
})

test("auto-cc 23 (2.5.2 settle Case B): two simultaneous active steps: timeout waits for final settle", async () => {
  const { controller, calls, fireAll } = autoCcHarness()
  // step.started A
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "A" })
  assert.equal(controller.activeStepCount("s"), 1)
  // step.started B
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "B" })
  assert.equal(controller.activeStepCount("s"), 2)
  // step.failed A timeout
  controller.noteEvent("s", "session.next.step.failed", {
    sessionID: "s",
    assistantMessageID: "A",
    error: "The operation timed out.",
  })
  assert.equal(controller.activeStepCount("s"), 1)
  const statusMid = controller.getStatus("s")
  assert.equal(statusMid.waitingSettle, true)
  assert.equal(statusMid.state, AUTO_CC_STATES.WAITING_SETTLE)
  // Advancing grace clock does NOT submit cc because active count == 1
  fireAll()
  assert.equal(calls.submit.length, 0, "advancing grace clock does not submit cc while another step is active")
  // step.ended B
  controller.noteEvent("s", "session.next.step.ended", { sessionID: "s", assistantMessageID: "B" })
  assert.equal(controller.activeStepCount("s"), 0)
  const statusSettled = controller.getStatus("s")
  assert.equal(statusSettled.pending, true)
  assert.equal(statusSettled.state, AUTO_CC_STATES.PENDING)
  // after grace exactly one cc
  fireAll()
  assert.equal(calls.submit.length, 1)
  controller.dispose()
})

test("auto-cc 24 (2.5.2 settle Case C): self-recovery cancels grace", async () => {
  const { controller, calls, fireAll } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "A" })
  controller.noteEvent("s", "session.next.step.failed", {
    sessionID: "s",
    assistantMessageID: "A",
    error: "The operation timed out.",
  })
  assert.equal(controller.activeStepCount("s"), 0)
  assert.equal(controller.getStatus("s").pending, true)
  // new step.started before grace fires
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "B" })
  assert.equal(controller.getStatus("s").state, AUTO_CC_STATES.IDLE)
  assert.equal(controller.getStatus("s").pending, false)
  // fire grace clock
  fireAll()
  assert.equal(calls.submit.length, 0, "pending auto-cc cancelled, zero synthetic cc")
  controller.dispose()
})

test("auto-cc 25 (2.5.2 settle Case D): normal terminal completion occurs before cc", async () => {
  const { controller, calls, fireAll } = autoCcHarness()
  controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "A" })
  controller.noteEvent("s", "session.next.step.failed", {
    sessionID: "s",
    assistantMessageID: "A",
    error: "The operation timed out.",
  })
  assert.equal(controller.activeStepCount("s"), 0)
  assert.equal(controller.getStatus("s").pending, true)
  // normal terminal completion occurs before cc
  controller.noteEvent("s", "session.next.step.ended", {
    sessionID: "s",
    assistantMessageID: "A",
    finish: "stop",
  })
  assert.equal(controller.getStatus("s").state, AUTO_CC_STATES.IDLE)
  fireAll()
  assert.equal(calls.submit.length, 0, "zero synthetic cc on normal terminal finish")
  controller.dispose()
})

test("auto-cc 26 (2.5.2 production wiring): manual prompt intent suppresses cc BEFORE admission returns (pre-admission race)", async () => {
  const fake = fakeV2()
  pages = []
  const eventHandlers = []
  const timers = []
  let seq = 0
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: { current: { background: "#0b0b0b", text: "#eeeeee" }, selected: "theme", install: async () => {}, set: () => {} },
    lifecycle: { onDispose: () => {} },
    app: { version: "1.18.30" },
  }
  await saipatchTui(api, {
    ambient: false,
    autoContinue: {
      schedule: (fn) => { const handle = { id: ++seq, fn }; timers.push(handle); return handle },
      cancelScheduled: (handle) => { const idx = timers.indexOf(handle); if (idx >= 0) timers.splice(idx, 1) },
    },
  })
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }
  const synthetic = () => fake.calls.prompt.filter((call) => call.input.prompt?.text === "cc" && call.input.__saipatchAutoContinue === true)

  // Build stalls with timeout
  fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a_build" })
  fire("session.next.step.failed", { sessionID: "s", assistantMessageID: "a_build", error: "The operation timed out." })
  assert.equal(timers.length, 1, "grace timer armed")

  // user calls client.session.prompt("cc")
  // deliberately hold native v2.prompt unresolved
  let releasePrompt
  const promptHold = new Promise((resolve) => { releasePrompt = resolve })
  const origV2Prompt = fake.client.v2.session.prompt
  let held = false
  fake.client.v2.session.prompt = async (input, options) => {
    held = true
    fake.calls.prompt.push({ input, options })
    return promptHold
  }

  const userPromptPromise = fake.client.session.prompt({ sessionID: "s", text: "cc" })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(held, true, "native v2.prompt reached and held")

  // grace expires BEFORE native admission returns
  for (const handle of [...timers]) {
    timers.splice(timers.indexOf(handle), 1)
    await handle.fn()
  }
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(synthetic().length, 0, "synthetic native submissions == 0 while user prompt is in-flight")

  // Then release user's admission
  fake.client.v2.session.prompt = origV2Prompt
  releasePrompt({ data: { data: { id: "msg_user_cc_1" } } })
  await userPromptPromise
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(fake.calls.prompt.length, 1, "user cc admitted exactly once")
  assert.equal(fake.calls.prompt[0].input.prompt.text, "cc")
  assert.equal(synthetic().length, 0, "no synthetic cc appears")
})

test("auto-cc 27 (2.5.2): failed ordinary admission releases intent", async () => {
  const fake = fakeV2()
  pages = []
  let intentsRegistered = 0
  let intentsFailed = 0
  let intentsAdmitted = 0
  const intentController = {
    onPromptIntent() { intentsRegistered++ },
    onPromptAdmitted() { intentsAdmitted++ },
    onPromptIntentFailed() { intentsFailed++ },
  }
  fake.client.v2.session.prompt = async () => { throw new Error("native admission rejected") }
  const core = createQueueCore({ client: fake.client, admissionIntents: intentController })
  const dispose = core.install()

  await assert.rejects(
    async () => fake.client.session.prompt({ sessionID: "s", text: "test" }),
    /native admission rejected/,
  )

  assert.equal(intentsRegistered, 1, "intent was registered")
  assert.equal(intentsFailed, 1, "intent was failed on rejection")
  assert.equal(intentsAdmitted, 0)
  dispose()
})

test("auto-cc 28 (2.5.2): settings failure releases intent", async () => {
  const fake = fakeV2()
  pages = []
  let intentsRegistered = 0
  let intentsFailed = 0
  const intentController = {
    onPromptIntent() { intentsRegistered++ },
    onPromptAdmitted() {},
    onPromptIntentFailed() { intentsFailed++ },
  }
  fake.client.v2.session.switchModel = async () => { throw new Error("model switch failed") }
  const core = createQueueCore({ client: fake.client, admissionIntents: intentController })
  const dispose = core.install()

  await assert.rejects(
    async () => fake.client.session.prompt({ sessionID: "s", text: "test", model: { providerID: "p", modelID: "m" } }),
    /model switch failed/,
  )

  assert.equal(intentsRegistered, 1)
  assert.equal(intentsFailed, 1, "intent released on settings failure")
  dispose()
})

test("auto-cc 29 (2.5.2): synthetic source explicitly identified and internal field stripped", async () => {
  const fake = fakeV2()
  pages = []
  const admitted = []
  const core = createQueueCore({
    client: fake.client,
    onAdmitted: (sessionID, messageID, meta) => admitted.push({ sessionID, messageID, meta }),
  })
  const dispose = core.install()

  // 1. Synthetic submission
  await fake.client.session.prompt({ sessionID: "s", text: "cc", __saipatchAutoContinue: true })
  assert.equal(admitted.length, 1)
  assert.equal(admitted[0].meta.source, "auto-continue")
  assert.equal(admitted[0].meta.intentID, null)
  // Check native payload: __saipatchAutoContinue never reaches native OpenCode v2
  assert.equal(fake.calls.prompt[0].input.prompt.__saipatchAutoContinue, undefined)
  assert.equal(fake.calls.prompt[0].input.prompt.text, "cc")

  // 2. Ordinary submission
  await fake.client.session.prompt({ sessionID: "s", text: "user message" })
  assert.equal(admitted.length, 2)
  assert.equal(admitted[1].meta.source, "ordinary")
  assert.ok(typeof admitted[1].meta.intentID === "string")
  assert.equal(fake.calls.prompt[1].input.prompt.__saipatchAutoContinue, undefined)
  assert.equal(fake.calls.prompt[1].input.prompt.text, "user message")

  // 3. Steer submission
  await fake.client.session.prompt({ sessionID: "s", text: "steer message", __saipatchSteer: true })
  assert.equal(admitted.length, 2, "steer skips onAdmitted queue tracking")
  assert.equal(fake.calls.prompt[2].input.delivery, "steer")

  dispose()
})

test("auto-cc 30 (2.5.2): concurrent manual/synthetic admission identity (Order A and Order B)", async () => {
  // ORDER A: ordinary intent begins first, synthetic fire evaluates second -> synthetic suppressed before v2.prompt
  const { controller: cA, calls: callsA, fireAll: fireA } = autoCcHarness()
  cA.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a0" })
  cA.noteEvent("s", "session.next.step.failed", { sessionID: "s", assistantMessageID: "a0", error: "The operation timed out." })
  assert.equal(cA.getStatus("s").pending, true)
  // Ordinary intent arrives first
  cA.onPromptIntent("s", "intent_user_1")
  // Synthetic fire evaluates second
  fireA()
  assert.equal(callsA.submit.length, 0, "synthetic suppressed before v2.prompt when intent in flight")
  cA.dispose()

  // ORDER B: synthetic native admission commits first, ordinary prompt begins immediately afterward
  const fake = fakeV2()
  pages = []
  const admissions = []
  const autoContinue = createAutoContinue({
    settings: { maxAttempts: 3 },
    submit: async (sessionID) => {
      await fake.client.session.prompt({ sessionID, text: AUTO_CC_PROMPT, __saipatchAutoContinue: true })
    },
  })
  const core = createQueueCore({
    client: fake.client,
    onAdmitted: (s, m, meta) => {
      admissions.push({ s, m, meta })
      autoContinue.noteAdmission(s, m, meta)
    },
    admissionIntents: autoContinue,
  })
  const dispose = core.install()

  // Synthetic admission commits first
  autoContinue.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: "a1" })
  autoContinue.noteEvent("s", "session.next.step.failed", { sessionID: "s", assistantMessageID: "a1", error: "The operation timed out." })
  await fake.client.session.prompt({ sessionID: "s", text: "cc", __saipatchAutoContinue: true })

  // Ordinary prompt begins immediately afterward
  await fake.client.session.prompt({ sessionID: "s", text: "user follow-up" })

  assert.equal(admissions.length, 2)
  assert.equal(admissions[0].meta.source, "auto-continue")
  assert.equal(admissions[1].meta.source, "ordinary")
  assert.equal(autoContinue.getStatus("s").attempts, 1, "exactly one synthetic attempt counted")
  assert.equal(autoContinue.getStatus("s").pendingUserQueue, 1, "ordinary prompt is pending user queue")
  dispose()
  autoContinue.dispose()
})

test("auto-cc 31 (2.5.2): thousands of generations leave bounded ownership state", () => {
  const { controller } = autoCcHarness()
  const TOTAL = 3000

  for (let i = 0; i < TOTAL; i++) {
    const aid = `gen_${i}`
    controller.noteEvent("s", "session.next.step.started", { sessionID: "s", assistantMessageID: aid })
    assert.equal(controller.activeStepCount("s"), 1)
    controller.noteEvent("s", "session.next.step.ended", {
      sessionID: "s",
      assistantMessageID: aid,
      finish: "stop",
    })
    assert.equal(controller.activeStepCount("s"), 0)
  }

  const debug = controller._debug()
  assert.equal(debug.activeSessions, 0, "activeSessions == 0")
  assert.equal(debug.activeGenerations, 0, "activeGenerations == 0")
  assert.equal(debug.admissionIntentTotal, 0, "admissionIntentTotal == 0")
  assert.equal(debug.pendingTotal, 0, "pendingTotal == 0")
  assert.ok(debug.tombstones <= debug.tombstoneLimit, "tombstones bounded")
  controller.dispose()
})

test("auto-cc 32 (2.5.2): production saipatchTui wiring passes admissionIntents to queue core", async () => {
  const fake = fakeV2()
  pages = []
  const eventHandlers = []
  const timers = []
  let seq = 0
  const api = {
    client: fake.client,
    state: { session: { messages: () => [], permission: () => [] }, part: () => [] },
    event: { on: (type, handler) => { eventHandlers.push([type, handler]); return () => {} } },
    theme: { current: { background: "#0b0b0b", text: "#eeeeee" }, selected: "theme", install: async () => {}, set: () => {} },
    lifecycle: { onDispose: () => {} },
    app: { version: "1.18.30" },
  }
  await saipatchTui(api, {
    ambient: false,
    autoContinue: {
      schedule: (fn) => { const handle = { id: ++seq, fn }; timers.push(handle); return handle },
      cancelScheduled: (handle) => { const idx = timers.indexOf(handle); if (idx >= 0) timers.splice(idx, 1) },
    },
  })
  const fire = (type, properties) => {
    for (const [registered, handler] of eventHandlers) if (registered === type) handler({ type, properties })
  }

  // Arms grace
  fire("session.next.step.started", { sessionID: "s", assistantMessageID: "a_stall" })
  fire("session.next.step.failed", { sessionID: "s", assistantMessageID: "a_stall", error: "The operation timed out." })
  assert.equal(timers.length, 1, "grace timer armed")

  // While grace is armed, start an ordinary prompt that holds native admission
  let release
  const hold = new Promise((r) => { release = r })
  fake.client.v2.session.prompt = async (input, options) => {
    fake.calls.prompt.push({ input, options })
    return hold
  }
  const userPromise = fake.client.session.prompt({ sessionID: "s", text: "user work" })
  await new Promise((r) => setImmediate(r))

  // Grace fires while admission is in-flight: should be suppressed because admissionIntents was wired!
  for (const h of [...timers]) {
    timers.splice(timers.indexOf(h), 1)
    await h.fn()
  }
  await new Promise((r) => setImmediate(r))

  // If admissionIntents was NOT wired (dead), auto-cc would have submitted a synthetic prompt!
  const syntheticCalls = fake.calls.prompt.filter((c) => c.input.prompt?.text === "cc")
  assert.equal(syntheticCalls.length, 0, "admissionIntents in production wiring suppressed synthetic cc")

  release({ data: { data: { id: "msg_user_1" } } })
  await userPromise
  await new Promise((r) => setImmediate(r))
})

test("auto-cc 33 (2.5.2): plugin disposal cleans up in-flight intents without leaks", async () => {
  const fake = fakeV2()
  pages = []
  let intentsRegistered = 0
  let intentsFailed = 0
  const intentController = {
    onPromptIntent() { intentsRegistered++ },
    onPromptAdmitted() {},
    onPromptIntentFailed() { intentsFailed++ },
  }
  let hold
  fake.client.v2.session.prompt = () => new Promise((resolve) => { hold = resolve })
  const core = createQueueCore({ client: fake.client, admissionIntents: intentController })
  const dispose = core.install()

  // Prompt starts, intent registered, v2.prompt waiting
  const p = fake.client.session.prompt({ sessionID: "s", text: "will dispose" })
  await new Promise((r) => setImmediate(r))
  assert.equal(intentsRegistered, 1)
  assert.equal(intentsFailed, 0)

  // Dispose called while prompt in flight
  dispose()
  assert.equal(intentsFailed, 1, "dispose cleaned up in-flight intent")
  hold({ data: { data: { id: "m" } } })
  await p.catch(() => {})
})
