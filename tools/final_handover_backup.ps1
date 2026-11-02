#Requires -Version 5.1
<#
  최종 인수인계 백업 (로컬 미러 + git commit/push)
  - 기본 대상일: 2026-11-24 (Cursor 구독 종료 전날)
  - Windows 작업 스케줄러: CursorAI_FinalBackup_2026-11-24

  수동 실행:
    pwsh -File tools\final_handover_backup.ps1
    pwsh -File tools\final_handover_backup.ps1 -Force
#>
param(
    [string]$RepoPath = "",
    [string]$BackupRoot = "C:\cursorAI_work_backup",
    [switch]$Force,
    [string]$ExpectedDate = "2026-11-24"
)

$ErrorActionPreference = "Continue"
$git = "C:\Program Files\Git\cmd\git.exe"
if (-not $RepoPath) {
    $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$logDir = Join-Path $RepoPath "log"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "final_handover_backup.log"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Encoding utf8 -Value $line
    Write-Host $line
}

$today = Get-Date -Format "yyyy-MM-dd"
Write-Log "=== final handover backup start (today=$today expected=$ExpectedDate force=$Force) ==="

if (-not $Force -and $today -ne $ExpectedDate) {
    Write-Log "SKIP: not $ExpectedDate (use -Force to run anyway)"
    exit 0
}

if (-not (Test-Path (Join-Path $RepoPath ".git"))) {
    Write-Log "ERROR: not a git repo: $RepoPath"
    exit 1
}

Set-Location -LiteralPath $RepoPath
$stamp = Get-Date -Format "yyyy-MM-dd"
$dest = Join-Path $BackupRoot $stamp
New-Item -ItemType Directory -Force -Path $dest | Out-Null
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

$excludeDirs = @(
    ".git", "__pycache__", ".venv", "venv", "env",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"
)
$xdArgs = @()
foreach ($d in $excludeDirs) { $xdArgs += @("/XD", $d) }

Write-Log "robocopy $RepoPath -> $dest"
& robocopy $RepoPath $dest /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XF "*.pyc" "*.pyo" @xdArgs | Out-Null
Write-Log "robocopy exit=$LASTEXITCODE (0-7 = ok for robocopy)"

# Git commit + push (secrets/data/logs excluded by .gitignore)
if (Test-Path $git) {
    Write-Log "git add/commit/push"
    & $git add -A 2>&1 | ForEach-Object { Write-Log "git: $_" }
    $staged = @(& $git diff --cached --name-only 2>$null)
    if ($staged.Count -gt 0) {
        $msg = "[final-backup] $stamp Cursor handover backup ($($staged.Count) files)"
        & $git commit -m $msg 2>&1 | ForEach-Object { Write-Log "git: $_" }
        Write-Log "commit exit=$LASTEXITCODE files=$($staged.Count)"
    } else {
        Write-Log "no staged changes - commit skipped"
    }
    & $git push origin HEAD 2>&1 | ForEach-Object { Write-Log "git: $_" }
    Write-Log "push exit=$LASTEXITCODE"
    $head = (& $git rev-parse HEAD 2>$null)
} else {
    Write-Log "WARN: git.exe not found"
    $head = "(unknown)"
}

$info = @"
backup_date: $stamp
role: Cursor 구독 종료 전 최종 인수인계 백업 (로컬 미러 + git)
source: $RepoPath
git_commit: $head
git_remote: origin (push attempted)
cursor_subscription_end: 2026-11-25
mock_live_parity: REQUIRED (MOCK_LIVE_PARITY=1, 모의=실전 규칙)
notes: 코드·파라미터·로그·전략데이터 포함. data/CSV는 git 제외·로컬 스냅샷에 포함될 수 있음.
"@
$info | Set-Content -Encoding UTF8 (Join-Path $dest "BACKUP_INFO.txt")
$info | Set-Content -Encoding UTF8 (Join-Path $BackupRoot "FINAL_BACKUP_INFO.txt")

# Keep a copy of handover pointer
$handoverSrc = Join-Path $dest "인수인계서.md"
if (-not (Test-Path $handoverSrc)) {
    $prev = Get-ChildItem $BackupRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' } |
        Sort-Object Name -Descending |
        Select-Object -First 5
    foreach ($p in $prev) {
        $cand = Join-Path $p.FullName "인수인계서.md"
        if (Test-Path $cand) {
            Copy-Item -Force $cand $handoverSrc
            break
        }
    }
}
if (Test-Path $handoverSrc) {
    Copy-Item -Force $handoverSrc (Join-Path $BackupRoot "인수인계서_최신.md")
}

Write-Log "DONE dest=$dest"
Write-Log "=== final handover backup end ==="
exit 0
