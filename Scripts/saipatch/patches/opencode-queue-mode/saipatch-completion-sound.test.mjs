// T-162 completion sound unit suite. No speakers, no real files.
//
// The completion sound is a projection of the authoritative ambient execution
// state, so these tests wire a real createAmbient and observe the SAME DONE
// transition the tint uses. No test keeps a second queue model.
import test from "node:test"
import assert from "node:assert/strict"
import {
  SOUND_DEFAULTS,
  normalizeSettings,
  clampVolume,
  enabledSounds,
  createSoundPicker,
  resetPickerState,
  createCompletionEpisodes,
  createCompletionSound,
  COMPLETION_DONE,
} from "./saipatch-completion-sound.js"
import { buildPlayCommand } from "./saipatch-sound-runtime.js"
import { createAmbient } from "./saipatch-native-queue-2x.js"
import { loadSoundSettings, saveSoundSettings } from "./saipatch-sound-settings.js"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

test("settings persistence: round-trip on disk + malformed file fails safe", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "saipatch-sound-settings-"))
  try {
    const file = path.join(dir, "opencode-queue-mode.settings.json")
    saveSoundSettings(normalizeSettings({ enabled: true, volume: 55, mode: "ordered", disabledSounds: ["PICKUP05.wav"], minimumRunSeconds: 2 }), file)
    const loaded = loadSoundSettings(file)
    assert.equal(loaded.enabled, true)
    assert.equal(loaded.volume, 55)
    assert.equal(loaded.mode, "ordered")
    assert.deepEqual(loaded.disabledSounds, ["PICKUP05.wav"])
    assert.equal(loaded.minimumRunSeconds, 2)
    fs.writeFileSync(file, "{ not json !!", "utf8")
    const safe = loadSoundSettings(file)
    assert.equal(safe.enabled, false, "malformed settings fall back to safe defaults")
    assert.deepEqual(safe, normalizeSettings(SOUND_DEFAULTS))
    // Missing file: safe defaults, no throw.
    assert.deepEqual(loadSoundSettings(path.join(dir, "absent.json")), normalizeSettings(SOUND_DEFAULTS))
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

// Integration rig: a REAL ambient state machine drives the sound module exactly
// as the plugin wires it (listener + admission opener + abort closer).
function rig(settingsOverride = {}) {
  const settings = normalizeSettings({ enabled: true, ...settingsOverride })
  const picker = createSoundPicker(settings)
  const episodes = createCompletionEpisodes()
  const requests = []
  const sound = createCompletionSound({
    settings,
    picker,
    episodes,
    requestPlayback: (sessionID, name, extra) => requests.push({ sessionID, name, ...(extra ?? {}) }),
  })
  const ambient = createAmbient({
    baseBackground: "#101010",
    listeners: [(sessionID, next, previous) => sound.onTransition(sessionID, next, previous)],
  })
  const admit = (sessionID, messageID = "m") => {
    ambient.onAdmitted(sessionID, messageID)
    sound.onEvent(sessionID, "session.next.prompt.admitted")
  }
  const feed = (sessionID, type, properties = {}) => {
    ambient.onEvent(sessionID, type, properties)
    sound.onEvent(sessionID, type, properties)
  }
  const stop = (sessionID, extra = {}) => feed(sessionID, "session.next.step.ended", { finish: "stop", ...extra })
  const abort = (sessionID) => {
    ambient.onAbort(sessionID)
    sound.onAbort(sessionID)
  }
  return { settings, picker, episodes, sound, ambient, requests, admit, feed, stop, abort }
}

test("settings: defaults are safe (disabled, sane volume, all sounds)", () => {
  const s = normalizeSettings(undefined)
  assert.equal(s.enabled, false)
  assert.equal(s.volume, SOUND_DEFAULTS.volume)
  assert.equal(s.mode, "random")
  assert.equal(s.sounds.length, 7)
})

test("settings: malformed JSON shapes fail safe", () => {
  for (const bad of [null, 42, "x", { volume: "loud" }, { mode: "shuffle" }, { sounds: [1, 2] }]) {
    const s = normalizeSettings(bad)
    assert.equal(typeof s.volume, "number")
    assert.ok(["random", "ordered"].includes(s.mode))
    assert.ok(Array.isArray(s.sounds) && s.sounds.every((x) => typeof x === "string"))
  }
})

test("settings: volume clamps 0..100", () => {
  assert.equal(clampVolume(-5), 0)
  assert.equal(clampVolume(250), 100)
  assert.equal(clampVolume(51.4), 51)
  assert.equal(clampVolume("40"), 40)
  assert.equal(clampVolume(Number.NaN), SOUND_DEFAULTS.volume)
})

test("settings: round-trip preserves every field", () => {
  const s = normalizeSettings({ enabled: true, volume: 33, mode: "ordered", disabledSounds: ["PICKUP01.wav"], minimumRunSeconds: 4 })
  const again = normalizeSettings(JSON.parse(JSON.stringify(s)))
  assert.deepEqual(again, s)
})

// A. single turn -> one sound
test("A single turn -> exactly one sound", () => {
  const r = rig()
  r.admit("s")
  r.stop("s")
  assert.equal(r.requests.length, 1)
})

// B. cc1..cc5 queued -> exactly one after the final drain
test("B cc1..cc5 queued -> exactly one sound, after cc5 only", () => {
  const r = rig()
  for (let i = 1; i <= 5; i++) r.admit("s", `cc${i}`)
  const after = []
  for (let i = 1; i <= 5; i++) {
    r.feed("s", "session.next.step.started", { assistantMessageID: `a${i}` })
    r.stop("s", { assistantMessageID: `a${i}` })
    after.push(r.requests.length)
  }
  assert.deepEqual(after, [0, 0, 0, 0, 1], "intermediate queued turns must never play")
})

// C. duplicate final event -> still one
test("C duplicate terminal event -> still exactly one sound", () => {
  const r = rig()
  r.admit("s")
  r.stop("s")
  r.stop("s")
  assert.equal(r.requests.length, 1)
})

// D. initial idle -> zero
test("D initial idle is NOT completion -> zero sounds", () => {
  const r = rig()
  r.stop("s")
  assert.equal(r.requests.length, 0)
})

// E. abort -> zero
test("E abort -> zero sounds", () => {
  const r = rig()
  r.admit("s")
  r.abort("s")
  r.stop("s")
  assert.equal(r.requests.length, 0)
})

// F. error -> zero
test("F terminal error -> zero sounds", () => {
  const r = rig()
  r.admit("s")
  r.feed("s", "session.next.step.failed", {})
  r.stop("s")
  assert.equal(r.requests.length, 0)
})

// G. permission pending -> zero
test("G permission pending -> zero sounds", () => {
  const r = rig()
  r.admit("s")
  r.feed("s", "permission.asked", { sessionID: "s" })
  r.stop("s")
  assert.equal(r.requests.length, 0)
})

// H. question pending -> zero
test("H question pending -> zero sounds", () => {
  const r = rig()
  r.admit("s")
  r.feed("s", "question.asked", { sessionID: "s" })
  r.stop("s")
  assert.equal(r.requests.length, 0)
})

// I. reconnect old DONE -> zero
test("I reconnect/replay of an already-DONE turn -> zero extra sounds", () => {
  const r = rig()
  r.admit("s")
  r.stop("s")
  assert.equal(r.requests.length, 1)
  r.stop("s")
  assert.equal(r.requests.length, 1, "a replayed DONE must not replay the sound")
})

// J. switch to old DONE -> zero
test("J switching to a session already DONE with no episode -> zero sounds", () => {
  const r = rig()
  r.admit("a")
  r.stop("a")
  assert.equal(r.requests.length, 1)
  r.stop("b")
  assert.equal(r.requests.length, 1, "a DONE transition with no open episode never plays")
})

// K. hydration old DONE -> zero
test("K hydrated old DONE (no episode opened) -> zero sounds", () => {
  const settings = normalizeSettings({ enabled: true })
  const picker = createSoundPicker(settings)
  const episodes = createCompletionEpisodes()
  const requests = []
  const sound = createCompletionSound({ settings, picker, episodes, requestPlayback: (s, n) => requests.push(n) })
  sound.onTransition("s", COMPLETION_DONE, "RUNNING")
  assert.equal(requests.length, 0)
  assert.equal(sound.onTransition("s", "RUNNING", "NEUTRAL"), undefined)
})

// L. new prompt after prior DONE -> one new sound
test("L new prompt after prior DONE -> exactly one NEW sound", () => {
  const r = rig()
  r.admit("s")
  r.stop("s")
  assert.equal(r.requests.length, 1)
  r.admit("s")
  r.stop("s")
  assert.equal(r.requests.length, 2)
})

// M. minimumRunSeconds
test("M minimumRunSeconds suppresses instant completions", () => {
  let now = 1_000_000
  const settings = normalizeSettings({ enabled: true, minimumRunSeconds: 5 })
  const picker = createSoundPicker(settings)
  const episodes = createCompletionEpisodes(() => now)
  const requests = []
  const sound = createCompletionSound({
    settings, picker, episodes,
    requestPlayback: (sessionID, name) => requests.push(name),
    clock: () => now,
  })
  const ambient = createAmbient({
    baseBackground: "#101010",
    listeners: [(sessionID, next, previous) => sound.onTransition(sessionID, next, previous)],
  })
  ambient.onAdmitted("s", "m1")
  sound.onEvent("s", "session.next.prompt.admitted")
  now += 1_000
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  assert.equal(requests.length, 0, "1s run is below the 5s floor")
  // A suppressed completion consumed its episode; a NEW admitted turn that
  // runs long enough plays.
  ambient.onAdmitted("s", "m2")
  sound.onEvent("s", "session.next.prompt.admitted")
  now += 6_000
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  assert.equal(requests.length, 1)
})

// N. random enabled-only
test("N random: enabled-only pool, no immediate repeat", () => {
  resetPickerState()
  const settings = normalizeSettings({ mode: "random", disabledSounds: ["PICKUP01.wav", "PICKUP02.wav", "PICKUP03.wav", "PICKUP04.wav", "PICKUP05.wav", "PICKUP06.wav"] })
  const picker = createSoundPicker(settings)
  assert.deepEqual(enabledSounds(settings), ["PICKUP07.wav"])
  assert.equal(picker.next(), "PICKUP07.wav")
  const pool = normalizeSettings({ mode: "random", disabledSounds: ["PICKUP01.wav"] })
  const p2 = createSoundPicker(pool)
  const picks = new Set(Array.from({ length: 40 }, () => p2.next()))
  assert.ok(!picks.has("PICKUP01.wav"))
  assert.ok(picks.size >= 2)
})

// O. ordered cycle
test("O ordered: enabled-only cycle in order", () => {
  resetPickerState()
  const settings = normalizeSettings({ mode: "ordered", disabledSounds: ["PICKUP02.wav"] })
  const picker = createSoundPicker(settings)
  const seq = Array.from({ length: 7 }, () => picker.next())
  const expected = ["PICKUP01.wav", "PICKUP03.wav", "PICKUP04.wav", "PICKUP05.wav", "PICKUP06.wav", "PICKUP07.wav", "PICKUP01.wav"]
  assert.deepEqual(seq, expected)
})

test("no enabled sounds -> no playback", () => {
  resetPickerState()
  const settings = normalizeSettings({ disabledSounds: SOUND_DEFAULTS.sounds })
  assert.deepEqual(enabledSounds(settings), [])
  const picker = createSoundPicker(settings)
  assert.equal(picker.next(), null)
  const episodes = createCompletionEpisodes()
  const requests = []
  const sound = createCompletionSound({ settings, picker, episodes, requestPlayback: (s, n) => requests.push(n) })
  const ambient = createAmbient({
    baseBackground: "#101010",
    listeners: [(sessionID, next, previous) => sound.onTransition(sessionID, next, previous)],
  })
  ambient.onAdmitted("s", "m1")
  sound.onEvent("s", "session.next.prompt.admitted")
  ambient.onEvent("s", "session.next.step.ended", { finish: "stop" })
  assert.equal(requests.length, 0)
})

test("preview does not consume a completion episode", () => {
  const r = rig()
  r.sound.preview("PICKUP03.wav")
  assert.equal(r.requests.length, 1)
  assert.equal(r.requests[0].preview, true)
  r.admit("s")
  r.stop("s")
  assert.equal(r.requests.length, 2)
  assert.equal(r.requests[1].preview, undefined)
})

// Clause 16: prove the real playback API receives 0, 37 and 100 — not just a
// number printed somewhere. WMPlayer.OCX player.settings.volume is the genuine
// per-playback volume control (0..100).
test("play command: volume 0/37/100 reaches player.settings.volume; WAV path quoted", () => {
  const args = buildPlayCommand({ file: "C:\\dir with space\\PICKUP0'1.wav", volumePercent: 37 })
  const cmd = args[args.length - 1]
  assert.ok(cmd.includes("'C:\\dir with space\\PICKUP0''1.wav'"), "single quotes in the path are doubled")
  assert.ok(cmd.includes("$player.settings.volume = 37"), "volume 37 reaches the playback API")
  assert.ok(cmd.includes(".newMedia("), "the WAV is opened through the real Windows media API")
  assert.ok(buildPlayCommand({ file: "x.wav", volumePercent: -3 }).at(-1).includes("$player.settings.volume = 0"))
  assert.ok(buildPlayCommand({ file: "x.wav", volumePercent: 100 }).at(-1).includes("$player.settings.volume = 100"))
})
