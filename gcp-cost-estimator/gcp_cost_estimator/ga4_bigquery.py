"""
Find GA4 -> BigQuery export datasets in a project and estimate what Google
charges for storing them.

GA4's linked BigQuery export always creates a dataset named
`analytics_<property_id>` containing one table per day named
`events_YYYYMMDD` (plus a same-day `events_intraday_YYYYMMDD` table that gets
replaced by the final `events_YYYYMMDD` table once the day closes).

Sizing is done via `tables.get` metadata only (numBytes / numLongTermBytes
etc.) - these are free metadata calls, NOT queries, so there is no need to
sample: we read every table's stored size directly rather than
extrapolating from a handful of sample days. If a dataset has an unusually
large number of tables (multi-year history), we cap detailed enumeration and
extrapolate from a recent sample to keep runtime reasonable.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery

from . import pricing
from .billing_export import BillingExportSource, query_service_cost_usd
from .models import GA4ExportFinding

logger = logging.getLogger(__name__)

_DATASET_RE = re.compile(r"^analytics_(\d+)$")
_TABLE_RE = re.compile(r"^events_(intraday_)?(\d{8})$")

# Above this many daily tables, stop enumerating every single one (still
# cheap, but keeps very old accounts fast) and extrapolate from the most
# recent sample instead.
MAX_TABLES_BEFORE_SAMPLING = 400
SAMPLE_SIZE_WHEN_CAPPED = 60


def find_ga4_datasets(bq_client: bigquery.Client, project_id: str) -> List[str]:
    try:
        datasets = bq_client.list_datasets(project=project_id)
    except GoogleAPIError as exc:
        logger.debug("Cannot list datasets in %s: %s", project_id, exc)
        return []
    return [ds.dataset_id for ds in datasets if _DATASET_RE.match(ds.dataset_id)]


def _table_bytes(table: bigquery.Table) -> dict:
    """Pull active/long-term byte counts out of a Table, preferring the
    newer explicit fields and falling back to the older numBytes/
    numLongTermBytes pair if the client library predates them."""
    props = table._properties  # noqa: SLF001 - no public accessor for these newer fields yet
    billing_model = "PHYSICAL" if props.get("numActivePhysicalBytes") is not None else "LOGICAL"

    if billing_model == "PHYSICAL":
        active = int(props.get("numActivePhysicalBytes", 0) or 0)
        longterm = int(props.get("numLongTermPhysicalBytes", 0) or 0)
    else:
        total = int(props.get("numBytes", 0) or 0)
        longterm = int(props.get("numLongTermBytes", 0) or 0)
        active = max(total - longterm, 0)

    return {"active": active, "longterm": longterm, "billing_model": billing_model}


def analyze_ga4_dataset(
    bq_client: bigquery.Client,
    project_id: str,
    dataset_id: str,
    account_email: str,
    fx_rate: float,
    billing_export_days: int = 30,
    billing_source: Optional[BillingExportSource] = None,
) -> GA4ExportFinding:
    dataset_ref = bigquery.DatasetReference(project_id, dataset_id)
    dataset = bq_client.get_dataset(dataset_ref)
    location = dataset.location or "US"
    storage_billing_model = dataset._properties.get("storageBillingModel", "LOGICAL") or "LOGICAL"  # noqa: SLF001

    match = _DATASET_RE.match(dataset_id)
    property_id = match.group(1) if match else None

    finding = GA4ExportFinding(
        project_id=project_id,
        account_email=account_email,
        dataset_id=dataset_id,
        location=location,
        property_id=property_id,
        storage_billing_model=storage_billing_model,
    )

    tables = [t for t in bq_client.list_tables(dataset_ref) if _TABLE_RE.match(t.table_id)]
    dates = sorted(m.group(2) for t in tables if (m := _TABLE_RE.match(t.table_id)))
    finding.table_count = len(tables)
    if dates:
        finding.oldest_table_date = dates[0]
        finding.newest_table_date = dates[-1]

    if not tables:
        finding.notes.append("No events_* tables found in this dataset.")
        return finding

    sampled = False
    tables_to_fetch = tables
    if len(tables) > MAX_TABLES_BEFORE_SAMPLING:
        sampled = True
        tables_to_fetch = sorted(tables, key=lambda t: t.table_id)[-SAMPLE_SIZE_WHEN_CAPPED:]
        finding.notes.append(
            f"Dataset has {len(tables)} daily tables; sized the most recent "
            f"{SAMPLE_SIZE_WHEN_CAPPED} and extrapolated the average across all of them "
            "to keep runtime reasonable."
        )

    total_active = 0
    total_longterm = 0
    for t in tables_to_fetch:
        try:
            full_table = bq_client.get_table(t.reference)
        except GoogleAPIError as exc:
            logger.debug("Could not fetch metadata for %s: %s", t.reference, exc)
            continue
        sizes = _table_bytes(full_table)
        total_active += sizes["active"]
        total_longterm += sizes["longterm"]

    if sampled and tables_to_fetch:
        scale = len(tables) / len(tables_to_fetch)
        total_active = int(total_active * scale)
        total_longterm = int(total_longterm * scale)

    finding.active_bytes = total_active
    finding.longterm_bytes = total_longterm

    # Prefer a real number from a billing export table, from whichever
    # account (board) actually has visibility into it, over estimating.
    monthly_usd = None
    if billing_source:
        monthly_usd = query_service_cost_usd(
            billing_source, project_id, "BigQuery", billing_export_days, sku_keyword="storage"
        )

    if monthly_usd is not None:
        finding.cost_source = "billing_export"
        finding.monthly_cost_usd = monthly_usd
        finding.monthly_cost_eur = pricing.usd_to_eur(monthly_usd, fx_rate)
        finding.notes.append(f"Real cost from billing export, read via account {billing_source.account.email}.")
    else:
        finding.cost_source = "estimated"
        rates = pricing.bq_storage_price(location, storage_billing_model)
        active_gib = total_active / pricing.GIB
        longterm_gib = total_longterm / pricing.GIB
        billable_active_gib = max(active_gib - pricing.BQ_FREE_ACTIVE_STORAGE_GIB, 0)
        monthly_usd = billable_active_gib * rates["active"] + longterm_gib * rates["longterm"]
        finding.monthly_cost_usd = monthly_usd
        finding.monthly_cost_eur = pricing.usd_to_eur(monthly_usd, fx_rate)
        finding.notes.append(
            "Estimated from BigQuery table storage metadata using public list prices; "
            "first 10 GiB/month active storage assumed free at the project level."
        )

    return finding


def analyze_project_ga4_exports(
    bq_client: bigquery.Client,
    project_id: str,
    account_email: str,
    fx_rate: float,
    billing_export_days: int = 30,
    billing_source: Optional[BillingExportSource] = None,
) -> List[GA4ExportFinding]:
    """`billing_source` should come from the cross-account registry built in
    `billing_export.build_billing_export_registry`, so it reflects whichever
    authorized account actually has visibility into the export table - that
    may not be this project's own account, which is exactly the case this
    is meant to cover (one board's account can see the billing export,
    another's can only see the GA4 project). Pass None to always fall back
    to the list-price estimate."""
    findings = []
    for dataset_id in find_ga4_datasets(bq_client, project_id):
        findings.append(
            analyze_ga4_dataset(
                bq_client,
                project_id,
                dataset_id,
                account_email,
                fx_rate,
                billing_export_days,
                billing_source,
            )
        )
    return findings
