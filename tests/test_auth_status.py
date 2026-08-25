"""check_auth_status / debug_session_loading must recognise every auth mode.

A cookie-mode session is stored without a `token` key -- load_session accepts
`token` OR `cookies` -- so anything that decides "am I authenticated?" by
asking load_token() reports a working cookie login as unauthenticated.
"""

import asyncio

import pytest

from monarch_mcp_server.tools import auth as auth_tools


@pytest.fixture
def stored_session(monkeypatch):
    """Make secure_session report a caller-supplied stored session."""

    def _install(session):
        monkeypatch.setattr(
            auth_tools.secure_session, "load_session", lambda: session
        )
        monkeypatch.setattr(
            auth_tools.secure_session,
            "load_token",
            lambda: (session or {}).get("token"),
        )

    return _install


COOKIE_SESSION = {
    "cookies": {"session_id": "s", "csrftoken": "c"},
    "auth_mode": "cookie",
}
TOKEN_SESSION = {"token": "tok", "device_uuid": "dev", "auth_mode": "token"}


def test_check_auth_status_recognises_a_cookie_session(stored_session):
    stored_session(COOKIE_SESSION)
    out = asyncio.run(auth_tools.check_auth_status())
    assert "❌" not in out, f"cookie session reported as unauthenticated:\n{out}"
    assert "cookie" in out.lower()


def test_check_auth_status_still_recognises_a_token_session(stored_session):
    stored_session(TOKEN_SESSION)
    out = asyncio.run(auth_tools.check_auth_status())
    assert "❌" not in out, f"token session reported as unauthenticated:\n{out}"


def test_check_auth_status_reports_no_session(stored_session):
    stored_session(None)
    out = asyncio.run(auth_tools.check_auth_status())
    assert "❌" in out


def test_debug_session_loading_recognises_a_cookie_session(stored_session):
    stored_session(COOKIE_SESSION)
    out = asyncio.run(auth_tools.debug_session_loading())
    assert "❌" not in out, f"cookie session reported as unauthenticated:\n{out}"


def test_debug_session_loading_reports_no_session(stored_session):
    stored_session(None)
    out = asyncio.run(auth_tools.debug_session_loading())
    assert "❌" in out
