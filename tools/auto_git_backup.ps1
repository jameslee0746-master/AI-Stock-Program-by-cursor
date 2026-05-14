#Requires -Version 5.1
<#
  파일 변경이 있으면 “마지막 변경 시각”을 갱신하고,
  메인 루프에서 그 시각부터 DebounceSeconds 이상 조용할 때만 git commit 합니다.

  사용:
    pwsh -File tools/auto_git_backup.ps1
    pwsh -File tools/auto_git_backup.ps1 -DebounceSeconds 300

  원격 백업: 별도로 git push (GitHub 등) 필요.
#>
param(
    [string]$RepoPath = "",
    [int]$DebounceSeconds = 120
)

$ErrorActionPreference = "Stop"

if (-not $RepoPath) {
    $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
if (-not (Test-Path (Join-Path $RepoPath ".git"))) {
    Write-Error "Git 저장소가 아닙니다: $RepoPath"
}

Set-Location -LiteralPath $RepoPath

$debounce = [Math]::Max(30, $DebounceSeconds)
$sync = [hashtable]::Synchronized(@{
        LastBumpUtc = [datetime]::UtcNow
    })

$handler = {
    try {
        $name = $Event.SourceEventArgs.Name
        if ($name -match '(\\|^)\.git(\\|$)') {
            return
        }
        $Event.MessageData.LastBumpUtc = [datetime]::UtcNow
    }
    catch {
        # ignore
    }
}

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $RepoPath
$watcher.IncludeSubdirectories = $true
$watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor
    [System.IO.NotifyFilters]::LastWrite -bor
    [System.IO.NotifyFilters]::CreationTime -bor
    [System.IO.NotifyFilters]::DirectoryName

$subs = @(
    Register-ObjectEvent -InputObject $watcher -EventName Changed -Action $handler -MessageData $sync
    Register-ObjectEvent -InputObject $watcher -EventName Created -Action $handler -MessageData $sync
    Register-ObjectEvent -InputObject $watcher -EventName Deleted -Action $handler -MessageData $sync
    Register-ObjectEvent -InputObject $watcher -EventName Renamed -Action $handler -MessageData $sync
)
$watcher.EnableRaisingEvents = $true

Write-Host "[auto-git-backup] watching: $RepoPath"
Write-Host "[auto-git-backup] debounce: ${debounce}s after last change (Ctrl+C 로 종료)"

function Invoke-AutoCommit {
    try {
        $dirty = git status --porcelain 2>$null
        if (-not $dirty) {
            return
        }
        git add -A
        if (git diff --cached --quiet 2>$null) {
            return
        }
        $msg = "chore: auto-backup $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        git commit -m $msg
        Write-Host "[auto-git-backup] committed: $msg"
    }
    catch {
        Write-Warning "[auto-git-backup] commit 실패: $_"
    }
}

try {
    while ($true) {
        Start-Sleep -Seconds 2
        $dirty = git status --porcelain 2>$null
        if (-not $dirty) {
            continue
        }
        $idle = [datetime]::UtcNow - $sync.LastBumpUtc
        if ($idle.TotalSeconds -ge $debounce) {
            Invoke-AutoCommit
            $sync.LastBumpUtc = [datetime]::UtcNow
        }
    }
}
finally {
    $watcher.EnableRaisingEvents = $false
    $watcher.Dispose()
    foreach ($s in $subs) {
        Unregister-Event -SubscriptionId $s.SubscriptionId -ErrorAction SilentlyContinue
    }
}
