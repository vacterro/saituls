// saipatch-native-queue-2x — OpenCode TUI plugin, generation 2.x
//
// NATIVE QUEUE CORE:
//   Every ordinary prompt admission goes through client.v2.session.prompt with
//   delivery:"queue". The native V2 scheduler owns promotion, ordering, steer
//   and execution boundaries. This plugin keeps NO queue, NO timers, NO busy
//   polling and NO replay loop: it only translates the composer's legacy
//   admission seam onto the native one and maps abort onto v2 interruption.
//
// AMBIENT SESSION STATUS:
//   A pure projection over authoritative native events
//   (session.status, session.next.step.*, permission.asked/replied,
//   question.asked/replied/replied) into NEUTRAL / RUNNING / NEEDS_HUMAN /
//   DONE, expressed by activating one of four background-tinted theme
//   variants. Execution never reads the visual state; the visual state never
//   writes execution.
//
// MESSAGE PROJECTION:
//   Hydration (client.session.messages) returns the native projected messages
//   (GET /api/session/{id}/message) re-shaped into the legacy {info, parts}
//   contract the transcript consumes; it stays the authority. While native
//   turns stream, the bridge projects the native event stream (prompted /
//   step.started / text.* / step.ended / step.failed) into the live sync
//   stores exposed through api.state in place: new rows are added once,
//   existing rows and parts are updated by stable identity, and every new-row
//   insert is verified; three unverified inserts fail the live seam closed
//   (hydration alone keeps the transcript correct).
//
// COMPLETION SOUND (T-162):
//   One playback request per completed queue drain, decided by the SAME
//   authoritative native execution truth as ambient DONE (finish:"stop" on
//   the final step, admitted == completed, no pending human turn, per-session
//   episode dedup). Never on initial idle, hydration, abort, error, permission
//   or question waits. Playback is asynchronous and never blocks the TUI;
//   failure is a warning only. User settings live OUTSIDE the OpenCode
//   install under %LOCALAPPDATA%\SAITULS\SAIPATCH\.

import fs from "node:fs"
import path from "node:path"
import os from "node:os"
import { spawn } from "node:child_process"
import {
  SOUND_DEFAULTS,
  normalizeSettings,
  clampVolume,
  enabledSounds,
  createSoundPicker,
  createCompletionEpisodes,
  createCompletionSound,
} from "./saipatch-completion-sound.js"

// DEBUG-ONLY perf trace — gated by SAIPATCH_QUEUE_DEBUG so it never exists in production
const perfTrace = (label, record) => {
  if (process.env.SAIPATCH_QUEUE_DEBUG) log("log", `perf ${label} ${JSON.stringify({ts: performance.now(), ...record})}`)
}
import {
  ATTENTION_DEFAULTS,
  normalizeAttentionSettings,
  createAttentionEpisodes,
  createAttention,
  ATTENTION,
  pulsePlan,
} from "./saipatch-attention.js"
import {
  AUTO_CC_DEFAULTS,
  AUTO_CC_PROMPT,
  AUTO_CC_EXHAUSTED,
  AUTO_CC_STATES,
  normalizeAutoContinueSettings,
  classifyStall,
  createAutoContinue,
} from "./saipatch-auto-continue.js"
import { buildPlayCommand } from "./saipatch-sound-runtime.js"

export { SOUND_DEFAULTS, normalizeSettings, clampVolume, enabledSounds, createSoundPicker, createCompletionEpisodes, createCompletionSound, buildPlayCommand }
export { normalizeAutoContinueSettings, classifyStall, createAutoContinue, AUTO_CC_DEFAULTS, AUTO_CC_PROMPT, AUTO_CC_EXHAUSTED, AUTO_CC_STATES }

// HOST SEAM B bootstrap (T-144): the exact-host compatibility seam patches the
// single TUI dispatch point to call the global hook `__SAIPATCH(w, H)` — the
// raw SSE wrapper plus the host sync consumer — with a catch fallback to the
// original dispatch, so a missing hook can never kill the SSE loop (§7: early
// boot events keep flowing through the original path). The hook name is
// SAIPATCH-namespaced (§5); `_P` was kitchen-only diagnostics.
export const HOST_HOOK = "__SAIPATCH"
// T-144: the product binary seam (host-patch.json) calls the compact global
// `__SPB(w, emit)` — binary alias of `__SAIPATCH`. The plugin re-exports the
// alias so tests and any future host patch agree on one constant.
export const HOST_SEAM_HOOK = "__SPB"
const HOST_HOOK_ALIASES = [HOST_HOOK, HOST_SEAM_HOOK]
// Pre-init passthrough: exactly the original host dispatch semantics — sync
// frames dropped, original payload handed to the sync consumer with the
// original directory/workspace context. Never throws.
// T-144 diagnostic sink: the ONLY capture-proven channel is the
// SAIPATCH_QUEUE_DEBUG file (appendFileSync; the TUI renderer swallows both
// console.* and pty stderr). Queued until node:fs resolves so the earliest
// module-top dispatches are not lost.
let seamDebugFs = null
const seamDebugPending = []
if (typeof process !== "undefined" && process.env?.SAIPATCH_QUEUE_DEBUG) {
  import("node:fs")
    .then((m) => {
      seamDebugFs = m
      try { for (const line of seamDebugPending) m.appendFileSync(process.env.SAIPATCH_QUEUE_DEBUG, line) } catch {}
      seamDebugPending.length = 0
    })
    .catch(() => {})
}
const seamDebug = (line) => {
  if (typeof process === "undefined" || !process.env?.SAIPATCH_QUEUE_DEBUG) return
  if (seamDebugFs) { try { seamDebugFs.appendFileSync(process.env.SAIPATCH_QUEUE_DEBUG, line) } catch {} }
  else seamDebugPending.push(line)
}
const hostProjectorPassthrough = (w, H) => {
  try {
    const payload = w?.payload
    // T-144 diagnostic: the passthrough is SILENT by design, which makes a
    // dead seam indistinguishable from an unwound install.
    if (payload && payload.type !== "sync") seamDebug(`${new Date().toISOString()} [saipatch-queue-2x] seam-passthrough ${payload.type}\n`)
    if (typeof H !== "function" || !payload || payload.type === "sync") return
    H(payload, { directory: w.directory, workspace: w.workspace })
  } catch {}
}
if (typeof globalThis !== "undefined") {
  // Pre-init passthrough on BOTH aliases, but never clobber an existing
  // implementation (§5: an earlier owner's hook keeps its value).
  for (const alias of HOST_HOOK_ALIASES) {
    if (typeof globalThis[alias] !== "function") {
      globalThis[alias] = hostProjectorPassthrough
    }
  }
}

const COLOR = {
  running: "#b35a1f",
  needsHuman: "#6f4fb0",
  done: "#3f7d4a",
  mixRatio: { running: 0.16, needsHuman: 0.2, done: 0.13 },
}

const AMBIENT = { NEUTRAL: "NEUTRAL", RUNNING: "RUNNING", NEEDS_HUMAN: "NEEDS_HUMAN", DONE: "DONE" }
export { AMBIENT }

const THEME_SUFFIX = {
  NEUTRAL: null,
  RUNNING: "saipatch-queue-running",
  NEEDS_HUMAN: "saipatch-queue-needs-human",
  DONE: "saipatch-queue-done",
}

// The host status bar reads every projected assistant row UNguarded:
// `findLast((m) => m.role === "assistant" && m.tokens.output > 0)`
// (T-144 render-crash evidence: "undefined is not an object (evaluating
// 'HU.tokens.output')" — the crash itself proved the live insert lands in the
// real legacy store). Every row we project must therefore carry the complete
// legacy Message info shape, never a partial one.
export function emptyTokens() {
  return { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }
}

export function normalizeTokens(tokens) {
  if (!tokens || typeof tokens !== "object") return emptyTokens()
  const cache = tokens.cache && typeof tokens.cache === "object" ? tokens.cache : {}
  const num = (value) => (typeof value === "number" && Number.isFinite(value) ? value : 0)
  return {
    input: num(tokens.input),
    output: num(tokens.output),
    reasoning: num(tokens.reasoning),
    cache: { read: num(cache.read), write: num(cache.write) },
  }
}

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return 0
  return Math.min(1, Math.max(0, value))
}

// The plugin theme API exposes colors as RGBA objects with r/g/b/a normalized
// to 0..1 (OpenTUI RGBA); theme JSON files carry hex strings. Convert so the
// background can be mixed and cloned into variant theme files.
export function rgbaToHex(value) {
  if (typeof value === "string") return value
  const components = [value?.r, value?.g, value?.b]
  if (components.some((channel) => typeof channel !== "number" || Number.isNaN(channel))) return undefined
  const byte = (channel) => Math.round(clamp01(channel) * 255)
  const alpha = typeof value?.a === "number" ? byte(value.a) : 255
  const parts = components.map(byte)
  if (alpha !== 255) parts.push(alpha)
  return "#" + parts.map((channel) => channel.toString(16).padStart(2, "0")).join("")
}

export function mixHex(base, target, ratio) {
  const parse = (value) => {
    if (typeof value !== "string") return null
    const match = /^#?([0-9a-fA-F]{6})$/.exec(value.trim())
    if (!match) return null
    const int = Number.parseInt(match[1], 16)
    return [(int >> 16) & 255, (int >> 8) & 255, int & 255]
  }
  const from = parse(base)
  const to = parse(target)
  if (!from || !to) return base
  const t = clamp01(ratio)
  const mixed = from.map((channel, index) => Math.round(channel + (to[index] - channel) * t))
  return "#" + mixed.map((channel) => channel.toString(16).padStart(2, "0")).join("")
}

export function createAmbient(options = {}) {
  const colors = { ...COLOR, ...(options.colors ?? {}) }
  const ratio = { ...COLOR.mixRatio, ...(options.mixRatio ?? {}) }
  let state = AMBIENT.NEUTRAL
  let selectedSessionID
  const sessions = new Map()

  const defaultSession = () => ({
    busy: false,
    activeSteps: 0,
    lastFinish: undefined,
    aborted: false,
    pendingHuman: false,
    admittedTotal: 0,
    completedTurns: 0,
    generation: 0,
    assistantMessageID: undefined,
  })

  const MAX_AMBIENT_SESSIONS = 500
  const record = (sessionID) => {
    let entry = sessions.get(sessionID)
    if (!entry) {
      if (sessions.size >= MAX_AMBIENT_SESSIONS) {
        for (const [k] of sessions) {
          if (k !== selectedSessionID) {
            sessions.delete(k)
            break
          }
        }
      }
      entry = defaultSession()
      sessions.set(sessionID, entry)
    }
    return entry
  }

  function evaluate(sessionID) {
    const entry = sessions.get(sessionID)
    if (!entry) return AMBIENT.NEUTRAL
    if (entry.pendingHuman) return AMBIENT.NEEDS_HUMAN
    if (entry.aborted) return AMBIENT.NEUTRAL
    // The native V2 runner publishes no session.status events (the busy/idle
    // publisher is legacy-runner machinery only), so RUNNING/DONE derive from
    // durable execution events alone: an active step or an admitted-but-
    // unfinished turn means RUNNING; a finished turn with nothing admitted
    // left means DONE.
    if (entry.activeSteps > 0 || entry.admittedTotal > entry.completedTurns) return AMBIENT.RUNNING
    if (entry.lastFinish === "stop") return AMBIENT.DONE
    return AMBIENT.NEUTRAL
  }

  function emit(sessionID, previous, next) {
    if (previous === next) return
    state = next
    for (const listener of options.listeners ?? []) listener(sessionID, next, previous)
  }

  function update(sessionID, mutation) {
    const entry = record(sessionID)
    const previous = evaluate(sessionID)
    mutation(entry)
    const next = evaluate(sessionID)
    if (options.debug) {
      options.debug(sessionID, {
        busy: entry.busy,
        steps: entry.activeSteps,
        admitted: entry.admittedTotal,
        completed: entry.completedTurns,
        last: entry.lastFinish,
        aborted: entry.aborted,
        human: entry.pendingHuman,
        from: previous,
        to: next,
      })
    }
    emit(sessionID, previous, next)
  }

  // W2-003: authoritative repair of a session's execution record from native
  // history after a reconnect.
  //
  // It deliberately emits NO transition. A reconnect that discovers an
  // already-finished turn must not play its completion sound -- that would be
  // a retrospective sound for a turn nobody watched finish -- and flipping to
  // DONE without an observed DONE edge would fabricate one.
  //
  // `verdict` comes from the native history itself and has three cases, not
  // two, because "finished" and "still running" are both provable but their
  // absence is neither:
  //   "terminal" - the newest assistant row carries finish:"stop".
  //   "running"  - there is an assistant row with no finish: the turn is
  //                demonstrably in flight, so RUNNING is EVIDENCE, not a
  //                stale claim, and must not be downgraded.
  //   "unknown"  - nothing to conclude. This fails SAFE to NEUTRAL: visibly
  //                non-running rather than an indefinitely stale RUNNING, and
  //                emphatically not a fabricated DONE.
  const reconcileFromHistory = (sessionID, verdict) => {
    if (typeof sessionID !== "string" || sessionID.length === 0) return AMBIENT.NEUTRAL
    const entry = sessions.get(sessionID)
    if (!entry) return AMBIENT.NEUTRAL
    if (verdict === "running") {
      // The turn is provably in flight. Clear the step/human counters a lost
      // terminal may have stranded, but keep the admitted-unfinished claim so
      // evaluate() still derives RUNNING.
      entry.activeSteps = 0
      entry.pendingHuman = false
      entry.aborted = false
      entry.lastFinish = undefined
    } else if (verdict === "terminal") {
      entry.activeSteps = 0
      entry.pendingHuman = false
      entry.aborted = false
      entry.completedTurns = entry.admittedTotal
      entry.lastFinish = "stop"
    } else {
      entry.activeSteps = 0
      entry.pendingHuman = false
      entry.aborted = false
      entry.completedTurns = entry.admittedTotal
      entry.lastFinish = undefined
    }
    const next = evaluate(sessionID)
    // Only the DISPLAYED state moves, and only for the selected session, which
    // is the authority rule every other writer already obeys.
    if (sessionID === selectedSessionID) state = next
    return next
  }

  return {
    get state() {
      return state
    },
    reconcileFromHistory,
    // Audit W2-003: the selected/displayed session is set ONLY through the
    // authoritative setSelectedSession source (host route/selection), never
    // by the latest execution-event producer. Execution events update the
    // per-session record and recompute only when that session IS selected.
    get selectedSessionID() {
      return selectedSessionID
    },
    setSelectedSession(sessionID) {
      if (sessionID === selectedSessionID) return
      selectedSessionID = sessionID ?? null
      // §11: null selection (non-session route) resets the DISPLAYED state to
      // NEUTRAL immediately; per-session records are never erased, so
      // switching back re-projects that session's own current state.
      if (!sessionID) {
        if (state !== AMBIENT.NEUTRAL) emit(null, state, AMBIENT.NEUTRAL)
        return
      }
      // Switching selection immediately projects the newly selected
      // session's own ambient state (NEUTRAL for an idle session included).
      const next = evaluate(sessionID)
      if (next !== state) emit(sessionID, state, next)
    },
    sessions,
    evaluate,
    colorFor(next) {
      switch (next) {
        case AMBIENT.RUNNING:
          return mixHex(options.baseBackground ?? "#000000", colors.running, ratio.running)
        case AMBIENT.NEEDS_HUMAN:
          return mixHex(options.baseBackground ?? "#000000", colors.needsHuman, ratio.needsHuman)
        case AMBIENT.DONE:
          return mixHex(options.baseBackground ?? "#000000", colors.done, ratio.done)
        default:
          return options.baseBackground
      }
    },
    themeNameFor(next) {
      return THEME_SUFFIX[next]
    },
    onEvent(sessionID, type, properties) {
      if (!sessionID) return
      switch (type) {
        case "session.status": {
          const status = properties?.status
          if (status?.type === "busy") {
            update(sessionID, (entry) => {
              // Audit W2-004: an identity-bearing busy event for a different
              // message generation must not restart this turn.
              if (properties?.messageID && entry.assistantMessageID && properties.messageID !== entry.assistantMessageID) return
              entry.busy = true
              entry.aborted = false
            })
          } else if (status?.type === "idle") {
            update(sessionID, (entry) => {
              if (properties?.messageID && entry.assistantMessageID && properties.messageID !== entry.assistantMessageID) return
              entry.busy = false
            })
          }
          return
        }
        case "session.next.step.started":
          update(sessionID, (entry) => {
            entry.activeSteps += 1
            entry.generation += 1
            entry.aborted = false
            if (properties?.assistantMessageID) entry.assistantMessageID = properties.assistantMessageID
          })
          return
        case "session.next.step.ended":
          update(sessionID, (entry) => {
            // Audit CORE-004 generation safety: a step end from an aborted
            // generation must not complete the next one. While the session is
            // still in its committed-abort state (no new admission since), an
            // end event with no running step is a late event from the dead
            // run; identity-bearing events from another message are stale too.
            if (entry.aborted && entry.activeSteps === 0) return
            if (properties?.assistantMessageID && entry.assistantMessageID && properties.assistantMessageID !== entry.assistantMessageID) return
            entry.activeSteps = Math.max(0, entry.activeSteps - 1)
            entry.lastFinish = properties?.finish ?? entry.lastFinish
            // A user turn completes exactly when one of its steps ends with
            // finish:"stop" (tool-call steps continue the same turn). The
            // native scheduler promotes the NEXT queued prompt at decision
            // time, so 'prompted' events may precede the previous turn's
            // completion and must never consume pending work here.
            if (properties?.finish === "stop") entry.completedTurns += 1
          })
          return
        case "session.next.step.failed":
          update(sessionID, (entry) => {
            // Same generation gate as step.ended: a failure reported while the
            // session is still in its committed-abort state belongs to the
            // interrupted run, not the next one.
            if (entry.aborted && entry.activeSteps === 0) return
            if (properties?.assistantMessageID && entry.assistantMessageID && properties.assistantMessageID !== entry.assistantMessageID) return
            entry.activeSteps = Math.max(0, entry.activeSteps - 1)
            entry.lastFinish = "error"
          })
          return
        case "session.next.prompted":
          // Promotion marker only. Queued-but-unpromoted work is counted by
          // admittedTotal > completedTurns, never consumed here.
          return
        case "permission.asked":
        case "question.asked":
          update(sessionID, (entry) => {
            entry.pendingHuman = true
          })
          return
        case "permission.replied":
        case "question.replied":
        case "question.rejected":
          update(sessionID, (entry) => {
            entry.pendingHuman = false
          })
          return
        default:
          return
      }
    },
    onAdmitted(sessionID, messageID) {
      if (!sessionID || !messageID) return
      update(sessionID, (entry) => {
        entry.admittedTotal += 1
        entry.busy = true
        entry.aborted = false
        // messageID is the admitted PROMPT (user) message id, not the
        // assistant message id the generation gates compare against. Writing
        // it into assistantMessageID clobbered the running step's identity and
        // dropped its step.ended whenever a later queued prompt was admitted
        // mid-step, so activeSteps never reached 0 and ambient never hit DONE.
        // Assistant identity is owned exclusively by step.started.
      })
    },
    onPromptAdmittedLocally(sessionID) {
      if (!sessionID) return
      update(sessionID, (entry) => {
        entry.busy = true
        entry.aborted = false
      })
    },
    // Audit CORE-004: attemptAbort NEVER mutates state (a rejected interrupt
    // must leave the running session untouched); commitAbort is the sole
    // abort writer and clears EVERY generation-scoped field — active steps,
    // human wait, terminal finish — not only the admission counters. onAbort
    // stays as the committed-abort entry point for existing callers/tests.
    attemptAbort(sessionID) {
      if (!sessionID) return null
      const entry = sessions.get(sessionID)
      return { sessionID, generation: entry?.generation ?? 0 }
    },
    onAbort(sessionID) {
      if (!sessionID) return
      update(sessionID, (entry) => {
        entry.generation += 1
        entry.aborted = true
        entry.busy = false
        entry.activeSteps = 0
        entry.pendingHuman = false
        entry.lastFinish = undefined
        entry.admittedTotal = 0
        entry.completedTurns = 0
        entry.assistantMessageID = undefined
      })
    },
    reset() {
      sessions.clear()
      selectedSessionID = undefined
      state = AMBIENT.NEUTRAL
    },
    commitAbort: (sessionID) => onAbort(sessionID),
  }
}

export function createProjection() {
  const legacyPartID = (messageID, index) => `${messageID}-part-${index}`

  function infoFromNative(sessionID, message) {
    if (!message || typeof message !== "object") return undefined
    const created = message.time?.created ?? Date.now()
    if (message.type === "user") {
      return {
        id: message.id,
        sessionID,
        role: "user",
        time: { created, completed: message.time?.completed },
        parentID: message.parentID,
        modelID: message.modelID ?? "",
        providerID: message.providerID ?? "",
        mode: message.mode ?? "",
        agent: message.agent,
        path: message.path,
        cost: 0,
        tokens: normalizeTokens(message.tokens),
      }
    }
    if (message.type === "assistant") {
      return {
        id: message.id,
        sessionID,
        role: "assistant",
        time: { created, completed: message.time?.completed },
        parentID: message.parentID,
        modelID: message.model?.id ?? "",
        providerID: message.model?.providerID ?? "",
        mode: message.model?.variant ?? "",
        agent: message.agent,
        path: message.path,
        cost: message.cost ?? 0,
        tokens: normalizeTokens(message.tokens),
        finish: message.finish,
        error: message.error,
      }
    }
    if (message.type === "system" || message.type === "synthetic" || message.type === "compaction") {
      return {
        id: message.id,
        sessionID,
        role: "assistant",
        time: { created, completed: message.time?.completed },
        modelID: "",
        providerID: "",
        mode: message.type,
        cost: 0,
        tokens: emptyTokens(),
      }
    }
    return undefined
  }

  function partsFromNative(sessionID, message) {
    const parts = []
    if (!message || typeof message !== "object") return parts
    if (message.type === "user") {
      parts.push({
        id: legacyPartID(message.id, parts.length),
        messageID: message.id,
        sessionID,
        type: "text",
        text: message.text ?? "",
        time: { created: message.time?.created ?? Date.now() },
        synthetic: message.synthetic === true,
      })
      return parts
    }
    if (message.type !== "assistant") return parts
    const content = Array.isArray(message.content) ? message.content : []
    for (const item of content) {
      if (!item || typeof item !== "object") continue
      if (item.type === "text") {
        parts.push({
          id: item.id ?? legacyPartID(message.id, parts.length),
          messageID: message.id,
          sessionID,
          type: "text",
          text: item.text ?? "",
          time: item.time ?? { created: message.time?.created ?? Date.now() },
        })
        continue
      }
      if (item.type === "reasoning") {
        parts.push({
          id: item.id ?? legacyPartID(message.id, parts.length),
          messageID: message.id,
          sessionID,
          type: "reasoning",
          text: item.text ?? "",
          time: item.time ?? { created: message.time?.created ?? Date.now() },
        })
        continue
      }
      if (item.type === "tool") {
        const state = item.state ?? {}
        parts.push({
          id: item.id ?? legacyPartID(message.id, parts.length),
          messageID: message.id,
          sessionID,
          type: "tool",
          callID: item.id,
          tool: item.name ?? item.tool,
          state: {
            status:
              state.status === "completed" ? "completed" : state.status === "error" ? "error" : state.status === "running" ? "running" : "pending",
            input: state.input,
            output: state.result,
            metadata: state.structured ?? {},
            title: item.name ?? item.tool,
            time: { start: item.time?.created, end: state.time?.completed },
          },
          time: item.time ?? { created: message.time?.created ?? Date.now() },
        })
      }
    }
    return parts
  }

  function project(sessionID, nativeMessages) {
    const rows = []
    const list = Array.isArray(nativeMessages) ? nativeMessages : []
    for (const message of list) {
      const info = infoFromNative(sessionID, message)
      if (!info) continue
      rows.push({ info, parts: partsFromNative(sessionID, message) })
    }
    rows.sort((a, b) => (a.info.time.created ?? 0) - (b.info.time.created ?? 0) || String(a.info.id).localeCompare(String(b.info.id)))
    return rows
  }

  function toLegacyResponse(sessionID, nativeMessages) {
    return { data: project(sessionID, nativeMessages) }
  }

  return { project, toLegacyResponse, infoFromNative, partsFromNative }
}

export function toNativePromptPayload(input = {}) {
  const parts = Array.isArray(input.parts) ? input.parts : []
  const textParts = parts.filter((part) => part?.type === "text" && typeof part.text === "string")
  const text = [typeof input.text === "string" ? input.text : "", ...textParts.map((part) => part.text)]
    .filter((chunk) => chunk.length > 0)
    .join("\n")
  const files = parts
    .filter((part) => part?.type === "file")
    .map((part) => ({ uri: part.url ?? part.uri ?? part.path ?? "" }))
    .filter((file) => file.uri.length > 0)
  const agents = parts
    .filter((part) => part?.type === "agent")
    .map((part) => part.name ?? part.agent)
    .filter((name) => typeof name === "string" && name.length > 0)
  return { text, files: files.length > 0 ? files : undefined, agents: agents.length > 0 ? agents : undefined }
}

// Host contract (probed from 1.18.29): v2.session.switchModel accepts a
// ModelV2.Ref = { id, providerID, variant? }, NOT the legacy { providerID,
// modelID } shape carried by the TUI session.prompt input. Sending the legacy
// shape fails the request schema with "Missing key at ['model']['id']".
// Normalize at the boundary and accept either shape so the live input and the
// session.get pre-state round-trip safely.
export function toModelRef(model) {
  if (!model || typeof model !== "object") return undefined
  const id = model.id ?? model.modelID
  if (!id || !model.providerID) return undefined
  return model.variant === undefined
    ? { id, providerID: model.providerID }
    : { id, providerID: model.providerID, variant: model.variant }
}

export function createQueueCore({ client, onAdmitted, onAbort, log, admissionIntents } = {}) {
  if (!client || typeof client !== "object") throw new Error("queue core requires the shared sdk client")
  const v2 = client.v2?.session
  if (!v2 || typeof v2.prompt !== "function") {
    throw new Error("native v2 session namespace missing on the shared client; refusing to install queue core")
  }

  const admissions = []
  let installed = false
  let disposed = false
  // Audit W2-001: instance-specific ownership token. Every property this core
  // installs is keyed by this token; dispose restores a property ONLY when the
  // token still matches (a newer external writer survives), never blindly.
  const ownershipToken = Symbol("saipatch.queue")

  // T-144 2.5.2 prompt-intent lifecycle: an ordinary prompt enters the
  // in-flight intent set BEFORE awaiting the native admission transaction, so
  // queue precedence (in-flight ordinary admission intents + admitted-but-
  // not-yet-promoted ordinary prompts) is visible even before the native
  // admission returns. Locally generated unique intent IDs; the INTERNAL
  // __saipatchAutoContinue field is consumed here and NEVER forwarded into the
  // native prompt payload.
  const intents = typeof admissionIntents?.onPromptIntent === "function" ? admissionIntents : null
  let intentSeq = 0
  const nextIntentID = () => `saipatch-intent-${Date.now().toString(36)}-${++intentSeq}`
  const inFlightIntents = new Map() // sessionID -> Set<intentID>

  // W2-004: is this pre-state reachable through the API at all?
  //
  // The v2 session surface was inspected on the installed 1.18.32 build: the
  // only model/agent writers are switchModel (which requires a ModelV2.Ref --
  // an id and a providerID, never absent) and switchAgent (a non-empty name).
  // There is no clearModel / resetModel / unsetModel / clearAgent anywhere in
  // that surface, so a rollback to "this session had no explicit model" is not
  // expressible. The old code returned silently here and the caller counted
  // that as successful compensation, which is how a failed admission on a
  // fresh session permanently committed a mutation with no drift reported.
  const PRE_STATE_NOT_EXPRESSIBLE = "PRE_STATE_NOT_EXPRESSIBLE"
  // W2-004: make the drift a first-class, catchable property of the failure
  // instead of a log line nobody is guaranteed to read.
  const attachStateDrift = (error, detail) => {
    const target = error instanceof Error ? error : new Error(String(error))
    target.stateDrift = { ...detail, unrecovered: detail.compensationFailures.map((f) => f.kind) }
    if (error instanceof Error) return error
    return target
  }
  const settingsRestorable = (before) => ({
    model: Boolean(toModelRef(before?.model)),
    agent: typeof before?.agent === "string" && before.agent.length > 0,
  })
  // Restores and REPORTS. Every path returns a verdict; nothing here can
  // finish silently, because a silent finish is indistinguishable from a
  // successful rollback at the call site.
  async function restoreSessionSetting(sessionID, kind, value, options) {
    if (kind === "model") {
      const ref = toModelRef(value)
      if (!ref) {
        return {
          ok: false,
          field: "model",
          reason: `${PRE_STATE_NOT_EXPRESSIBLE}: the v2 API cannot clear an absent model`,
        }
      }
      await v2.switchModel({ sessionID, model: ref }, options)
      return { ok: true, field: "model" }
    }
    if (kind === "agent") {
      // Forwarding the prior value straight through was the second half of the
      // defect: an undefined agent reached switchAgent, whose normal write
      // path only accepts a non-empty string.
      if (typeof value !== "string" || value.length === 0) {
        return {
          ok: false,
          field: "agent",
          reason: `${PRE_STATE_NOT_EXPRESSIBLE}: the v2 API cannot clear an absent agent`,
        }
      }
      await v2.switchAgent({ sessionID, agent: value }, options)
      return { ok: true, field: "agent" }
    }
    return { ok: false, field: String(kind), reason: `unknown setting kind ${kind}` }
  }

  // Audit W2-004: the model/agent settings plus the native admission form ONE
  // small transaction per session. Pre-operation values are captured from the
  // authoritative session object, writes are serialized per session (two
  // translated prompts can never interleave their settings), a failure
  // compensates ONLY writes this invocation made, and compensation is
  // ownership-aware: a newer external value that appeared after our write is
  // never clobbered.
  const settingsLocks = new Map()
  const readCurrentSettings = (sessionID) => {
    const session = client.session?.get?.({ sessionID }) ?? client.session?.get?.(sessionID)
    return Promise.resolve(session).then((resolved) => {
      // Audit W2-004: the SDK session.get is async and may return the session
      // wrapped as {data:{data:...}} or {data:...}. Unwrap the documented
      // response shapes before reading the authoritative pre-state; never fall
      // back to a local model/agent cache.
      const record = resolved?.data?.data ?? resolved?.data ?? resolved
      if (!record || typeof record !== "object") return undefined
      return {
        model: record.model && typeof record.model === "object" ? { ...record.model } : undefined,
        agent: typeof record.agent === "string" ? record.agent : undefined,
      }
    })
  }
  const withSettingsLock = async (sessionID, job) => {
    const previous = settingsLocks.get(sessionID) ?? Promise.resolve()
    const run = previous.catch(() => {}).then(job)
    settingsLocks.set(sessionID, run)
    try {
      return await run
    } finally {
      if (settingsLocks.get(sessionID) === run) settingsLocks.delete(sessionID)
    }
  }

  async function syncSessionSettings(input) {
    const jobs = []
    if (input.model && typeof input.model === "object" && input.model.providerID && (input.model.modelID ?? input.model.id)) {
      jobs.push(
        v2.switchModel({ sessionID: input.sessionID, model: toModelRef(input.model) }).catch((error) => {
          log?.("warn", "switchModel failed", error)
          throw error
        }),
      )
    }
    if (typeof input.agent === "string" && input.agent.length > 0) {
      jobs.push(
        v2.switchAgent({ sessionID: input.sessionID, agent: input.agent }).catch((error) => {
          log?.("warn", "switchAgent failed", error)
          throw error
        }),
      )
    }
    await Promise.all(jobs)
  }

  // W2-004 transactional admission: settings + prompt inside one per-session
  // critical section. Compensation order: undo the LAST successful write
  // first. Ownership-aware: before each compensation write, re-read the
  // session; if the current value is no longer what this invocation wrote,
  // someone changed it after us — leave it and surface STATE_DRIFT instead of
  // clobbering.
  // §23: ALL admissions (including settings-free) enter the same short critical
  // section so settings-changing and settings-free prompts serialize on the
  // authoritative current setting read / delta / native v2.prompt / compensation.
  // §24: within the critical section compare requested state to authoritative
  // current; for ordinary continuation with unchanged model/agent, skip the
  // switchModel/switchAgent calls entirely (0/0 switches, 1 v2.prompt).
  async function admittedPromptTransaction(input, options) {
    const wantsModel = input.model && typeof input.model === "object" && input.model.providerID && (input.model.modelID ?? input.model.id)
    const wantsAgent = typeof input.agent === "string" && input.agent.length > 0
    return withSettingsLock(input.sessionID, async () => {
      // Capture authoritative current state BEFORE any mutation (§24).
      const before = await readCurrentSettings(input.sessionID)
      // Determine what actually needs changing (skip if unchanged).
      const needsModel = wantsModel && !modelsEqual(before?.model, input.model)
      const needsAgent = wantsAgent && before?.agent !== input.agent
      const written = []
      const compensationFailures = []
      try {
        if (needsModel) {
          await v2.switchModel({ sessionID: input.sessionID, model: toModelRef(input.model) }, options)
          written.push("model")
        }
        if (needsAgent) {
          await v2.switchAgent({ sessionID: input.sessionID, agent: input.agent }, options)
          written.push("agent")
        }
      } catch (settingError) {
        for (const kind of [...written].reverse()) {
          try {
            const current = await readCurrentSettings(input.sessionID)
            const ours = kind === "model"
              ? current?.model && input.model && (current.model.id ?? current.model.modelID) === (input.model.id ?? input.model.modelID) && current.model.providerID === input.model.providerID
              : current?.agent === input.agent
            if (!ours) {
              log?.("warn", `settings compensation skipped: ${kind} changed externally after our write; leaving newer value in place`)
              continue
            }
            // W2-004: the restore reports a verdict. An unrecoverable
            // pre-state is drift, NOT a silent success.
            const restored = await restoreSessionSetting(input.sessionID, kind, kind === "model" ? before?.model : before?.agent, options)
            if (!restored?.ok) {
              compensationFailures.push({ kind, error: restored?.reason ?? "restore returned no verdict" })
            }
          } catch (compensationError) {
            compensationFailures.push({ kind, error: compensationError?.message ?? String(compensationError) })
          }
        }
        if (compensationFailures.length > 0) {
          // W2-004: the drift is attached to the thrown error, not only
          // logged. A caller that catches this can see WHICH field was left
          // unrecovered instead of having to go read the log.
          // `reversible` says up front what this transaction COULD have
          // undone, so the drift report names the pre-state that was never
          // recoverable rather than leaving it implicit.
          attachStateDrift(settingError, { stage: "compensation", sessionID: input.sessionID, compensationFailures, reversible: settingsRestorable(before) })
          log?.("warn", "STATE_DRIFT: failed settings compensation", { sessionID: input.sessionID, compensationFailures, settingError: settingError?.message })
        }
        throw settingError
      }
      try {
        return await v2.prompt(
          { sessionID: input.sessionID, prompt: toNativePromptPayload(input), delivery: input.__saipatchSteer === true ? "steer" : "queue" },
          options,
        )
      } catch (admissionError) {
        // Rollback policy: restore pre-operation settings when admission
        // fails after our settings writes, with the same ownership guard.
        for (const kind of [...written].reverse()) {
          try {
            const current = await readCurrentSettings(input.sessionID)
            const ours = kind === "model"
              ? current?.model && input.model && (current.model.id ?? current.model.modelID) === (input.model.id ?? input.model.modelID) && current.model.providerID === input.model.providerID
              : current?.agent === input.agent
            if (!ours) {
              log?.("warn", `settings rollback skipped: ${kind} changed externally after our write; leaving newer value in place`)
              continue
            }
            // W2-004: the restore reports a verdict. An unrecoverable
            // pre-state is drift, NOT a silent success.
            const restored = await restoreSessionSetting(input.sessionID, kind, kind === "model" ? before?.model : before?.agent, options)
            if (!restored?.ok) {
              compensationFailures.push({ kind, error: restored?.reason ?? "restore returned no verdict" })
            }
          } catch (compensationError) {
            compensationFailures.push({ kind, error: compensationError?.message ?? String(compensationError) })
          }
        }
        if (compensationFailures.length > 0) {
          attachStateDrift(admissionError, { stage: "admission", sessionID: input.sessionID, compensationFailures, reversible: settingsRestorable(before) })
          log?.("warn", "STATE_DRIFT: failed settings rollback after failed admission", { sessionID: input.sessionID, compensationFailures, admissionError: admissionError?.message })
        }
        throw admissionError
      }
    })
  }

  function modelsEqual(current, requested) {
    if (!current || !requested) return false
    const cId = current.id ?? current.modelID
    const rId = requested.id ?? requested.modelID
    if (!cId || !rId || cId !== rId) return false
    if (current.providerID !== requested.providerID) return false
    if (current.variant !== requested.variant) return false
    return true
  }

  function install() {
    if (installed) throw new Error("queue core already installed on this client")
    if (client.session.__saipatchQueueCore !== undefined) throw new Error("another queue core already owns this client")
    installed = true
    // Audit W2-001: the ownership marker is the instance token, not a boolean.
    // A newer external owner that replaces the marker survives our dispose.
    client.session.__saipatchQueueCore = ownershipToken

    const originalPrompt = client.session.prompt
    const originalAbort = client.session.abort

    client.session.prompt = async function saipatchNativePrompt(input, options) {
      if (disposed || !input || typeof input !== "object" || typeof input.sessionID !== "string") {
        return originalPrompt.call(client.session, input, options)
      }
      const steer = input.__saipatchSteer === true
      const autoContinueSource = input.__saipatchAutoContinue === true
      const source = steer
        ? "steer"
        : autoContinueSource
          ? "auto-continue"
          : "ordinary"
      const intentID = source === "ordinary" ? nextIntentID() : null

      if (intents && source === "ordinary" && intentID) {
        let set = inFlightIntents.get(input.sessionID)
        if (!set) {
          set = new Set()
          inFlightIntents.set(input.sessionID, set)
        }
        set.add(intentID)
        try {
          intents.onPromptIntent(input.sessionID, intentID)
        } catch {}
      }

      let intentClosed = false
      const closeIntent = (ok) => {
        if (intentClosed) return
        intentClosed = true
        if (intents && source === "ordinary" && intentID) {
          const set = inFlightIntents.get(input.sessionID)
          if (set) {
            set.delete(intentID)
            if (set.size === 0) inFlightIntents.delete(input.sessionID)
          }
          try {
            if (ok) intents.onPromptAdmitted(input.sessionID, intentID)
            else intents.onPromptIntentFailed(input.sessionID, intentID)
          } catch {}
        }
      }

      try {
        const response = await admittedPromptTransaction(input, options)
        const raw = response?.data?.data ?? response?.data
        const admitted = raw?.data?.id ? raw.data : (raw?.id ? raw : undefined)
        if (admissions.length >= 1000) {
          admissions.shift()
        }
        admissions.push({
          sessionID: input.sessionID,
          id: admitted?.id,
          admittedSeq: admitted?.admittedSeq,
          delivery: steer ? "steer" : "queue",
          time: Date.now(),
        })
        if (admitted?.id && !steer) {
          if (process.env.SAIPATCH_QUEUE_DEBUG) {
            log?.("log", `admitted ${admitted.id} on ${input.sessionID}`)
          }
          onAdmitted?.(input.sessionID, admitted.id, { source, intentID })
        }
        closeIntent(true)
        return response
      } catch (error) {
        closeIntent(false)
        throw error
      }
    }
    client.session.prompt.__saipatchNative = true
    client.session.prompt[ownershipToken] = ownershipToken

    client.session.abort = async function saipatchNativeAbort(input, options) {
      const sessionID = input?.sessionID
      if (disposed || typeof sessionID !== "string") {
        return originalAbort.call(client.session, input, options)
      }
      // Audit CORE-004 abort authority: the native interrupt is requested
      // FIRST; ambient abort state is committed only after the interrupt
      // succeeds (or an authoritative interruption event arrives). A rejected
      // interrupt must leave execution/visual truth unchanged.
      await v2.interrupt({ sessionID }, options)
      onAbort?.(sessionID)
      return undefined
    }
    client.session.abort.__saipatchNative = true
    client.session.abort[ownershipToken] = ownershipToken

    return () => {
      // Audit W2-001 ownership-aware dispose: restore a property only when it
      // is still THIS instance's wrapper (token match); a newer external
      // wrapper survives untouched. Repeated dispose is idempotent.
      if (disposed) return
      disposed = true
      if (intents && inFlightIntents.size > 0) {
        for (const [sessionID, set] of inFlightIntents) {
          for (const intentID of set) {
            try {
              intents.onPromptIntentFailed(sessionID, intentID)
            } catch {}
          }
        }
        inFlightIntents.clear()
      }
      if (client.session.prompt?.[ownershipToken] === ownershipToken) {
        client.session.prompt = originalPrompt
      }
      if (client.session.abort?.[ownershipToken] === ownershipToken) {
        client.session.abort = originalAbort
      }
      if (client.session.__saipatchQueueCore === ownershipToken) {
        delete client.session.__saipatchQueueCore
      }
    }
  }

  return { install, get admissions() { return admissions }, get disposed() { return disposed } }
}

// HOST SEAM B projector (T-144): the patched TUI host calls
// `__SAIPATCH(w, H)` at the one point every event passes through before the
// sync switch, where `w` is the raw SSE wrapper and `H` the host sync consumer.
// This translates native `session.next.*` transcript events into the legacy
// event vocabulary the existing sync switch/store/renderer already consume,
// then passes the original payload on to the SAME consumer with the SAME
// original context object `{directory, workspace}` (§6: the original callback
// contract is preserved for both projected and passthrough events). It is
// stateless: the host store owns identity and merge; this only translates.
// `emit` may be called zero or many times per payload.
export function createHostProjector({ onProject } = {}) {
  const eventTime = (properties) => {
    const parsed = Date.parse(properties?.timestamp)
    return Number.isFinite(parsed) ? parsed : Date.now()
  }
  const info = (id, sessionID, role, created, extra) => ({
    id,
    sessionID,
    role,
    time: { created },
    cost: 0,
    tokens: emptyTokens(),
    ...extra,
  })
  const partEvent = (part) => ({ type: "message.part.updated", properties: { part } })
  const deltaEvent = (properties) => ({ type: "message.part.delta", properties })

  // T-144 contract: the binary seam calls `__SPB(w, e=>H(e,w))`, so the
  // projector receives the WHOLE raw SSE wrapper plus an emit callback. Every
  // projected event and the original payload are handed to that callback with
  // the ORIGINAL {directory, workspace} context (§6); the callback forwards to
  // the host sync consumer (the 1.18.29 sync switch reads only the payload).
  return function projectHostEvent(w, H) {
    const payload = w?.payload
    if (!payload || typeof H !== "function") return
    if (payload.type === "sync") return
    const emit = (event) => H(event, { directory: w.directory, workspace: w.workspace })
    const { type, properties } = payload
    const projected = []
    const send = (event) => {
      projected.push(event)
      emit(event)
    }
    if (
      typeof type === "string" &&
      type.startsWith("session.next.") &&
      properties &&
      typeof properties.sessionID === "string"
    ) {
      const sessionID = properties.sessionID
      const created = eventTime(properties)
      switch (type) {
        case "session.next.prompted":
          if (properties.messageID) {
            send({
              type: "message.updated",
              properties: {
                info: info(properties.messageID, sessionID, "user", created, {
                  modelID: "",
                  providerID: "",
                  mode: "",
                }),
              },
            })
            send(partEvent({
              id: `${properties.messageID}:text`,
              messageID: properties.messageID,
              sessionID,
              type: "text",
              text: properties.prompt?.text ?? "",
              time: { created },
            }))
          }
          break
        case "session.next.step.started":
          if (properties.assistantMessageID) {
            send({
              type: "message.updated",
              properties: {
                info: info(properties.assistantMessageID, sessionID, "assistant", created, {
                  modelID: properties.model?.id ?? "",
                  providerID: properties.model?.providerID ?? "",
                  mode: properties.agent ?? "",
                  agent: properties.agent,
                }),
              },
            })
          }
          break
        case "session.next.text.started":
          if (properties.assistantMessageID && properties.textID) {
            send(partEvent({
              id: properties.textID,
              messageID: properties.assistantMessageID,
              sessionID,
              type: "text",
              text: "",
              time: { created },
            }))
          }
          break
        case "session.next.text.delta":
          if (properties.assistantMessageID && properties.textID && typeof properties.delta === "string") {
            send(deltaEvent({
              messageID: properties.assistantMessageID,
              partID: properties.textID,
              sessionID,
              field: "text",
              delta: properties.delta,
            }))
          }
          break
        case "session.next.text.ended":
          if (properties.assistantMessageID && properties.textID) {
            send(partEvent({
              id: properties.textID,
              messageID: properties.assistantMessageID,
              sessionID,
              type: "text",
              text: typeof properties.text === "string" ? properties.text : "",
              time: { created },
            }))
          }
          break
        case "session.next.reasoning.started":
          if (properties.assistantMessageID && properties.reasoningID) {
            send(partEvent({
              id: properties.reasoningID,
              messageID: properties.assistantMessageID,
              sessionID,
              type: "reasoning",
              text: "",
              time: { created },
            }))
          }
          break
        case "session.next.reasoning.delta":
          if (properties.assistantMessageID && properties.reasoningID && typeof properties.delta === "string") {
            send(deltaEvent({
              messageID: properties.assistantMessageID,
              partID: properties.reasoningID,
              sessionID,
              field: "text",
              delta: properties.delta,
            }))
          }
          break
        case "session.next.reasoning.ended":
          if (properties.assistantMessageID && properties.reasoningID) {
            send(partEvent({
              id: properties.reasoningID,
              messageID: properties.assistantMessageID,
              sessionID,
              type: "reasoning",
              text: typeof properties.text === "string" ? properties.text : "",
              time: { created, completed: created },
            }))
          }
          break
        default:
          break
      }
    }
    if (onProject && projected.length) onProject(type, projected)
    // The original payload still reaches the host switch unchanged, with the
    // same directory/workspace context the unpatched host passes: native
    // consumers need the native event, and the switch's own session.next.moved
    // case must still run.
    emit(payload)
  }
}

// §8 exception barrier for the UI compatibility projector only: a projector
// failure must never terminate the host SSE loop. The original payload is
// passed through with the original context and the failure is recorded when a
// log sink exists. Queue/execution errors are NOT routed through this barrier.
export function createBarrierHook(hostProjector, { log } = {}) {
  return (w, H) => {
    try {
      // T-144 diagnostic: prove the hook is LIVE and receiving dispatches.
      seamDebug(`${new Date().toISOString()} [saipatch-queue-2x] seam-call ${w?.payload?.type ?? typeof w}\n`)
      hostProjector(w, H)
    } catch (error) {
      // T-144: the caller contract is the host dispatch contract — H receives
      // (payload, context). On any projector failure the ORIGINAL payload is
      // still dispatched with the ORIGINAL context, so the SSE loop survives
      // and the sync switch keeps its own event (fail-open to host truth).
      try {
        const payload = w?.payload ?? w
        if (payload && typeof H === "function") {
          H(payload, { directory: w?.directory, workspace: w?.workspace })
        }
      } catch {}
      log?.("warn", "host projector failed; original payload passed through", error)
    }
  }
}

// PERF-002: a bounded, LRU, session-keyed map.
//
// The process-lifetime caches in this module (live drafts, last verified
// native rows, partial hydration pages, completion episodes) all grew
// monotonically with the number of sessions a window ever visited: the
// audit's 20,000-session probe ended with 20,000 drafts, 20,000 ambient
// records and +29.03 MiB of post-GC heap, using five-character strings.
// JavaScript Map iterates in insertion order, so re-inserting a key on every
// access makes eviction least-recently-USED with no extra bookkeeping.
//
// `protect` vetoes eviction. The guardrails that must survive any cleanup:
// the currently selected session, a session with a RUNNING / queued /
// NEEDS_HUMAN turn, and a session whose verified native history is currently
// standing in for an unavailable hydration are never dropped. No cleanup here
// touches native OpenCode history; authoritative server/history state stays
// the recovery source.
export const MAX_TRACKED_SESSIONS = 64

export function createSessionLru(limit, { protect = () => false, onEvict = null } = {}) {
  const entries = new Map()
  const ceiling = Number.isInteger(limit) && limit > 0 ? limit : MAX_TRACKED_SESSIONS
  const evict = () => {
    if (entries.size <= ceiling) return
    for (const key of [...entries.keys()]) {
      if (entries.size <= ceiling) return
      if (protect(key)) continue
      const value = entries.get(key)
      entries.delete(key)
      try { onEvict?.(key, value) } catch { /* eviction must never throw */ }
    }
  }
  return {
    has: (key) => entries.has(key),
    get(key) {
      if (!entries.has(key)) return undefined
      const value = entries.get(key)
      // Refresh recency. The delete+set is the whole LRU mechanism.
      entries.delete(key)
      entries.set(key, value)
      return value
    },
    set(key, value) {
      if (entries.has(key)) entries.delete(key)
      entries.set(key, value)
      evict()
      return value
    },
    delete: (key) => entries.delete(key),
    // Drop without touching recency bookkeeping; used when a session is
    // authoritatively finished and its accumulated state is no longer live.
    clear: () => entries.clear(),
    keys: () => [...entries.keys()],
    get size() { return entries.size },
  }
}

export function createMessageBridge({ client, projection, state, log, isProtected, liveBatch = false } = {}) {
  // Audit W2-001: instance-specific ownership token for the message bridge;
  // separate from the queue-core token so disposals stay independent.
  const bridgeOwnershipToken = Symbol("saipatch.bridge")
  if (!client || typeof client !== "object") throw new Error("message bridge requires the shared sdk client")
  const v2 = client.v2?.session
  if (!v2 || typeof v2.messages !== "function") {
    throw new Error("native v2 messages missing on the shared client; refusing to install message bridge")
  }

  // Audit W2-002: the V2 history endpoint pages NEWEST-FIRST (default order
  // desc). Explicitly request order:"desc" with a bounded page limit, follow
  // cursor continuation without re-sending an order, RETAIN the FIRST cap
  // rows (the newest N), then convert to chronological display order. The
  // old slice(-cap) discarded exactly the newest rows on non-page-aligned
  // limits.
  const NATIVE_PAGE_MAX = 200
  // W2-002 partial-failure capture: pages collected before a cursor failure
  // are still verified native rows; CORE-003 lets a failed hydration return
  // them (never legacy rows).
  // PERF-002 eviction guard. Two vetoes here, both required by the audit's
  // guardrails: any session `isProtected` rejects -- injected, and covering
  // the selected session plus any session with a RUNNING / queued /
  // NEEDS_HUMAN turn -- and any session whose last hydration attempt failed.
  // For that second class the verified native rows ARE the only history left,
  // so evicting them would turn a recoverable hydration blip into a
  // permanently empty transcript. `isProtected` is read lazily, on eviction,
  // never at construction, because the bridge is built before ambient exists.
  const hydrationUnavailable = new Set()
  const protectedSession = (sessionID) => (
    hydrationUnavailable.has(sessionID) || isProtected?.(sessionID) === true
  )
  const lastPartialNative = createSessionLru(MAX_TRACKED_SESSIONS, { protect: protectedSession })
  async function fetchNative(sessionID, limit, options) {
    const cap = typeof limit === "number" && limit > 0 ? limit : 100
    const collected = []
    let cursor
    do {
      const remaining = cap - collected.length
      const requestLimit = Math.min(remaining, NATIVE_PAGE_MAX)
      const request = cursor ? { sessionID, cursor } : { sessionID, order: "desc", limit: requestLimit }
      const page = await v2.messages(request, options)
      const items = page?.data?.data ?? []
      collected.push(...items)
      lastPartialNative.set(sessionID, collected.slice())
      if (collected.length >= cap) break
      cursor = page?.data?.cursor?.next
    } while (cursor)
    const newestN = collected.slice(0, cap)
    return newestN.slice().reverse()
  }

  function install() {
    const originalMessages = client.session.messages
    if (client.session.messages?.__saipatchProjection) throw new Error("message bridge already installed")
    client.session.messages = async function saipatchProjectedMessages(input, options) {
      if (!input || typeof input.sessionID !== "string") {
        return originalMessages.call(client.session, input, options)
      }
      // Audit CORE-003: a native-owned session NEVER falls back to legacy
      // history. On native failure the last verified projection stays
      // authoritative; the failure is surfaced as HYDRATION_UNAVAILABLE with
      // whatever native rows were already projected.
      try {
        const native = await fetchNative(input.sessionID, input.limit, options)
        lastVerifiedNative.set(input.sessionID, native)
        hydrationUnavailable.delete(input.sessionID)
        // PERF-002: authoritative history has landed, so the streamed draft
        // for this session has been replaced by it and is no longer needed.
        retainConverged(input.sessionID)
        return projection.toLegacyResponse(input.sessionID, native)
      } catch (error) {
        log?.("warn", "native projection fetch failed; hydration unavailable (no legacy fallback)", error)
        hydrationUnavailable.add(input.sessionID)
        // Verified native rows stay authoritative: the last complete native
        // projection, or the partial pages this failed run already collected.
        const lastNative = lastVerifiedNative.get(input.sessionID) ?? lastPartialNative.get(input.sessionID) ?? []
        return { data: projection.project(input.sessionID, lastNative), hydrationUnavailable: true }
      }
    }
    client.session.messages.__saipatchProjection = true
    client.session.messages[bridgeOwnershipToken] = bridgeOwnershipToken
    return () => {
      // Audit W2-001 ownership-aware dispose for the hydration wrapper.
      if (client.session.messages?.[bridgeOwnershipToken] === bridgeOwnershipToken) {
        client.session.messages = originalMessages
      }
      // PERF-002: releasing the wrapper is not releasing the state it kept.
      dispose()
    }
  }

  // Live native message projection (clause 8/10): the native runner emits no
  // legacy transcript events, so the bridge projects the native event stream
  // into drafts and mutates the live sync stores in place: add new message
  // rows, update existing message metadata, add/update parts by stable part
  // identity. Hydration through the wrapped client.session.messages stays the
  // authority; native message IDs are identical in both paths, so hydration
  // replaces live rows instead of duplicating them.
  const drafts = createSessionLru(MAX_TRACKED_SESSIONS, { protect: protectedSession })
  // CORE-003: last verified native rows per session — the only thing a failed
  // hydration may still return (never legacy rows).
  const lastVerifiedNative = createSessionLru(MAX_TRACKED_SESSIONS, { protect: protectedSession })
  const seam = { unverified: 0, unsupported: false, reason: null }

  function sessionDraft(sessionID) {
    let entry = drafts.get(sessionID)
    if (!entry) {
      entry = new Map()
      drafts.set(sessionID, entry)
    }
    return entry
  }

  const MAX_SESSION_DRAFTS = 500
  function draftRow(sessionID, messageID) {
    const entry = sessionDraft(sessionID)
    let row = entry.get(messageID)
    if (!row) {
      if (entry.size >= MAX_SESSION_DRAFTS) {
        const firstKey = entry.keys().next().value
        if (firstKey) entry.delete(firstKey)
      }
      row = { info: { id: messageID, sessionID, tokens: emptyTokens() }, parts: [] }
      entry.set(messageID, row)
    }
    return row
  }

  const eventTime = (properties) => {
    const parsed = Date.parse(properties?.timestamp)
    return Number.isFinite(parsed) ? parsed : Date.now()
  }

  function applyLiveEvent(sessionID, type, properties) {
    if (!sessionID || !properties) return undefined
    const timestamp = eventTime(properties)
    switch (type) {
      case "session.next.prompted": {
        if (!properties.messageID) return undefined
        const row = draftRow(sessionID, properties.messageID)
        row.info = {
          id: properties.messageID,
          sessionID,
          role: "user",
          time: { created: timestamp },
          modelID: "",
          providerID: "",
          mode: "",
          cost: 0,
          tokens: emptyTokens(),
        }
        row.parts = [{
          id: `${properties.messageID}:text`,
          messageID: properties.messageID,
          sessionID,
          type: "text",
          text: properties.prompt?.text ?? "",
          time: { created: timestamp },
        }]
        return row
      }
      case "session.next.step.started": {
        if (!properties.assistantMessageID) return undefined
        const row = draftRow(sessionID, properties.assistantMessageID)
        row.info = {
          id: properties.assistantMessageID,
          sessionID,
          role: "assistant",
          time: { created: timestamp },
          modelID: properties.model?.id ?? "",
          providerID: properties.model?.providerID ?? "",
          mode: properties.agent ?? "",
          agent: properties.agent,
          cost: 0,
          tokens: emptyTokens(),
        }
        row.parts = row.parts.filter((part) => part.type !== "text" || part.text)
        return row
      }
      case "session.next.text.started": {
        if (!properties.assistantMessageID || !properties.textID) return undefined
        const row = draftRow(sessionID, properties.assistantMessageID)
        row.info.role = "assistant"
        if (!row.parts.some((part) => part.id === properties.textID)) {
          row.parts.push({
            id: properties.textID,
            messageID: properties.assistantMessageID,
            sessionID,
            type: "text",
            text: "",
            time: { created: timestamp },
          })
        }
        return row
      }
      case "session.next.text.delta": {
        if (!properties.assistantMessageID || !properties.textID) return undefined
        const row = draftRow(sessionID, properties.assistantMessageID)
        let part = (row._lastTextPart && row._lastTextPart.id === properties.textID) ? row._lastTextPart : row.parts.find((item) => item.id === properties.textID)
        if (!part) {
          part = { id: properties.textID, messageID: properties.assistantMessageID, sessionID, type: "text", text: "" }
          row.parts.push(part)
        }
        row._lastTextPart = part
        if (typeof properties.delta === "string" && properties.delta.length > 0) part.text += properties.delta
        return row
      }
      case "session.next.text.ended": {
        if (!properties.assistantMessageID || !properties.textID) return undefined
        const row = draftRow(sessionID, properties.assistantMessageID)
        let part = (row._lastTextPart && row._lastTextPart.id === properties.textID) ? row._lastTextPart : row.parts.find((item) => item.id === properties.textID)
        if (!part) {
          part = { id: properties.textID, messageID: properties.assistantMessageID, sessionID, type: "text", text: "" }
          row.parts.push(part)
        }
        row._lastTextPart = part
        if (typeof properties.text === "string") part.text = properties.text
        return row
      }
      case "session.next.step.ended": {
        if (!properties.assistantMessageID) return undefined
        const row = draftRow(sessionID, properties.assistantMessageID)
        row.info.role = "assistant"
        row.info.tokens = normalizeTokens(properties.tokens)
        if (properties.finish !== undefined) row.info.finish = properties.finish
        if (properties.cost !== undefined) row.info.cost = properties.cost
        row.info.time = { ...(row.info.time ?? { created: timestamp }), completed: timestamp }
        return row
      }
      case "session.next.step.failed": {
        if (!properties.assistantMessageID) return undefined
        const row = draftRow(sessionID, properties.assistantMessageID)
        row.info.role = "assistant"
        if (properties.error !== undefined) row.info.error = properties.error
        return row
      }
      default:
        return undefined
    }
  }

  // Write-through into the live sync stores. `view.session.messages(sessionID)`
  // returns the real store array when the session key exists (the transcript
  // watches it) and a detached fresh [] otherwise. Discriminator: re-fetch the
  // view after the push. Same array identity with the row present = verified
  // live insert; same identity with the row missing = broken seam (three
  // consecutive failures close the live projection, clause 11); different
  // identity = pre-hydration detached store, the push is a no-op and
  // hydration owns the row.
  // PERF-001/PERF-002: the id->index cache that keeps the live path off the
  // linear store scan. Bounded on both axes: sessions through the shared LRU,
  // and ids per session through a FIFO cap. Evicting an id is always safe --
  // the lookup validates the cached index and falls back to one rescan, so a
  // cold entry costs a scan, never correctness.
  const MAX_STORE_INDEX_IDS = 4096
  const storeIndexCache = createSessionLru(MAX_TRACKED_SESSIONS, { protect: protectedSession })
  const indexId = (sMap, id, index) => {
    if (sMap.size >= MAX_STORE_INDEX_IDS && !sMap.has(id)) {
      sMap.delete(sMap.keys().next().value)
    }
    sMap.set(id, index)
  }

  function injectLive(sessionID, rows, stateOverride) {
    const view = stateOverride ?? state
    if (!view?.session?.messages) return { inserted: 0, updated: 0, verified: 0 }
    let inserted = 0
    let updated = 0
    let verified = 0
    let sMap = storeIndexCache.get(sessionID)
    if (!sMap) {
      sMap = new Map()
      storeIndexCache.set(sessionID, sMap)
    }
    for (const row of rows) {
      try {
        const store = view.session.messages(sessionID)
        if (!Array.isArray(store)) continue
        let index = sMap.get(row.info.id)
        if (index === undefined || index >= store.length || store[index]?.id !== row.info.id) {
          index = store.findIndex((existing) => existing?.id === row.info.id)
          if (index !== -1) indexId(sMap, row.info.id, index)
        }
        if (index === -1) {
          store.push(row.info)
          indexId(sMap, row.info.id, store.length - 1)
          const check = view.session.messages(sessionID)
          const present = Array.isArray(check) && check.some((existing) => existing?.id === row.info.id)
          if (present) {
            inserted += 1
            verified += 1
            seam.unverified = 0
            if (verified === 1) log?.("log", "live projection seam verified (store insert visible)")
          } else if (check === store) {
            seam.unverified += 1
            if (seam.unverified >= 3 && !seam.unsupported) {
              seam.unsupported = true
              seam.reason = "LIVE_PROJECTION_SEAM_UNSUPPORTED"
              log?.("warn", "LIVE_PROJECTION_SEAM_UNSUPPORTED: live store inserts never verify; staying on hydration only")
            }
          }
        } else {
          const existing = store[index]
          for (const key of Object.keys(row.info)) {
            if (key === "id") continue
            if (row.info[key] !== undefined) existing[key] = row.info[key]
          }
          updated += 1
        }
        const partStore = view.part?.(row.info.id)
        if (Array.isArray(partStore)) {
          if (!row._partIndices) row._partIndices = new Map()
          for (const part of row.parts) {
            let partIndex = row._partIndices.get(part.id)
            if (partIndex === undefined || partIndex >= partStore.length || partStore[partIndex]?.id !== part.id) {
              partIndex = partStore.findIndex((existing) => existing?.id === part.id)
              if (partIndex !== -1) row._partIndices.set(part.id, partIndex)
            }
            if (partIndex === -1) {
              partStore.push(part)
              row._partIndices.set(part.id, partStore.length - 1)
              continue
            }
            const existing = partStore[partIndex]
            for (const key of Object.keys(part)) {
              if (key === "id") continue
              if (part[key] !== undefined) existing[key] = part[key]
            }
          }
        }
      } catch (error) {
        log?.("warn", "live injection failed", error)
      }
    }
    return { inserted, updated, verified }
  }

  // PERF-001 item 4: batch fallback live mutations at the SAME granularity the
  // host uses. The patched host queues native events and deliberately batches
  // emission inside a roughly 16ms window before touching its store
  // (packages/tui/src/context/sdk.tsx:68-80); the fallback bridge path used to
  // force one store mutation per SSE frame. Arrival order INSIDE a batch is
  // preserved exactly, so this only ever DELAYS - it never reorders a delta,
  // terminal text or a queued turn, which is the guardrail that matters.
  //
  // This is the UNPATCHED-host path. In the shipped exact-seam configuration
  // the host owns the transcript and handleLiveEvent is never called at all.
  // Batching is OPT-IN via liveBatch so the bridge's default stays the
  // historical immediate write: every existing caller and store-semantics test
  // is unchanged, and only the plugin asks for the batched path.
  const LIVE_BATCH_MS = 16
  let batchSession = null
  let batchRows = []
  let batchTimer = null

  function flushLiveBatch() {
    if (batchTimer !== null) { clearTimeout(batchTimer); batchTimer = null }
    const sessionID = batchSession
    const rows = batchRows
    batchSession = null
    batchRows = []
    if (!sessionID || rows.length === 0) return null
    return injectLive(sessionID, rows)
  }

  function queueLiveBatch(sessionID, row) {
    // A session switch must not interleave two sessions in one store write.
    if (batchSession !== null && batchSession !== sessionID) flushLiveBatch()
    batchSession = sessionID
    batchRows.push(row)
    if (batchTimer === null) {
      batchTimer = setTimeout(flushLiveBatch, LIVE_BATCH_MS)
    }
    return null
  }

  function handleLiveEvent(sessionID, type, properties) {
    // Fail-closed first: a seam the store never verified stops live injection
    // entirely, and hydration through the wrapped messages keeps the transcript
    // correct. A batched flush that discovers the seam is unsupported sets the
    // flag, so the NEXT event is refused -- the guard cannot be bypassed by
    // deferring the write.
    if (seam.unsupported) return null
    const row = applyLiveEvent(sessionID, type, properties)
    if (!row) return null
    if (!liveBatch) {
      const start = performance.now()
      const immediate = injectLive(sessionID, [row])
      const ms = performance.now() - start
      if (type === "session.next.text.delta") {
        perfTrace("transcript-delta", { sessionID, ms })
      }
      if (type === "session.next.text.started") {
        perfTrace("transcript-start", { sessionID, ms })
      }
      return { ...immediate, seam: { unsupported: seam.unsupported } }
    }
    queueLiveBatch(sessionID, row)
    return { inserted: 0, updated: 0, verified: 0, seam: { unsupported: seam.unsupported } }
  }

  // PERF-002: release the live drafts of a session that has converged. A draft
  // exists only to accumulate streamed text until authoritative hydration
  // replaces it; once a successful hydration has landed for that session the
  // accumulated copy is redundant, and keeping it is what made the 20,000
  // -session probe retain 20,000 entries. Hydration is the authority for
  // replay and recovery, so dropping the draft cannot lose history -- and no
  // native state is touched.
  const retainConverged = (sessionID) => {
    if (typeof sessionID !== "string") return false
    if (hydrationUnavailable.has(sessionID)) return false
    if (!lastVerifiedNative.has(sessionID)) return false
    return drafts.delete(sessionID)
  }

  // PERF-002: dispose drops every plugin-owned structure immediately rather
  // than waiting for the GC to find the closures. A reinstall in the same
  // process must start from zero.
  const dispose = () => {
    // PERF-001: a pending batch must not outlive the instance that owns it.
    if (batchTimer !== null) { clearTimeout(batchTimer); batchTimer = null }
    batchSession = null
    batchRows = []
    drafts.clear()
    lastVerifiedNative.clear()
    lastPartialNative.clear()
    hydrationUnavailable.clear()
  }

  // W2-003: converge the live transcript from authoritative native history
  // after a reconnect. injectLive upserts by STABLE native id, so running this
  // repeatedly is idempotent: no duplicate rows, no duplicate parts, no part
  // growth. The rows come from the same projection the hydration path uses, so
  // the two converge on identical shapes.
  //
  // Only the BRIDGE-authority configuration calls this. When the host seam owns
  // the transcript (PERF-001) the host's own sync is the authority and this
  // must not write behind its back.
  async function reconcileTranscript(sessionID) {
    const native = await fetchNative(sessionID, NATIVE_PAGE_MAX)
    lastVerifiedNative.set(sessionID, native)
    hydrationUnavailable.delete(sessionID)
    const rows = projection.project(sessionID, native)
    if (!Array.isArray(rows) || rows.length === 0) return { rows: 0, inserted: 0, updated: 0 }
    const start = performance.now()
    const result = injectLive(sessionID, rows)
    perfTrace("transcript-reconcile", { sessionID, rows: rows.length, ms: performance.now() - start })
    return { rows: rows.length, ...result }
  }

  // Every session whose derived state a reconnect could have made stale.
  const trackedSessionIDs = () => [...new Set([
    ...drafts.keys(),
    ...lastVerifiedNative.keys(),
    ...lastPartialNative.keys(),
  ])]

  return {
    install, fetchNative, injectLive, handleLiveEvent, applyLiveEvent, seam,
    drafts, lastVerifiedNative, lastPartialNative,
    retainConverged, dispose, reconcileTranscript, trackedSessionIDs,
    flushLiveBatch, LIVE_BATCH_MS,
    get trackedSessions() { return drafts.size },
  }
}

async function installThemeVariants(api, ambient, log) {
  const fs = await import("node:fs")
  const os = await import("node:os")
  const path = await import("node:path")
  const current = api.theme?.current
  if (!current || typeof current !== "object") {
    log?.("warn", "ambient tint unavailable: theme.current missing")
    return null
  }
  const baseBackground = rgbaToHex(current.background)
  if (!baseBackground) {
    log?.("warn", "ambient tint unavailable: theme background missing")
    return null
  }
  let clone
  try {
    // Every RGBA color becomes a hex string so the variant files stay valid
    // ThemeJson documents; non-color scalars pass through. Internal derived
    // flags (e.g. _hasSelectedListItemText) must NOT reach the file:
    // resolveTheme re-derives them and would otherwise resolve a boolean as a
    // ColorValue, crashing the renderer.
    clone = {}
    for (const [key, value] of Object.entries(current)) {
      if (key.startsWith("_")) continue
      if (value && typeof value === "object" && typeof value.r === "number") {
        const hex = rgbaToHex(value)
        if (hex) clone[key] = hex
        continue
      }
      if (typeof value !== "object" && typeof value !== "function") clone[key] = value
    }
    if (!clone.background) {
      log?.("warn", "ambient tint unavailable: theme background missing")
      return null
    }
  } catch (error) {
    log?.("warn", "ambient tint unavailable: theme clone failed", error)
    return null
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "saipatch-queue-theme-"))
  // Audit W2-001: the temp directory is plugin-owned. Return a disposer so a
  // failed install or a normal dispose removes it exactly once; never leak one
  // directory per OpenCode launch and never touch host-owned theme resources.
  const dispose = () => {
    try {
      fs.rmSync(dir, { recursive: true, force: true })
    } catch {}
  }
  const names = {}
  try {
    for (const next of [AMBIENT.RUNNING, AMBIENT.NEEDS_HUMAN, AMBIENT.DONE]) {
      const name = THEME_SUFFIX[next]
      const variant = { ...clone }
      for (const key of ["background", "backgroundPanel"]) {
        if (typeof variant[key] === "string") {
          variant[key] =
            next === AMBIENT.RUNNING
              ? mixHex(baseBackground, COLOR.running, COLOR.mixRatio.running)
              : next === AMBIENT.NEEDS_HUMAN
                ? mixHex(baseBackground, COLOR.needsHuman, COLOR.mixRatio.needsHuman)
                : mixHex(baseBackground, COLOR.done, COLOR.mixRatio.done)
        }
      }
      const file = path.join(dir, `${name}.json`)
      fs.writeFileSync(file, JSON.stringify({ $schema: "https://opencode.ai/theme.json", name, theme: variant }))
      try {
        await api.theme.install(file)
        names[next] = name
      } catch (error) {
        log?.("warn", `theme install failed for ${name}`, error)
      }
    }
  } catch (error) {
    dispose()
    log?.("warn", "theme variant installation failed; owned temp resources removed", error)
    return null
  }
  return { names, baseBackground, dir, dispose }
}

const LIVE_EVENTS = [
  "session.next.prompted",
  "session.next.step.started",
  "session.next.text.started",
  "session.next.text.delta",
  "session.next.text.ended",
  "session.next.step.ended",
  "session.next.step.failed",
]

const SOUND_EVENTS = [
  "session.next.prompt.admitted",
  "session.next.prompted",
  "session.next.step.ended",
  "session.next.step.failed",
]

function loadSoundSettings() {
  try {
    const file = path.join(process.env.LOCALAPPDATA ?? os.homedir(), "SAITULS", "SAIPATCH", "opencode-queue-mode.settings.json")
    return normalizeSettings(JSON.parse(fs.readFileSync(file, "utf8")))
  } catch {
    return normalizeSettings(SOUND_DEFAULTS)
  }
}

function getQueueSoundDir() {
  return path.join(path.dirname(new URL(import.meta.url).pathname), "sounds")
}

function loadAttentionSettings() {
  try {
    const file = path.join(process.env.LOCALAPPDATA ?? os.homedir(), "SAITULS", "SAIPATCH", "opencode-queue-mode.settings.json")
    const raw = JSON.parse(fs.readFileSync(file, "utf8"))
    return normalizeAttentionSettings(raw.attention ?? ATTENTION_DEFAULTS)
  } catch {
    return normalizeAttentionSettings(ATTENTION_DEFAULTS)
  }
}

// T-144 auto-cc recovery: settings live in the SAME SAIPATCH settings contract
// (opencode-queue-mode.settings.json), section `autoContinue` — no second
// config file.
function loadAutoContinueSettings() {
  try {
    const file = path.join(process.env.LOCALAPPDATA ?? os.homedir(), "SAITULS", "SAIPATCH", "opencode-queue-mode.settings.json")
    const raw = JSON.parse(fs.readFileSync(file, "utf8"))
    return normalizeAutoContinueSettings(raw.autoContinue ?? AUTO_CC_DEFAULTS)
  } catch {
    return normalizeAutoContinueSettings(AUTO_CC_DEFAULTS)
  }
}

export { normalizeAttentionSettings, createAttentionEpisodes, createAttention, ATTENTION_DEFAULTS, ATTENTION }
export { pulsePlan }

// FAST PASTE / Ctrl+V (module 4, independent of queue and ambient):
//   The host declares the ctrl+v binding (input_paste -> "prompt.paste") but
//   its handler reads the clipboard through `await import("clipboardy")`,
//   which is ABSENT from the compiled 1.18.29 bundle (the string does not
//   occur in the binary), so the command dies silently and Ctrl+V never
//   works on Windows. This module registers its own keymap layer: Ctrl+V
//   reads the clipboard ONCE (PowerShell Get-Clipboard, a Windows component,
//   no third-party executable), preserves all characters, and inserts the whole
//   text at the caret through ONE editor insertText call. Paste is never a
//   submission and never touches queue, ambient or session state.
const FAST_PASTE_COMMAND = "saipatch.paste"

export function normalizePastedText(text) {
  if (typeof text !== "string") return ""
  // Preserve input exactly. The host edit buffer owns newline semantics;
  // the plugin must not impose an additional lossy normalization.
  return text
}

export function createFastPaste({ keymap, readClipboard, log, renderer, trace } = {}) {
  if (!keymap || typeof keymap.registerLayer !== "function") {
    log?.("warn", "fast paste unavailable: keymap.registerLayer missing")
    return null
  }
  let active = false

  const pasteCommand = {
    name: FAST_PASTE_COMMAND,
    async run(ctx) {
      trace?.({event: "command", keys: Object.keys(ctx ?? {}), focusedType: ctx?.focused?.constructor?.name,
        insertText: typeof ctx?.focused?.insertText, rendererFocusedType: renderer?.currentFocusedRenderable?.constructor?.name})
      if (active) return true // one paste at a time; re-entry is ignored, never queued
      const focused = ctx?.focused
      if (!focused || typeof focused.insertText !== "function") return false
      active = true
      try {
        trace?.({event: "clipboard.begin"})
        const raw = await readClipboard()
        trace?.({event: "clipboard.end", chars: raw?.length ?? 0})
        const text = normalizePastedText(raw)
        if (text.length === 0) return true
        trace?.({event: "insert.begin", chars: text.length})
        const result = focused.insertText(text)
        trace?.({event: "insert.end", result: result === false ? false : true})
        return result !== false
      } catch (error) {
        log?.("warn", "ctrl+v paste failed", error)
        return false
      } finally {
        active = false
      }
    },
  }

  const binding = { key: "ctrl+v", cmd: pasteCommand.run, preventDefault: true }
  // priority 1000: win over host prompt.paste layer for the same ctrl+v stroke.
  const diagTrace = process.env.SAIPATCH_KEY_TRACE
  const diagBinding = diagTrace
    ? { key: "ctrl+y", preventDefault: true, cmd: async () => {
        log?.("log", "diag ctrl+y binding fired")
        return true
      } }
    : null
  const dispose = keymap.registerLayer({
    priority: 1000,
    commands: [pasteCommand],
    bindings: diagBinding ? [binding, diagBinding] : [binding],
  })
  return typeof dispose === "function" ? dispose : () => {}
}

async function readWindowsClipboard(log) {
  const { execFile } = await import("node:child_process")
  const exec = (file, args, options) =>
    new Promise((resolve, reject) => {
      execFile(file, args, options, (error, stdout) => (error ? reject(error) : resolve(stdout)))
    })
  const stdout = await exec(
    "powershell.exe",
    ["-NoProfile", "-NonInteractive", "-Command",
     "[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Console]::Write((Get-Clipboard -Raw))"],
    { timeout: 10000, maxBuffer: 256 * 1024 * 1024, encoding: "utf8", windowsHide: true },
  ).catch((error) => {
    log?.("warn", "clipboard read failed", error)
    return ""
  })
  return stdout ?? ""
}

// PERF-003 (SRC-019): reconnect cadence for the plugin's own V2 event stream.
// The host batches its reconnect with min(retryDelay * 2 ** (attempt - 1),
// maxRetryDelay); these are this stream's equivalent, jittered. BASE is the
// ceiling for the FIRST retry and is below the old fixed 1000ms sleep, so a
// single short transient recovers faster than before while a sustained outage
// stops hammering the endpoint. STABLE is how long a silent-but-open stream
// must stay up before it counts as recovered evidence for the counter.
export const STREAM_BACKOFF_BASE_MS = 500
export const STREAM_BACKOFF_CAP_MS = 30000
export const STREAM_STABLE_MS = 30000

export async function saipatchTui(api, options = {}) {
  const log = (level, message, error) => {
    const detail = error ? ` :: ${error?.message ?? String(error)}` : ""
    const line = `[saipatch-queue-2x] ${message}${detail}`
    console[level === "warn" ? "warn" : "log"](line)
    // TEST-ONLY boundary trace (SAIPATCH_QUEUE_DEBUG names a file): the TUI
    // renderer swallows plugin console output, so diagnostics need their own
    // sink. Production default is OFF.
    const debugPath = process.env.SAIPATCH_QUEUE_DEBUG
    if (debugPath) {
      import("node:fs")
        .then((fs) => fs.appendFileSync(debugPath, `${new Date().toISOString()} ${line}\n`))
        .catch(() => {})
    }
  }

  const client = api?.client
  const v2 = client?.v2?.session
  if (!client || !v2 || typeof v2.prompt !== "function") {
    log("warn", "host build does not expose the native v2 session namespace; plugin inactive (fail-closed)")
    return
  }

  const projection = createProjection()
  // HOST SEAM B (T-144): the host hook is a TRANSACTION-OWNED resource
  // (§4): it is installed here and its disposer is pushed onto `resources`
  // IMMEDIATELY, so a failure in any later install stage unwinds it and the
  // previous hook value is restored — but only while this install still owns
  // the hook (a newer owner's hook survives untouched, §5).
  let hostHookOwned = false
  // PERF-001: who is allowed to mutate the live transcript.
  //
  // The patched topology has TWO paths to the same store: host SSE -> batched
  // host event bus -> __SPB projector -> native host sync/store, and plugin
  // SSE -> per-frame handleEvent -> draft accumulation -> direct store
  // mutation. The second one bypasses the host's ~16ms batching and does a
  // store scan per delta, and the two streams carry no cross-stream ordering
  // guarantee. One native text delta must have exactly one authority.
  //
  // "auto" starts on the bridge (safe on an UNPATCHED host, where the
  // projector never fires and the bridge is the only path) and hands the
  // transcript to the host the moment the projector actually delivers
  // something -- which is proof, not assumption, that the binary seam is live.
  // Ambient, sound and the recovery machine keep reading the plugin stream
  // either way: they need truth the host projector does not carry.
  const requestedAuthority = options?.transcriptAuthority
    ?? (typeof process !== "undefined" ? process.env.SAIPATCH_TRANSCRIPT_AUTHORITY : undefined)
    ?? "auto"
  const authorityIsHost = requestedAuthority === "host"
  let hostSeamDelivered = authorityIsHost
  const transcriptAuthority = () => {
    if (requestedAuthority === "bridge") return "bridge"
    if (requestedAuthority === "host") return "host"
    return hostSeamDelivered ? "host" : "bridge"
  }
  const installHostProjector = () => {
    if (typeof globalThis === "undefined") return null
    const debugProjection = typeof process !== "undefined" && process.env?.SAIPATCH_QUEUE_DEBUG
    // T-144: install on BOTH the binary seam alias (__SPB — what the patched
    // 1.18.29 host calls) and the canonical hook (__SAIPATCH). The barrier
    // forwards the original payload to the host consumer on projector failure,
    // so a projection bug can never kill the SSE loop.
    const hook = createBarrierHook(
      createHostProjector({
        // Always wired, not just under the debug flag: this callback is the
        // only in-process proof that the patched host is actually calling the
        // seam, which is what the PERF-001 authority flip turns on.
        onProject: (type, events) => {
          if (hostSeamDelivered === false && Array.isArray(events) && events.length > 0) {
            hostSeamDelivered = true
            log("log", `PERF-001: host seam delivered (${type}); live transcript authority -> host`)
          }
          if (!debugProjection) return
          log("log", `host-project ${type} -> ${events.map((event) => event.type).join(",")}`)
          for (const event of events) {
            const p = event.properties ?? {}
            const msg = p.info?.id ?? p.part?.messageID ?? p.messageID ?? ""
            const part = p.part?.id ?? ""
            const sid = p.info?.sessionID ?? p.part?.sessionID ?? p.sessionID ?? ""
            const len = typeof p.part?.text === "string" ? p.part.text.length : typeof p.delta === "string" ? p.delta.length : ""
            log("log", `project-trace ${event.type} msg=${msg} part=${part} sid=${sid} role=${p.info?.role ?? ""} finish=${p.info?.finish ?? ""} len=${len}`)
          }
        },
      }),
      { log },
    )
    const restore = []
    for (const alias of HOST_HOOK_ALIASES) {
      const previous = globalThis[alias]
      globalThis[alias] = hook
      restore.push(() => {
        if (globalThis[alias] !== hook) return // a newer owner replaced it
        if (previous === undefined) delete globalThis[alias]
        else globalThis[alias] = previous
      })
    }
    hostHookOwned = true
    return () => {
      if (!hostHookOwned) return
      hostHookOwned = false
      for (const undo of restore) undo()
    }
  }
  const originalThemeName = api.theme?.selected
  let ambient = null
  let themeControl = null
  let activeSessionID
  let completionSound = null
  // WS-ATT attention: transient overlay over the semantic state (pulse + taskbar).
  // Installed lazily; no-op when settings disable it.
  let attentionController = null
  // WS-ATT: episode bookkeeping lives at plugin scope because onAdmitted (the
  // admission path) opens episodes even for transitions observed before the
  // ambient block below assigns the controller.
  let attentionEpisodes = null
  let attentionPulseTimer = null
  // T-144 auto-cc recovery: same pattern — created inside the install
  // transaction, but referenced by the admission/abort closures first.
  let autoContinue = null

  // TEST-ONLY ambient state trace (clause 16): timestamp, sessionID, ambient
  // state, theme name. Off unless SAIPATCH_TEST_TRACE names an output file;
  // production default is OFF. No prompt contents ever reach the trace.
  const tracePath = options?.testTrace ?? process.env.SAIPATCH_TEST_TRACE
  let traceFs = null
  const pendingTraceLines = []
  import("node:fs")
    .then((module) => {
      traceFs = module
      try {
        for (const line of pendingTraceLines) traceFs.appendFileSync(tracePath, line)
      } catch {}
      pendingTraceLines.length = 0
    })
    .catch(() => {})
  const traceListener = tracePath
    ? (sessionID, next) => {
        // Flush on a microtask so the theme name recorded for this transition
        // is the one applyVisual installs FOR this state, not the previous one.
        Promise.resolve().then(() => {
          const line = JSON.stringify({
            ts: new Date().toISOString(),
            sessionID: sessionID ?? null,
            state: next,
            theme: themeControl?.currentName ?? api.theme?.selected ?? null,
          }) + "\n"
          if (traceFs) {
            try {
              traceFs.appendFileSync(tracePath, line)
            } catch {}
            return
          }
          pendingTraceLines.push(line)
        })
      }
    : null

  // Audit W2-003: the one authoritative foreground-selection event. It is
  // emitted by the exact-host compatibility seam (api.route has no change
  // subscription); it is never derived from execution events.
  const SELECTION_EVENT = "saipatch.session.selected"

  const AMBIENT_EVENTS = [
    "session.status",
    "session.next.step.started",
    "session.next.step.ended",
    "session.next.step.failed",
    "session.next.prompted",
    "permission.asked",
    "permission.replied",
    "question.asked",
    "question.replied",
    "question.rejected",
  ]

  const applyVisual = (sessionID, next) => {
    if (!themeControl || !next) return
    const name = themeControl.names[next]
    if (!name) return
    if (themeControl.currentName === name) return
    themeControl.currentName = name
    try {
      api.theme.set(name)
    } catch (error) {
      log("warn", "theme switch failed", error)
    }
  }

  const recompute = (sessionID) => {
    const next = ambient?.evaluate(sessionID)
    if (sessionID === activeSessionID) applyVisual(sessionID, next)
    return next
  }

  // Audit W2-003 / §11: the foreground session is set ONLY by the authoritative
  // host selection event (`saipatch.session.selected`, emitted by the exact-host
  // compatibility seam) or the initial `api.route.current` inspection (§10).
  // api.route exposes {register,navigate,current} with no change subscription
  // (probed 1.18.29 @110890733), and execution events must never move the
  // selection. selectSession(null) is the explicit NON-SESSION reset (§11):
  // activeSessionID = null, ambient selection = null, displayed state = NEUTRAL,
  // native theme restored. Stored per-session state is never erased — switching
  // back re-projects that session's own current projection.
  const applyNeutralVisual = () => {
    if (!themeControl) return
    // T-144 corrective: the plugin's own theme.set rewrites api.theme.selected,
    // so after /new the read-back here captured the plugin's LAST variant name
    // as the "native" theme and the visual latch never returned to the real
    // native theme (switch-back re-projection was then suppressed). The
    // init-captured originalThemeName is the only native-theme authority.
    themeControl.currentName = originalThemeName ?? themeControl.currentName
    try {
      api.theme.set(originalThemeName)
    } catch (error) {
      log("warn", "theme switch failed", error)
    }
  }
  const selectSession = (sessionID) => {
    if (sessionID === null) {
      activeSessionID = null
      ambient?.setSelectedSession(null)
      applyNeutralVisual()
      if (traceListener) traceListener(null, AMBIENT.NEUTRAL)
      return
    }
    if (typeof sessionID !== "string" || sessionID.length === 0) return
    activeSessionID = sessionID
    ambient?.setSelectedSession(sessionID)
    const next = ambient?.evaluate(sessionID) ?? AMBIENT.NEUTRAL
    applyVisual(sessionID, next)
    if (traceListener) traceListener(sessionID, next)
  }

  // Audit W2-003: admission/events update the session's own ambient record;
  // they NEVER select the foreground session. The selected session comes only
  // from an authoritative host source (api.route/session selection) delivered
  // through setSelectedSession below.
  const onAdmitted = (sessionID, messageID, meta) => {
    if (meta?.source !== "steer") {
      ambient?.onAdmitted(sessionID, messageID)
      completionSound?.onEvent(sessionID, "session.next.prompt.admitted", {})
      // WS-ATT: a real admission opens an attention episode for this session so
      // a later genuine DONE/NEEDS_HUMAN transition may pulse/flash exactly once.
      attentionEpisodes?.admit(sessionID)
    }
    // T-144: the recovery machine learns about EVERY queue admission — its
    // own synthetic cc (explicit source) and user prompts (precedence).
    autoContinue?.noteAdmission(sessionID, messageID, meta)
    recompute(sessionID)
  }

  const onAbort = (sessionID) => {
    ambient?.onAbort(sessionID)
    completionSound?.onAbort(sessionID)
    // T-144: a user abort is a legitimate terminal state — never auto-continue.
    autoContinue?.onAbort(sessionID)
    recompute(sessionID)
  }

  // T-162: completion sound. Settings live OUTSIDE the OpenCode install
  // (%LOCALAPPDATA%\SAITULS\SAIPATCH\). WAV assets live in the patch-owned
  // tui-modules directory staged by SAIPATCH Apply; playback is asynchronous
  // through a detached powershell.exe process (never blocks the TUI), and
  // playback failure is a warning only. SAIPATCH_SOUND_SINK (TEST-ONLY) names
  // a file that receives one JSON line per playback request instead of
  // spawning the player, so unit/acceptance tests never need speakers.
  const soundSettings = loadSoundSettings()
  const soundDir = getQueueSoundDir()
  const picker = createSoundPicker(soundSettings)
  const episodes = createCompletionEpisodes()
  let requestPlayback = () => {}
  if (soundSettings.enabled === true) {
    const sink = process.env.SAIPATCH_SOUND_SINK
    if (sink) {
      requestPlayback = (sessionID, name, extra = {}) => {
        try {
          fs.appendFileSync(sink, JSON.stringify({
            ts: new Date().toISOString(),
            sessionID: sessionID ?? null,
            sound: name,
            volume: soundSettings.volume,
            preview: extra.preview === true,
          }) + "\n")
        } catch {}
      }
    } else {
      requestPlayback = (sessionID, name) => {
        const file = path.join(soundDir, name)
        if (!fs.existsSync(file)) {
          log("warn", `completion sound asset missing: ${file}`)
          return
        }
        try {
          const args = buildPlayCommand({ file, volumePercent: soundSettings.volume })
          const child = spawn("powershell.exe", args, { stdio: "ignore", detached: true, windowsHide: true })
          child.on("error", (error) => log("warn", "completion sound playback failed", error))
          child.unref()
        } catch (error) {
          log("warn", "completion sound playback failed", error)
        }
      }
    }
    completionSound = createCompletionSound({ settings: soundSettings, picker, episodes, requestPlayback, log })
  }

  // Audit W2-001: installation is a TRANSACTION. Every resource pushes its
  // disposer onto one stack; if any required later stage throws, everything
  // already installed unwinds in strict reverse order and the refusal is
  // rethrown — no half-installed queue, retry in the same process works.
  const resources = []
  const unwind = () => {
    while (resources.length > 0) {
      const dispose = resources.pop()
      try {
        dispose?.()
      } catch {}
    }
  }

  try {
    // §4: the host hook is transaction-owned. Its disposer goes on the stack
    // BEFORE any later stage that can throw.
    const disposeHostHook = installHostProjector()
    if (disposeHostHook) resources.push(disposeHostHook)

    // T-144: bounded auto-cc recovery for transient stalls. The synthetic
    // continuation goes through the SAME native queue path (the wrapper
    // installed below), never steer, never direct store injection. Diagnostics
    // sink (TEST-ONLY, SAIPATCH_AUTOCC_SINK) receives one JSON line per state
    // transition — session id, episode id, reason, attempt number, decision;
    // never prompt contents.
    const autoCcSinkPath = process.env.SAIPATCH_AUTOCC_SINK
    // P1 DEBUG-ONLY sanitized event-evidence sink (env opt-in): records the
    // classification shape of every step.failed so the live 1.18.30 timeout
    // event shape can be established without recording any text content.
    const autoCcEventSinkPath = process.env.SAIPATCH_AUTOCC_EVENT_SINK
    // options.autoContinue carries TEST-ONLY seams (schedule/cancelScheduled/
    // now/queueDepth/settings/diagnosticSink); production passes nothing and
    // every seam takes its production default.
    const autoCcSeams = options?.autoContinue ?? {}
    autoContinue = createAutoContinue({
      settings: autoCcSeams.settings ?? loadAutoContinueSettings(),
      submit: async (sessionID) => {
        await client.session.prompt({ sessionID, text: AUTO_CC_PROMPT, __saipatchAutoContinue: true })
      },
      sink: autoCcSinkPath
        ? (record) => {
            try { fs.appendFileSync(autoCcSinkPath, JSON.stringify(record) + String.fromCharCode(10)) } catch {}
          }
        : undefined,
      diagnosticSink: autoCcSeams.diagnosticSink
        ?? (autoCcEventSinkPath
          ? (record) => {
              try { fs.appendFileSync(autoCcEventSinkPath, JSON.stringify(record) + String.fromCharCode(10)) } catch {}
            }
          : undefined),
      schedule: autoCcSeams.schedule,
      cancelScheduled: autoCcSeams.cancelScheduled,
      now: autoCcSeams.now,
      queueDepth: autoCcSeams.queueDepth,
      log,
      onExhausted: (sessionID) => {
        // Existing attention behavior: exhaustion needs a human, so it pulses
        // exactly like a NEEDS_HUMAN ask (one episode, never a loop).
        attentionController?.onNeedsHumanAsked(sessionID)
      },
    })
    resources.push(() => autoContinue?.dispose())

    const queue = createQueueCore({ client, onAdmitted, onAbort, log, admissionIntents: autoContinue })
    resources.push(queue.install())

    // PERF-002: the eviction veto is a lazy read of ambient state, which does
    // not exist yet at this point in init. The bridge only calls it while
    // evicting, long after ambient has been assigned. `ambient.evaluate` is
    // read-only -- it derives from the session's own record and mutates
    // nothing -- so calling it from an eviction path is safe.
    const isLiveAmbient = (sessionID) => {
      const state = ambient?.evaluate(sessionID)
      return state === AMBIENT.RUNNING || state === AMBIENT.NEEDS_HUMAN
    }
    const bridge = createMessageBridge({
      client, projection, state: api.state, log,
      // PERF-001 item 4: the plugin runs the fallback path batched at the
      // host's granularity; the bridge default stays immediate.
      liveBatch: true,
      isProtected: (sessionID) => (
        ambient?.selectedSessionID === sessionID || isLiveAmbient(sessionID)
      ),
    })
    resources.push(bridge.install())
    // PERF-002: dispose drops the plugin-owned per-session structures now, not
    // whenever the GC gets round to the closures. A dispose/reinstall in the
    // same process must start from zero retained state. ambient.reset() and
    // episodes.clear() existed but no production path ever called them.
    resources.push(() => { episodes.clear(); ambient?.reset() })

    if (options?.ambient !== false && process.env.SAIPATCH_AMBIENT_DISABLE !== "1") {
      const baseBackground = rgbaToHex(api.theme?.current?.background)
      const listeners = []
      if (traceListener) listeners.push(traceListener)
      // T-162: completion sound observes the SAME authoritative DONE
      // transition the tint does; it never keeps its own queue state.
      // T-144: a recoverable timeout awaiting automatic cc is NOT final
      // completion — the DONE sound is vetoed while a recovery is pending or
      // admitted; the next real drain transition plays normally.
      if (completionSound) {
        listeners.push((sessionID, next, previous) => {
          if (autoContinue?.suppressesCompletion(sessionID, next, previous)) {
            log("log", "completion sound suppressed: auto-cc recovery in flight")
            return
          }
          completionSound.onTransition(sessionID, next, previous)
        })
      }
      // WS-ATT: attention observes the same authoritative transition into DONE/NEEDS_HUMAN.
      // It is a separate projection (ATTENTION_PULSE vs semantic tint); it never
      // replaces the tint and never influences queue/completion truth.
      attentionEpisodes = null
      {
        const rawAtt = loadAttentionSettings()
        const sinkPulse = process.env.SAIPATCH_ATTENTION_PULSE_SINK
        const sinkFlash = process.env.SAIPATCH_ATTENTION_FLASH_SINK
        const attLog = process.env.SAIPATCH_QUEUE_DEBUG ? log : undefined
        attentionEpisodes = createAttentionEpisodes()
        const requestPulse = (sessionID, kind, n) => {
          // Visible-window pulse: short orange overlay over the current semantic tint.
          // Distinguishable from steady RUNNING orange by rhythm (repeated pulses) and
          // a brighter tint (#ff8c2e @ 22% over the current semantic background).
          const plan = pulsePlan({ pulseCount: n ?? rawAtt.pulseCount })
          if (sinkPulse) {
            try { fs.appendFileSync(sinkPulse, JSON.stringify({ ts: new Date().toISOString(), sessionID, kind, pulseCount: plan.n, perPulseMs: plan.perPulseMs, totalMs: plan.totalMs }) + "\n") } catch {}
          }
          // If themeControl exists, schedule bright pulses and restore semantic state after.
          if (themeControl && api?.theme && typeof api.theme.set === "function") {
            const attentionColor = "#ff8c2e"
            const bg = baseBackground ?? "#000000"
            const pulseHex = mixHex(bg, attentionColor, 0.22)
            // Current semantic variant name (restore after pulse ends).
            const semanticName = themeControl.names[ambient ? (ambient.evaluate(sessionID) ?? AMBIENT.NEUTRAL) : AMBIENT.NEUTRAL] ?? themeControl.currentName
            if (rawAtt.onlyWhenUnfocused) return
            let step = 0
            clearInterval(attentionPulseTimer)
            const tick = () => {
              step += 1
              const on = step % 2 === 1
              try {
                if (on) api.theme.set(pulseHex)
                else if (semanticName) api.theme.set(semanticName)
                else if (originalThemeName) api.theme.set(originalThemeName)
              } catch (e) { log?.("warn", "attention pulse tick failed", e) }
              if (step >= plan.n * 2) {
                clearInterval(attentionPulseTimer)
                attentionPulseTimer = null
                try {
                  if (semanticName) api.theme.set(semanticName)
                  else if (originalThemeName) api.theme.set(originalThemeName)
                } catch {}
              }
            }
            try { api.theme.set(pulseHex) } catch {}
            attentionPulseTimer = setInterval(tick, plan.perPulseMs / 2)
          }
        }
        const requestFlash = (sessionID, kind, n) => {
          const count = Math.min(20, Math.max(1, Math.round(n ?? rawAtt.taskbarFlashCount)))
          if (sinkFlash) {
            try { fs.appendFileSync(sinkFlash, JSON.stringify({ ts: new Date().toISOString(), sessionID, kind, flashCount: count, setForegroundCalls: 0 }) + "\n") } catch {}
            return
          }
          const helper = path.join(path.dirname(new URL(import.meta.url).pathname), "saipatch-attention-flash.ps1")
          const hwnd = process.env.OPENCODE_HWND ?? String(process.pid)
          try {
            const args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helper, "-Hwnd", hwnd, "-Count", String(count)]
            if (rawAtt.onlyWhenUnfocused) args.push("-ForegroundProbe")
            const child = spawn("powershell.exe", args, { stdio: "ignore", detached: true, windowsHide: true })
            child.on("error", (e) => log?.("warn", "attention flash helper failed", e))
            child.unref()
          } catch (e) { log?.("warn", "attention flash request failed", e) }
        }
        const controller = createAttention({ settings: rawAtt, episodes: attentionEpisodes, requestPulse, requestFlash, log: attLog })
        attentionController = controller
        listeners.push((sessionID, next, previous) => controller.onTransition(sessionID, next, previous))
      }
      ambient = createAmbient({
        baseBackground,
        listeners,
        debug: process.env.SAIPATCH_QUEUE_DEBUG
          ? (sessionID, snapshot) => log("log", `ambient ${sessionID} ${JSON.stringify(snapshot)}`)
          : undefined,
      })
      if (baseBackground) {
        themeControl = await installThemeVariants(api, ambient, log)
        if (themeControl) {
          resources.push(themeControl.dispose)
          themeControl.currentName = api.theme?.selected
        } else log("warn", "ambient tint unavailable: theme variants not installed; sound state stays active")
      } else {
        log("warn", "ambient tint unavailable: theme background missing; sound state stays active")
      }
    }

    // Startup baseline trace line: the ambient projection starts NEUTRAL.
    if (traceListener && ambient) {
      traceListener(activeSessionID ?? null, ambient.evaluate(activeSessionID ?? "none") ?? AMBIENT.NEUTRAL)
    }

    const seenEventTypes = new Set()
    const handleEvent = (event) => {
      const type = event?.type
      const sessionID = event?.properties?.sessionID
      if (process.env.SAIPATCH_QUEUE_DEBUG && type && !seenEventTypes.has(type)) {
        seenEventTypes.add(type)
        log("log", `event-seen ${type} session=${sessionID ?? "none"}`)
      }
      if (!type) return
      if (type === SELECTION_EVENT) {
        // §11: an explicit non-session / new route (`/new`, home, blank
        // composer) arrives as a selection event whose sessionID is absent or
        // null. That must reset to NEUTRAL, not keep the previous session's
        // tint.
        selectSession(event?.properties?.sessionID ?? null)
        return
      }
      if (typeof sessionID !== "string") return
      // W2-001: a native event is proof the host is alive, so it is the trigger
      // that converges a navigation the api wrapper never saw (keyboard and
      // TUI-internal navigation). The SELECTION comes from the host's own
      // route, never from this event's sessionID -- background execution must
      // not repaint the foreground.
      syncSelectionFromRoute("event")
      if (LIVE_EVENTS.includes(type)) {
        // PERF-001: exactly one authority mutates the live transcript. When
        // the host seam owns it, the plugin stream still runs for
        // ambient/sound/recovery below -- it just no longer writes the
        // transcript a second time.
        if (transcriptAuthority() === "bridge") {
          const result = bridge.handleLiveEvent(sessionID, type, event.properties)
          if (result?.seam?.unsupported) {
            // Clause 11: fail closed — hydration through the wrapped
            // client.session.messages keeps the transcript correct; live
            // injection stops entirely.
            return
          }
        }
      }
      if (ambient) {
        ambient.onEvent(sessionID, type, event.properties)
        recompute(sessionID)
      }
      // T-144: the recovery machine consumes the same authoritative native
      // stream (stall detection, activity, progress, human gates).
      autoContinue?.noteEvent(sessionID, type, event.properties)
      // T-162: completion sound consumes the same authoritative stream. The
      // queue-core admission hook also opens an episode via onAdmitted below.
      completionSound?.onEvent(sessionID, type, event.properties)
      // WS-ATT: direct NEEDS_HUMAN ask events also feed attention (one episode per ask).
      if (type === "permission.asked" || type === "question.asked") {
        attentionController?.onNeedsHumanAsked(sessionID)
      }
      // Focus acknowledgement: when the session that caused attention becomes
      // foreground (api.onFocus / window activation), stop flashing. For now,
      // the attention pulse timer already self-expires; taskbar flash is one-shot.
    }

    if (process.env.SAIPATCH_QUEUE_DEBUG) {
      log("log", "event api " + JSON.stringify({
        event: typeof api.event,
        on: typeof api.event?.on,
        subscribe: typeof api.event?.subscribe,
        keys: api.event && typeof api.event === "object" ? Object.keys(api.event) : [],
      }))
      try { log("log", "event.on source " + String(api.event?.on).slice(0, 500)) } catch (error) { log("warn", "event.on probe failed", error) }
      try { log("log", "api keys " + JSON.stringify(Object.keys(api))) } catch (error) { log("warn", "api keys probe failed", error) }
    }
    const disposers = resources

    if (options?.fastPaste !== false) {
      const disposeFastPaste = createFastPaste({
        keymap: api.keymap,
        renderer: api.renderer,
        trace: process.env.SAIPATCH_QUEUE_DEBUG ? (record) => log("log", `paste ${JSON.stringify({ts: performance.now(), ...record})}`) : undefined,
        readClipboard: () => readWindowsClipboard(log),
        log,
      })
      if (disposeFastPaste) disposers.push(disposeFastPaste)
    }

    // TEST-ONLY key normalization trace (clause 11): logs name/ctrl/shift/meta
    // and the integer char code of every parsed key event. No text content.
    const keyTracePath = process.env.SAIPATCH_KEY_TRACE
    if (keyTracePath && api.keymap && typeof api.keymap.intercept === "function") {
      try {
        const off = api.keymap.intercept("key", ({ event }) => {
          try {
            const rec = {
              ts: new Date().toISOString(),
              name: event?.name ?? null,
              ctrl: event?.ctrl === true,
              shift: event?.shift === true,
              meta: event?.meta === true,
              charCode: typeof event?.raw === "string" && event.raw.length === 1 ? event.raw.charCodeAt(0) : null,
              rawLen: typeof event?.raw === "string" ? event.raw.length : null,
              source: event?.source ?? null,
            }
            import("node:fs").then((fs) => fs.appendFileSync(keyTracePath, JSON.stringify(rec) + "\n")).catch(() => {})
          } catch {}
        })
        if (typeof off === "function") disposers.push(off)
        log("log", "key trace intercept installed")
      } catch (error) {
        log("warn", "key trace install failed", error)
      }
    }
    if (keyTracePath && api.keymap && typeof api.keymap.on === "function") {
      try {
        const offDispatch = api.keymap.on("dispatch", (info) => {
          try {
            const rec = {
              ts: new Date().toISOString(),
              kind: info ?? null,
            }
            import("node:fs").then((fs) => fs.appendFileSync(keyTracePath, JSON.stringify(rec) + "\n")).catch(() => {})
          } catch {}
        })
        if (typeof offDispatch === "function") disposers.push(offDispatch)
      } catch (error) {
        log("warn", "dispatch trace install failed", error)
      }
    }

    // Event delivery. On the pinned 1.18.29 build the host `api.event` bus
    // accepts subscribers but never emits (probed: zero events across a full
    // session while api.client.global.event() received session.next.*). The SDK
    // client's own /event stream carries the identical native payloads, so it is
    // the primary source; `api.event` is kept only for the non-native host
    // selection seam and as a fallback when the stream cannot open.
    const nativeEventTypes = [...new Set([...AMBIENT_EVENTS, ...LIVE_EVENTS, ...SOUND_EVENTS])]
    let selectionSubscribed = false
    const subscribeSelection = () => {
      if (selectionSubscribed) return
      const off = api.event?.on?.(SELECTION_EVENT, handleEvent)
      if (typeof off === "function") {
        selectionSubscribed = true
        disposers.push(off)
      }
    }
    const subscribeHostFallback = () => {
      for (const type of nativeEventTypes) {
        const off = api.event?.on?.(type, handleEvent)
        if (typeof off === "function") disposers.push(off)
      }
    }
    subscribeSelection()
    const openEventStream = api.client?.global?.event
    if (typeof openEventStream === "function") {
      const abortEvents = new AbortController()
      // PERF-003: bounded exponential backoff with jitter. The fixed 1000ms
      // sleep this replaces was MORE aggressive during a sustained outage
      // than the host's own reconnect (packages/tui/src/context/sdk.tsx does
      // min(retryDelay * 2 ** (attempt - 1), maxRetryDelay)), so every
      // established SAIPATCH window retried once a second in lockstep and a
      // transient event-service blip became a synchronized retry storm. The
      // first recovery stays FASTER than the old fixed sleep: the ceiling for
      // attempt 1 is STREAM_BACKOFF_BASE_MS, jittered down to half of it.
      // PERF-003/W2-003: reconnect success is where authoritative resync would
      // run; W2-003's reconciliation is not implemented yet, so this only
      // fixes the retry cadence, not the missed-event window.
      const failedAttempts = { count: 0 }
      const streamRetryDelay = () => {
        const ceiling = Math.min(
          STREAM_BACKOFF_CAP_MS,
          STREAM_BACKOFF_BASE_MS * 2 ** Math.max(0, failedAttempts.count - 1),
        )
        // Full-width jitter over [ceiling/2, ceiling]: enough spread that
        // windows opened seconds apart do not realign on the same tick.
        return Math.round(ceiling / 2 + Math.random() * (ceiling / 2))
      }
      // A retry sleep that Dispose can cut short. The old bare setTimeout kept
      // the runner (and its socket) alive for a full second after abort.
      const sleepUntilAborted = (ms) => new Promise((resolve) => {
        if (abortEvents.signal.aborted) { resolve(); return }
        const onAbort = () => { clearTimeout(timer); resolve() }
        const timer = setTimeout(() => {
          abortEvents.signal.removeEventListener("abort", onAbort)
          resolve()
        }, ms)
        abortEvents.signal.addEventListener("abort", onAbort, { once: true })
      })
      // W2-003: reconciliation after a reconnect.
      //
      // The V2 stream is volatile and non-replayable -- the host's own
      // regression suite names it the "volatile V2 event stream" and asserts a
      // reconnect request carries no last-event-id. So reconnecting transport
      // recovers NOTHING: any execution or transcript event emitted during the
      // gap is lost from this process's memory, and the strongest symptom is
      // ambient liveness stuck at RUNNING forever across successful
      // reconnects.
      //
      // Authority is the native history, not the stream: an assistant message
      // carrying finish:"stop" is a real terminal record. Its ABSENCE is not
      // proof of a running turn, so that case lands on the safe NEUTRAL
      // projection instead of either a stale RUNNING or a fabricated DONE.
      //
      // Ordering: the new stream is established FIRST, then reconciliation
      // runs, and only then are frames consumed -- anything arriving during
      // the resync stays buffered in the stream rather than creating another
      // blind gap.
      let reconnects = 0
      const reconcileAfterReconnect = async (why) => {
        reconnects += 1
        const targets = new Set(bridge.trackedSessionIDs())
        const selected = ambient?.selectedSessionID
        if (typeof selected === "string" && selected) targets.add(selected)
        for (const sessionID of targets) {
          if (abortEvents.signal.aborted) return
          try {
            const native = await bridge.fetchNative(sessionID, 200)
            const rows = Array.isArray(native) ? native : []
            // Newest assistant row decides: finish:"stop" is a real terminal
            // record, an assistant row with no finish is a turn demonstrably
            // in flight, and no assistant row at all concludes nothing.
            let verdict = "unknown"
            for (let i = rows.length - 1; i >= 0; i -= 1) {
              const row = rows[i]
              if (row?.type !== "assistant") continue
              verdict = row?.finish === "stop" ? "terminal" : "running"
              break
            }
            ambient?.reconcileFromHistory(sessionID, verdict)
            // The transcript converges only where the BRIDGE owns it. Under
            // the host seam the host's own sync is the authority and writing
            // behind its back would recreate the two-writer defect.
            if (transcriptAuthority() === "bridge") {
              await bridge.reconcileTranscript(sessionID)
            }
          } catch (error) {
            log?.("warn", `reconnect reconciliation failed for ${sessionID}`, error)
          }
        }
        log("log", `W2-003: reconciled ${targets.size} session(s) after reconnect #${reconnects} (${why})`)
      }
      const runEventStream = async () => {
        let openedOnce = false
        // Whether ANY previous connection proved itself. This has to live
        // outside the loop: a stream that opened and ended without yielding a
        // frame is not a healthy connection, so re-opening it is a retry, not
        // a reconnect, and must not trigger a resync storm.
        let everHealthy = false
        while (!abortEvents.signal.aborted) {
          let openedAt = 0
          let carriedFrame = false
          const isReconnect = openedOnce && everHealthy
          try {
            const res = await openEventStream.call(api.client.global, {
              signal: abortEvents.signal,
              sseMaxRetryAttempts: 0,
            })
            openedOnce = true
            openedAt = Date.now()
            if (process.env.SAIPATCH_QUEUE_DEBUG) log("log", "event stream open (client.sse)")
            if (isReconnect) await reconcileAfterReconnect("stream re-established")
            for await (const frame of res.stream) {
              const payload = frame?.payload
              if (!payload || payload.type === "sync") continue
              carriedFrame = true
              handleEvent(payload)
            }
          } catch (error) {
            if (abortEvents.signal.aborted) return
            if (!openedOnce) {
              log("warn", "event stream unavailable; falling back to api.event", error)
              subscribeHostFallback()
              return
            }
            log("warn", `event stream dropped; reconnecting (attempt ${failedAttempts.count + 1})`, error)
          }
          // The counter retires only on a stream that proved itself: it carried
          // at least one native frame, or it stayed open past the stability
          // window. Merely receiving a stream object is NOT evidence -- the
          // audit's probe was a stream that opened and ended immediately,
          // which is exactly the case that spun at 1 Hz forever.
          if (carriedFrame || (openedAt > 0 && Date.now() - openedAt >= STREAM_STABLE_MS)) {
            failedAttempts.count = 0
            // Sticky: once a real connection has existed, every later re-open
            // IS a reconnect and owes a resync. Downgrading back to "retry"
            // would let a stream that fails after a good one skip its resync.
            everHealthy = true
          } else {
            failedAttempts.count += 1
          }
          if (abortEvents.signal.aborted) return
          await sleepUntilAborted(streamRetryDelay())
        }
      }
      runEventStream().catch((error) => log("warn", "event stream runner failed", error))
      disposers.push(() => { try { abortEvents.abort() } catch {} })
    } else {
      log("warn", "no client event stream; falling back to api.event")
      subscribeHostFallback()
    }

    // W2-001: the authoritative foreground-selection producer.
    //
    // The host exposes exactly three route members (read out of the installed
    // build): route.register, route.navigate, and a LIVE `current` getter --
    // route: { register, navigate, get current() }. There is NO route-change
    // subscription, so the plugin cannot be TOLD that the foreground moved.
    // The only authoritative statement available is `current` itself.
    //
    // So selection is never inferred from an event's sessionID -- that is
    // exactly what would let background execution repaint the foreground,
    // which the "execution events never select" invariant forbids. Instead the
    // host's own route is re-read at the moments a real change can be
    // observed: at init, immediately after a navigation issued through
    // api.route.navigate, and on every native host event (an O(1) live-getter
    // read). The selection changes only when the route itself says so.
    //
    // HONEST LIVENESS BOUND: the host publishes no route-change event, so a
    // navigation that produces no subsequent host activity is not observed
    // until the next one. Navigation through the api is immediate; everything
    // else converges on the next event. That bound belongs to the host and is
    // stated here rather than hidden behind a polling loop.
    const selectionFromRoute = () => {
      const current = api.route?.current
      if (current && typeof current === "object") {
        const parts = Array.isArray(current.parts) ? current.parts : []
        const at = typeof current.at === "string" ? current.at : ""
        const sessionID = typeof parts[1] === "string" ? parts[1] : undefined
        return at.startsWith("session/") && sessionID ? sessionID : null
      }
      if (typeof current === "string") {
        // belt and braces: a raw path form "/session/<id>"
        const match = /^\/?session\/([^/?#]+)/.exec(current)
        return match ? match[1] : null
      }
      return undefined // route unreadable: leave the selection alone
    }
    const syncSelectionFromRoute = (why) => {
      let next
      try {
        next = selectionFromRoute()
      } catch (error) {
        log("warn", "route inspection failed", error)
        return
      }
      if (next === undefined) return
      if (next === activeSessionID) return
      selectSession(next)
      if (process.env.SAIPATCH_QUEUE_DEBUG) {
        log("log", `selection from host route (${why}): ${next ?? "none"}`)
      }
    }

    // §10: INITIAL session selection at plugin init. `api.route.current` is the
    // exact proven route source: if the current route is a session, select it
    // IMMEDIATELY — restored session, reopened session, reconnect, startup on
    // an existing DONE session all need the correct projection without waiting
    // for a navigation. A non-session route (home, /new) means no selection.
    syncSelectionFromRoute("init")

    // A navigation issued THROUGH the api is the one transition observable
    // without waiting for an event. The wrapper is ownership-aware and restores
    // the prior function on dispose, so uninstalling or reloading the plugin
    // returns the host to its exact prior behaviour. It is deliberately NOT
    // claimed to be the only navigation path -- keyboard and TUI-internal
    // navigation bypass it, which is precisely why syncSelectionFromRoute also
    // runs on every native event.
    const hostRoute = api.route
    if (hostRoute && typeof hostRoute.navigate === "function") {
      const originalNavigate = hostRoute.navigate
      const wrappedNavigate = function navigating(...args) {
        const result = originalNavigate.apply(this, args)
        try { syncSelectionFromRoute("navigate") } catch { /* never break host navigation */ }
        return result
      }
      try {
        hostRoute.navigate = wrappedNavigate
        disposers.push(() => {
          // Restore ONLY if we still own the property: a newer owner that
          // replaced our wrapper must survive untouched.
          if (hostRoute.navigate === wrappedNavigate) hostRoute.navigate = originalNavigate
        })
      } catch (error) {
        log("warn", "route.navigate wrapper not installed", error)
      }
    }

    api.lifecycle?.onDispose?.(() => {
      for (const dispose of disposers) {
        try {
          dispose()
        } catch {}
      }
      if (originalThemeName) {
        try {
          api.theme.set(originalThemeName)
        } catch {}
      }
    })

    log("log", `native queue core active (ambient ${ambient ? "on" : "off"}; build ${api.app?.version ?? "unknown"})`)
  } catch (error) {
    // W2-001: roll back every already-installed resource (queue wrappers,
    // bridge, themes, subscriptions) before reporting the refusal, so the
    // shared client is left exactly as found and a same-process retry can
    // install cleanly.
    unwind()
    ambient = null
    themeControl = null
    log("warn", "installation refused; rolled back to pre-install state", error)
    throw error
  }
}

export const pluginID = "saipatch-native-queue-2x"

export default {
  id: pluginID,
  tui: saipatchTui,
}
