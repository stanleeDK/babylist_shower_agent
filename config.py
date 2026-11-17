"""Settings from .env, plus secrets generated once and kept in instance/ (gitignored)."""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

INSTANCE = ROOT / "instance"  # database + generated secrets
INSTANCE.mkdir(exist_ok=True)
DB_PATH = INSTANCE / "phase2.db"  # phase 2 started a fresh database


def _persistent_secret(name: str, make) -> str:
    """Use the env var if set; otherwise generate once and reuse across restarts."""
    if os.environ.get(name):
        return os.environ[name]
    path = INSTANCE / name.lower()
    if not path.exists():
        path.write_text(make())
        path.chmod(0o600)
    return path.read_text().strip()


# Google OAuth client ("Web application") from the Google Cloud Console.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
OAUTH_REDIRECT_URI = f"{BASE_URL}/auth/callback"
if BASE_URL.startswith("http://localhost"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # allow http only for local dev
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")  # Google may return scopes in another order

SESSION_SECRET = _persistent_secret("SESSION_SECRET", lambda: secrets.token_urlsafe(32))
# Encrypts each user's Google tokens at rest.
TOKEN_ENCRYPTION_KEY = _persistent_secret("TOKEN_ENCRYPTION_KEY", lambda: Fernet.generate_key().decode())

# Replies come back to reply+<token>@REPLY_DOMAIN via SendGrid Inbound Parse (production).
REPLY_DOMAIN = os.environ.get("REPLY_DOMAIN", "replies.example.com").strip().lower()
INBOUND_SECRET = _persistent_secret("INBOUND_SECRET", lambda: secrets.token_urlsafe(24))

# Models, picked for a snappy demo
AGENT_MODEL = "claude-opus-5-5"  # the event agent: chat, emails, decisions
AGENT_EFFORT = os.environ.get("AGENT_EFFORT", "low")  # low | medium | high | xhigh | max
FAST_MODEL = "claude-haiku-5-5"  # simulated guests/restaurant, product matching
LIST_MODEL = "claude-sonnet-5-5"  # plans the shopping list

# Demo mode: simulated guests and restaurants reply after this many seconds, plus up to
# DEMO_REPLY_JITTER_SECONDS more so replies don't all land at once.
DEMO_REPLY_DELAY_SECONDS = float(os.environ.get("DEMO_REPLY_DELAY_SECONDS", "10"))
DEMO_REPLY_JITTER_SECONDS = float(os.environ.get("DEMO_REPLY_JITTER_SECONDS", "2"))

# The baby registry linked in invitations. A placeholder until registries are wired up:
# {slug} becomes the organizer's name, e.g. stan-lee.
REGISTRY_URL = os.environ.get("REGISTRY_URL", "https://my.babylist.com/{slug}-baby-registry")

# Chat limits (typed messages; clicking options is unlimited)
MAX_MESSAGE_CHARS = 500
MAX_MESSAGES_PER_EVENT = 40

# Kroger (shopping list). Product search only, so just the app's client credentials.
KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID", "").strip()
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET", "").strip()

# Venues and stores are San Francisco only for now. Visitors elsewhere get SF results.
DEFAULT_ZIP = "94118"
SF_CENTER = (37.7816, -122.4628)  # 94118, Inner Richmond
