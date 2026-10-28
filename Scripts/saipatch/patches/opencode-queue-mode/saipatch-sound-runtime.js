// Windows-native nonblocking WAV playback (clause 23). Volume applies per
// playback via a tiny PowerShell SoundPlayer helper process — no ffmpeg, no
// blocking of the TUI. Playback failure is a warning only.

import { spawn } from "node:child_process"

export function buildPlayCommand({ file, volumePercent }) {
  const safe = file.replace(/'/g, "''")
  const volume = Math.min(1000, Math.max(0, Math.round((Number(volumePercent) || 0) * 10)))
  return [
    "-NoProfile", "-NonInteractive", "-Command",
    `$m = New-Object -ComObject WMPlayer.OCX; $media = $m.newMedia('${safe}'); $player = $m.newPlayer; $player.settings.volume = ${volume / 10}; $player.URL = $media.source; while ($player.playState -notin @(0,1,10)) { Start-Sleep -Milliseconds 50 }; $player.close()`,
  ]
}

export function createPlaybackRunner({ log, spawnImpl, windowsHide = true } = {}) {
  let active = 0
  return {
    play(file, volumePercent) {
      if (typeof file !== "string" || file.length === 0) return false
      if (active > 4) {
        log?.("warn", "completion sound: too many concurrent plays; dropping")
        return false
      }
      active += 1
      const args = buildPlayCommand({ file, volumePercent })
      const child = (spawnImpl ?? spawn)("powershell.exe", args, {
        stdio: "ignore",
        detached: true,
        windowsHide,
      })
      child.on("error", (error) => {
        active = Math.max(0, active - 1)
        log?.("warn", "completion sound playback failed", error)
      })
      child.on("exit", () => {
        active = Math.max(0, active - 1)
      })
      child.unref()
      return true
    },
    get activePlays() {
      return active
    },
  }
}
