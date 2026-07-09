"""
Optional: use each project's real Cloud Billing "standard usage cost" export
to BigQuery, when one is accessible, instead of estimating from list prices.

This only works if someone already enabled billing export to BigQuery
(https://cloud.google.com/billing/docs/how-to/export-data-bigquery) and the
authorized account can read the destination dataset. Detection is a
best-effort scan: we look, across every project the account can see, for a
table named like `gcp_billing_export_v1_<BILLING_ACCOUNT_ID>`. If found and
its billing_account_id matches the project we're pricing, we query it
directly for exact cost. Otherwise callers fall back to pricing.py list-price
estimates.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

from google.cloud import bigquery
from google.api_core.exceptions import GoogleAPIError

logger = logging.getLogger(__name__)

_EXPORT_TABLE_RE = re.compile(r"^gcp_billing_export_(v1|resource_v1)_([A-Za-z0-9]+)$")


def find_billing_export_tables(bq_client: bigquery.Client, project_id: str) -> Dict[str, str]:
    """Scan one project's BigQuery datasets for billing export tables.

    Returns {billing_account_id: "project.dataset.table"}.
    """
    found: Dict[str, str] = {}
    try:
        datasets = list(bq_client.list_datasets(project=project_id))
    except GoogleAPIError as exc:
        logger.debug("Cannot list datasets in %s for billing export scan: %s", project_id, exc)
        return found

    for ds in datasets:
        try:
            tables = bq_client.list_tables(ds.reference)
        except GoogleAPIError:
            continue
        for table in tables:
            match = _EXPORT_TABLE_RE.match(table.table_id)
            if match:
                billing_account_id = match.group(2)
                fq_name = f"{project_id}.{ds.dataset_id}.{table.table_id}"
                found[billing_account_id] = fq_name
    return found


def query_bigquery_storage_cost_eur(
    bq_client: bigquery.Client,
    export_table: str,
    target_project_id: str,
    days: int,
    fx_rate: float,
) -> Optional[float]:
    """Query a real billing export table for BigQuery storage cost incurred
    by `target_project_id` over the trailing `days`. Returns EUR or None if
    the query fails (table schema mismatch, no rows, no permission, etc).

    Note: this runs a real (small) BigQuery query against the export table,
    which is billed like any other query - keep `days` reasonable.
    """
    query = f"""
        SELECT SUM(cost) AS total_cost_usd
        FROM `{export_table}`
        WHERE project.id = @project_id
          AND service.description = 'BigQuery'
          AND LOWER(sku.description) LIKE '%storage%'
          AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("project_id", "STRING", target_project_id),
            bigquery.ScalarQueryParameter("days", "INT64", days),
        ]
    )
    try:
        result = list(bq_client.query(query, job_config=job_config).result())
    except GoogleAPIError as exc:
        logger.warning("Billing export query failed against %s: %s", export_table, exc)
        return None

    if not result or result[0]["total_cost_usd"] is None:
        return None

    total_usd = float(result[0]["total_cost_usd"])
    # Normalize to a monthly figure if the window isn't ~30 days.
    monthly_usd = total_usd * (30.0 / days)
    return monthly_usd * fx_rate
