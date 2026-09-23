Add-Type -Path ".\TagLibSharp.dll"

$folderPath = Get-Location

Get-ChildItem $folderPath -Filter *.mp3 | ForEach-Object {

    $filePath = $_.FullName
    $title = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)

    $audio = [TagLib.File]::Create($filePath)

    $audio.Tag.Title = $title
    $audio.Tag.Performers = @("potatoddas")

    $audio.Save()
    $audio.Dispose()

    Write-Host "Updated: $($_.Name)"
}
