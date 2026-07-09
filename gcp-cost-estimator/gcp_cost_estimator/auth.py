"""
Multi-account OAuth handling, shared by both entrypoints:

- The CLI (`cli.py`) authorizes accounts with the "installed app" flow: a
  browser window opens, you sign in, and control returns to the terminal.
- The web app (`webapp/app.py`) authorizes accounts with a normal browser
  redirect flow (see `web_auth.py`), since there's no terminal to return
  control to.

Both funnel into the same on-disk token cache under `token_dir`, keyed by
account email, so either entrypoint can pick up an account the other one
already authorized.

Nothing here creates or touches API keys or service-account keys - it is
pure user OAuth, and every scope requested is read-only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .models import Account

logger = logging.getLogger(__name__)

# Read-only scope that covers Resource Manager, BigQuery, Cloud Run, App
# Engine, Compute (for load balancer lookups), Monitoring and Billing GETs,
# plus openid/email so the web app can ask Google which account actually
# signed in rather than trusting a user-supplied label.
# If a call 403s for an account whose org restricts this scope, widen to
# "https://www.googleapis.com/auth/cloud-platform" for that account only.
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform.read-only",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def token_path(token_dir: Path, email: str) -> Path:
    safe = email.replace("@", "_at_").replace("/", "_")
    return token_dir / f"{safe}.token.json"


def save_credentials(token_dir: Path, email: str, creds: Credentials) -> None:
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path(token_dir, email).write_text(creds.to_json())


def load_cached_credentials(token_dir: Path, email: str) -> Optional[Credentials]:
    """Load a cached credential for `email`, refreshing it if expired.
    Returns None if there's no cache entry or it can't be refreshed (the
    caller should then re-run the OAuth consent flow)."""
    path = token_path(token_dir, email)
    if not path.exists():
        return None

    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(token_dir, email, creds)
        except Exception as exc:  # noqa: BLE001 - refresh can fail many ways
            logger.warning("Cached token for %s could not be refreshed (%s).", email, exc)
            return None

    return creds if creds.valid else None


def list_cached_emails(token_dir: Path) -> List[str]:
    """Every account email with a token cached on disk, valid or not (the
    caller decides whether an expired one needs re-authorizing)."""
    if not token_dir.exists():
        return []
    emails = []
    for path in token_dir.glob("*.token.json"):
        emails.append(path.stem.removesuffix(".token").replace("_at_", "@"))
    return emails


def remove_cached_account(token_dir: Path, email: str) -> bool:
    path = token_path(token_dir, email)
    if path.exists():
        path.unlink()
        return True
    return False


def load_or_authorize_account(
    client_secret_file: Path,
    token_dir: Path,
    email_hint: str,
) -> Account:
    """Load a cached token for `email_hint`, or run an interactive
    (terminal / installed-app) OAuth flow if none exists / it can't be
    refreshed.

    `email_hint` is just a label you choose (e.g. the account's email) used
    to name the cache file - it does not have to match the account that
    ends up signing in, but you should keep it consistent so the right
    cache is reused. (The web app's OAuth flow, by contrast, asks Google
    directly which account signed in - see `web_auth.py`.)
    """
    creds = load_cached_credentials(token_dir, email_hint)

    if not creds:
        logger.info("Opening browser for OAuth consent as: %s", email_hint)
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
        creds = flow.run_local_server(port=0)
        save_credentials(token_dir, email_hint, creds)

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
