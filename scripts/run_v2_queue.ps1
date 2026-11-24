##
## v2 experiment queue -- full rerun on the fixed harness (Nov 2026).
##
## Harness fixes in effect (see the README, "How measurement fooled us"): CB thresholds in percent, user-visible error accounting,
## standard Retry->CB->Bulkhead order, no Locust load shape for W2, runtime fault
## switching in Service B, rollback ignores shadow windows, baselines without rollback.
##
## Every job: fresh Service A/B (and C / Toxiproxy when needed), 50-user W2 Locust,
## 80 windows (drift: 160). Outputs: logs\v2\decisions-<tag>.jsonl, locust-<tag>_*.csv
## A job whose output already has the expected record count is skipped (resumable).
## Progress: logs\v2\queue.log
##
param([string]$only = "",   # optional regex to run a subset of tags
      [int]$smoke = 0,       # >0: run each selected job for this many windows into logs\v2smoke
      [string]$variant = "v2")  # v3: safe menu + --safe-explore + bounded reward, output logs\v3

$base   = Split-Path -Parent $PSScriptRoot          # repository root
$java   = if ($env:JAVA_EXE) { $env:JAVA_EXE } else { "java" }
$prom   = if ($env:PROMETHEUS_EXE) { $env:PROMETHEUS_EXE } else { "prometheus" }
$toxi   = if ($env:TOXIPROXY_EXE) { $env:TOXIPROXY_EXE } else { "toxiproxy-server" }
$python = "$base\.venv\Scripts\python.exe"
$locust = "$base\.venv\Scripts\locust.exe"
$jarA   = "$base\service-a\target\service-a-1.0.0-SNAPSHOT.jar"
$jarB   = "$base\service-b\target\service-b-1.0.0-SNAPSHOT.jar"
$jarC   = "$base\service-c\target\service-c-1.0.0-SNAPSHOT.jar"
$out    = if ($smoke -gt 0) { "$base\logs\${variant}smoke" } else { "$base\logs\$variant" }
New-Item -ItemType Directory -Force $out | Out-Null
$qlog   = "$out\queue.log"

# fault profiles: latency ms, error rate
$F = @{ S1=@(50,0.04); S2=@(200,0.10); S3=@(500,0.0); S4=@(0,0.30); S5=@(0,0.0); S6=@(0,0.0) }

# ---------------------------------------------------------------- job list
$J = [System.Collections.ArrayList]::new()
function Add($tag, $sc, $kind, $arg = "", $win = 80) {
    if ($smoke -gt 0) { $win = $smoke }
    [void]$J.Add(@{tag=$tag; sc=$sc; kind=$kind; arg=$arg; win=$win})
}

# Trimmed to 39 jobs (~57 h). Dropped: Random (UCB1/Rule cover it), S3-S6 replications,
# UCB1 outside S5, sweep/B3 for S6, no-guardrails ablation, fan-out S8.

# Phase A -- S1/S2 core (16 jobs; checkpoint ~8 Nov)
foreach ($s in "S1","S2") { Add "sweep-$s" $s "baseline" "sweep" 65 }
foreach ($s in "S1","S2") { Add "adaptr4j-$s-r1" $s "engine" }
foreach ($s in "S1","S2") { Add "b3-$s" $s "b3" }
foreach ($s in "S1","S2") { foreach ($b in "static-p01","ucb1","rule") { Add "$b-$s" $s "baseline" $b } }
foreach ($r in 2,3) { foreach ($s in "S1","S2") { Add "adaptr4j-$s-r$r" $s "engine" } }
# Phase B -- ablations (S1), admission control, network latency (5 jobs)
Add "abl-nocontext-S1"  "S1" "engine" "--no-context"
Add "abl-nocoord-S1"    "S1" "nocoord"
foreach ($s in "S1","S2") { Add "rajomon-$s" $s "rajomon" }
Add "toxi-S1" "S1" "toxi"
# Phase C -- S3-S6, one run each (15 jobs)
foreach ($s in "S3","S4","S5") {
    Add "sweep-$s" $s "baseline" "sweep" 65
    Add "adaptr4j-$s-r1" $s "engine"
    Add "b3-$s" $s "b3"
    Add "static-p01-$s" $s "baseline" "static-p01"
}
Add "ucb1-S5" "S5" "baseline" "ucb1"
Add "adaptr4j-S6-r1"   "S6" "engine"
Add "static-p01-S6"    "S6" "baseline" "static-p01"
# Phase D -- fan-out and drift (3 jobs)
Add "fanout-S7" "S7" "fanout"
Add "drift-adaptr4j" "S1" "drift" "" 160
Add "drift-b3"       "S1" "drift" "b3" 160

# v3 job list (safe menu + safe exploration + bounded reward). Replaces the list above
# when -variant v3 is given. Baselines under v3 also use the safe menu.
if ($variant -eq "v3") {
    $J.Clear()
    foreach ($s in "S1","S2","S3","S4","S5") { Add "adaptr4j-$s-r1" $s "engine" }      # done 11-21
    foreach ($s in "S1","S2","S4","S5") { Add "adaptr4j-$s-r2" $s "engine" }         # replication
    foreach ($s in "S1","S2","S4","S5") { Add "ucb1-$s" $s "baseline" "ucb1" }       # safe menu, no context
    foreach ($s in "S1","S2","S4","S5") { Add "adaptr4j-$s-r3" $s "engine" }         # replication
    foreach ($s in "S1","S4") { Add "abl-nocontext-$s" $s "engine" "--no-context" }  # ablation
}

# ---------------------------------------------------------------- helpers
function Q($m) { $l = "[$(Get-Date -Format 'MM-dd HH:mm:ss')] $m"; Write-Host $l; Add-Content $qlog $l }
function Listening($p) { [bool](netstat -ano 2>$null | Select-String ":${p}\s.*LISTEN") }
function WaitPort($p) { for ($i=0; $i -lt 90; $i++) { if (Listening $p) { return $true }; Start-Sleep 2 }; Q "WARN port $p down"; $false }
function KillMatching($pat) { Get-WmiObject Win32_Process | ? { $_.CommandLine -like $pat } | % { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue } }
function StopAll {
    foreach ($p in "*main.py*","*baseline_runner.py*","*no_coord_main.py*","*rajomon_controller.py*","*locust*","*SNAPSHOT.jar*") { KillMatching $p }
    Get-Process toxiproxy-server -EA SilentlyContinue | Stop-Process -Force
    Get-Job | Stop-Job -PassThru | Remove-Job -Force -EA SilentlyContinue
    Start-Sleep 5
}
function Records($f) { if (Test-Path $f) { (Get-Content $f | Measure-Object -Line).Lines } else { 0 } }
function Fault($port, $lat, $err) {
    try { Invoke-RestMethod -Method Post "http://localhost:$port/api/fault?latencyMs=$lat&errorRate=$err" -TimeoutSec 5 | Out-Null } catch { Q "WARN fault switch failed: $_" }
}
$javaQuiet = @("--logging.level.root=WARN","--logging.level.org.apache.catalina.core=OFF")

function StartJava($jar, $port, $extra, $tag) {
    # -Xmx512m: the JVM default (1/4 of RAM = 2 GB each) let the services grow until the
    # 8 GB laptop ran out of memory on 11-09 and the queue was killed.
    Start-Process $java -NoNewWindow -ArgumentList (@("-Xmx512m","-jar",$jar,"--server.port=$port") + $javaQuiet + $extra) `
      -RedirectStandardOutput "$out\svc-$port-$tag.out" -RedirectStandardError "$out\svc-$port-$tag.err"
    WaitPort $port
}

function RunJob($j) {
    $tag = $j.tag; $dec = "$out\decisions-$tag.jsonl"
    if ($only -and ($tag -notmatch $only)) { return }
    if ((Records $dec) -ge $j.win) { Q "skip $tag ($(Records $dec) records)"; return }
    Remove-Item $dec -EA SilentlyContinue
    StopAll
    Remove-Item Env:ADAPTR4J_SHAPE -EA SilentlyContinue

    $fanout = $j.kind -eq "fanout"
    $sc = $j.sc; if ($fanout) { $sc = "S1" }
    $lat, $err = $F[$sc]
    Q "=== $tag : $($j.kind) $($j.arg) | $($j.sc) | $($j.win) windows ==="

    # Service B (and C for fan-out)
    if (-not (StartJava $jarB 8081 @("--fault.latency-ms=$lat","--fault.error-rate=$err") $tag)) { Q "FAILED $tag (B)"; return }
    $aDown = "http://localhost:8081"
    if ($fanout) {
        $cLat, $cErr = $F[$(if ($j.sc -eq "S7") { "S2" } else { "S1" })]
        if (-not (StartJava $jarC 8083 @("--fault.latency-ms=$cLat","--fault.error-rate=$cErr") $tag)) { Q "FAILED $tag (C)"; return }
    }
    if ($j.kind -eq "toxi") {
        Start-Process $toxi -NoNewWindow -ArgumentList "-host","127.0.0.1","-port","8474" -RedirectStandardOutput "$out\toxi-$tag.out" -RedirectStandardError "$out\toxi-$tag.err"
        WaitPort 8474 | Out-Null
        & $python "$base\scripts\configure-toxiproxy.py" --listen 127.0.0.1:8082 --upstream 127.0.0.1:8081 --latency 15 --jitter 5 2>&1 | Out-Null
        WaitPort 8082 | Out-Null
        $aDown = "http://localhost:8082"
    }
    if (-not (StartJava $jarA 8080 @("--downstream.base-url=$aDown","--downstream-c.base-url=http://localhost:8083") $tag)) { Q "FAILED $tag (A)"; return }

    # warm-up: 200 sequential requests so JIT/connection set-up is not measured
    $uri = if ($fanout) { "/api/checkout-fanout" } else { "/api/checkout" }
    1..200 | % { try { Invoke-WebRequest "http://localhost:8080$uri" -UseBasicParsing -TimeoutSec 5 | Out-Null } catch {} }

    # Locust: 50 users, W2 (or fan-out W2)
    $cls = if ($fanout) { "WorkloadFanoutW2" } else { "WorkloadW2" }
    $loc = Start-Process $locust -NoNewWindow -PassThru -WorkingDirectory $base `
      -ArgumentList "-f","$base\locust\workloads.py",$cls,"--host","http://localhost:8080","--users","50","--spawn-rate","5",
                    "--run-time","$($j.win + 15)m","--headless","--csv","$out\locust-$tag","--logfile","$out\locust-$tag.log" `
      -RedirectStandardOutput "$out\locust-$tag.out" -RedirectStandardError "$out\locust-$tag.err"
    Start-Sleep 15

    # S5: alternate Service B latency 0 <-> 500 ms every 5 minutes (no restart)
    if ($j.sc -eq "S5") {
        Start-Job -ScriptBlock { $on = $false; while ($true) { Start-Sleep 300; $on = -not $on
            $l = if ($on) { 500 } else { 0 }
            try { Invoke-RestMethod -Method Post "http://localhost:8081/api/fault?latencyMs=$l&errorRate=0" -TimeoutSec 5 | Out-Null } catch {} } } | Out-Null
    }
    # drift: S1 -> S2 after window 80
    if ($j.kind -eq "drift") {
        Start-Job -ScriptBlock { Start-Sleep (80 * 60 + 20)
            Invoke-RestMethod -Method Post "http://localhost:8081/api/fault?latencyMs=200&errorRate=0.10" -TimeoutSec 5 | Out-Null } | Out-Null
    }

    $menu = if ($variant -eq "v3") { "$base\policy-menu-safe.yml" } else { "$base\policy-menu.yml" }
    $common = @("--prometheus","http://localhost:9090","--service-a","http://localhost:8080","--shadow","5","--window","60","--max-windows","$($j.win)","--log-file",$dec)
    $kind = $j.kind; $arg = $j.arg
    if ($kind -eq "b3" -or ($kind -eq "drift" -and $arg -eq "b3")) {
        $sw = "$out\decisions-sweep-$($j.sc).jsonl"
        if (-not (Test-Path $sw)) { Q "FAILED $tag (no sweep log $sw)"; return }
        $best = (& $python "$base\analysis\pick_b3.py" $sw).Trim()
        Q "$tag : B3 oracle for $($j.sc) = $best"
        $a = @("$base\baseline_runner.py","--strategy","static:$best","--menu",$menu) + $common
    } elseif ($kind -eq "baseline") {
        $a = @("$base\baseline_runner.py","--strategy",$arg,"--menu",$menu) + $common
    } elseif ($kind -eq "nocoord") {
        $a = @("$base\no_coord_main.py","--menu",$menu) + $common
    } elseif ($kind -eq "rajomon") {
        $a = @("$base\rajomon_controller.py") + $common
    } else {
        $a = @("$base\main.py","--menu",$menu) + $common
        if ($fanout) { $a += @("--target-uri","/api/checkout-fanout") }
        if ($arg) { $a += $arg }
        if ($variant -eq "v3") { $a += "--safe-explore" }
    }
    $pe = Start-Process $python -ArgumentList $a -WorkingDirectory $base -NoNewWindow -PassThru `
      -RedirectStandardOutput "$out\run-$tag.out" -RedirectStandardError "$out\run-$tag.err"
    $limit = (Get-Date).AddMinutes($j.win * 1.2 + 15)
    while (-not $pe.HasExited) {
        Start-Sleep 30
        if ((Get-Date) -gt $limit) { Q "TIMEOUT $tag"; Stop-Process -Id $pe.Id -Force; break }
    }
    Q "done $tag : $(Records $dec) records"
}

# ---------------------------------------------------------------- main
Add-Type -Namespace Win32 -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
[Win32.Power]::SetThreadExecutionState([uint32]"0x80000001") | Out-Null
Q "v2 queue starting: $($J.Count) jobs"
if (-not (Listening 9090)) {
    Start-Process $prom -NoNewWindow -WorkingDirectory "$base\logs" `
      -ArgumentList "--config.file=$base\prometheus.yml","--storage.tsdb.path=$base\logs\prometheus-data","--storage.tsdb.retention.time=3d" `
      -RedirectStandardOutput "$out\prom.out" -RedirectStandardError "$out\prom.err"
    WaitPort 9090 | Out-Null
}
foreach ($j in $J) { try { RunJob $j } catch { Q "ERROR $($j.tag): $_" } }
StopAll
Q "v2 queue complete"

