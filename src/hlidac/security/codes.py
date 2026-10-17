"""One-time login codes (D18, WP30).

Six digits by email. Only an HMAC of the code is stored, keyed with the app's secret and salted
with the code's id, so the stored value is useless without the server's key. A code expires after
10 minutes, works once, and allows a few wrong attempts; only the newest code of an account counts,
and an account gets only a few codes per window.

Timing uses the real clock (`SystemClock`), never the demo clock: advancing or freezing the demo
must not keep a code alive or lock a patient out.
"""

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta
from typing import Literal

from hlidac.schemas import LoginCode
from hlidac.store import Store

CODE_DIGITS = 6
CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS_PER_CODE = 5
MAX_CODES_PER_WINDOW = 3
CODE_WINDOW = timedelta(minutes=15)

VerifyResult = Literal["ok", "invalid", "expired", "used", "locked"]

# One process checks a code at a time, so the same code can't be spent twice by racing requests.
# The IRIS store (WP10) must keep this single-use property with a conditional update.
_verify_lock = threading.Lock()


class RateLimited(Exception):
    """The account already got MAX_CODES_PER_WINDOW codes in the current window."""


def hash_code(secret: bytes, code_id: str, code: str) -> str:
    return hmac.new(secret, f"{code_id}:{code}".encode(), hashlib.sha256).hexdigest()


def normalise(code: str) -> str:
    """Pasted codes may carry spaces or dashes; anything else stays and simply won't match."""
    return code.strip().replace(" ", "").replace("-", "")


def issue_code(store: Store, account_id: str, now: datetime, secret: bytes) -> str:
    """Store a new code for the account and return it, for the email only. Never log it."""
    recent = [c for c in store.list_login_codes(account_id) if c.created_at > now - CODE_WINDOW]
    if len(recent) >= MAX_CODES_PER_WINDOW:
        raise RateLimited
    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    code_id = secrets.token_hex(16)
    store.save_login_code(
        LoginCode(
            code_id=code_id,
            account_id=account_id,
            code_hash=hash_code(secret, code_id, code),
            created_at=now,
            expires_at=now + CODE_TTL,
        )
    )
    return code


def verify_code(
    store: Store, account_id: str, code: str, now: datetime, secret: bytes
) -> VerifyResult:
    """Check `code` against the account's newest code, and spend it on success."""
    with _verify_lock:
        codes = store.list_login_codes(account_id)
        if not codes:
            return "invalid"
        latest = codes[-1]
        if latest.used_at is not None:
            return "used"
        if now >= latest.expires_at:
            return "expired"
        if latest.attempts >= MAX_ATTEMPTS_PER_CODE:
            return "locked"
        expected = hash_code(secret, latest.code_id, normalise(code))
        if hmac.compare_digest(latest.code_hash, expected):
            store.save_login_code(latest.evolve(used_at=now))
            return "ok"
        attempts = latest.attempts + 1
        store.save_login_code(latest.evolve(attempts=attempts))
        return "locked" if attempts >= MAX_ATTEMPTS_PER_CODE else "invalid"
