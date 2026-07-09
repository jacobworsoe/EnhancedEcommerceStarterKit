"""
Browser-redirect OAuth flow for the Flask web app - the counterpart to
`auth.py`'s terminal-based InstalledAppFlow.

Flow:
1. `build_authorization_url()` - the app redirects the user's browser here;
   Google shows the consent screen.
2. Google redirects back to our `/oauth2callback` route with `code` and
   `state` query params.
3. `exchange_code_for_credentials()` trades `code` for real credentials.
4. `fetch_account_email()` asks Google (not the user) which account those
   credentials belong to, so the token cache is always keyed by the real
   signed-in address rather than a guess.

Requires an OAuth client of type **Web application** (not Desktop) with
`redirect_uri` registered as an authorized redirect URI in Google Cloud
Console - see the README.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow

from .auth import SCOPES


def build_flow(client_secret_file: Path, redirect_uri: str) -> Flow:
    flow = Flow.from_client_secrets_file(str(client_secret_file), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    return flow


def build_authorization_url(client_secret_file: Path, redirect_uri: str) -> Tuple[str, str]:
    """Returns (authorization_url, state). Stash `state` in the user's
    session so the callback can verify it matches."""
    flow = build_flow(client_secret_file, redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",  # request a refresh token
        include_granted_scopes="true",
        prompt="consent",  # force a refresh token even on repeat consent
    )
    return auth_url, state


def exchange_code_for_credentials(
    client_secret_file: Path, redirect_uri: str, state: str, authorization_response_url: str
) -> Credentials:
    """Trade the callback's `code` for credentials. `authorization_response_url`
    is the full URL Flask received the callback on (including query string)."""
    flow = build_flow(client_secret_file, redirect_uri)
    flow.state = state
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials


def fetch_account_email(credentials: Credentials) -> str:
    """Ask Google which account these credentials belong to - never trust a
    client-supplied label for this, since the cache file (and every scan
    result) is keyed off it."""
    oauth2 = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
    userinfo = oauth2.userinfo().get().execute()
    return userinfo["email"]
