import fs from "node:fs"
import path from "node:path"
import os from "node:os"
import { SOUND_DEFAULTS, normalizeSettings } from "./saipatch-completion-sound.js"

export function settingsPath(home = process.env.LOCALAPPDATA ?? os.homedir()) {
  return path.join(home, "SAITULS", "SAIPATCH", "opencode-queue-mode.settings.json")
}

export function loadSoundSettings(file = settingsPath()) {
  try {
    return normalizeSettings(JSON.parse(fs.readFileSync(file, "utf8")))
  } catch {
    return normalizeSettings(SOUND_DEFAULTS)
  }
}

export function saveSoundSettings(settings, file = settingsPath()) {
  const target = path.resolve(file)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  const temp = `${target}.${process.pid}.${Date.now()}.tmp`
  fs.writeFileSync(temp, JSON.stringify(normalizeSettings(settings), null, 2), "utf8")
  fs.renameSync(temp, target)
  return target
}
