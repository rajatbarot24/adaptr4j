##
## Watchdog for the v2 experiment queue.
## Every $interval seconds: health checks + result backup.
##   Alerts   -> logs\v2\watchdog-alerts.log  (one line per anomaly; also printed)
##   Activity -> logs\v2\watchdog.log
##   Backups  -> optional: set ADAPTR4J_BACKUP_DIR (mirror + hourly zip snapshots) and/or
##               ADAPTR4J_BACKUP_DIR2 (results-only mirror, e.g. a cloud-synced folder)
##
param([int]$interval = 600, [string]$dir = "v2")

$base   = Split-Path -Parent $PSScriptRoot          # repository root
$java   = if ($env:JAVA_EXE) { $env:JAVA_EXE } else { "java" }
$prom   = if ($env:PROMETHEUS_EXE) { $env:PROMETHEUS_EXE } else { "prometheus" }
$toxi   = if ($env:TOXIPROXY_EXE) { $env:TOXIPROXY_EXE } else { "toxiproxy-server" }
$v2     = "$base\logs\$dir"
New-Item -ItemType Directory -Force $v2 | Out-Null
$alerts = "$v2\watchdog-alerts.log"
$wlog   = "$v2\watchdog.log"
$bakD   = $env:ADAPTR4J_BACKUP_DIR
$bakO   = $env:ADAPTR4J_BACKUP_DIR2
$python = "$base\.venv\Scripts\python.exe"

if ($bakD) { New-Item -ItemType Directory -Force "$bakD\$dir", "$bakD\snapshots" | Out-Null }
if ($bakO) { New-Item -ItemType Directory -Force "$bakO\$dir" | Out-Null }

function W($m) { Add-Content $wlog "[$(Get-Date -Format 'MM-dd HH:mm:ss')] $m" }
function Alert($m) { $l = "[$(Get-Date -Format 'MM-dd HH:mm:ss')] ALERT $m"; Add-Content $alerts $l; Add-Content $wlog $l; Write-Output $l }

# Start after the lines already in queue.log, so a restarted watchdog does not
# re-alert on history from earlier runs.
$seenQueueLines = if (Test-Path "$v2\queue.log") { @(Get-Content "$v2\queue.log").Count } else { 0 }
$highFail = 0
$lastSnapshot = (Get-Date).AddHours(-2)
$lastHeartbeat = Get-Date

while ($true) {
    try {
        $q = if (Test-Path "$v2\queue.log") { @(Get-Content "$v2\queue.log") } else { @() }
        # Complete only if the LAST queue start has been followed by a completion line
        # (queue.log keeps the history of earlier runs of the queue).
        $lastStart = -1; $lastDone = -1
        for ($k = 0; $k -lt $q.Count; $k++) {
            if ($q[$k] -match "queue starting") { $lastStart = $k }
            if ($q[$k] -match "queue complete") { $lastDone = $k }
        }
        $complete = $lastDone -gt $lastStart

        # 1. queue process alive
        $alive = @(Get-WmiObject Win32_Process | ? { $_.CommandLine -like "*run_v2_queue.ps1*" -and $_.CommandLine -notlike "*-smoke*" }).Count
        if (-not $complete -and $alive -eq 0) { Alert "queue process is NOT running and queue is not complete (last: $($q[-1]))" }

        # 2. new error lines in queue.log, and short jobs
        for ($i = $seenQueueLines; $i -lt $q.Count; $i++) {
            $l = $q[$i]
            if ($l -match "FAILED|TIMEOUT|ERROR|WARN") { Alert "queue.log: $l" }
            if ($l -match "done (\S+) : (\d+) records") {
                $tag = $Matches[1]; $n = [int]$Matches[2]
                $start = ($q[0..$i] -match "=== $([regex]::Escape($tag)) :")[-1]
                if ($start -match "\| (\d+) windows") { if ($n -lt [int]$Matches[1]) { Alert "$tag finished with $n of $($Matches[1]) windows" } }
            }
        }
        $seenQueueLines = $q.Count

        # 3. current job: progress, user-facing failures, reward vs users
        $cur = ($q -match "^\[.*\] === ")[-1]
        if (-not $complete -and $cur -match "=== (\S+) :") {
            $tag = $Matches[1]
            $dec = "$v2\decisions-$tag.jsonl"
            $startedAt = [datetime]::ParseExact(($cur.Substring(1, 14)), "MM-dd HH:mm:ss", $null)
            $mins = ((Get-Date) - $startedAt).TotalMinutes
            if (Test-Path $dec) {
                $age = ((Get-Date) - (Get-Item $dec).LastWriteTime).TotalMinutes
                if ($age -gt 6) { Alert "$tag : no new window for $([int]$age) min (stalled?)" }
            } elseif ($mins -gt 12) { Alert "$tag : no decisions file $([int]$mins) min after start" }

            $h = "$v2\locust-${tag}_stats_history.csv"
            if (Test-Path $h) {
                $res = & $python -c @"
import csv,json,sys
import datetime as dt
rows=[r for r in csv.DictReader(open(r'$h',newline='')) if r.get('Name')=='Aggregated' and r.get('Total Request Count')]
if len(rows)<70: print('na'); sys.exit()
users=int(float(rows[-1]['User Count']))
# Compare over the SAME minute the last decision measured (ending at its timestamp);
# comparing against "now" mixed two different policies and caused false alerts.
end=None; err=None
try:
    last=[json.loads(x) for x in open(r'$dec')][-1]; err=last['metrics_after']['error_rate']
    end=dt.datetime.fromisoformat(last['ts']).timestamp()
except Exception: pass
if end is None: end=int(rows[-1]['Timestamp'])
win=[r for r in rows if end-60<=int(r['Timestamp'])<=end]
if len(win)<2: win=rows[-61:]
a,b=win[0],win[-1]
req=int(b['Total Request Count'])-int(a['Total Request Count']); fail=int(b['Total Failure Count'])-int(a['Total Failure Count'])
print(f'{users} {req} {fail} {err}')
"@
                if ($res -and $res -ne "na") {
                    $u, $req, $fail, $err = $res -split ' '
                    $fr = if ([int]$req -gt 0) { [double]$fail / [int]$req } else { 0 }
                    if ([int]$req -lt 600) { Alert "$tag : only $req requests in last minute (load generator down?)" }
                    if ([int]$u -ne 50) { Alert "$tag : Locust has $u users (expected 50)" }
                    if ($fr -gt 0.35) { $highFail++ } else { $highFail = 0 }
                    if ($highFail -ge 2) { Alert "$tag : $([math]::Round($fr*100,1))% user-facing failures for 2 checks in a row" }
                    if ($err -ne "None" -and $err) {
                        if ([math]::Abs([double]$err - $fr) -gt 0.10) { Alert "$tag : reward error $([math]::Round([double]$err*100,1))% vs users $([math]::Round($fr*100,1))%" }
                    }
                    W "$tag : users=$u req/min=$req userFail=$([math]::Round($fr*100,2))% rewardErr=$err"
                }
            }
        }

        # 4. resources (a battery-forced sleep on 11-07 lost 25 h)
        $bat = Get-CimInstance Win32_Battery -EA SilentlyContinue
        if ($bat -and $bat.BatteryStatus -eq 1) { Alert "laptop is ON BATTERY ($($bat.EstimatedChargeRemaining)%) - plug in or runs will stop when it sleeps" }
        $free = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
        if ($free -lt 15) { Alert "C: only $free GB free" }
        Get-ChildItem $v2 -Filter "svc-*.out" -EA SilentlyContinue | ? { $_.Length -gt 1GB } | % { Alert "$($_.Name) is $([math]::Round($_.Length/1GB,1)) GB" }

        # 5. backups (results only; large service logs excluded)
        if ($bakD) { robocopy $v2 "$bakD\$dir" decisions-*.jsonl locust-*_stats*.csv locust-*_failures.csv run-*.err queue.log watchdog*.log /XO /NP /NJH /NJS /R:1 /W:2 | Out-Null }
        if ($bakO) { robocopy $v2 "$bakO\$dir" decisions-*.jsonl locust-*_stats.csv locust-*_failures.csv queue.log /XO /NP /NJH /NJS /R:1 /W:2 | Out-Null }
        if ($bakD -and ((Get-Date) - $lastSnapshot).TotalMinutes -ge 60) {
            $z = "$bakD\snapshots\$dir-$(Get-Date -Format 'yyyyMMdd-HHmm').zip"
            # zip the backup mirror (live files are held open by the running job)
            Compress-Archive -Path "$bakD\$dir\*" -DestinationPath $z -Force
            $lastSnapshot = Get-Date
            W "snapshot $z"
        }

        if (((Get-Date) - $lastHeartbeat).TotalMinutes -ge 120) {
            Write-Output "[$(Get-Date -Format 'MM-dd HH:mm:ss')] HEARTBEAT $(($q -match 'done ').Count) jobs done, current: $($cur -replace '^\[.*?\] ','')"
            $lastHeartbeat = Get-Date
        }
        if ($complete) { Write-Output "[$(Get-Date -Format 'MM-dd HH:mm:ss')] QUEUE COMPLETE"; W "queue complete, watchdog exiting"; break }
    } catch { Alert "watchdog error: $_" }
    Start-Sleep $interval
}


