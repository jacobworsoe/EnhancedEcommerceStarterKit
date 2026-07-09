"""
Multi-account OAuth handling.

Each account is authorized once via the standard OAuth "installed app" flow
(a browser window opens, you sign in as that Google account, and consent to
the read-only scope below). The resulting refresh token is cached to disk so
subsequent runs are non-interactive for accounts you've already authorized.

Nothing here creates or touches API keys or service-account keys - it is
pure user OAuth, and every scope requested is read-only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .models import Account

logger = logging.getLogger(__name__)

# Read-only scope that covers Resource Manager, BigQuery, Cloud Run, App
# Engine, Compute (for load balancer lookups), Monitoring and Billing GETs.
# If a call 403s for an account whose org restricts this scope, widen to
# "https://www.googleapis.com/auth/cloud-platform" for that account only.
SCOPES = ["https://www.googleapis.com/auth/cloud-platform.read-only"]


def _token_path(token_dir: Path, email_hint: str) -> Path:
    safe = email_hint.replace("@", "_at_").replace("/", "_")
    return token_dir / f"{safe}.token.json"


def load_or_authorize_account(
    client_secret_file: Path,
    token_dir: Path,
    email_hint: str,
) -> Account:
    """Load a cached token for `email_hint`, or run an interactive OAuth
    flow if none exists / it can't be refreshed.

    `email_hint` is just a label you choose (e.g. the account's email) used
    to name the cache file - it does not have to match the account that
    ends up signing in, but you should keep it consistent so the right
    cache is reused.
    """
    token_dir.mkdir(parents=True, exist_ok=True)
    path = _token_path(token_dir, email_hint)

    creds = None
    if path.exists():
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - refresh can fail many ways
            logger.warning("Cached token for %s could not be refreshed (%s); re-authorizing.", email_hint, exc)
            creds = None

    if not creds or not creds.valid:
        logger.info("Opening browser for OAuth consent as: %s", email_hint)
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
        creds = flow.run_local_server(port=0)
        path.write_text(creds.to_json())

    return Account(email=email_hint, credentials=creds)


def load_accounts(
    client_secret_file: Path,
    token_dir: Path,
    email_hints: Iterable[str],
) -> List[Account]:
    accounts = []
    for hint in email_hints:
        accounts.append(load_or_authorize_account(client_secret_file, token_dir, hint))
    return accounts
