"""
Local web app: OAuth into as many Google accounts as you like from the
browser, then run the same GA4/GTM cost scan the CLI does, with a live log
and a results table.

Run from the `gcp-cost-estimator/` directory:

    python -m webapp.app --config config.yaml

Then open http://127.0.0.1:8765/ (or whatever host/port your config sets).
The server binds to 127.0.0.1 by default - it is not exposed to your
network unless you explicitly change `host` in config.yaml.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

import yaml
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from gcp_cost_estimator import pricing
from gcp_cost_estimator.auth import (
    list_cached_emails,
    load_cached_credentials,
    remove_cached_account,
    save_credentials,
)
from gcp_cost_estimator.models import Account
from gcp_cost_estimator.pipeline import run_scan
from gcp_cost_estimator.report import write_csv, write_json
from gcp_cost_estimator.web_auth import build_authorization_url, exchange_code_for_credentials, fetch_account_email

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gcp_cost_estimator.webapp")

# Google's OAuth libraries refuse to run a flow over plain HTTP by default.
# This is only safe because the Flask dev server here is bound to
# 127.0.0.1 (see main() below) - never set this for anything reachable
# over a real network.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

SCAN_LOCK = threading.Lock()
SCAN_STATE = {"status": "idle", "log": [], "error": None}  # status: idle | running | done | error
SCAN_RESULT = {"ga4": [], "gtm": []}


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}. Copy config.example.yaml to get started.")
    config = yaml.safe_load(config_path.read_text())
    config.setdefault("token_dir", "tokens")
    config.setdefault("output_dir", "output")
    config.setdefault("fx_rate_usd_to_eur", pricing.DEFAULT_USD_TO_EUR)
    config.setdefault("billing_export_days", 30)
    config.setdefault("host", "127.0.0.1")
    config.setdefault("port", 8765)
    config.setdefault("redirect_uri", f"http://localhost:{config['port']}/oauth2callback")
    return config


def create_app(config: dict) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)  # per-process; restarting mid-OAuth-flow requires reconnecting

    client_secret_file = Path(config["client_secret_file"]).expanduser()
    token_dir = Path(config["token_dir"]).expanduser()
    output_dir = Path(config["output_dir"]).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    redirect_uri = config["redirect_uri"]

    def connected_accounts():
        accounts = []
        for email in sorted(list_cached_emails(token_dir)):
            creds = load_cached_credentials(token_dir, email)
            accounts.append({"email": email, "status": "connected" if creds else "needs_reauth"})
        return accounts

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            fx_rate=config["fx_rate_usd_to_eur"],
            billing_export_days=config["billing_export_days"],
        )

    @app.route("/api/accounts")
    def api_accounts():
        return jsonify({"accounts": connected_accounts()})

    @app.route("/oauth/connect")
    def oauth_connect():
        auth_url, state = build_authorization_url(client_secret_file, redirect_uri)
        session["oauth_state"] = state
        return redirect(auth_url)

    @app.route("/oauth2callback")
    def oauth_callback():
        state = session.get("oauth_state")
        if not state or request.args.get("state") != state:
            return "OAuth state mismatch - please try connecting the account again.", 400
        if "error" in request.args:
            return redirect(url_for("index", oauth_error=request.args["error"]))

        try:
            creds = exchange_code_for_credentials(client_secret_file, redirect_uri, state, request.url)
            email = fetch_account_email(creds)
            save_credentials(token_dir, email, creds)
        except Exception as exc:  # noqa: BLE001 - surface whatever went wrong to the user
            logger.exception("OAuth callback failed")
            return f"OAuth failed: {exc}", 400
        finally:
            session.pop("oauth_state", None)

        return redirect(url_for("index", connected=email))

    @app.route("/api/accounts/<email>/disconnect", methods=["POST"])
    def api_disconnect(email: str):
        removed = remove_cached_account(token_dir, email)
        return jsonify({"removed": removed})

    def _run_scan_in_background(fx_rate: float, billing_export_days: int) -> None:
        def progress(message: str) -> None:
            with SCAN_LOCK:
                SCAN_STATE["log"].append(message)

        try:
            accounts = [
                Account(email=e["email"], credentials=load_cached_credentials(token_dir, e["email"]))
                for e in connected_accounts()
                if e["status"] == "connected"
            ]
            ga4_findings, gtm_findings = run_scan(accounts, fx_rate, billing_export_days, progress)

            write_json(ga4_findings, gtm_findings, output_dir / "report.json")
            write_csv(ga4_findings, gtm_findings, output_dir)

            with SCAN_LOCK:
                SCAN_RESULT["ga4"] = [dataclasses.asdict(f) for f in ga4_findings]
                SCAN_RESULT["gtm"] = [dataclasses.asdict(f) for f in gtm_findings]
                SCAN_STATE["status"] = "done"
        except Exception as exc:  # noqa: BLE001 - report failure to the UI instead of dying silently
            logger.exception("Scan failed")
            with SCAN_LOCK:
                SCAN_STATE["status"] = "error"
                SCAN_STATE["error"] = str(exc)

    @app.route("/api/scan/start", methods=["POST"])
    def api_scan_start():
        with SCAN_LOCK:
            if SCAN_STATE["status"] == "running":
                return jsonify({"error": "A scan is already running."}), 409
            SCAN_STATE["status"] = "running"
            SCAN_STATE["log"] = []
            SCAN_STATE["error"] = None

        body = request.get_json(silent=True) or {}
        fx_rate = float(body.get("fx_rate") or config["fx_rate_usd_to_eur"])
        billing_export_days = int(body.get("billing_export_days") or config["billing_export_days"])

        thread = threading.Thread(target=_run_scan_in_background, args=(fx_rate, billing_export_days), daemon=True)
        thread.start()
        return jsonify({"status": "started"})

    @app.route("/api/scan/status")
    def api_scan_status():
        with SCAN_LOCK:
            return jsonify({"status": SCAN_STATE["status"], "log": SCAN_STATE["log"], "error": SCAN_STATE["error"]})

    @app.route("/api/scan/result")
    def api_scan_result():
        with SCAN_LOCK:
            if SCAN_STATE["status"] != "done":
                return jsonify({"error": "No completed scan yet."}), 409
            return jsonify(SCAN_RESULT)

    @app.route("/download/<path:filename>")
    def download(filename: str):
        return send_from_directory(output_dir, filename, as_attachment=True)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args()

    config = load_config(args.config)
    app = create_app(config)
    logger.info("Starting web app on http://%s:%s (localhost only unless you changed 'host')", config["host"], config["port"])
    app.run(host=config["host"], port=config["port"], debug=False)


if __name__ == "__main__":
    main()
