param(
    [int]$DebounceSeconds = 3
)

$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Git = Get-Command git -ErrorAction SilentlyContinue
if (-not $Git) {
    $Git = Get-Item 'C:\Users\msi\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe'
}

Set-Location -LiteralPath $Project

function Invoke-Git([string[]]$Args) {
    & $Git.Source -C $Project @Args
    if ($LASTEXITCODE -ne 0) { throw "git failed: $($Args -join ' ')" }
}

if (-not (Test-Path (Join-Path $Project '.git'))) {
    Invoke-Git @('init', '-b', 'main')
}

if (-not (& $Git.Source -C $Project config user.name)) {
    & $Git.Source -C $Project config user.name 'Monitoring Bot'
}
if (-not (& $Git.Source -C $Project config user.email)) {
    & $Git.Source -C $Project config user.email 'monitoring-bot@users.noreply.github.com'
}

$watcher = New-Object IO.FileSystemWatcher $Project
$watcher.IncludeSubdirectories = $true
$watcher.Filter = '*'
$watcher.EnableRaisingEvents = $true
$watcher.NotifyFilter = [IO.NotifyFilters]'FileName, DirectoryName, LastWrite, Size'

$pending = $false
$lastChange = [DateTime]::MinValue
$handler = {
    if ($Event.SourceEventArgs.FullPath -notmatch '\\.git(\\|$)') {
        $script:pending = $true
        $script:lastChange = [DateTime]::UtcNow
    }
}

$events = @('Created','Changed','Deleted','Renamed')
$subscriptions = foreach ($eventName in $events) {
    Register-ObjectEvent -InputObject $watcher -EventName $eventName -Action $handler
}

Write-Host "Watching $Project for changes. Press Ctrl+C to stop."
try {
    while ($true) {
        Start-Sleep -Seconds 1
        if (-not $pending -or (([DateTime]::UtcNow - $lastChange).TotalSeconds -lt $DebounceSeconds)) { continue }
        $pending = $false

        & $Git.Source -C $Project add -A
        $hasChanges = (& $Git.Source -C $Project diff --cached --quiet; $LASTEXITCODE -ne 0)
        if (-not $hasChanges) { continue }

        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        & $Git.Source -C $Project commit -m "Auto-sync: $stamp"
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Commit failed; will retry on the next change.'; continue }

        $remote = (& $Git.Source -C $Project remote get-url origin 2>$null)
        if ($remote) {
            & $Git.Source -C $Project push -u origin main
            if ($LASTEXITCODE -eq 0) { Write-Host "Pushed $stamp" } else { Write-Warning 'Push failed; commit is retained locally.' }
        } else {
            Write-Warning 'No origin remote configured; commit is local only.'
        }
    }
}
finally {
    $subscriptions | ForEach-Object { Unregister-Event -SubscriptionId $_.Id -ErrorAction SilentlyContinue }
    $watcher.Dispose()
}
