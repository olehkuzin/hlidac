"""CSRF protection (WP30): a synchronizer token kept in the signed session.

`csrf_protect` runs on every route of the app (an app-wide dependency), so a new form is protected
without anyone remembering to add it. Forms send the token in the `csrf_token` field; JavaScript
(voice upload, WP17) sends it in the `X-CSRF-Token` header.
"""

import hmac
import secrets

from fastapi import Request

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SESSION_KEY = "csrf"


class CsrfError(Exception):
    """The request changes state without this session's token."""


def csrf_token(request: Request) -> str:
    """This session's token, created on first use."""
    token = request.session.get(_SESSION_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_KEY] = token
    return token


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get(_SESSION_KEY)
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            sent = (await request.form()).get(CSRF_FIELD)
    if not isinstance(expected, str) or not isinstance(sent, str):
        raise CsrfError
    if not hmac.compare_digest(expected.encode(), sent.encode()):
        raise CsrfError
