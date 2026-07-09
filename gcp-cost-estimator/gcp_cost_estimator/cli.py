"""Entrypoint: python -m gcp_cost_estimator.cli --config config.yaml"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

from . import pricing
from .auth import load_accounts
from .pipeline import run_scan
from .report import print_console_report, write_csv, write_json

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gcp_cost_estimator")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to config.yaml")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Where to write CSV/JSON reports")
    parser.add_argument("--fx-rate", type=float, default=None, help="Override USD->EUR rate from config")
    parser.add_argument(
        "--billing-export-days", type=int, default=30, help="Trailing days to query real billing export data for"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.config.exists():
        raise SystemExit(f"Config file not found: {args.config}. Copy config.example.yaml to get started.")

    config = yaml.safe_load(args.config.read_text())
    client_secret_file = Path(config["client_secret_file"]).expanduser()
    token_dir = Path(config.get("token_dir", "tokens")).expanduser()
    email_hints = config["accounts"]
    fx_rate = args.fx_rate or config.get("fx_rate_usd_to_eur", pricing.DEFAULT_USD_TO_EUR)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Authorizing %d account(s)...", len(email_hints))
    accounts = load_accounts(client_secret_file, token_dir, email_hints)

    ga4_findings, gtm_findings = run_scan(accounts, fx_rate, args.billing_export_days)

    print_console_report(ga4_findings, gtm_findings)
    write_json(ga4_findings, gtm_findings, args.output_dir / "report.json")
    write_csv(ga4_findings, gtm_findings, args.output_dir)
    logger.info("Reports written to %s", args.output_dir)


if __name__ == "__main__":
    main()
