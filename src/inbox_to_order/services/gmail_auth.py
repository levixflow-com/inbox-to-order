"""Local, single-user OAuth for a developer-owned Gmail test account (read-only scope only).

gmail.readonly is a *restricted* Google scope. This module is for a local experiment where each
developer uses their own OAuth client and their own test mailbox; it is not a verified public app.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from ..config import Settings

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

REAUTH_HINT = "Run `uv run inbox-to-order gmail-auth` to authorize again."


class GmailNotConfigured(RuntimeError):
    """No OAuth client file or no token yet. Fixture mode keeps working."""


class GmailAuthError(RuntimeError):
    """Token expired, revoked or has the wrong scope."""


def load_credentials(settings: Settings):  # -> google.oauth2.credentials.Credentials
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not settings.gmail_credentials_file.exists():
        raise GmailNotConfigured(
            f"Gmail is not configured: OAuth client file not found at {settings.gmail_credentials_file.name}. "
            "See docs/GMAIL_SETUP.md. Fixture mode still works (INBOX_MODE=fixture)."
        )
    if not settings.gmail_token_file.exists():
        raise GmailNotConfigured(f"Gmail is configured but not authorized yet. {REAUTH_HINT}")
    try:
        # No `scopes` argument here: passing one would override the scopes recorded in the token file,
        # which is exactly what we want to check.
        creds = Credentials.from_authorized_user_file(str(settings.gmail_token_file))
    except (ValueError, OSError) as exc:
        raise GmailAuthError(f"The stored Gmail token could not be read ({type(exc).__name__}). {REAUTH_HINT}") from exc
    granted = set(creds.scopes or [])
    if not set(GMAIL_SCOPES) <= granted:
        raise GmailAuthError(f"The stored token does not include gmail.readonly. {REAUTH_HINT}")
    if not creds.valid:
        if not creds.refresh_token:
            raise GmailAuthError(f"The Gmail token is invalid and has no refresh token. {REAUTH_HINT}")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise GmailAuthError(
                "The Gmail authorization expired or was revoked. OAuth apps in 'Testing' status get refresh "
                f"tokens that expire after 7 days for Gmail scopes. {REAUTH_HINT}"
            ) from exc
        _write_token(settings.gmail_token_file, creds.to_json())
    return creds


def run_oauth_flow(settings: Settings) -> Path:
    """Interactive browser consent on localhost. Stores the token next to the client file."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not settings.gmail_credentials_file.exists():
        raise GmailNotConfigured(
            f"OAuth client file not found at {settings.gmail_credentials_file}. "
            "Download it from Google Cloud Console (Desktop app client) - see docs/GMAIL_SETUP.md."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.gmail_credentials_file), GMAIL_SCOPES)
    creds = flow.run_local_server(host="localhost", port=0, open_browser=True)
    _write_token(settings.gmail_token_file, creds.to_json())
    return settings.gmail_token_file


def _write_token(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    with contextlib.suppress(OSError):  # best effort on Windows
        os.chmod(tmp, 0o600)
    tmp.replace(path)
