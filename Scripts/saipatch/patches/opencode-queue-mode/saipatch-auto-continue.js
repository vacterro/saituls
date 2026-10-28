// saipatch-auto-continue — T-144 bounded auto-cc recovery for transient stalls.
//
// WHAT IT IS:
//   An opt-out (default-enabled) queue recovery mode. When OpenCode settles
//   idle after a clearly recoverable transient interruption (the live case:
//   a tool operation fails with "The operation timed out." while the task is
//   unfinished), this module submits the literal continuation prompt "cc"
//   through the EXISTING native queue path (client.session.prompt ->
//   v2.session.prompt delivery:"queue"). No second dispatch engine, no queue
//   reordering, no steer, no injection into internal message arrays.
//
// HARD BOUNDS:
//   - The synthetic cc is armed only after the session settles (no activity),
//     only when no ordinary user prompt is queued (user work ALWAYS has
//     priority), and only after a bounded grace period.
//   - Strict per-chain retry budget (default 3). The chain resets ONLY on
//     positive progress evidence (new step / new assistant content / a
//     completed turn) — never on another empty settled turn.
//   - Exhaustion surfaces AUTO_CC_EXHAUSTED, keeps the session inspectable,
//     preserves queued user work and requires human intervention.
//   - Non-recoverable outcomes (normal completion, user abort, explicit stop,
//     permission/question waits, auth/quota/model failures, plugin pause,
//     disposal/restart) NEVER auto-continue. Detection is deny-by-default:
//     only an explicitly classified recoverable timeout arms a recovery.
//   - Recovery episodes are tied to the session + assistant-message identity
//     the native stream already carries; a stale event from an older
//     generation can never inject cc into a newer run. When generation
//     ownership cannot be proven, the module fails closed.
//   - Nothing is persisted as an admissible synthetic prompt across process
//     restart: recovery state is in-memory, so a pre-restart timeout can
//     never duplicate a continuation OpenCode already resumed. Fail closed.

export const AUTO_CC_DEFAULTS = {
  enabled: true,
  maxAttempts: 3,
  graceMs: 4000,
  paused: false,
}

// The synthetic continuation prompt. Literal, never LLM-generated text.
export const AUTO_CC_PROMPT = "cc"

// Surfaced when the retry budget is spent; the session stays inspectable and
// every queued user prompt is preserved.
export const AUTO_CC_EXHAUSTED = "AUTO_CC_EXHAUSTED"

export const AUTO_CC_STATES = {
  IDLE: "IDLE",
  WAITING_SETTLE: "WAITING_SETTLE", // stall seen while native execution still active; grace NOT armed until settle
  PENDING: "PENDING", // grace timer armed
  ADMITTED: "ADMITTED", // synthetic cc queued, awaiting progress evidence
  EXHAUSTED: "EXHAUSTED",
  PAUSED_HOLD: "PAUSED_HOLD", // stall seen while queue paused; re-evaluated on resume
}

// Source identity carried through the local admission callback. The synthetic
// auto-cc is identified by explicit source, NEVER by session-level timing.
export const ADMISSION_SOURCE = {
  AUTO_CONTINUE: "auto-continue",
  ORDINARY: "ordinary",
  STEER: "steer",
}

const clampInt = (value, lo, hi, fallback) => {
  const n = typeof value === "number" ? value : Number.parseInt(value, 10)
  if (!Number.isFinite(n)) return fallback
  return Math.min(hi, Math.max(lo, Math.round(n)))
}

export function normalizeAutoContinueSettings(raw) {
  const src = raw && typeof raw === "object" ? raw : {}
  return {
    enabled: src.enabled !== false,
    maxAttempts: clampInt(src.maxAttempts, 1, 10, AUTO_CC_DEFAULTS.maxAttempts),
    graceMs: clampInt(src.graceMs, 250, 60000, AUTO_CC_DEFAULTS.graceMs),
    paused: src.paused === true,
  }
}

// ── Recoverable stall classification ───────────────────────────────────────
//
// Structured fields first (error.code / error.name / explicit timeout flags);
// text matching is a NARROW fallback that requires an operation noun directly
// beside "timed out" ("The operation timed out.", "Read operation timed out",
// "command execution timed out"...). Arbitrary assistant prose containing the
// word "timeout" never classifies. Everything unmatched returns null: only an
// explicitly recoverable timeout may trigger a continuation.

const TIMEOUT_CODES = new Set([
  "ETIMEDOUT",
  "TIMEOUT",
  "TIMEOUT_ERROR",
  "OPERATION_TIMED_OUT",
  "REQUEST_TIMED_OUT",
])
const TIMEOUT_NAMES = new Set(["TimeoutError", "OperationTimeoutError", "TimedOutError"])
const TIMEOUT_TEXT = /\b(?:operation|command|tool|read|edit|write|execution|request)\s+timed\s+out\b/i

export function classifyStall(properties) {
  const error = properties?.error
  if (!error) return null
  if (properties?.timeout === true || (error && typeof error === "object" && error.timeout === true)) {
    return "TOOL_TIMEOUT"
  }
  if (typeof error === "object" && error !== null) {
    const code = typeof error.code === "string" ? error.code.toUpperCase() : undefined
    if (code && TIMEOUT_CODES.has(code)) return "TOOL_TIMEOUT"
    const name = typeof error.name === "string" ? error.name : undefined
    if (name && TIMEOUT_NAMES.has(name)) return "TOOL_TIMEOUT"
  }
  const message = typeof error === "string" ? error : typeof error?.message === "string" ? error.message : ""
  if (message && TIMEOUT_TEXT.test(message)) return "TOOL_TIMEOUT"
  return null
}

// Events the recovery state machine consumes. Every one is already part of
// the plugin's native stream subscription (AMBIENT_EVENTS / LIVE_EVENTS).
const STEP_EVENTS = new Set([
  "session.next.step.started",
  "session.next.step.ended",
  "session.next.step.failed",
])

export function createAutoContinue({
  settings,
  submit,
  queueDepth,
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancelScheduled = (handle) => clearTimeout(handle),
  now = () => Date.now(),
  log,
  sink,
  diagnosticSink,
  onExhausted,
} = {}) {
  const config = normalizeAutoContinueSettings(settings)
  if (typeof submit !== "function") throw new Error("auto-continue requires the native queue submit seam")

  // Per-session recovery state. In-memory only: nothing here survives a
  // process restart, which is exactly the fail-closed restart contract.
  const episodes = new Map()
  // Sessions with a synthetic submission in flight (dedup against the
  // admission hook double-firing for the same attempt).
  const inFlight = new Set()

  // ── AUTHORITATIVE pending-ordinary-user-queue truth ──────────────────────
  // Tracked by the STABLE native message ID — never inferred from historical
  // admission counters, ambient visual state, elapsed time or assistant
  // prose. An ordinary prompt enters on its admission and leaves ONLY on its
  // observed native promotion. Work admitted BEFORE a timeout is just as
  // visible as work admitted after it, because membership does not depend on
  // any episode baseline.
  const pendingUserPrompts = new Map() // sessionID -> Set<messageID>
  // T-144 2.5.2: in-flight ordinary prompt admission intents (before native admission returns)
  const ordinaryAdmissionIntents = new Map() // sessionID -> Set<intentID>
  // Race safety (both event orders must end pending==false): a fast native
  // promotion can arrive BEFORE the wrapper's admission callback. The
  // promotion leaves a bounded tombstone so the later admission cannot
  // resurrect a ghost queued message. Oldest entries are evicted — no
  // unbounded growth.
  const promotedTombstones = new Map() // messageID -> notedAt
  const TOMBSTONE_LIMIT = 1024
  const pendingSet = (sessionID) => {
    let set = pendingUserPrompts.get(sessionID)
    if (!set) {
      set = new Set()
      pendingUserPrompts.set(sessionID, set)
    }
    return set
  }
  const ordinaryIntentCount = (sessionID) => ordinaryAdmissionIntents.get(sessionID)?.size ?? 0
  const pendingUserCount = (sessionID) => (pendingUserPrompts.get(sessionID)?.size ?? 0) + ordinaryIntentCount(sessionID)
  const noteTombstone = (messageID) => {
    if (!messageID || promotedTombstones.has(messageID)) return
    promotedTombstones.set(messageID, now())
    while (promotedTombstones.size > TOMBSTONE_LIMIT) {
      promotedTombstones.delete(promotedTombstones.keys().next().value)
    }
  }

  // Authoritative active-step accounting per session. Derived from native step
  // lifecycle (no polling, no idle event). Shape:
  //   activeSteps: Map<sessionID, Map<assistantMessageID, count>>
  // On step.started: increment exact session + assistantMessageID count.
  // On step.ended / step.failed: decrement matching count. Never below zero.
  // Prune zero-count entries immediately. No unbounded generation history.
  const activeSteps = new Map()
  const getActiveStepCount = (sessionID) => {
    const m = activeSteps.get(sessionID)
    if (!m) return 0
    let total = 0
    for (const c of m.values()) total += c
    return total
  }
  const incrementActiveStep = (sessionID, assistantMessageID) => {
    if (!assistantMessageID) return
    let m = activeSteps.get(sessionID)
    if (!m) {
      m = new Map()
      activeSteps.set(sessionID, m)
    }
    const prev = m.get(assistantMessageID) ?? 0
    m.set(assistantMessageID, prev + 1)
  }
  const decrementActiveStep = (sessionID, assistantMessageID) => {
    if (!assistantMessageID) return
    const m = activeSteps.get(sessionID)
    if (!m) return
    const prev = m.get(assistantMessageID) ?? 0
    const next = Math.max(0, prev - 1)
    if (next === 0) {
      m.delete(assistantMessageID)
      if (m.size === 0) activeSteps.delete(sessionID)
    } else {
      m.set(assistantMessageID, next)
    }
  }
  // Bounded stale-event guard: tracks which generations THIS process observed
  // step.started for, keyed by session. Removed when the generation completes
  // or settles terminal.
  const activeGenerations = new Map() // sessionID -> Set<assistantMessageID>
  const trackGeneration = (sessionID, aid) => {
    if (!sessionID || !aid) return
    let set = activeGenerations.get(sessionID)
    if (!set) {
      set = new Set()
      activeGenerations.set(sessionID, set)
    }
    set.add(aid)
  }
  const untrackGeneration = (sessionID, aid) => {
    if (!sessionID || !aid) return
    const set = activeGenerations.get(sessionID)
    if (set) {
      set.delete(aid)
      if (set.size === 0) activeGenerations.delete(sessionID)
    }
  }
  const hasActiveGeneration = (sessionID, aid) => {
    if (!sessionID || !aid) return false
    return activeGenerations.get(sessionID)?.has(aid) === true
  }
  let recoveryGeneration = null
  // Last decision reason per session, retained for diagnostics even after the
  // episode is gone (suppressions and cancellations delete the episode).
  const lastReasons = new Map()
  let paused = config.paused
  let disposed = false

  const setLastReason = (sessionID, reason) => {
    if (reason) lastReasons.set(sessionID, reason)
  }

  const record = (entry) => {
    if (!sink) return
    try {
      sink({ ts: new Date().toISOString(), ...entry })
    } catch {}
  }

  // P1 diagnostic sink (DEBUG-ONLY, environment-variable opt-in): sanitized
  // classification evidence for every step.failed event — structured code,
  // structured name, timeout flag, match result. NEVER prompt text, assistant
  // text, tool arguments, file contents or secrets.
  const diagnose = (sessionID, properties, classification) => {
    if (!diagnosticSink) return
    const error = properties?.error
    const errorObject = error && typeof error === "object" ? error : null
    try {
      diagnosticSink({
        ts: new Date().toISOString(),
        type: "session.next.step.failed",
        sessionID,
        assistantMessageID: properties?.assistantMessageID ?? null,
        errorCode: (errorObject && typeof errorObject.code === "string" ? errorObject.code : null),
        errorName: (errorObject && typeof errorObject.name === "string" ? errorObject.name : null),
        timeout: properties?.timeout === true || errorObject?.timeout === true,
        matched: classification !== null,
        classification,
      })
    } catch {}
  }

  const say = (message, extra) => {
    log?.("log", `auto-cc: ${message}${extra ? ` ${extra}` : ""}`)
  }

  // Queue depth authority: the exact pending-ordinary-prompt tracker. An
  // injected queueDepth override exists ONLY as a deterministic test seam.
  const depthFor = (sessionID) => {
    if (typeof queueDepth === "function") return Math.max(0, queueDepth(sessionID) | 0)
    return pendingUserCount(sessionID)
  }

  const episodeID = (sessionID, episode) =>
    `${sessionID}:${episode?.assistantMessageID ?? "?"}#${episode?.chainStartedAt ?? 0}`

  function disarm(sessionID) {
    const episode = episodes.get(sessionID)
    if (episode?.timer) {
      cancelScheduled(episode.timer)
      episode.timer = null
    }
  }

  function setEpisode(sessionID, episode) {
    if (episode) episodes.set(sessionID, episode)
    else episodes.delete(sessionID)
  }

  // Cancel a pending recovery and return to a terminal-for-this-chain state.
  // WAITING_SETTLE is NOT the armed state; a cancel from it keeps the same
  // terminal semantics (no timer was ever armed).
  function cancelPending(sessionID, reason) {
    const episode = episodes.get(sessionID)
    disarm(sessionID)
    inFlight.delete(sessionID)
    if (!episode) return
    episode.state = AUTO_CC_STATES.IDLE
    episode.lastReason = reason
    setLastReason(sessionID, reason)
    setEpisode(sessionID, null)
    if (getActiveStepCount(sessionID) === 0) {
      activeGenerations.delete(sessionID)
    }
    record({ sessionID, episodeID: episodeID(sessionID, episode), event: "cancel", reason, attempts: episode.attempts, state: "IDLE" })
    say(`cancelled (${reason})`, `session=${sessionID} attempts=${episode.attempts}`)
  }

  function armPending(sessionID, episode) {
    disarm(sessionID)
    episode.state = AUTO_CC_STATES.PENDING
    episode.pendingSince = now()
    episode.timer = schedule(() => fire(sessionID), config.graceMs)
    record({ sessionID, episodeID: episodeID(sessionID, episode), event: "arm", reason: episode.lastReason, attempts: episode.attempts, state: "PENDING", graceMs: config.graceMs })
    say(`grace armed`, `session=${sessionID} attempts=${episode.attempts}/${config.maxAttempts} reason=${episode.lastReason}`)
  }

  // Settle re-evaluation: the stalled generation has terminated. Arm the grace
  // timer ONLY when every settle condition holds NOW:
  //   active step count for that session == 0
  //   AND no ordinary user queue/intention is pending
  //   AND queue is not paused
  //   AND no permission/question gate exists
  //   AND the recovery generation is owned by this process.
  // Otherwise the episode stays WAITING_SETTLE for a later terminal event.
  function settleOrArmPending(sessionID, episode) {
    if (getActiveStepCount(sessionID) > 0) {
      // Still executing: keep waiting, do NOT arm the grace timer.
      episode.state = AUTO_CC_STATES.WAITING_SETTLE
      setLastReason(sessionID, "WAITING_SETTLE")
      record({ sessionID, episodeID: episodeID(sessionID, episode), event: "wait", reason: "ACTVE_STEPS_REMAIN", attempts: episode.attempts, state: "WAITING_SETTLE" })
      return
    }
    if (depthFor(sessionID) > 0) {
      episode.state = AUTO_CC_STATES.IDLE
      episode.lastReason = "QUEUED_WORK_TAKES_PRECEDENCE"
      setLastReason(sessionID, "QUEUED_WORK_TAKES_PRECEDENCE")
      setEpisode(sessionID, null)
      record({ sessionID, episodeID: `${sessionID}:${episode.assistantMessageID ?? "?"}`, event: "suppress", reason: "QUEUED_WORK_TAKES_PRECEDENCE", attempts: episode.attempts, state: "IDLE" })
      say(`suppressed: queued user work takes precedence`, `session=${sessionID}`)
      return
    }
    if (!config.enabled) return cancelPending(sessionID, "DISABLED")
    armPending(sessionID, episode)
  }

  function markExhausted(sessionID, episode) {
    disarm(sessionID)
    inFlight.delete(sessionID)
    episode.state = AUTO_CC_STATES.EXHAUSTED
    episode.lastReason = AUTO_CC_EXHAUSTED
    setLastReason(sessionID, AUTO_CC_EXHAUSTED)
    record({ sessionID, episodeID: episodeID(sessionID, episode), event: "exhausted", reason: AUTO_CC_EXHAUSTED, attempts: episode.attempts, state: "EXHAUSTED" })
    say(`exhausted after ${episode.attempts} attempts; human intervention required`, `session=${sessionID}`)
    try {
      onExhausted?.(sessionID)
    } catch {}
  }

  // Timer callback: the grace period elapsed. Every guard re-verifies against
  // CURRENT truth before the prompt leaves the process, INCLUDING the settle
  // re-check: activeSteps(sessionID) must still be 0 at fire time.
  async function fire(sessionID) {
    const episode = episodes.get(sessionID)
    if (disposed || !episode || episode.state !== AUTO_CC_STATES.PENDING) return
    episode.timer = null
    if (paused) {
      episode.state = AUTO_CC_STATES.PAUSED_HOLD
      record({ sessionID, episodeID: episodeID(sessionID, episode), event: "pause-hold", reason: "QUEUE_PAUSED", attempts: episode.attempts, state: "PAUSED_HOLD" })
      return
    }
    if (!config.enabled) return cancelPending(sessionID, "DISABLED")
    if (getActiveStepCount(sessionID) > 0) {
      // Fire-time settle re-check: execution became active again between
      // arm and fire. Suppress — native truth outranks the timer.
      episode.state = AUTO_CC_STATES.IDLE
      episode.lastReason = "SELF_RECOVERY_STEP_STARTED"
      setLastReason(sessionID, "SELF_RECOVERY_STEP_STARTED")
      setEpisode(sessionID, null)
      record({ sessionID, episodeID: episodeID(sessionID, episode), event: "suppress", reason: "SELF_RECOVERY_STEP_STARTED", attempts: episode.attempts, state: "IDLE" })
      say(`suppressed at fire time: native execution active again`, `session=${sessionID}`)
      return
    }
    if (depthFor(sessionID) > 0) {
      episode.state = AUTO_CC_STATES.IDLE
      episode.lastReason = "QUEUED_WORK_TAKES_PRECEDENCE"
      setLastReason(sessionID, "QUEUED_WORK_TAKES_PRECEDENCE")
      setEpisode(sessionID, null)
      record({ sessionID, episodeID: episodeID(sessionID, episode), event: "suppress", reason: "QUEUED_WORK_TAKES_PRECEDENCE", attempts: episode.attempts, state: "IDLE" })
      say(`suppressed: queued user work takes precedence`, `session=${sessionID}`)
      return
    }
    // Submit through the EXISTING native queue path. The queue core admission
    // hook (noteAdmission) completes the ADMITTED transition when the native
    // scheduler accepts the prompt.
    episode.state = AUTO_CC_STATES.ADMITTED
    inFlight.add(sessionID)
    record({ sessionID, episodeID: episodeID(sessionID, episode), event: "submit", reason: episode.lastReason, attempts: episode.attempts + 1, state: "ADMITTED" })
    try {
      await submit(sessionID)
    } catch (error) {
      // Fail closed: a rejected submission never loops. Human intervention
      // (or a fresh qualifying stall event) starts the next evaluation.
      inFlight.delete(sessionID)
      episode.state = AUTO_CC_STATES.IDLE
      episode.lastReason = "SUBMIT_FAILED"
      setEpisode(sessionID, null)
      record({ sessionID, episodeID: episodeID(sessionID, episode), event: "submit-failed", reason: "SUBMIT_FAILED", attempts: episode.attempts, state: "IDLE" })
      log?.("warn", "auto-cc: native submission failed; recovery dropped", error)
    }
  }

  function noteStall(sessionID, properties, reason) {
    const stallID = properties?.assistantMessageID
    if (!stallID) {
      // Fail closed: without generation identity the episode cannot be tied
      // to the exact session/generation that produced the stall.
      setLastReason(sessionID, "GENERATION_IDENTITY_UNPROVEN")
      record({ sessionID, episodeID: `${sessionID}:?`, event: "rejected", reason: "GENERATION_IDENTITY_UNPROVEN", attempts: 0, state: "IDLE" })
      say(`stall rejected: generation identity unproven`, `session=${sessionID}`)
      return
    }
    if (!hasActiveGeneration(sessionID, stallID)) {
      // The generation was never observed starting in THIS process: a stale
      // pre-restart or cross-session event cannot prove ownership. Fail closed.
      setLastReason(sessionID, "GENERATION_NOT_STARTED_HERE")
      record({ sessionID, episodeID: `${sessionID}:${stallID}`, event: "rejected", reason: "GENERATION_NOT_STARTED_HERE", attempts: 0, state: "IDLE" })
      say(`stall rejected: generation never started in this process`, `session=${sessionID} gen=${stallID}`)
      return
    }
    const existing = episodes.get(sessionID)
    if (existing) {
      if (existing.state === AUTO_CC_STATES.PENDING) {
        // Dedup: a recovery is already armed for this settle window; a
        // repeated stall event must never double-arm a second synthetic cc.
        existing.lastReason = reason
        record({ sessionID, episodeID: episodeID(sessionID, existing), event: "dedup", reason, attempts: existing.attempts, state: existing.state })
        return
      }
      if (existing.state === AUTO_CC_STATES.ADMITTED) {
        if (stallID === existing.lastStallID) {
          // Redelivery of the SAME generation's stall that this attempt
          // already answered: dedup, never a second cc for one stall.
          record({ sessionID, episodeID: episodeID(sessionID, existing), event: "dedup", reason, attempts: existing.attempts, state: existing.state })
          return
        }
        // The resumed run stalled AGAIN (a new generation, no completed turn
        // in between): this consumes the next budget slot.
        existing.lastReason = reason
        existing.lastStallID = stallID
        existing.assistantMessageID = stallID
        if (existing.attempts >= config.maxAttempts) {
          markExhausted(sessionID, existing)
          return
        }
        armPending(sessionID, existing)
        return
      }
      if (existing.state === AUTO_CC_STATES.EXHAUSTED) {
        // Budget spent: stay exhausted until positive progress (handled in
        // the progress path) resets the chain.
        record({ sessionID, episodeID: episodeID(sessionID, existing), event: "suppressed", reason: AUTO_CC_EXHAUSTED, attempts: existing.attempts, state: "EXHAUSTED" })
        return
      }
      if (existing.state === AUTO_CC_STATES.PAUSED_HOLD) {
        existing.lastReason = reason
        existing.lastStallID = stallID
        existing.assistantMessageID = stallID
        record({ sessionID, episodeID: episodeID(sessionID, existing), event: "stall-while-paused", reason, attempts: existing.attempts, state: "PAUSED_HOLD" })
        return
      }
    }
    const episode = {
      sessionID,
      assistantMessageID: stallID,
      lastStallID: stallID,
      reason,
      lastReason: reason,
      attempts: 0,
      state: AUTO_CC_STATES.PENDING,
      chainStartedAt: now(),
      timer: null,
    }
    setEpisode(sessionID, episode)
    if (paused) {
      episode.state = AUTO_CC_STATES.PAUSED_HOLD
      record({ sessionID, episodeID: episodeID(sessionID, episode), event: "stall-while-paused", reason, attempts: 0, state: "PAUSED_HOLD" })
      say(`stall held while queue paused`, `session=${sessionID}`)
      return
    }
    if (!config.enabled) {
      setEpisode(sessionID, null)
      setLastReason(sessionID, "DISABLED")
      record({ sessionID, episodeID: `${sessionID}:${stallID}`, event: "rejected", reason: "DISABLED", attempts: 0, state: "IDLE" })
      return
    }
    // Settle gate: the grace timer may only be armed when the session's
    // active native step count is 0. A timeout while OTHER execution is still
    // active for the same session waits for settle instead.
    if (getActiveStepCount(sessionID) > 0) {
      episode.state = AUTO_CC_STATES.WAITING_SETTLE
      record({ sessionID, episodeID: episodeID(sessionID, episode), event: "wait", reason: "ACTIVE_STEPS_REMAIN", attempts: 0, state: "WAITING_SETTLE" })
      say(`stall recorded; waiting for native execution to settle`, `session=${sessionID} gen=${stallID}`)
      return
    }
    if (depthFor(sessionID) > 0) {
      // Ordinary queued user work takes precedence; it is sufficient to
      // continue the agent. The episode is dropped: if the queue later drains
      // and the run settles stalled AGAIN, that stall event re-evaluates.
      episode.state = AUTO_CC_STATES.IDLE
      episode.lastReason = "QUEUED_WORK_TAKES_PRECEDENCE"
      setLastReason(sessionID, "QUEUED_WORK_TAKES_PRECEDENCE")
      setEpisode(sessionID, null)
      record({ sessionID, episodeID: `${sessionID}:${stallID}`, event: "suppress", reason: "QUEUED_WORK_TAKES_PRECEDENCE", attempts: 0, state: "IDLE" })
      say(`suppressed: queued user work takes precedence`, `session=${sessionID}`)
      return
    }
    armPending(sessionID, episode)
  }

  function noteActivity(sessionID, kind) {
    const episode = episodes.get(sessionID)
    if (!episode) return
    // Activity WITHOUT our synthetic cc means the session resumed by itself
    // (or promoted queued user work): the pending continuation is cancelled
    // and never submitted. Activity while ADMITTED is handled as progress.
    if (episode.state === AUTO_CC_STATES.PENDING) cancelPending(sessionID, `SELF_RECOVERY_${kind}`)
  }

  return {
    // ── event input (native stream, already subscribed by the plugin) ──
    noteEvent(sessionID, type, properties) {
      if (disposed || !sessionID) return
      if (type === "session.next.step.failed") {
        const reason = classifyStall(properties)
        // P1 diagnosis: sanitized evidence for EVERY failure event, classified
        // or not. The live retest establishes the real 1.18.30 event shape.
        diagnose(sessionID, properties, reason)
        // Authoritative active-step accounting: a terminal event for the
        // failed generation decrements its count (never below zero, pruned
        // immediately). A terminal event for a generation never observed
        // active in this process is stale and does not decrement.
        const aid = properties?.assistantMessageID
        if (aid && hasActiveGeneration(sessionID, aid)) {
          decrementActiveStep(sessionID, aid)
        }
        if (!reason) {
          if (aid && getActiveStepCount(sessionID) === 0 && !episodes.has(sessionID)) {
            activeGenerations.delete(sessionID)
          }
          return // deny-by-default: only classified timeouts recover
        }
        // Generation guard: the stall event MUST carry the assistant message
        // identity of the generation that failed. Anything else is stale or
        // unprovable and is dropped (fail closed).
        noteStall(sessionID, properties, reason)
        // If a stall is recorded while other execution remains active for
        // this session, the episode already entered WAITING_SETTLE above.
        // When later terminal step events reduce the active count to zero,
        // the step.ended path transitions WAITING_SETTLE -> PENDING and arms
        // the grace timer.
        const pendingEpisode = episodes.get(sessionID)
        if (pendingEpisode?.state === AUTO_CC_STATES.WAITING_SETTLE && getActiveStepCount(sessionID) === 0) {
          // Terminal timeout reduced the count to zero: settle holds NOW.
          // Re-evaluate and arm the grace timer.
          settleOrArmPending(sessionID, pendingEpisode)
        }
        return
      }
      if (type === "session.next.step.started") {
        const aid = properties?.assistantMessageID
        if (aid) {
          // Authoritative active-step accounting: increment the exact
          // session + assistantMessageID count. Also record this generation as
          // owned by THIS process for the bounded stale-event guard.
          incrementActiveStep(sessionID, aid)
          trackGeneration(sessionID, aid)
        }
        const episode = episodes.get(sessionID)
        if (episode?.state === AUTO_CC_STATES.PENDING) {
          // Activity before our cc fired: the session resumed by itself (or
          // promoted queued user work). Cancel the pending continuation.
          noteActivity(sessionID, "STEP_STARTED")
          return
        }
        if (episode?.state === AUTO_CC_STATES.WAITING_SETTLE) {
          // A NEW generation started while we were waiting for the stalled
          // generation to settle: native execution is active again. Stay
          // WAITING_SETTLE; the pending cc is NOT armed (count > 0). When this
          // new generation terminates and the count returns to 0 the settle
          // re-evaluation below arms the grace.
          record({ sessionID, episodeID: episodeID(sessionID, episode), event: "waiting-still-active", reason: "STEP_STARTED", attempts: episode.attempts, state: "WAITING_SETTLE" })
          return
        }
        if (episode?.state === AUTO_CC_STATES.ADMITTED) {
          // A new generation after our cc is evidence the run resumed — but a
          // bare step start never resets the budget: the chain only resets on
          // a COMPLETED turn, so timeout -> cc -> immediate timeout -> cc
          // consumes two recovery attempts (clause 6).
          episode.sawProgress = true
          record({ sessionID, episodeID: episodeID(sessionID, episode), event: "progress-evidence", reason: "STEP_STARTED", attempts: episode.attempts, state: episode.state })
          return
        }
        if (episode?.state === AUTO_CC_STATES.EXHAUSTED) {
          // New execution after exhaustion means a human intervened; the next
          // qualifying stall gets a fresh chain.
          disarm(sessionID)
          setEpisode(sessionID, null)
          if (getActiveStepCount(sessionID) === 0) activeGenerations.delete(sessionID)
          record({ sessionID, episodeID: episodeID(sessionID, episode), event: "resolve", reason: "HUMAN_PROGRESS_AFTER_EXHAUSTED", attempts: episode.attempts, state: "IDLE" })
          return
        }
        return
      }
      if (type === "session.next.step.ended") {
        decrementActiveStep(sessionID, properties?.assistantMessageID)
        const episode = episodes.get(sessionID)
        if (episode?.state === AUTO_CC_STATES.WAITING_SETTLE && getActiveStepCount(sessionID) === 0) {
          // All active native steps have finished: native execution has settled.
          // Arm the grace timer now.
          episode.lastReason = episode.lastReason ?? "TOOL_TIMEOUT"
          settleOrArmPending(sessionID, episode)
          return
        }
        if (properties?.finish === "stop") {
          untrackGeneration(sessionID, properties?.assistantMessageID)
          // A normally completed turn is a legitimate terminal state. After
          // our cc it is ALSO the positive-progress reset: the chain closes
          // and a later stall starts a fresh budget.
          if (episode?.state === AUTO_CC_STATES.ADMITTED || episode?.state === AUTO_CC_STATES.EXHAUSTED) {
            disarm(sessionID)
            inFlight.delete(sessionID)
            const resolvedAttempts = episode.attempts
            setEpisode(sessionID, null)
            if (getActiveStepCount(sessionID) === 0) activeGenerations.delete(sessionID)
            record({ sessionID, episodeID: episodeID(sessionID, episode), event: "resolve", reason: "TURN_COMPLETED", attempts: resolvedAttempts, state: "IDLE" })
            say(`turn completed; recovery chain resolved (attempts used: ${resolvedAttempts})`, `session=${sessionID}`)
            return
          }
          if (episode) cancelPending(sessionID, "TURN_COMPLETED")
          if (getActiveStepCount(sessionID) === 0) activeGenerations.delete(sessionID)
          return
        }
        if (getActiveStepCount(sessionID) === 0 && !episodes.has(sessionID)) {
          activeGenerations.delete(sessionID)
        }
        noteActivity(sessionID, "STEP_ENDED")
        return
      }
      if (type === "session.next.prompted") {
        const messageID = properties?.messageID
        if (messageID) {
          // Promotion removes the EXACT queued message (race A: admitted
          // first, promoted second). A promotion observed before its
          // admission leaves a bounded tombstone so the later admission
          // cannot resurrect a ghost queued message (race B).
          const set = pendingUserPrompts.get(sessionID)
          if (set?.delete(messageID) && set.size === 0) pendingUserPrompts.delete(sessionID)
          noteTombstone(messageID)
        }
        const episode = episodes.get(sessionID)
        if (episode?.state === AUTO_CC_STATES.ADMITTED && messageID === episode.admittedMessageID) {
          // Our own synthetic promotion: NOT progress by definition.
          episode.ccPromoted = true
          return
        }
        // A different prompt was promoted: user work became active.
        noteActivity(sessionID, "USER_PROMPT_PROMOTED")
        return
      }
      if (type === "session.next.text.started" || type === "session.next.text.delta" || type === "session.next.text.ended") {
        const episode = episodes.get(sessionID)
        if (episode?.state === AUTO_CC_STATES.ADMITTED) {
          // Assistant work content after our cc: progress evidence, budget
          // still chain-scoped (see step.started above).
          if (!episode.sawProgress) {
            episode.sawProgress = true
            record({ sessionID, episodeID: episodeID(sessionID, episode), event: "progress-evidence", reason: "ASSISTANT_CONTENT", attempts: episode.attempts, state: episode.state })
          }
          return
        }
        if (episode?.state === AUTO_CC_STATES.PENDING) noteActivity(sessionID, "ASSISTANT_CONTENT")
        return
      }
      if (type === "permission.asked" || type === "question.asked") {
        // A human gate is never auto-continued past.
        const episode = episodes.get(sessionID)
        if (episode) cancelPending(sessionID, "NEEDS_HUMAN")
        return
      }
      if (type === "session.status" && properties?.status?.type === "busy") {
        noteActivity(sessionID, "BUSY")
        return
      }
    },

    // ── admission hook (queue core onAdmitted) ──
    noteAdmission(sessionID, messageID, meta) {
      if (disposed || !sessionID) return
      const source = meta && typeof meta === "object" && meta.source !== undefined
        ? meta.source
        : (inFlight.has(sessionID) ? ADMISSION_SOURCE.AUTO_CONTINUE : ADMISSION_SOURCE.ORDINARY)

      if (source === ADMISSION_SOURCE.AUTO_CONTINUE) {
        // Our synthetic cc was accepted by the native scheduler.
        inFlight.delete(sessionID)
        const episode = episodes.get(sessionID)
        if (!episode) return
        episode.attempts += 1
        episode.admittedMessageID = messageID
        episode.state = AUTO_CC_STATES.ADMITTED
        record({ sessionID, episodeID: episodeID(sessionID, episode), event: "admitted", reason: episode.lastReason, attempts: episode.attempts, state: "ADMITTED", messageID })
        say(`synthetic cc admitted (attempt ${episode.attempts}/${config.maxAttempts})`, `session=${sessionID} messageID=${messageID ?? "?"}`)
        if (episode.attempts >= config.maxAttempts) {
          // The budget is now spent. The admitted cc may still resume work;
          // exhaustion is finalized when the NEXT qualifying stall (or a
          // progress-free settle) arrives — but the surface must show the
          // budget truth immediately, so mark exhausted-with-open-cc: keep
          // ADMITTED semantics for progress, surface the budget via status.
          episode.budgetSpent = true
          record({ sessionID, episodeID: episodeID(sessionID, episode), event: "budget-spent", reason: AUTO_CC_EXHAUSTED, attempts: episode.attempts, state: "ADMITTED" })
        }
        return
      }
      if (source === ADMISSION_SOURCE.ORDINARY) {
        // An ordinary user (or external) admission. Mark the exact message ID
        // pending unless its promotion was already observed (promotion-before-
        // admission race): queued user work takes precedence over any pending
        // synthetic cc.
        if (messageID) {
          if (promotedTombstones.has(messageID)) {
            promotedTombstones.delete(messageID)
            record({ sessionID, event: "admission-after-promotion", reason: "RACE_TOMBSTONE_CONSUMED", messageID })
          } else {
            pendingSet(sessionID).add(messageID)
          }
        }
        const episode = episodes.get(sessionID)
        if (episode?.state === AUTO_CC_STATES.PENDING && pendingUserCount(sessionID) > 0) {
          cancelPending(sessionID, "USER_PROMPT_QUEUED")
        }
        return
      }
      // Steer must enter neither set.
    },

    // ── abort (user interrupt: NEVER auto-continue) ──
    onAbort(sessionID) {
      if (!sessionID) return
      // Abort clears this session's owned pending-queue truth: the native
      // runner drops queued work on interrupt, so no ghost prompt may keep
      // suppressing (or later justify) a synthetic cc.
      ordinaryAdmissionIntents.delete(sessionID)
      pendingUserPrompts.delete(sessionID)
      activeGenerations.delete(sessionID)
      const episode = episodes.get(sessionID)
      if (episode) cancelPending(sessionID, "USER_ABORT")
      else inFlight.delete(sessionID)
    },

    // ── pause semantics (clause 17): paused queue = paused recovery ──
    setPaused(value) {
      paused = value === true
      if (paused) {
        for (const [sessionID, episode] of episodes) {
          if (episode.state === AUTO_CC_STATES.PENDING) {
            disarm(sessionID)
            episode.state = AUTO_CC_STATES.PAUSED_HOLD
            record({ sessionID, episodeID: episodeID(sessionID, episode), event: "pause-hold", reason: "QUEUE_PAUSED", attempts: episode.attempts, state: "PAUSED_HOLD" })
          }
        }
        return
      }
      // Resume: re-evaluate still-current episodes held by the pause.
      for (const [sessionID, episode] of episodes) {
        if (episode.state === AUTO_CC_STATES.PAUSED_HOLD && config.enabled && !disposed) {
          armPending(sessionID, episode)
        }
      }
    },
    get paused() {
      return paused
    },

    // ── completion sound veto (clause 16) ──
    suppressesCompletion(sessionID, next, previous) {
      if (next !== "DONE" || previous === "DONE") return false
      const episode = episodes.get(sessionID)
      return episode?.state === AUTO_CC_STATES.PENDING || episode?.state === AUTO_CC_STATES.ADMITTED
    },

    // ── diagnostics (clause 15) ──
    getStatus(sessionID) {
      const episode = episodes.get(sessionID)
      return {
        enabled: config.enabled,
        paused,
        state: episode?.state ?? AUTO_CC_STATES.IDLE,
        attempts: episode?.attempts ?? 0,
        maxAttempts: config.maxAttempts,
        budgetSpent: episode?.budgetSpent === true,
        lastReason: episode?.lastReason ?? lastReasons.get(sessionID) ?? null,
        episodeID: episode ? episodeID(sessionID, episode) : null,
        pending: episode?.state === AUTO_CC_STATES.PENDING,
        waitingSettle: episode?.state === AUTO_CC_STATES.WAITING_SETTLE,
        exhausted: episode?.state === AUTO_CC_STATES.EXHAUSTED || episode?.budgetSpent === true,
        pendingUserQueue: pendingUserCount(sessionID),
        activeSteps: getActiveStepCount(sessionID),
      }
    },
    // Authoritative pending ordinary-user-queue count (stable message IDs).
    pendingUserCount,
    // Authoritative active native step count for the session (settle truth).
    activeStepCount: getActiveStepCount,
    // Test/diagnostic introspection: proves the race structures stay bounded.
    _debug() {
      let pendingTotal = 0
      for (const set of pendingUserPrompts.values()) pendingTotal += set.size
      let admissionIntentTotal = 0
      for (const set of ordinaryAdmissionIntents.values()) admissionIntentTotal += set.size
      let activeGenerationCount = 0
      for (const set of activeGenerations.values()) activeGenerationCount += set.size
      return {
        pendingTotal,
        admissionIntentTotal,
        tombstones: promotedTombstones.size,
        tombstoneLimit: TOMBSTONE_LIMIT,
        activeSessions: activeSteps.size,
        activeGenerations: activeGenerationCount,
        episodes: episodes.size,
      }
    },
    // ── T-144 2.5.2 admission-intent interface ──
    onPromptIntent(sessionID, intentID) {
      if (disposed || !sessionID || !intentID) return
      let set = ordinaryAdmissionIntents.get(sessionID)
      if (!set) {
        set = new Set()
        ordinaryAdmissionIntents.set(sessionID, set)
      }
      set.add(intentID)
      const episode = episodes.get(sessionID)
      if (episode?.state === AUTO_CC_STATES.PENDING || episode?.state === AUTO_CC_STATES.WAITING_SETTLE) {
        cancelPending(sessionID, "USER_PROMPT_QUEUED")
      }
    },
    onPromptAdmitted(sessionID, intentID) {
      if (!sessionID || !intentID) return
      const set = ordinaryAdmissionIntents.get(sessionID)
      if (set) {
        set.delete(intentID)
        if (set.size === 0) ordinaryAdmissionIntents.delete(sessionID)
      }
    },
    onPromptIntentFailed(sessionID, intentID) {
      if (!sessionID || !intentID) return
      const set = ordinaryAdmissionIntents.get(sessionID)
      if (set) {
        set.delete(intentID)
        if (set.size === 0) ordinaryAdmissionIntents.delete(sessionID)
      }
    },
    describe(sessionID) {
      const s = this.getStatus(sessionID)
      const lines = [`Auto-cc: ${s.paused ? "PAUSED" : s.enabled ? "ON" : "OFF"}`]
      if (s.pending) lines.push("Auto-cc pending")
      else if (s.exhausted) lines.push("Auto-cc exhausted")
      lines.push(`Recovery attempts: ${s.attempts}/${s.maxAttempts}`)
      if (s.lastReason) lines.push(`Last reason: ${s.lastReason}`)
      return lines
    },

    dispose() {
      disposed = true
      for (const sessionID of [...episodes.keys()]) {
        disarm(sessionID)
        episodes.delete(sessionID)
      }
      inFlight.clear()
      ordinaryAdmissionIntents.clear()
      pendingUserPrompts.clear()
      promotedTombstones.clear()
      activeSteps.clear()
      activeGenerations.clear()
    },
  }
}
