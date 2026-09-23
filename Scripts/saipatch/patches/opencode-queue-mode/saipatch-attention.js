// saipatch-attention -- WS-ATT / APPEND 20260910_0327
// Attention is a SEPARATE projection from the authoritative ambient state
// (DONE/NEEDS_HUMAN/RUNNING/NEUTRAL). One episode per NEW transition into the
// target state; hydration/reconnect/duplicate/switch never emit. Admission-gated:
// a DONE attention requires a prior admission for that session (hasAdmitted).
export const ATTENTION = { NONE: "ATTENTION_NONE", PULSE: "ATTENTION_PULSE" }
export const ATTENTION_DONE = "DONE"
export const ATTENTION_NEEDS_HUMAN = "NEEDS_HUMAN"

export const ATTENTION_DEFAULTS = {
  visualPulseEnabled: true,
  taskbarFlashEnabled: true,
  pulseCount: 4,
  taskbarFlashCount: 5,
  triggerOnDone: true,
  triggerOnNeedsHuman: true,
  onlyWhenUnfocused: false,
}

function clampInt(v, lo, hi, fallback) {
  const n = typeof v === "number" ? v : Number.parseInt(v, 10)
  if (!Number.isFinite(n)) return fallback
  return Math.min(hi, Math.max(lo, Math.round(n)))
}

export function normalizeAttentionSettings(raw) {
  const src = raw && typeof raw === "object" ? raw : {}
  return {
    visualPulseEnabled: src.visualPulseEnabled !== false,
    taskbarFlashEnabled: src.taskbarFlashEnabled !== false,
    pulseCount: clampInt(src.pulseCount, 1, 10, ATTENTION_DEFAULTS.pulseCount),
    taskbarFlashCount: clampInt(src.taskbarFlashCount, 1, 20, ATTENTION_DEFAULTS.taskbarFlashCount),
    triggerOnDone: src.triggerOnDone !== false,
    triggerOnNeedsHuman: src.triggerOnNeedsHuman !== false,
    onlyWhenUnfocused: src.onlyWhenUnfocused === true,
  }
}

export function createAttentionEpisodes(clock = () => Date.now()) {
  const st = new Map()
  const entry = (id) => { let e = st.get(id); if (!e) { e = { doneEmitted: false, needsHumanEmitted: false, lastAmbient: null, hasAdmitted: false }; st.set(id, e) } return e }
  return {
    noteAmbient(sessionID, next, previous) {
      const e = entry(sessionID)
      e.lastAmbient = next
      if (next !== ATTENTION_DONE && e.doneEmitted) e.doneEmitted = false
      if (next !== ATTENTION_NEEDS_HUMAN && e.needsHumanEmitted) e.needsHumanEmitted = false
      void previous
    },
    admit(sessionID) { entry(sessionID).hasAdmitted = true },
    canEmitDone(sessionID) { const e = st.get(sessionID); return Boolean(e && !e.doneEmitted && e.hasAdmitted) },
    canEmitNeedsHuman(sessionID) { const e = st.get(sessionID); return Boolean(e && !e.needsHumanEmitted && e.hasAdmitted) },
    markDone(sessionID) { entry(sessionID).doneEmitted = true },
    markNeedsHuman(sessionID) { entry(sessionID).needsHumanEmitted = true },
    isDoneArmed(sessionID) { const e = st.get(sessionID); return Boolean(e && !e.doneEmitted && e.hasAdmitted) },
    isNeedsHumanArmed(sessionID) { const e = st.get(sessionID); return Boolean(e && !e.needsHumanEmitted && e.hasAdmitted) },
    inspect(sessionID) { const e = st.get(sessionID); return e ? { ...e } : undefined },
    forget(sessionID) { st.delete(sessionID) },
    clear() { st.clear() },
    _clock: clock,
  }
}

export function createAttention({ settings, episodes, requestPulse, requestFlash, log } = {}) {
  if (!settings || !episodes) throw new Error("attention requires settings and episodes")
  function shouldTrigger(kind) {
    if (kind === ATTENTION_DONE) return settings.triggerOnDone !== false && (settings.visualPulseEnabled || settings.taskbarFlashEnabled)
    if (kind === ATTENTION_NEEDS_HUMAN) return settings.triggerOnNeedsHuman !== false && (settings.visualPulseEnabled || settings.taskbarFlashEnabled)
    return false
  }
  function onTransition(sessionID, next, previous) {
    if (!sessionID) return
    episodes.noteAmbient(sessionID, next, previous)
    if (next === ATTENTION_DONE && previous !== ATTENTION_DONE) {
      if (!shouldTrigger(ATTENTION_DONE)) return
      if (!episodes.canEmitDone(sessionID)) return
      episodes.markDone(sessionID)
      if (settings.visualPulseEnabled) requestPulse?.(sessionID, ATTENTION_DONE, settings.pulseCount)
      if (settings.taskbarFlashEnabled) requestFlash?.(sessionID, ATTENTION_DONE, settings.taskbarFlashCount)
      log?.("log", `attention DONE ${sessionID}`)
      return
    }
    if (next === ATTENTION_NEEDS_HUMAN && previous !== ATTENTION_NEEDS_HUMAN) {
      if (!shouldTrigger(ATTENTION_NEEDS_HUMAN)) return
      if (!episodes.canEmitNeedsHuman(sessionID)) return
      episodes.markNeedsHuman(sessionID)
      if (settings.visualPulseEnabled) requestPulse?.(sessionID, ATTENTION_NEEDS_HUMAN, settings.pulseCount)
      if (settings.taskbarFlashEnabled) requestFlash?.(sessionID, ATTENTION_NEEDS_HUMAN, settings.taskbarFlashCount)
      log?.("log", `attention NEEDS_HUMAN ${sessionID}`)
      return
    }
  }
  function onNeedsHumanAsked(sessionID) {
    if (!sessionID) return
    if (!shouldTrigger(ATTENTION_NEEDS_HUMAN)) return
    if (!episodes.canEmitNeedsHuman(sessionID)) return
    episodes.noteAmbient(sessionID, ATTENTION_NEEDS_HUMAN, null)
    episodes.markNeedsHuman(sessionID)
    if (settings.visualPulseEnabled) requestPulse?.(sessionID, ATTENTION_NEEDS_HUMAN, settings.pulseCount)
    if (settings.taskbarFlashEnabled) requestFlash?.(sessionID, ATTENTION_NEEDS_HUMAN, settings.taskbarFlashCount)
  }
  function onFocusAck(sessionID) { return sessionID }
  return { onTransition, onNeedsHumanAsked, onFocusAck }
}

export function pulsePlan({ pulseCount = ATTENTION_DEFAULTS.pulseCount } = {}) {
  const n = clampInt(pulseCount, 1, 10, ATTENTION_DEFAULTS.pulseCount)
  const perPulseMs = 520
  const totalMs = n * perPulseMs
  const frames = n
  return { n, perPulseMs, totalMs, frames }
}
