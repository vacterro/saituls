param([string]$ScriptSource = (Join-Path $PSScriptRoot '..\Installers\INSTALL_ALL.PS1'))
$ErrorActionPreference = 'Stop'
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('installer-hive-' + [Guid]::NewGuid().ToString('N'))
$keyName = 'SAITULS_Test_' + [Guid]::NewGuid().ToString('N')
$hive = "HKCU:\Software\$keyName"
$regName = "HKEY_CURRENT_USER\Software\$keyName"
$checks = 0
function Assert([bool]$Ok, [string]$Name) {
    if (-not $Ok) { throw "FAIL: $Name" }
    $script:checks++; Write-Output "PASS: $Name"
}
function Fingerprint {
    $all = @((Get-Item -LiteralPath $hive)) + @(Get-ChildItem -LiteralPath $hive -Recurse)
    $records = @(foreach ($key in ($all | Sort-Object Name)) {
        try {
            [ordered]@{ key = $key.Name; values = @(foreach ($name in ($key.GetValueNames() | Sort-Object)) {
                [ordered]@{ name = $name; kind = [string]$key.GetValueKind($name)
                    value = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }
            }) }
        } finally { $key.Close() }
    })
    ConvertTo-Json -InputObject $records -Depth 20 -Compress
}
function Run([string]$Lang = '', [string]$Failure = '') {
    $env:SAITULS_HIVE_FAIL = $Failure
    try {
        $args = @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $sandbox 'Installers/INSTALL_ALL.PS1'))
        if ($Lang) { $args += @('-Lang', $Lang) }
        $output = & powershell.exe @args 2>&1
        return @{ code = $LASTEXITCODE; text = $output -join [Environment]::NewLine }
    } finally { $env:SAITULS_HIVE_FAIL = $null }
}
try {
    foreach ($dir in @('Installers','Registry','i18n/reg/et')) { [void][IO.Directory]::CreateDirectory((Join-Path $sandbox $dir)) }
    $source = Get-Content -LiteralPath $ScriptSource -Raw
    $source = $source -replace '(?s)\$isAdmin = .*?\r?\n\}\r?\n', "`$isAdmin = `$true`r`n"
    $source = $source -replace 'Start-Sleep -Seconds 2', '# no delay in disposable hive harness'
    # All reg.exe operations remain real; only fixture REG paths identify the
    # sandbox. Fail AFTER reg.exe successfully created/deleted keys, not before.
    $source = $source.Replace('if ($proc.ExitCode -ne 0) {', @'
if ($env:SAITULS_HIVE_FAIL -eq 'import' -and (Split-Path -Leaf $path) -eq 'PACK.REG') {
    $surface = Get-Content -Raw -LiteralPath (Join-Path $rootDir 'hive-path.txt')
    $observed = @{ oldAbsent = -not (Test-Path -LiteralPath ($surface + '\OldA')); newPresent = Test-Path -LiteralPath ($surface + '\NewA') }
    $observed | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rootDir 'observed.json')
    throw 'injected PACK.REG failure after real registry mutation'
}
if ($proc.ExitCode -ne 0) {
'@)
    $source = $source.Replace('$rec = @{ lang = $lang; files = @($files) }', @'
if ($env:SAITULS_HIVE_FAIL -eq 'marker') { throw 'injected installed language marker failure after registry mutation' }
$rec = @{ lang = $lang; files = @($files) }
'@)
    Set-Content -LiteralPath (Join-Path $sandbox 'Installers/INSTALL_ALL.PS1') -Value $source -Encoding UTF8
    [IO.File]::WriteAllText((Join-Path $sandbox 'hive-path.txt'), $hive)
    foreach ($locale in @('Registry','i18n/reg/et')) {
        $prefix = if ($locale -eq 'Registry') { 'Old' } else { 'New' }
        $names = @{ COPY_PATH = 'A'; PACK = 'B' }
        foreach ($file in $names.Keys) {
            $name = $prefix + $names[$file]
            $lines = @('Windows Registry Editor Version 5.00', '', "[$regName\$name]", '@="fixture"', "[$regName\$name\command]", '@="cmd.exe /c echo fixture"')
            Set-Content -LiteralPath (Join-Path $sandbox "$locale/$file.REG") -Value $lines -Encoding Unicode
            Set-Content -LiteralPath (Join-Path $sandbox "$locale/${file}_REM.REG") -Value @('Windows Registry Editor Version 5.00', '', "[-$regName\$name]") -Encoding Unicode
        }
    }
    New-Item -Path $hive -Force | Out-Null
    New-Item -Path "$hive\Foreign" -Force | Out-Null
    Set-ItemProperty -LiteralPath "$hive\Foreign" -Name 'keep' -Value 'unrelated sibling'
    $r = Run
    Assert ($r.code -eq 0) "real English generation installed: $($r.text)"
    # The deleted old subtree carries types that string-only snapshots lose.
    $typed = New-Item -Path "$hive\OldA\private" -Force
    try {
        $typed.SetValue('binary', [byte[]]@(0,1,128,255), [Microsoft.Win32.RegistryValueKind]::Binary)
        $typed.SetValue('multi', [string[]]@('one','two'), [Microsoft.Win32.RegistryValueKind]::MultiString)
        $typed.SetValue('expand', '%TEMP%\literal', [Microsoft.Win32.RegistryValueKind]::ExpandString)
        $typed.SetValue('number', [long]4294967297, [Microsoft.Win32.RegistryValueKind]::QWord)
        $typed.Flush()
    } finally { $typed.Close() }
    $before = Fingerprint
    $marker = Join-Path $sandbox 'Installers/.installed_lang'
    $markerHash = (Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash
    $r = Run 'et' 'import'
    Assert ($r.code -ne 0 -and $r.text -match 'PACK.REG') 'failed locale switch reports import failure'
    $observed = Get-Content -Raw -LiteralPath (Join-Path $sandbox 'observed.json') | ConvertFrom-Json
    Assert ($observed.oldAbsent -and $observed.newPresent) 'failure occurred AFTER old keys disappeared and new keys existed'
    if ((Fingerprint) -cne $before) { Write-Output $r.text; Write-Output "BEFORE=$before"; Write-Output "AFTER=$(Fingerprint)" }
    Assert ((Fingerprint) -ceq $before) 'real hive logically byte-equivalent after failed switch, including value types and subkeys'
    Assert (-not (Test-Path -LiteralPath "$hive\NewA")) 'newly-created key removed by rollback'
    Assert (Test-Path -LiteralPath "$hive\OldA\private") 'previously-deleted subtree recreated'
    Assert ((Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash -eq $markerHash) 'marker exact bytes restored after failed import'
    $r = Run 'et' 'marker'
    Assert ($r.code -ne 0 -and $r.text -match 'marker failure') 'post-mutation marker failure surfaced'
    Assert ((Fingerprint) -ceq $before) 'real hive restored after marker failure'
    Assert ((Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash -eq $markerHash) 'marker exact bytes restored after marker failure'
    $missing = Join-Path $sandbox 'i18n/reg/et/COPY_PATH.REG'
    $save = [IO.File]::ReadAllBytes($missing)
    [IO.File]::Delete($missing)
    $r = Run 'et'
    Assert ($r.code -ne 0 -and $r.text -match 'COPY_PATH.REG') 'missing desired source rejected'
    Assert ((Fingerprint) -ceq $before) 'missing desired source leaves previous generation intact'
    [IO.File]::WriteAllBytes($missing, $save)
    $r = Run 'et'
    Assert ($r.code -eq 0) 'real locale switch succeeds'
    Assert ((Test-Path -LiteralPath "$hive\NewA") -and -not (Test-Path -LiteralPath "$hive\OldA")) 'successful switch publishes only desired generation'
    Assert (Test-Path -LiteralPath "$hive\Foreign") 'unrelated registry sibling preserved'
    Write-Output "PASS: $checks real registry transaction checks"
} finally {
    if ($hive -notmatch '^HKCU:\\Software\\SAITULS_Test_[0-9a-f]{32}$') { throw 'Unsafe disposable registry cleanup' }
    if (Test-Path -LiteralPath $hive) { Remove-Item -LiteralPath $hive -Recurse -Force }
    $resolved = [IO.Path]::GetFullPath($sandbox)
    if ([IO.Path]::GetFileName($resolved) -notmatch '^installer-hive-[0-9a-f]{32}$') { throw 'Unsafe fixture cleanup' }
    if ([IO.Directory]::Exists($resolved)) { Remove-Item -LiteralPath $resolved -Recurse -Force }
    $env:SAITULS_HIVE_FAIL = $null
}
