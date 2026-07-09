"""
The actual scan pipeline: discover projects -> build the billing-export
registry -> scan each project for GA4 exports and GTM hosting candidates ->
dedupe. Shared by `cli.py` and the Flask web app so there's exactly one
place this logic lives.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from google.cloud import bigquery

from . import pricing
from .billing_export import billing_account_id_from_name, build_billing_export_registry
from .dedupe import dedupe_ga4_findings, dedupe_gtm_findings
from .discovery import dedupe_projects, discover_projects_by_account
from .ga4_bigquery import analyze_project_ga4_exports
from .gtm_hosting import discover_app_engine_services, discover_cloud_run_services
from .models import Account, GA4ExportFinding, GTMHostingFinding

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[str], None]]


def _report(progress: ProgressCallback, message: str) -> None:
    logger.info(message)
    if progress:
        progress(message)


def run_scan(
    accounts: List[Account],
    fx_rate: float = pricing.DEFAULT_USD_TO_EUR,
    billing_export_days: int = 30,
    progress: ProgressCallback = None,
) -> Tuple[List[GA4ExportFinding], List[GTMHostingFinding]]:
    """Run the full scan across every project every given account can see.
    Returns (ga4_findings, gtm_findings), already deduplicated."""
    if not accounts:
        _report(progress, "No connected accounts - nothing to scan.")
        return [], []

    _report(progress, f"Discovering projects for {len(accounts)} account(s)...")
    projects_by_account = discover_projects_by_account(accounts)
    projects = dedupe_projects(projects_by_account)
    overlapping = [p for p in projects if p.also_visible_via]
    _report(progress, f"Found {len(projects)} distinct project(s) across all accounts.")
    if overlapping:
        _report(progress, f"{len(overlapping)} project(s) are visible through more than one account.")

    _report(progress, "Scanning for billing export tables across every project every account can see...")
    billing_registry = build_billing_export_registry(accounts, projects_by_account)
    _report(
        progress,
        f"{len(billing_registry)} billing account(s) have a reachable export table "
        "(used in preference to estimation).",
    )

    account_by_email = {a.email: a for a in accounts}
    ga4_findings: List[GA4ExportFinding] = []
    gtm_findings: List[GTMHostingFinding] = []

    for project in projects:
        account = account_by_email[project.account_email]
        billing_account_id = billing_account_id_from_name(project.billing_account_name)
        billing_source = billing_registry.get(billing_account_id) if billing_account_id else None

        _report(
            progress,
            f"Scanning project {project.project_id} (account {project.account_email})"
            + (" [real billing data available]" if billing_source else ""),
        )

        try:
            bq_client = bigquery.Client(project=project.project_id, credentials=account.credentials)
            ga4_findings.extend(
                analyze_project_ga4_exports(
                    bq_client,
                    project.project_id,
                    project.account_email,
                    fx_rate,
                    billing_export_days,
                    billing_source,
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep scanning other projects on failure
            _report(progress, f"GA4/BigQuery scan failed for {project.project_id}: {exc}")

        try:
            gtm_findings.extend(
                discover_cloud_run_services(
                    project.project_id,
                    account.credentials,
                    project.account_email,
                    fx_rate,
                    billing_source,
                    billing_export_days,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _report(progress, f"Cloud Run scan failed for {project.project_id}: {exc}")

        try:
            gtm_findings.extend(
                discover_app_engine_services(
                    project.project_id,
                    account.credentials,
                    project.account_email,
                    fx_rate,
                    billing_source,
                    billing_export_days,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _report(progress, f"App Engine scan failed for {project.project_id}: {exc}")

    ga4_findings = dedupe_ga4_findings(ga4_findings)
    gtm_findings = dedupe_gtm_findings(gtm_findings)
    _report(progress, f"Done: {len(ga4_findings)} GA4 dataset(s), {len(gtm_findings)} hosting candidate(s).")

    return ga4_findings, gtm_findings
