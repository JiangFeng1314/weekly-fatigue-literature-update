# login_trigger.ps1
# Scheduled-task entry point.  Runs at user log on.
# If no pending batch exists, exit silently.  Otherwise delegate to check_zotero.ps1,
# which starts Zotero if needed and then runs sync_zotero.py.

param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json')
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8

function Resolve-Config {
    $path = [System.IO.Path]::GetFullPath($ConfigPath)
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Error "Config not found: $path"
        exit 2
    }
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Find-InboxPath {
    param([object]$Config)

    if ($Config.google_drive_inbox) {
        $configured = [Environment]::ExpandEnvironmentVariables([string]$Config.google_drive_inbox)
        $p = [System.IO.Path]::GetFullPath($configured)
        if (Test-Path -LiteralPath $p -PathType Container) { return $p }
    }

    $candidates = @()
    $candidates += Join-Path $env:USERPROFILE 'Documents\Zotero周报同步\待入库'
    $candidates += Join-Path $env:USERPROFILE 'Zotero周报同步\待入库'
    $candidates += Join-Path $env:USERPROFILE 'OneDrive\Zotero周报同步\待入库'
    $candidates += 'G:\Zotero周报同步\待入库'
    $candidates += 'D:\Zotero周报同步\待入库'
    $candidates += 'E:\Zotero周报同步\待入库'

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Container) { return $candidate }
    }

    return $null
}

function Test-PendingBatch {
    param([string]$InboxPath)
    if (-not $InboxPath) { return $false }
    $files = Get-ChildItem -LiteralPath $InboxPath -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'Zotero_本周入库清单_*.json' -or $_.Name -like 'Zotero_本周入库_*.ris' }
    return [bool]($files)
}

$config = Resolve-Config
$inbox = Find-InboxPath -Config $config

if (-not $inbox) {
    exit 0
}

if (-not (Test-PendingBatch -InboxPath $inbox)) {
    exit 0
}

$checkScript = Join-Path $PSScriptRoot 'check_zotero.ps1'
& $checkScript -ConfigPath $ConfigPath
exit $LASTEXITCODE
