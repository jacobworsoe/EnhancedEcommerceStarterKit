"""
Optional: use the real Cloud Billing "standard usage cost" export to
BigQuery, when one is accessible to ANY authorized account, instead of
estimating from list prices.

This only works if someone already enabled billing export to BigQuery
(https://cloud.google.com/billing/docs/how-to/export-data-bigquery). The
export dataset is very often *not* in the same project as the resources it
bills for (many orgs centralize it in one "billing" project), and different
accounts commonly have different visibility: one board member's account
might have Billing Account Viewer on the shared billing project, while
another's only has editor rights on the client's actual GA4/Cloud Run
project. So detection is done once, globally, across every project every
authorized account can see - not just the project currently being priced -
and the resulting table is then usable for pricing any project that shares
its billing account, regardless of which account happens to be doing that
project's resource scan.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, NamedTuple, Optional

from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery

from .models import Account, ProjectInfo

logger = logging.getLogger(__name__)

_EXPORT_TABLE_RE = re.compile(r"^gcp_billing_export_(v1|resource_v1)_([A-Za-z0-9_]+)$")


class BillingExportSource(NamedTuple):
    table: str  # "project.dataset.table"
    account: Account  # credentials known to be able to read this table


def billing_account_id_from_name(billing_account_name: Optional[str]) -> Optional[str]:
    """'billingAccounts/017A1B-2C3D4E-5F6789' -> '017A1B_2C3D4E_5F6789'
    (export table names substitute underscores for the hyphens)."""
    if not billing_account_name:
        return None
    raw_id = billing_account_name.split("/")[-1]
    return raw_id.replace("-", "_")


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


def build_billing_export_registry(
    accounts: List[Account],
    projects_by_account: Dict[str, List[ProjectInfo]],
) -> Dict[str, BillingExportSource]:
    """Scan every project visible to every account for a billing export
    table, so that if *any* board's account has that visibility, all
    projects sharing that billing account benefit from it - not just
    projects visible to the specific account that happens to hold the
    export-table permission.

    This is a metadata-only scan (list_datasets/list_tables, no bytes
    scanned) but it does mean one such pass per project per account that can
    see it, so it can take a while for accounts with very many projects.
    """
    accounts_by_email = {a.email: a for a in accounts}
    registry: Dict[str, BillingExportSource] = {}

    for account_email, projects in projects_by_account.items():
        account = accounts_by_email[account_email]
        for project in projects:
            try:
                bq_client = bigquery.Client(project=project.project_id, credentials=account.credentials)
            except Exception as exc:  # noqa: BLE001 - keep scanning other projects
                logger.debug("Could not open BigQuery client for %s as %s: %s", project.project_id, account_email, exc)
                continue

            for billing_account_id, table_name in find_billing_export_tables(bq_client, project.project_id).items():
                if billing_account_id not in registry:
                    registry[billing_account_id] = BillingExportSource(table=table_name, account=account)
                    logger.info(
                        "Found billing export for billing account %s at %s (via %s)",
                        billing_account_id, table_name, account_email,
                    )

    return registry


def query_service_cost_usd(
    source: BillingExportSource,
    target_project_id: str,
    service_description: str,
    days: int,
    sku_keyword: Optional[str] = None,
) -> Optional[float]:
    """Query a real billing export table for the monthly cost of one GCP
    service (e.g. 'BigQuery', 'Cloud Run', 'App Engine') incurred by
    `target_project_id` over the trailing `days`, normalized to a 30-day
    month. Returns USD, or None if the query fails or returns no rows.

    Note: this runs a real (small) BigQuery query against the export table
    under `source.account`'s credentials - keep `days` reasonable.
    """
    bq_client = bigquery.Client(project=target_project_id, credentials=source.account.credentials)

    sku_filter = "AND LOWER(sku.description) LIKE @sku_keyword" if sku_keyword else ""
    query = f"""
        SELECT SUM(cost) AS total_cost_usd
        FROM `{source.table}`
        WHERE project.id = @project_id
          AND service.description = @service_description
          {sku_filter}
          AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
    """
    query_parameters = [
        bigquery.ScalarQueryParameter("project_id", "STRING", target_project_id),
        bigquery.ScalarQueryParameter("service_description", "STRING", service_description),
        bigquery.ScalarQueryParameter("days", "INT64", days),
    ]
    if sku_keyword:
        query_parameters.append(bigquery.ScalarQueryParameter("sku_keyword", "STRING", f"%{sku_keyword.lower()}%"))

    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        result = list(bq_client.query(query, job_config=job_config).result())
    except GoogleAPIError as exc:
        logger.warning("Billing export query failed against %s: %s", source.table, exc)
        return None

    if not result or result[0]["total_cost_usd"] is None:
        return None

    total_usd = float(result[0]["total_cost_usd"])
    return total_usd * (30.0 / days)
