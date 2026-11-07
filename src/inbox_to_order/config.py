"""Runtime configuration, read from environment variables (optionally via a local .env file).

Nothing in here is secret by itself; secrets (API key, OAuth files) are referenced by path or
read by their own SDKs and are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = PROJECT_ROOT / "fixtures"

# Resource limits. Deliberately small: this is a proof of concept, not a mail client.
MAX_THREADS_LISTED = 25
MAX_MESSAGES_PER_THREAD = 10
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_TEXT_CHARS = 40_000
MAX_BODY_CHARS = 20_000
MAX_MODEL_TOOL_ROUNDS = 8
MAX_LINE_QUANTITY = 10_000

DEFAULT_MODEL = "claude-haiku-5-5"
# List prices in USD per million tokens (input, output), used only to show an estimated cost next to
# the token counts actually reported by the API. Check https://www.anthropic.com/pricing for changes.
# Haiku 5.5: this price applies to prompts of 100K tokens or fewer (ours are far smaller).
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# Models that accept the server-side fallback parameter. Claude Haiku 5.5 has no server-side fallback.
SERVER_FALLBACK_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5"})


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = MODEL_PRICES_USD_PER_MTOK.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


@dataclass(frozen=True)
class Settings:
    inbox_mode: str  # "fixture" | "gmail"
    extractor: str  # "auto" | "claude" | "rules"
    db_path: Path  # the app's own working state: proposals, revisions, processing log, audit
    erp_db_path: Path  # the fictional Demo ERP: read and written through its MCP server; the app only creates/resets it
    fixtures_dir: Path
    anthropic_model: str
    anthropic_effort: str
    gmail_credentials_file: Path
    gmail_token_file: Path
    gmail_query: str
    extra_customer_senders: str

    @property
    def anthropic_configured(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def resolved_extractor(self) -> str:
        """'auto' means: Claude when a key is configured, otherwise the rule-based baseline."""
        if self.extractor == "auto":
            return "claude" if self.anthropic_configured else "rules"
        return self.extractor


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    mode = os.environ.get("INBOX_MODE", "fixture").strip().lower()
    if mode not in {"fixture", "gmail"}:
        raise ValueError(f"INBOX_MODE must be 'fixture' or 'gmail', got {mode!r}")
    extractor = os.environ.get("EXTRACTOR", "auto").strip().lower()
    if extractor not in {"auto", "claude", "rules"}:
        raise ValueError(f"EXTRACTOR must be 'auto', 'claude' or 'rules', got {extractor!r}")
    return Settings(
        inbox_mode=mode,
        extractor=extractor,
        db_path=_path(os.environ.get("INBOX_TO_ORDER_DB", "data/inbox_to_order.db")),
        erp_db_path=_path(os.environ.get("DEMO_ERP_DB", "data/demo_erp.db")),
        fixtures_dir=_path(os.environ.get("FIXTURES_DIR", str(FIXTURES_DIR))),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", "medium"),
        gmail_credentials_file=_path(os.environ.get("GMAIL_CREDENTIALS_FILE", "secrets/credentials.json")),
        gmail_token_file=_path(os.environ.get("GMAIL_TOKEN_FILE", "secrets/token.json")),
        # Only messages you put under this Gmail label are read (see docs/GMAIL_SETUP.md).
        gmail_query=os.environ.get("GMAIL_QUERY", "label:orders-demo"),
        extra_customer_senders=os.environ.get("EXTRA_CUSTOMER_SENDERS", ""),
    )
