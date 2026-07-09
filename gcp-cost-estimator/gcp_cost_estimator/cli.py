"""Entrypoint: python -m gcp_cost_estimator.cli --config config.yaml"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml
from google.cloud import bigquery

from . import pricing
from .auth import load_accounts
from .billing_export import billing_account_id_from_name, build_billing_export_registry
from .dedupe import dedupe_ga4_findings, dedupe_gtm_findings
from .discovery import dedupe_projects, discover_projects_by_account
from .ga4_bigquery import analyze_project_ga4_exports
from .gtm_hosting import discover_app_engine_services, discover_cloud_run_services
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

    logger.info("Discovering projects per account...")
    projects_by_account = discover_projects_by_account(accounts)
    projects = dedupe_projects(projects_by_account)
    logger.info("Found %d distinct project(s) across all accounts.", len(projects))
    overlapping = [p for p in projects if p.also_visible_via]
    if overlapping:
        logger.info(
            "%d project(s) are visible through more than one account (e.g. %s via %s + %s).",
            len(overlapping), overlapping[0].project_id, overlapping[0].account_email,
            ", ".join(overlapping[0].also_visible_via),
        )

    logger.info("Scanning for billing export tables across every project every account can see...")
    billing_registry = build_billing_export_registry(accounts, projects_by_account)
    logger.info(
        "%d billing account(s) have a reachable export table (used in preference to estimation).",
        len(billing_registry),
    )

    account_by_email = {a.email: a for a in accounts}

    ga4_findings = []
    gtm_findings = []

    for project in projects:
        account = account_by_email[project.account_email]
        billing_account_id = billing_account_id_from_name(project.billing_account_name)
        billing_source = billing_registry.get(billing_account_id) if billing_account_id else None

        logger.info(
            "Scanning project %s (account %s)%s...",
            project.project_id, project.account_email,
            " [real billing data available]" if billing_source else "",
        )

        try:
            bq_client = bigquery.Client(project=project.project_id, credentials=account.credentials)
            ga4_findings.extend(
                analyze_project_ga4_exports(
                    bq_client,
                    project.project_id,
                    project.account_email,
                    fx_rate,
                    args.billing_export_days,
                    billing_source,
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep scanning other projects on failure
            logger.warning("GA4/BigQuery scan failed for %s: %s", project.project_id, exc)

        try:
            gtm_findings.extend(
                discover_cloud_run_services(
                    project.project_id,
                    account.credentials,
                    project.account_email,
                    fx_rate,
                    billing_source,
                    args.billing_export_days,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cloud Run scan failed for %s: %s", project.project_id, exc)

        try:
            gtm_findings.extend(
                discover_app_engine_services(
                    project.project_id,
                    account.credentials,
                    project.account_email,
                    fx_rate,
                    billing_source,
                    args.billing_export_days,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("App Engine scan failed for %s: %s", project.project_id, exc)

    ga4_findings = dedupe_ga4_findings(ga4_findings)
    gtm_findings = dedupe_gtm_findings(gtm_findings)

    print_console_report(ga4_findings, gtm_findings)
    write_json(ga4_findings, gtm_findings, args.output_dir / "report.json")
    write_csv(ga4_findings, gtm_findings, args.output_dir)
    logger.info("Reports written to %s", args.output_dir)


if __name__ == "__main__":
    main()
