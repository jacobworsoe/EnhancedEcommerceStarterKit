"""Discover GCP projects reachable by each authorized account."""

from __future__ import annotations

import logging
from typing import Dict, List

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import Account, ProjectInfo

logger = logging.getLogger(__name__)


def list_projects_for_account(account: Account) -> List[ProjectInfo]:
    """Return every active project this account can see, via Resource
    Manager v3 `projects.search` (this returns projects the caller has at
    least some IAM role on - not just ones they own)."""
    service = build("cloudresourcemanager", "v3", credentials=account.credentials, cache_discovery=False)
    billing = build("cloudbilling", "v1", credentials=account.credentials, cache_discovery=False)

    projects: List[ProjectInfo] = []
    request = service.projects().search(query="state:ACTIVE")
    while request is not None:
        try:
            response = request.execute()
        except HttpError as exc:
            logger.warning("Project search failed for %s: %s", account.email, exc)
            break

        for proj in response.get("projects", []):
            info = ProjectInfo(
                project_id=proj["projectId"],
                project_number=proj.get("name", "").split("/")[-1],
                display_name=proj.get("displayName", proj["projectId"]),
                account_email=account.email,
            )
            _attach_billing_info(billing, info)
            projects.append(info)

        request = service.projects().search_next(previous_request=request, previous_response=response)

    return projects


def _attach_billing_info(billing_service, info: ProjectInfo) -> None:
    try:
        billing_info = billing_service.projects().getBillingInfo(name=f"projects/{info.project_id}").execute()
        info.billing_account_name = billing_info.get("billingAccountName")
        info.billing_enabled = billing_info.get("billingEnabled", False)
    except HttpError as exc:
        logger.debug("No billing info for %s (%s)", info.project_id, exc)


def discover_projects_by_account(accounts: List[Account]) -> Dict[str, List[ProjectInfo]]:
    """List projects per account, without collapsing overlap. Needed
    because two accounts can see the same project but have different IAM
    roles on it (e.g. one has BigQuery access, the other has Billing Account
    Viewer) - later steps need to know about every account that can see a
    project, not just the first one discovery happened to hit."""
    return {account.email: list_projects_for_account(account) for account in accounts}


def dedupe_projects(projects_by_account: Dict[str, List[ProjectInfo]]) -> List[ProjectInfo]:
    """Collapse per-account project lists into one entry per project_id, for
    use as the scan work-list. Prefers the account that has billing enabled
    as the "primary" one to run the main per-project scan under (a good
    proxy for "more likely to also have cost visibility"), and records every
    other account that can also see the project in `also_visible_via` so
    later steps (e.g. the billing-export registry) can still use them."""
    by_project: Dict[str, List[ProjectInfo]] = {}
    for account_email, projects in projects_by_account.items():
        for proj in projects:
            by_project.setdefault(proj.project_id, []).append(proj)

    deduped: List[ProjectInfo] = []
    for project_id, entries in by_project.items():
        billing_first = sorted(entries, key=lambda p: not p.billing_enabled)
        primary = billing_first[0]
        primary.also_visible_via = [e.account_email for e in entries if e.account_email != primary.account_email]
        deduped.append(primary)
    return deduped


def discover_all_projects(accounts: List[Account]) -> List[ProjectInfo]:
    """Convenience wrapper: discover per account, then dedupe to a single
    scan work-list. See `discover_projects_by_account` / `dedupe_projects`
    if you need the un-collapsed per-account view (the CLI does, to build
    the cross-account billing-export registry)."""
    return dedupe_projects(discover_projects_by_account(accounts))
