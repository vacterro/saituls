// WS-ATT attention unit suite. No speakers, no windows, no fixtures.
// Attention is a separate projection from the authoritative ambient DONE/NEEDS_HUMAN.
import test from "node:test"
import assert from "node:assert/strict"
import { createAmbient } from "./saipatch-native-queue-2x.js"
import { normalizeAttentionSettings, ATTENTION_DEFAULTS, createAttentionEpisodes, createAttention } from "./saipatch-attention.js"

function rig(settingsOverride = {}) {
  const settings = normalizeAttentionSettings({ ...ATTENTION_DEFAULTS, ...settingsOverride })
  const episodes = createAttentionEpisodes()
  const pulses = []
  const flashes = []
  const controller = createAttention({
    settings, episodes,
    requestPulse: (sessionID, kind, n) => pulses.push({ sessionID, kind, n }),
    requestFlash: (sessionID, kind, n) => flashes.push({ sessionID, kind, n }),
  })
  const ambient = createAmbient({
    baseBackground: "#101010",
    listeners: [(s, next, prev) => controller.onTransition(s, next, prev)],
  })
  const admit = (s, mid = "m") => { ambient.onAdmitted(s, mid); episodes.admit(s) }
  const feed = (s, type, props = {}) => ambient.onEvent(s, type, props)
  const stop = (s, extra = {}) => feed(s, "session.next.step.ended", { finish: "stop", ...extra })
  return { settings, episodes, pulses, flashes, controller, ambient, admit, feed, stop }
}

test("A RUNNING -> no attention", () => {
  const r = rig()
  r.admit("s")
  assert.equal(r.pulses.length, 0)
  assert.equal(r.flashes.length, 0)
})

test("B cc1..cc5 -> exactly one DONE after final drain", () => {
  const r = rig()
  for (let i = 1; i <= 5; i++) r.admit("s", `cc${i}`)
  for (let i = 1; i <= 5; i++) {
    r.feed("s", "session.next.step.started", { assistantMessageID: `a${i}` })
    r.stop("s", { assistantMessageID: `a${i}` })
    if (i < 5) { assert.equal(r.pulses.length, 0, `pulse at cc${i}`); assert.equal(r.flashes.length, 0) }
  }
  assert.equal(r.pulses.length, 1, "exactly one pulse on final DONE")
  assert.equal(r.flashes.length, 1, "exactly one flash on final DONE")
  assert.equal(r.pulses[0].kind, "DONE")
})

test("C duplicate DONE -> no second episode", () => {
  const r = rig()
  r.admit("s")
  r.stop("s")
  r.stop("s")
  assert.equal(r.pulses.length, 1)
  assert.equal(r.flashes.length, 1)
})

test("D permission.asked -> exactly one NEEDS_HUMAN", () => {
  const r = rig()
  r.admit("s")
  r.feed("s", "permission.asked", {})
  assert.equal(r.pulses.length, 1)
  assert.equal(r.pulses[0].kind, "NEEDS_HUMAN")
  assert.equal(r.flashes.length, 1)
})

test("E question.asked -> exactly one NEEDS_HUMAN", () => {
  const r = rig()
  r.admit("s")
  r.feed("s", "question.asked", {})
  assert.equal(r.pulses.length, 1)
  assert.equal(r.pulses[0].kind, "NEEDS_HUMAN")
})

test("F duplicate permission/question -> no spam", () => {
  const r = rig()
  r.admit("s")
  r.feed("s", "permission.asked", {})
  r.feed("s", "permission.asked", {})
  r.feed("s", "question.asked", {})
  assert.equal(r.pulses.length, 1)
  assert.equal(r.flashes.length, 1)
})

test("G resolve -> RUNNING then final DONE -> new DONE episode", () => {
  const r = rig()
  r.admit("s")
  r.feed("s", "permission.asked", {})
  assert.equal(r.pulses.length, 1)
  r.feed("s", "permission.replied", {})
  // permission cleared, still RUNNING (admitted > completed unless we stopped)
  assert.equal(r.pulses.length, 1, "reply must not pulse")
  r.stop("s", {})
  assert.equal(r.pulses.length, 2)
  assert.equal(r.pulses[1].kind, "DONE")
})

test("H hydrate old DONE -> zero", () => {
  const settings = normalizeAttentionSettings(ATTENTION_DEFAULTS)
  const episodes = createAttentionEpisodes()
  const pulses = []
  const controller = createAttention({ settings, episodes, requestPulse: (s,k) => pulses.push(k) })
  // No admission -> no episode; calling transition as if hydrated should not pulse.
  controller.onTransition("s", "DONE", "RUNNING")
  assert.equal(pulses.length, 0)
})

test("I reconnect old DONE -> zero", () => {
  const r = rig()
  r.admit("s"); r.stop("s")
  assert.equal(r.pulses.length, 1)
  // Reconnect replays same DONE transition:
  r.ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  assert.equal(r.pulses.length, 1, "reconnect duplicate DONE must not pulse")
})

test("J switch to old DONE -> zero", () => {
  const r = rig()
  r.admit("a"); r.stop("a")
  assert.equal(r.pulses.length, 1)
  // A different session reaching DONE without its own episode:
  r.stop("b")
  assert.equal(r.pulses.length, 1)
})

test("settings: disabled triggers suppress attention", () => {
  const r = rig({ triggerOnDone: false })
  r.admit("s"); r.stop("s")
  assert.equal(r.pulses.length, 0)
  assert.equal(r.flashes.length, 0)
  const r2 = rig({ triggerOnNeedsHuman: false })
  r2.admit("s"); r2.feed("s", "permission.asked", {})
  assert.equal(r2.pulses.length, 0)
})

test("settings: defaults are safe (4/5 bounds)", () => {
  const s = normalizeAttentionSettings(undefined)
  assert.equal(s.visualPulseEnabled, true)
  assert.equal(s.taskbarFlashEnabled, true)
  assert.equal(s.pulseCount, 4)
  assert.equal(s.taskbarFlashCount, 5)
  assert.equal(s.triggerOnDone, true)
  assert.equal(s.triggerOnNeedsHuman, true)
  const clamped = normalizeAttentionSettings({ pulseCount: 99, taskbarFlashCount: -2 })
  assert.equal(clamped.pulseCount, 10)
  assert.equal(clamped.taskbarFlashCount, 1)
})

test("WS-ATT-ACK: focus ack does not clear underlying DONE", () => {
  const r = rig()
  r.admit("s"); r.stop("s")
  assert.equal(r.pulses.length, 1)
  r.controller.onFocusAck("s")
  // Underlying ambient still DONE
  assert.equal(r.ambient.evaluate("s"), "DONE")
  // No extra pulse on ack
  assert.equal(r.pulses.length, 1)
})
