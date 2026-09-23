function Find-OpenCodeInstall {
    $shim = Get-Command opencode -ErrorAction SilentlyContinue
    $cmd = Get-Command opencode.cmd -ErrorAction SilentlyContinue
    $shimPath = if ($cmd) { $cmd.Source } elseif ($shim) { $shim.Source } else { $null }
    if (-not $shimPath) {
        return [pscustomobject]@{ found = $false; shim = $null; exe = $null; root = $null; version = $null; package = $null }
    }
    $shimText = if ([IO.Path]::GetExtension($shimPath) -eq '.cmd') { Get-Content -Raw -LiteralPath $shimPath } else { '' }
    $exe = $null
    if ($shimText -match '"(?<exe>[^"]*opencode\.exe)"') {
        $raw = $Matches.exe
        if ($raw -match '(?i)%dp0%') {
            # npm-shaped shim: the path is relative to the shim's own directory.
            $tail = $raw -replace '(?i)^.*%dp0%[\\/]*', ''
            $tail = $tail -replace '[\\/]', [IO.Path]::DirectorySeparatorChar
            $exe = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $shimPath) $tail))
        }
        else { $exe = $raw }
    }
    if (-not $exe) {
        $candidate = Join-Path (Split-Path -Parent $shimPath) 'node_modules\opencode-ai\bin\opencode.exe'
        if (Test-Path -LiteralPath $candidate) { $exe = $candidate }
    }
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
        return [pscustomobject]@{ found = $false; shim = $shimPath; exe = $exe; root = $null; version = $null; package = $null }
    }
    $root = Split-Path -Parent (Split-Path -Parent $exe)
    $packagePath = Join-Path $root 'package.json'
    $package = if (Test-Path -LiteralPath $packagePath) { Get-Content -Raw -LiteralPath $packagePath | ConvertFrom-Json } else { $null }
    $version = if ($package.version) { [string]$package.version } else {
        (& $exe --version 2>$null | Select-Object -First 1).Trim()
    }
    [pscustomobject]@{ found = $true; shim = $shimPath; exe = $exe; root = $root; version = $version; package = $package }
}
