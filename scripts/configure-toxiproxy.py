#!/usr/bin/env python3
"""
Configure Toxiproxy toxics for AdaptR4j network-jitter experiments.

The proxy itself (service-b, listen :8082 → upstream service-b:8081) is
created at Toxiproxy startup via docker/toxiproxy.json.  This script adds or
updates the latency toxic on that proxy.

Examples
--------
# Add 15 ms ± 5 ms latency (default)
python scripts/configure-toxiproxy.py

# Custom values
python scripts/configure-toxiproxy.py --latency 30 --jitter 10

# Remove all toxics (pass-through, reproduces single-machine baseline)
python scripts/configure-toxiproxy.py --disable

# Show current state
python scripts/configure-toxiproxy.py --status
"""
import argparse
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("requests not installed — run: pip install requests")

DEFAULT_API  = "http://localhost:8474"
PROXY_NAME   = "service-b"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_ready(api: str, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{api}/version", timeout=2)
            if r.status_code == 200:
                print(f"[toxi] Toxiproxy {r.text.strip()} ready at {api}")
                return
        except requests.ConnectionError:
            pass
        time.sleep(1)
    sys.exit(f"[toxi] ERROR: Toxiproxy not reachable at {api} after {timeout} s\n"
             f"       Is 'docker compose ... up -d toxiproxy' running?")


def get_proxy(api: str) -> dict | None:
    r = requests.get(f"{api}/proxies/{PROXY_NAME}", timeout=5)
    return r.json() if r.status_code == 200 else None


def ensure_proxy(api: str, listen: str, upstream: str) -> None:
    """Create the proxy if it doesn't exist (Docker: created via JSON config;
    native: pass --listen 127.0.0.1:8082 --upstream 127.0.0.1:8081)."""
    if get_proxy(api) is not None:
        return
    print(f"[toxi] Proxy '{PROXY_NAME}' not found - creating {listen} -> {upstream}")
    r = requests.post(f"{api}/proxies", json={
        "name":     PROXY_NAME,
        "listen":   listen,
        "upstream": upstream,
        "enabled":  True,
    }, timeout=5)
    r.raise_for_status()
    print(f"[toxi] Created proxy: {PROXY_NAME}")


def list_toxics(api: str) -> list:
    r = requests.get(f"{api}/proxies/{PROXY_NAME}/toxics", timeout=5)
    r.raise_for_status()
    return r.json()


def remove_all_toxics(api: str) -> None:
    for toxic in list_toxics(api):
        requests.delete(
            f"{api}/proxies/{PROXY_NAME}/toxics/{toxic['name']}", timeout=5
        ).raise_for_status()
        print(f"[toxi] Removed toxic '{toxic['name']}'")


def set_latency_toxic(api: str, latency_ms: int, jitter_ms: int) -> None:
    # Remove existing latency toxics first (idempotent re-runs)
    for toxic in list_toxics(api):
        if toxic["type"] == "latency":
            requests.delete(
                f"{api}/proxies/{PROXY_NAME}/toxics/{toxic['name']}", timeout=5
            ).raise_for_status()

    if latency_ms == 0 and jitter_ms == 0:
        print("[toxi] No latency toxic added (pass-through mode)")
        return

    r = requests.post(f"{api}/proxies/{PROXY_NAME}/toxics", json={
        "name":       "network-latency",
        "type":       "latency",
        "stream":     "upstream",
        "toxicity":   1.0,
        "attributes": {"latency": latency_ms, "jitter": jitter_ms},
    }, timeout=5)
    r.raise_for_status()
    print(f"[toxi] Latency toxic: {latency_ms} ms ± {jitter_ms} ms  "
          f"(effective range {latency_ms - jitter_ms}–{latency_ms + jitter_ms} ms)")


def print_status(api: str) -> None:
    proxy = get_proxy(api)
    if proxy is None:
        print(f"[toxi] Proxy '{PROXY_NAME}' does not exist")
        return
    print(f"[toxi] Proxy '{PROXY_NAME}': {proxy['listen']} → {proxy['upstream']}  "
          f"enabled={proxy['enabled']}")
    toxics = list_toxics(api)
    if not toxics:
        print("[toxi] No toxics (pass-through)")
    for t in toxics:
        print(f"       toxic '{t['name']}' ({t['type']}, stream={t['stream']}): "
              f"{t['attributes']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Configure Toxiproxy latency toxic for AdaptR4j experiments"
    )
    p.add_argument("--api",     default=DEFAULT_API,
                   help=f"Toxiproxy management URL (default: {DEFAULT_API})")
    p.add_argument("--latency", type=int, default=15,
                   help="One-way added latency in ms (default: 15)")
    p.add_argument("--jitter",  type=int, default=5,
                   help="Latency jitter ± ms (default: 5)")
    p.add_argument("--wait",    type=int, default=30,
                   help="Seconds to wait for Toxiproxy to be ready (default: 30)")
    p.add_argument("--disable", action="store_true",
                   help="Remove all toxics — pass-through mode")
    p.add_argument("--status",  action="store_true",
                   help="Print current proxy/toxic state and exit")
    p.add_argument("--listen",   default="0.0.0.0:8082",
                   help="Proxy listen address if it must be created (default: 0.0.0.0:8082)")
    p.add_argument("--upstream", default="service-b:8081",
                   help="Proxy upstream if it must be created (default: service-b:8081; "
                        "use 127.0.0.1:8081 for native runs)")
    args = p.parse_args()

    wait_ready(args.api, args.wait)
    ensure_proxy(args.api, args.listen, args.upstream)

    if args.status:
        print_status(args.api)
        return

    if args.disable:
        remove_all_toxics(args.api)
        print("[toxi] Pass-through mode — no latency injected")
    else:
        set_latency_toxic(args.api, args.latency, args.jitter)

    print_status(args.api)


if __name__ == "__main__":
    main()
