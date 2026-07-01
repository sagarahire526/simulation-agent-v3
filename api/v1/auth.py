"""
HTTP Basic auth for the BKG admin surface.

Credentials come from the environment only (config.BKG_ADMIN_USER /
config.BKG_ADMIN_PASSWORD) — there is no user store, session, or DB.

Attach `Depends(require_bkg_admin)` to both the /bkg-admin page and every
/api/v1/bkg-admin/* route so the write endpoints cannot be reached by calling
the API directly (bypassing the page).

Security properties:
  • Fails closed — if BKG_ADMIN_PASSWORD is empty/unset, every request is
    rejected. A missing config never leaves the admin open.
  • Constant-time comparison (secrets.compare_digest on utf-8 bytes) so the
    check does not leak the username/password via timing.
  • Returns 401 + WWW-Authenticate so browsers show the native login prompt
    and reuse the credentials for the page's same-origin API calls.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config

# auto_error=False so this dependency always runs — letting the fail-closed 503
# below take precedence and letting us attach WWW-Authenticate on the 401 so the
# browser shows its native login prompt.
_basic = HTTPBasic(auto_error=False)

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Basic realm="BKG Admin"'}


def require_bkg_admin(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    """Validate HTTP Basic credentials against the env-configured admin login."""
    expected_user = config.BKG_ADMIN_USER
    expected_pass = config.BKG_ADMIN_PASSWORD

    # Fail closed: no password configured → deny everyone with a clear signal.
    if not expected_pass:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BKG admin auth is not configured. "
                   "Set BKG_ADMIN_USER and BKG_ADMIN_PASSWORD in the environment.",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers=_UNAUTHORIZED_HEADERS,
        )

    # Compare both fields in constant time; evaluate both before deciding so the
    # response time does not reveal whether the username alone was correct.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), expected_user.encode("utf-8")
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), expected_pass.encode("utf-8")
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers=_UNAUTHORIZED_HEADERS,
        )

    return credentials.username
