##
## Validation run 2: short AdaptR4j run on the fixed harness, so the reward's view of
## each window can be checked against what Locust users actually experienced.
## Output: logs\diag\decisions-val-<tag>.jsonl + Locust full-history CSV
##
param([string]$tag = "S1", [int]$lat = 50, [double]$err = 0.04, [int]$windows = 30)

$base   = Split-Path -Parent $PSScriptRoot          # repository root
$java   = if ($env:JAVA_EXE) { $env:JAVA_EXE } else { "java" }
$prom   = if ($env:PROMETHEUS_EXE) { $env:PROMETHEUS_EXE } else { "prometheus" }
$toxi   = if ($env:TOXIPROXY_EXE) { $env:TOXIPROXY_EXE } else { "toxiproxy-server" }
$python = "$base\.venv\Scripts\python.exe"
$locust = "$base\.venv\Scripts\locust.exe"
$d      = "$base\logs\diag"; New-Item -ItemType Directory -Force $d | Out-Null

function Listening($p) { [bool](netstat -ano 2>$null | Select-String ":${p}\s.*LISTEN") }
function WaitPort($p) { for ($i=0; $i -lt 60; $i++) { if (Listening $p) { return }; Start-Sleep 2 } }
function KillMatching($pat) { Get-WmiObject Win32_Process | ? { $_.CommandLine -like $pat } | % { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue } }

KillMatching "*locust*"; KillMatching "*1.0.0-SNAPSHOT*"; KillMatching "*main.py*"; Start-Sleep 4
Remove-Item Env:ADAPTR4J_SHAPE -EA SilentlyContinue

if (-not (Listening 9090)) {
    Start-Process $prom -NoNewWindow -WorkingDirectory "$base\logs" `
      -ArgumentList "--config.file=$base\prometheus.yml","--storage.tsdb.path=$base\logs\prometheus-data" `
      -RedirectStandardOutput "$d\prom.out" -RedirectStandardError "$d\prom.err"
    WaitPort 9090
}
Start-Process $java -NoNewWindow -ArgumentList "-jar","$base\service-b\target\service-b-1.0.0-SNAPSHOT.jar","--server.port=8081","--fault.latency-ms=$lat","--fault.latency-jitter-ms=0","--fault.error-rate=$err","--logging.level.root=WARN" `
  -RedirectStandardOutput "$d\vb-$tag.out" -RedirectStandardError "$d\vb-$tag.err"
WaitPort 8081
Start-Process $java -NoNewWindow -ArgumentList "-jar","$base\service-a\target\service-a-1.0.0-SNAPSHOT.jar","--server.port=8080","--downstream.base-url=http://localhost:8081","--logging.level.root=WARN" `
  -RedirectStandardOutput "$d\va-$tag.out" -RedirectStandardError "$d\va-$tag.err"
WaitPort 8080; Start-Sleep 8

$loc = Start-Process $locust -NoNewWindow -PassThru -WorkingDirectory $base `
  -ArgumentList "-f","$base\locust\workloads.py","WorkloadW2","--host","http://localhost:8080","--users","50","--spawn-rate","5",
                "--run-time","$($windows + 5)m","--headless","--csv","$d\locust-val-$tag","--csv-full-history" `
  -RedirectStandardOutput "$d\locust-val-$tag.out" -RedirectStandardError "$d\locust-val-$tag.err"
Start-Sleep 10

$out = "$d\decisions-val-$tag.jsonl"; Remove-Item $out -EA SilentlyContinue
& $python "$base\main.py" --menu "$base\policy-menu.yml" --prometheus "http://localhost:9090" --service-a "http://localhost:8080" `
    --shadow 5 --window 60 --max-windows $windows --log-file $out 2> "$d\val-$tag-engine.err"

KillMatching "*locust*"; KillMatching "*1.0.0-SNAPSHOT*"
"done -> $out"

