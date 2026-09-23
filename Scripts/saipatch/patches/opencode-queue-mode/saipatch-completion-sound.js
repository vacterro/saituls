// saipatch-completion-sound — T-162 authoritative completion sound module.
//
// COMPLETION PREDICATE (clauses 8-9): a projection of the SAME authoritative
// ambient execution state the session tint uses — never a second queue counter,
// never assistant prose, never "DONE" markers in output. The ambient machine
// already encodes: last terminal finish "stop" (never error/abort), no active
// step, admitted == completed (queue drained), no pending permission/question.
// The sound fires exactly once on the ambient transition previous != DONE &&
// next == DONE while a completion episode opened by a real admission is open
// and unplayed. Initial idle is NOT completion. Hydration / reconnect / session
// switch / abort / provider error / tool-only finish never play.

export const SOUND_DEFAULTS = {
  enabled: false,
  volume: 70,
  mode: "random",
  sounds: ["PICKUP01.wav", "PICKUP02.wav", "PICKUP03.wav", "PICKUP04.wav", "PICKUP05.wav", "PICKUP06.wav", "PICKUP07.wav"],
  disabledSounds: [],
  minimumRunSeconds: 0,
}

const MODES = new Set(["random", "ordered"])

export function clampVolume(value) {
  const n = typeof value === "number" ? value : Number.parseFloat(value)
  if (!Number.isFinite(n)) return SOUND_DEFAULTS.volume
  return Math.min(100, Math.max(0, Math.round(n)))
}

export function normalizeSettings(raw) {
  const src = raw && typeof raw === "object" ? raw : {}
  const mode = MODES.has(src.mode) ? src.mode : SOUND_DEFAULTS.mode
  const sounds = Array.isArray(src.sounds) && src.sounds.every((s) => typeof s === "string")
    ? src.sounds.filter((s) => s.length > 0)
    : [...SOUND_DEFAULTS.sounds]
  const disabled = Array.isArray(src.disabledSounds) && src.disabledSounds.every((s) => typeof s === "string")
    ? src.disabledSounds.filter((s) => sounds.includes(s))
    : []
  const seconds = typeof src.minimumRunSeconds === "number" && Number.isFinite(src.minimumRunSeconds)
    ? Math.max(0, src.minimumRunSeconds)
    : SOUND_DEFAULTS.minimumRunSeconds
  return {
    enabled: src.enabled === true,
    volume: clampVolume(src.volume),
    mode,
    sounds,
    disabledSounds: disabled,
    minimumRunSeconds: seconds,
  }
}

export function enabledSounds(settings) {
  const set = new Set(settings.disabledSounds ?? [])
  return (settings.sounds ?? []).filter((name) => !set.has(name))
}

// Random among enabled sounds, avoiding an immediate repeat when more than one
// is available. Process-local only — no cross-window coordination (clause 22).
export function createSoundPicker(settings) {
  let last
  return {
    next() {
      const pool = enabledSounds(settings)
      if (pool.length === 0) return null
      let pick
      if (settings.mode === "ordered") {
        if (pickerState.orderCursor === undefined || pickerState.orderCursor >= pool.length) {
          pickerState.orderCursor = 0
        }
        pick = pool[pickerState.orderCursor]
        pickerState.orderCursor += 1
        return pick
      }
      if (pool.length === 1) {
        pick = pool[0]
      } else {
        do {
          pick = pool[Math.floor(Math.random() * pool.length)]
        } while (pick === last)
      }
      last = pick
      return pick
    },
    reset() {
      last = undefined
      pickerState.orderCursor = undefined
    },
  }
}

// Module-scoped ordered cursor: one process = one playback sequence. Kept on
// an object so createSoundPicker stays a pure factory in tests.
const pickerState = {}

export function resetPickerState() {
  pickerState.orderCursor = undefined
}

// Per-session notification dedup (clause 20). A new admitted turn OPENS a new
// completion episode; hydration/reconnect/navigation does not. Notification
// state only — never influences native scheduling.
export function createCompletionEpisodes(clock = () => Date.now()) {
  const episodes = new Map()

  function entry(sessionID) {
    let e = episodes.get(sessionID)
    if (!e) {
      e = { open: false, played: false, startedAt: 0 }
      episodes.set(sessionID, e)
    }
    return e
  }

  return {
    openTurn(sessionID) {
      const e = entry(sessionID)
      e.open = true
      e.played = false
      e.startedAt = clock()
    },
    closeTurn(sessionID) {
      const e = episodes.get(sessionID)
      if (e) e.open = false
    },
    canPlay(sessionID) {
      const e = episodes.get(sessionID)
      return Boolean(e && e.open && !e.played)
    },
    isOpen(sessionID) {
      return Boolean(episodes.get(sessionID)?.open)
    },
    inspect(sessionID) {
      const e = episodes.get(sessionID)
      return e ? { ...e } : undefined
    },
    markPlayed(sessionID) {
      const e = entry(sessionID)
      e.played = true
    },
    forget(sessionID) {
      episodes.delete(sessionID)
    },
    clear() {
      episodes.clear()
    },
  }
}

// The authoritative DONE token shared with the ambient projection. Completion
// sound never re-derives it; it observes the ambient transition.
export const COMPLETION_DONE = "DONE"

// CompletionSound: a pure projection of the authoritative ambient execution
// state (clauses 8-9). It does NOT count queue depth, completions or pending
// humans itself — the ambient state machine already owns that truth. It fires
// once when the SAME state the ambient tint shows transitions into DONE:
//   previous != DONE && next == DONE && episode open && not already played
// A new admitted prompt opens an episode; abort / terminal failure close it.
// Hydration, reconnect and session navigation never open one.
export function createCompletionSound({ settings, picker, episodes, requestPlayback, log, clock } = {}) {
  if (!settings || !picker || !episodes) throw new Error("completion sound requires settings, picker and episodes")
  const now = clock ?? (() => Date.now())

  function minimumRunSatisfied(sessionID) {
    if (!settings.minimumRunSeconds || settings.minimumRunSeconds <= 0) return true
    const e = episodes.inspect(sessionID)
    if (!e || !e.startedAt) return true
    return (now() - e.startedAt) / 1000 >= settings.minimumRunSeconds
  }

  function openEpisode(sessionID) {
    if (sessionID) episodes.openTurn(sessionID)
  }

  function closeEpisode(sessionID) {
    if (sessionID) episodes.closeTurn(sessionID)
  }

  // Driven by the ambient state machine (the authoritative execution truth).
  // A whole cc1..cc5 drain is exactly one NEUTRAL/RUNNING -> DONE transition,
  // so intermediate turns can never request a sound.
  function onTransition(sessionID, next, previous) {
    if (!sessionID || settings.enabled !== true) return
    if (next !== COMPLETION_DONE || previous === COMPLETION_DONE) return
    if (!episodes.canPlay(sessionID)) return
    if (!minimumRunSatisfied(sessionID)) {
      // A suppressed completion consumes its episode so a duplicate DONE
      // transition for the same turn can never play later.
      episodes.markPlayed(sessionID)
      return
    }
    episodes.markPlayed(sessionID)
    const name = picker.next()
    if (name) requestPlayback?.(sessionID, name)
  }

  // Opening / closing only. Hydration and reconnect do not reach this path.
  function onEvent(sessionID, type) {
    if (!sessionID) return
    if (type === "session.next.prompt.admitted") openEpisode(sessionID)
    else if (type === "session.next.step.failed") closeEpisode(sessionID)
  }

  function onAbort(sessionID) {
    closeEpisode(sessionID)
  }

  function preview(name) {
    // Preview never consumes a completion episode (clause 26).
    requestPlayback?.(null, name, { preview: true })
  }

  return { onEvent, onTransition, openEpisode, closeEpisode, onAbort, preview }
}
