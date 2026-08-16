# check_zotero.ps1
# Entry point for manual checks and for login_trigger.ps1.
# Quietly exits 0 when there is no pending batch; otherwise ensures Zotero is
# running on 127.0.0.1:23119 and then runs sync_zotero.py.

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
        $p = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Config.google_drive_inbox))
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

function Test-ZoteroPort {
    param([string]$BaseUrl = 'http://127.0.0.1:23119')
    try {
        $uri = New-Object System.Uri($BaseUrl)
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($uri.Host, $uri.Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(2000)) {
            $client.Close()
            return $false
        }
        $client.EndConnect($iar)
        $client.Close()
        return $true
    }
    catch {
        return $false
    }
}

function Show-BalloonTip {
    param([string]$Title, [string]$Message)
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $notify = New-Object System.Windows.Forms.NotifyIcon
        $notify.Icon = [System.Drawing.SystemIcons]::Information
        $notify.BalloonTipTitle = $Title
        $notify.BalloonTipText = $Message
        $notify.Visible = $true
        $notify.ShowBalloonTip(8000)
        Start-Sleep -Milliseconds 300
        $notify.Dispose()
    }
    catch {
        Write-Host "$Title : $Message"
    }
}

function Start-ZoteroIfNeeded {
    param([object]$Config)
    if (Test-ZoteroPort -BaseUrl $Config.zotero_base_url) { return $true }

    $exe = $Config.zotero_exe
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        Write-Output "ZOTERO_EXE_NOT_FOUND: $exe"
        return $false
    }

    Show-BalloonTip -Title '发现新的每周疲劳文献等待入库' -Message '发现新的每周疲劳文献等待入库，请打开 Zotero。'
    try {
        Start-Process -FilePath $exe -WindowStyle Hidden
    }
    catch {
        Write-Output "ZOTERO_START_FAILED: $($_.Exception.Message)"
        return $false
    }

    $timeout = [int]$Config.poll_timeout_seconds
    if ($timeout -le 0) { $timeout = 120 }
    $deadline = (Get-Date).AddSeconds($timeout)
    while ((Get-Date) -lt $deadline) {
        if (Test-ZoteroPort -BaseUrl $Config.zotero_base_url) { return $true }
        Start-Sleep -Seconds 3
    }

    Write-Output "ZOTERO_START_TIMEOUT"
    return $false
}

$config = Resolve-Config
$inbox = Find-InboxPath -Config $config

if (-not $inbox) {
    Write-Output 'GOOGLE_DRIVE_NOT_SYNCED'
    exit 0
}

if (-not (Test-PendingBatch -InboxPath $inbox)) {
    Write-Output 'NO_BATCH'
    exit 0
}

if (-not (Start-ZoteroIfNeeded -Config $config)) {
    exit 3
}

$python = $config.python_exe
if (-not $python -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Error "Python not found: $python"
    exit 2
}

$syncScript = Join-Path $PSScriptRoot 'sync_zotero.py'
& $python $syncScript --config $ConfigPath
exit $LASTEXITCODE
