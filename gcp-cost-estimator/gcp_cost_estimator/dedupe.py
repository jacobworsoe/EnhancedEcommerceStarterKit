"""
Final pass over the collected findings: collapse anything describing the
same underlying resource (same project + dataset, or same project +
service) down to one row, preferring whichever duplicate's cost came from a
real Billing Account export over one that's only a list-price estimate.

Duplicates show up because different boards' accounts can overlap on which
projects they can see, and can each independently produce a finding for the
same GA4 dataset or Cloud Run/App Engine service (e.g. if a project's scan
somehow runs under more than one account, or a merge of separate runs).
Rather than assume discovery-level dedup always prevents this, this module
is the single place that guarantees the final report has one row per
resource.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .models import GA4ExportFinding, GTMHostingFinding

# Lower index = more trustworthy; anything not listed sorts last.
_GA4_COST_SOURCE_RANK = {"billing_export": 0, "estimated": 1}
_GTM_COST_CONFIDENCE_RANK = {
    "billing_export": 0,
    "billing_export-prorated": 1,
    "fixed-config": 2,
    "estimated-from-usage": 3,
    "estimated": 4,
}


def _merge_visibility(winner_accounts: List[str], loser: object) -> List[str]:
    merged = list(winner_accounts)
    for email in [getattr(loser, "account_email", None), *getattr(loser, "also_visible_via", [])]:
        if email and email not in merged:
            merged.append(email)
    return merged


def dedupe_ga4_findings(findings: List[GA4ExportFinding]) -> List[GA4ExportFinding]:
    groups: Dict[Tuple[str, str], List[GA4ExportFinding]] = {}
    order: List[Tuple[str, str]] = []
    for f in findings:
        key = (f.project_id, f.dataset_id)
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(f)

    deduped = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            deduped.append(group[0])
            continue

        winner = min(group, key=lambda f: _GA4_COST_SOURCE_RANK.get(f.cost_source, 99))
        accounts = [winner.account_email]
        for other in group:
            if other is winner:
                continue
            accounts = _merge_visibility(accounts, other)
        winner.also_visible_via = [a for a in accounts if a != winner.account_email]
        winner.notes.append(
            f"Deduplicated {len(group)} scans of this dataset across accounts "
            f"{', '.join(accounts)}; kept the {winner.cost_source} cost."
        )
        deduped.append(winner)
    return deduped


def dedupe_gtm_findings(findings: List[GTMHostingFinding]) -> List[GTMHostingFinding]:
    groups: Dict[Tuple[str, str, str], List[GTMHostingFinding]] = {}
    order: List[Tuple[str, str, str]] = []
    for f in findings:
        key = (f.project_id, f.service_type, f.service_name)
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(f)

    deduped = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            deduped.append(group[0])
            continue

        winner = min(group, key=lambda f: _GTM_COST_CONFIDENCE_RANK.get(f.cost_confidence, 99))
        accounts = [winner.account_email]
        for other in group:
            if other is winner:
                continue
            accounts = _merge_visibility(accounts, other)
        winner.also_visible_via = [a for a in accounts if a != winner.account_email]
        winner.notes.append(
            f"Deduplicated {len(group)} scans of this service across accounts "
            f"{', '.join(accounts)}; kept the {winner.cost_confidence} cost."
        )
        deduped.append(winner)
    return deduped
