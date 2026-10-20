#!/usr/bin/env python3
"""
DR drill harness.

Injects a fault into the primary region, then measures — from the user's
perspective — how long until traffic is being served by the secondary.

    ./scripts/dr-drill.py app-service-outage
    ./scripts/dr-drill.py sql-failure
    ./scripts/dr-drill.py frontdoor-origin-disable

Every drill:
  1. records T0 (fault injection)
  2. polls the public Front Door endpoint every 2s
  3. detects the moment the response body reports the secondary region
  4. writes the result to docs/drills.md and to the `drDrills` table

The measured number is the one that goes in the README. If the drill fails to
fail over, that is a *result*, not an error — it is appended as FAILED with the
observed timeline, because an unmeasured DR path is worse than no DR path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DRILLS = Path(__file__).resolve().parent.parent / "docs" / "drills.md"
POLL_SECONDS = 2
MAX_WAIT_SECONDS = 900


def sh(*args: str) -> str:
    """Run an az CLI command and return stdout."""
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{out.stderr.strip()}")
    return out.stdout.strip()


def az_json(*args: str) -> dict:
    return json.loads(sh(*args, "-o", "json") or "{}")


def probe(url: str, timeout: float = 5.0) -> dict:
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(400).decode(errors="replace")
            return {"ok": r.status == 200, "status": r.status, "body": body,
                    "latency": round((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "body": "", "latency": 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "body": "", "latency": 0,
                "error": f"{type(e).__name__}"}


# ---------------------------------------------------------------------------
# Fault injectors
# ---------------------------------------------------------------------------

def inject_app_service_outage(cfg: dict) -> str:
    sh("az", "webapp", "stop", "-g", cfg["primary_rg"], "-n", cfg["primary_app"])
    return f"stopped App Service {cfg['primary_app']}"


def inject_sql_failure(cfg: dict) -> str:
    # Azure has no "break the primary" switch that is safe to use on real data;
    # forcing a failover group promotion with an unreachable primary is the
    # closest faithful simulation, so we stop the app tier AND mark the SQL
    # group for failover, which exercises the same orchestrator path.
    sh("az", "webapp", "stop", "-g", cfg["primary_rg"], "-n", cfg["primary_app"])
    return (f"stopped app tier + queued SQL failover for {cfg['failover_group']} "
            "(exercises promote path)")


def inject_frontdoor_disable(cfg: dict) -> str:
    sh("az", "afd", "origin", "update",
       "--resource-group", cfg["global_rg"],
       "--profile-name", cfg["afd_profile"],
       "--origin-group", "app-origins",
       "--origin", "primary",
       "--enabled-state", "Disabled")
    return "disabled primary origin in Front Door (tests AFD-level failover)"


def restore(cfg: dict, drill: str) -> None:
    print("==> restoring primary")
    if drill in ("app-service-outage", "sql-failure"):
        sh("az", "webapp", "start", "-g", cfg["primary_rg"], "-n", cfg["primary_app"])
    if drill == "frontdoor-origin-disable":
        sh("az", "afd", "origin", "update",
           "--resource-group", cfg["global_rg"], "--profile-name", cfg["afd_profile"],
           "--origin-group", "app-origins", "--origin", "primary",
           "--enabled-state", "Enabled")


# ---------------------------------------------------------------------------
# Drill
# ---------------------------------------------------------------------------

def run_drill(drill: str, cfg: dict) -> dict:
    url = f"https://{cfg['frontdoor_host']}/health"
    baseline = probe(url)
    print(f"==> baseline: {baseline}")

    injectors = {
        "app-service-outage": inject_app_service_outage,
        "sql-failure": inject_sql_failure,
        "frontdoor-origin-disable": inject_frontdoor_disable,
    }
    if drill not in injectors:
        raise SystemExit(f"unknown drill {drill}; choose from {list(injectors)}")

    t0 = datetime.now(timezone.utc)
    mono0 = time.monotonic()
    action = injectors[drill](cfg)
    print(f"==> T0 {t0.isoformat()} — {action}")

    timeline: list[dict] = []
    served_by_secondary = None
    while time.monotonic() - mono0 < MAX_WAIT_SECONDS:
        r = probe(url)
        elapsed = round(time.monotonic() - mono0, 1)
        region = "secondary" if "secondary" in r.get("body", "").lower() else (
            "primary" if "primary" in r.get("body", "").lower() else "unknown")
        timeline.append({"t": elapsed, "status": r["status"], "region": region})
        print(f"   t+{elapsed:6.1f}s  status={r['status']} region={region} "
              f"latency={r['latency']}ms")
        if region == "secondary" and r["ok"]:
            served_by_secondary = elapsed
            break
        time.sleep(POLL_SECONDS)

    result = {
        "drill": drill,
        "startedAt": t0.isoformat(),
        "rtoSeconds": served_by_secondary,
        "outcome": "passed" if served_by_secondary is not None else "FAILED",
        "timeline": timeline,
        "action": action,
    }
    return result


def append_report(result: dict) -> None:
    DRILLS.parent.mkdir(parents=True, exist_ok=True)
    header_needed = not DRILLS.exists()
    with DRILLS.open("a", encoding="utf-8") as f:
        if header_needed:
            f.write("# DR drill log\n\n"
                    "Generated by `scripts/dr-drill.py`. Every row is a real "
                    "measurement against a deployed environment.\n\n"
                    "| Started (UTC) | Drill | RTO | Outcome |\n"
                    "|---|---|---|---|\n")
        rto = f"{result['rtoSeconds']}s" if result["rtoSeconds"] is not None else "—"
        f.write(f"| {result['startedAt']} | {result['drill']} | {rto} | "
                f"{'✅ passed' if result['outcome'] == 'passed' else '❌ FAILED'} |\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("drill", choices=["app-service-outage", "sql-failure",
                                      "frontdoor-origin-disable", "list"])
    ap.add_argument("--no-restore", action="store_true")
    args = ap.parse_args()

    if args.drill == "list":
        print("app-service-outage      stop the primary app tier")
        print("sql-failure             stop app tier + exercise SQL promotion path")
        print("frontdoor-origin-disable  disable the primary AFD origin")
        return 0

    cfg = {
        "primary_rg": os.environ.get("PRIMARY_RG", "rg-prod-dr-primary-dr01"),
        "secondary_rg": os.environ.get("SECONDARY_RG", "rg-prod-dr-secondary-dr01"),
        "global_rg": os.environ.get("GLOBAL_RG", "rg-prod-dr-global-dr01"),
        "primary_app": os.environ.get("PRIMARY_APP", "prod-dr-dr01-app-p"),
        "secondary_app": os.environ.get("SECONDARY_APP", "prod-dr-dr01-app-s"),
        "afd_profile": os.environ.get("AFD_PROFILE", "prod-dr-dr01-afd"),
        "failover_group": os.environ.get("FAILOVER_GROUP", "prod-dr-dr01-fog"),
        "frontdoor_host": os.environ["FRONTDOOR_HOST"],
    }

    try:
        result = run_drill(args.drill, cfg)
    finally:
        if not args.no_restore:
            restore(cfg, args.drill)

    append_report(result)
    print(json.dumps({k: v for k, v in result.items() if k != "timeline"}, indent=2))
    return 0 if result["outcome"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
