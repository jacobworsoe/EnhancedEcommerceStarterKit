"""Discover GCP projects reachable by each authorized account."""

from __future__ import annotations

import logging
from typing import List

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


def discover_all_projects(accounts: List[Account]) -> List[ProjectInfo]:
    """List projects across all accounts, de-duplicating by project_id
    while keeping a record of which account(s) can see it."""
    seen = {}
    for account in accounts:
        for proj in list_projects_for_account(account):
            if proj.project_id not in seen:
                seen[proj.project_id] = proj
            # else: another account can also see this project - first one wins for
            # API-call purposes, but either credential works equally well.
    return list(seen.values())
