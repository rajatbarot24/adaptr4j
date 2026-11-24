##
## Harness diagnostic: does Service A collapse into bulkhead-full HTTP 500s on its own?
## Default policy only (no policy engine), S1 fault, W2 load, 10 minutes.
## Samples Locust totals and Service A Resilience4j / HTTP metrics every 20 s.
## Output: logs\diag\diag-<tag>.csv
##
param([string]$tag = "baseline", [int]$minutes = 10, [string]$aArgs = "")

$base   = Split-Path -Parent $PSScriptRoot          # repository root
$java   = if ($env:JAVA_EXE) { $env:JAVA_EXE } else { "java" }
$prom   = if ($env:PROMETHEUS_EXE) { $env:PROMETHEUS_EXE } else { "prometheus" }
$toxi   = if ($env:TOXIPROXY_EXE) { $env:TOXIPROXY_EXE } else { "toxiproxy-server" }
$locust = "$base\.venv\Scripts\locust.exe"
$jarA   = "$base\service-a\target\service-a-1.0.0-SNAPSHOT.jar"
$jarB   = "$base\service-b\target\service-b-1.0.0-SNAPSHOT.jar"
$d      = "$base\logs\diag"; New-Item -ItemType Directory -Force $d | Out-Null
$out    = "$d\diag-$tag.csv"

function Listening($p) { [bool](netstat -ano 2>$null | Select-String ":${p}\s.*LISTEN") }
function WaitPort($p) { for ($i=0; $i -lt 60; $i++) { if (Listening $p) { return }; Start-Sleep 2 } }
function KillMatching($pat) { Get-WmiObject Win32_Process | ? { $_.CommandLine -like $pat } | % { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue } }

KillMatching "*locust*"; KillMatching "*1.0.0-SNAPSHOT*"; Start-Sleep 4

Start-Process $java -NoNewWindow -ArgumentList "-jar",$jarB,"--server.port=8081","--fault.latency-ms=50","--fault.latency-jitter-ms=0","--fault.error-rate=0.04" `
  -RedirectStandardOutput "$d\b-$tag.out" -RedirectStandardError "$d\b-$tag.err"
WaitPort 8081
$a = @("-jar",$jarA,"--server.port=8080","--downstream.base-url=http://localhost:8081","--logging.level.root=WARN") + ($aArgs -split ' ' | ? { $_ })
Start-Process $java -NoNewWindow -ArgumentList $a -RedirectStandardOutput "$d\a-$tag.out" -RedirectStandardError "$d\a-$tag.err"
WaitPort 8080; Start-Sleep 8

$loc = Start-Process $locust -NoNewWindow -PassThru -WorkingDirectory $base `
  -ArgumentList "-f","$base\locust\workloads.py","WorkloadW2","--host","http://localhost:8080","--users","50","--spawn-rate","5",
                "--run-time","$($minutes)m","--headless","--csv","$d\locust-$tag","--csv-full-history" `
  -RedirectStandardOutput "$d\locust-$tag.out" -RedirectStandardError "$d\locust-$tag.err"

function Metric($text, $name, $filter) {
    $v = 0.0
    foreach ($l in ($text -split "`n")) {
        if ($l.StartsWith($name) -and ($filter -eq "" -or $l -match $filter)) {
            $v += [double]($l.Trim() -split ' ')[-1]
        }
    }
    return $v
}

"t_s,http_200,http_500,bh_available,cb_open,cb_not_permitted,downstream_calls,downstream_errors,retries,rej_cb_open,rej_bulkhead,rej_downstream" | Set-Content $out
$t0 = Get-Date
while (-not $loc.HasExited) {
    Start-Sleep 20
    try { $m = (Invoke-WebRequest "http://localhost:8080/actuator/prometheus" -UseBasicParsing -TimeoutSec 5).Content } catch { $m = "" }
    $row = @(
        [int]((Get-Date) - $t0).TotalSeconds,
        (Metric $m 'http_server_requests_seconds_count' 'status="200".*uri="/api/checkout"'),
        (Metric $m 'http_server_requests_seconds_count' 'status="500".*uri="/api/checkout"'),
        (Metric $m 'resilience4j_bulkhead_available_concurrent_calls' ''),
        (Metric $m 'resilience4j_circuitbreaker_state' 'state="open"'),
        (Metric $m 'resilience4j_circuitbreaker_not_permitted_calls_total' ''),
        (Metric $m 'adaptr4j_downstream_call_duration_seconds_count' ''),
        (Metric $m 'adaptr4j_downstream_error_total' ''),
        (Metric $m 'adaptr4j_downstream_retry_total' ''),
        (Metric $m 'adaptr4j_downstream_rejected_total' 'reason="cb_open"'),
        (Metric $m 'adaptr4j_downstream_rejected_total' 'reason="bulkhead_full"'),
        (Metric $m 'adaptr4j_downstream_rejected_total' 'reason="downstream"')
    ) -join ','
    Add-Content $out $row
}
KillMatching "*1.0.0-SNAPSHOT*"
"done -> $out"

