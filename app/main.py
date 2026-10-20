"""Minimal app for the DR topology.

It exists to answer two questions with certainty:

  GET /health  -> {"status":"ok","role":"primary|secondary","region":"..."}
                  The Front Door health probe hits this. The drill harness
                  reads `role` to know which region actually served the request.

  GET /        -> the same, as HTML, so you can eyeball it in a browser.

Deliberately stateless: DR is about the *platform*, not the app. Any stateful
behaviour would muddy the RTO measurement.
"""

from __future__ import annotations

import os
import json
import socket
from datetime import datetime, timezone

from flask import Flask, jsonify, Response

app = Flask(__name__)

ROLE = os.environ.get("REGION_ROLE", "unknown")
REGION = os.environ.get("REGION_NAME", "unknown")
STARTED = datetime.now(timezone.utc)
HOSTNAME = socket.gethostname()


def payload() -> dict:
    return {
        "status": "ok",
        "role": ROLE,
        "region": REGION,
        "instance": HOSTNAME,
        "uptimeSeconds": int((datetime.now(timezone.utc) - STARTED).total_seconds()),
        "dbHost": os.environ.get("DB_HOST", "not-configured"),
        # Deliberately NOT a connection string: the app resolves the password
        # from Key Vault with its managed identity.
        "dbSecretUri": os.environ.get("DB_PASSWORD_URI", ""),
        "at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health")
def health() -> Response:
    # Body must contain the role so the drill harness can tell regions apart.
    return jsonify(payload())


@app.get("/")
def index() -> Response:
    p = payload()
    colour = "#3fb950" if p["role"] == "primary" else "#d29922"
    return Response(f"""<!doctype html><html><head><title>DR demo — {p['role']}</title>
<style>body{{font-family:system-ui;background:#0b1020;color:#e6edf3;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}
.c{{text-align:center}}h1{{color:{colour}}}code{{background:#161b22;padding:2px 6px;
border-radius:4px}}</style></head><body><div class="c">
<h1>{p['role'].upper()} — {p['region']}</h1>
<p>instance <code>{p['instance']}</code></p>
<p>uptime {p['uptimeSeconds']}s &nbsp;|&nbsp; <a style="color:#58a6ff"
href="/health">/health</a></p>
</div></body></html>""", mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
