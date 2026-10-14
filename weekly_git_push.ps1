# Weekly auto commit + push (Windows Task Scheduler: CursorAI_WeeklyGitPush)
# Secrets/data/logs are excluded by .gitignore (data/*, *.txt, *.csv, *.log, log/*.flag).
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$git = "C:\Program Files\Git\cmd\git.exe"
$logFile = Join-Path $repo "log\weekly_git_push.log"

function Write-Log([string]$msg) {
    "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg | Add-Content -Path $logFile -Encoding utf8
}

Set-Location $repo
Write-Log "=== weekly push start ==="
& $git add -A 2>&1 | ForEach-Object { Write-Log "$_" }
$staged = @(& $git diff --cached --name-only)
if ($staged.Count -gt 0) {
    $msg = "[weekly] $(Get-Date -Format yyyy-MM-dd) auto commit ($($staged.Count) files)"
    & $git commit -m $msg 2>&1 | ForEach-Object { Write-Log "$_" }
    Write-Log "commit exit=$LASTEXITCODE files=$($staged.Count)"
} else {
    Write-Log "no changes - commit skipped"
}
& $git push origin main 2>&1 | ForEach-Object { Write-Log "$_" }
Write-Log "push exit=$LASTEXITCODE"
