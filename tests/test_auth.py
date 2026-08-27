"""Tests for elicitation-based auth tools."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from monarchmoney import RequireMFAException

from monarch_mcp_server import auth


def make_ctx(*elicit_results):
    """Build a mock Context whose elicit() returns the given results in order."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=list(elicit_results))
    return ctx


def accept(**fields):
    return SimpleNamespace(action="accept", data=SimpleNamespace(**fields))


def cancel():
    return SimpleNamespace(action="cancel", data=None)


@pytest.fixture(autouse=True)
def no_session_save():
    """Prevent real keyring writes during auth tests."""
    with patch("monarch_mcp_server.auth.secure_session") as mock:
        yield mock


class TestLoginInteractive:
    def test_happy_path_no_mfa(self, no_session_save):
        mm = AsyncMock()
        with patch("monarch_mcp_server.auth.MonarchMoney", return_value=mm):
            ctx = make_ctx(accept(email="a@b.com", password="pw"))
            result = asyncio.run(auth.login_interactive(ctx))
        assert "Logged in" in result
        mm.login.assert_awaited_once()
        no_session_save.save_authenticated_session.assert_called_once_with(mm)

    def test_mfa_required(self, no_session_save):
        mm = AsyncMock()
        mm.login.side_effect = RequireMFAException("mfa")
        with patch("monarch_mcp_server.auth.MonarchMoney", return_value=mm):
            ctx = make_ctx(
                accept(email="a@b.com", password="pw"),
                accept(mfa_code="123456"),
            )
            result = asyncio.run(auth.login_interactive(ctx))
        assert "Logged in" in result
        mm.multi_factor_authenticate.assert_awaited_once_with("a@b.com", "pw", "123456")
        no_session_save.save_authenticated_session.assert_called_once_with(mm)

    def test_user_cancels_initial_form(self, no_session_save):
        ctx = make_ctx(cancel())
        result = asyncio.run(auth.login_interactive(ctx))
        assert result == "Login cancelled."
        no_session_save.save_authenticated_session.assert_not_called()

    def test_user_cancels_mfa(self, no_session_save):
        mm = AsyncMock()
        mm.login.side_effect = RequireMFAException("mfa")
        with patch("monarch_mcp_server.auth.MonarchMoney", return_value=mm):
            ctx = make_ctx(accept(email="a@b.com", password="pw"), cancel())
            result = asyncio.run(auth.login_interactive(ctx))
        assert result == "Login cancelled."
        no_session_save.save_authenticated_session.assert_not_called()


class TestLoginWithTokenInteractive:
    def test_happy_path(self, no_session_save):
        mm = AsyncMock()
        with patch("monarch_mcp_server.auth.MonarchMoney", return_value=mm):
            ctx = make_ctx(accept(token="raw-token"))
            result = asyncio.run(auth.login_with_token_interactive(ctx))
        assert "saved" in result.lower()
        mm.get_subscription_details.assert_awaited_once()
        no_session_save.save_token.assert_called_once_with("raw-token")

    def test_strips_whitespace(self, no_session_save):
        mm = AsyncMock()
        with patch("monarch_mcp_server.auth.MonarchMoney", return_value=mm):
            ctx = make_ctx(accept(token="  token-with-spaces  "))
            asyncio.run(auth.login_with_token_interactive(ctx))
        no_session_save.save_token.assert_called_once_with("token-with-spaces")

    def test_empty_token_rejected(self, no_session_save):
        ctx = make_ctx(accept(token="   "))
        result = asyncio.run(auth.login_with_token_interactive(ctx))
        assert "Empty" in result
        no_session_save.save_token.assert_not_called()

    def test_user_cancels(self, no_session_save):
        ctx = make_ctx(cancel())
        result = asyncio.run(auth.login_with_token_interactive(ctx))
        assert result == "Login cancelled."
        no_session_save.save_token.assert_not_called()


class TestLogout:
    def test_clears_session(self, no_session_save):
        result = asyncio.run(auth.logout())
        assert "Cleared" in result
        no_session_save.delete_token.assert_called_once()


class TestDebugSessionLoading:
    """These patch load_session, not load_token: a cookie-mode session carries
    no `token` key, so load_token() cannot answer "am I authenticated?" (see
    tests/test_auth_status.py). The requirements asserted here are unchanged --
    report absence, never disclose the credential, never dump a traceback."""

    def test_no_session_message(self):
        from monarch_mcp_server.tools import auth as tools_auth

        with patch(
            "monarch_mcp_server.tools.auth.secure_session.load_session",
            return_value=None,
        ):
            result = asyncio.run(tools_auth.debug_session_loading())
        assert "No session" in result
        assert "❌" in result

    def test_session_present_does_not_leak_the_credential(self):
        from monarch_mcp_server.tools import auth as tools_auth

        secret = "a-secret-token-value"
        with patch(
            "monarch_mcp_server.tools.auth.secure_session.load_session",
            return_value={"token": secret, "auth_mode": "token"},
        ):
            result = asyncio.run(tools_auth.debug_session_loading())
        assert "✅" in result
        assert "length" not in result.lower()
        assert secret not in result
        # Nor any substring long enough to be useful, nor the length itself.
        assert secret[:8] not in result
        assert str(len(secret)) not in result

    def test_cookie_session_does_not_leak_cookie_values(self):
        from monarch_mcp_server.tools import auth as tools_auth

        with patch(
            "monarch_mcp_server.tools.auth.secure_session.load_session",
            return_value={
                "cookies": {"session_id": "cookie-secret-abc"},
                "auth_mode": "cookie",
            },
        ):
            result = asyncio.run(tools_auth.debug_session_loading())
        assert "✅" in result
        assert "cookie-secret-abc" not in result

    def test_keyring_failure_omits_traceback(self):
        from monarch_mcp_server.tools import auth as tools_auth

        with patch(
            "monarch_mcp_server.tools.auth.secure_session.load_session",
            side_effect=RuntimeError("keyring backend unavailable"),
        ):
            result = asyncio.run(tools_auth.debug_session_loading())
        assert "Keyring access failed" in result
        assert "RuntimeError" in result
        assert "keyring backend unavailable" in result
        assert "Traceback" not in result
        assert 'File "' not in result


class TestElicitNotSupported:
    """A Context without .elicit must produce a hint, not a crash.

    Asserting on the declared floor rather than a hardcoded version string: the
    tests previously pinned "1.10", which silently described an SDK the project
    no longer supports once the floor moved. The number now comes from the
    dependency constraint, so it cannot drift from what is actually required.
    """

    def _declared_mcp_floor(self):
        """The mcp floor from requirements.txt.

        Fails with the file contents rather than an AttributeError on None if the
        line format ever changes -- a test helper that dies opaquely is worse
        than one that says what it could not find.
        """
        import re
        from pathlib import Path

        requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        text = requirements.read_text(encoding="utf-8")
        match = re.search(r"^\s*mcp\[cli\]\s*>=\s*([0-9][0-9.]*)", text, re.MULTILINE)
        assert match is not None, (
            f"could not find an mcp[cli] floor in {requirements}; "
            f"declared dependencies were:\n{text}"
        )
        return match.group(1)

    def test_login_interactive_returns_upgrade_hint(self, no_session_save):
        ctx = SimpleNamespace()  # no elicit attribute
        result = asyncio.run(auth.login_interactive(ctx))
        assert self._declared_mcp_floor() in result
        assert "login_setup.py" in result
        no_session_save.save_authenticated_session.assert_not_called()

    def test_login_with_token_returns_upgrade_hint(self, no_session_save):
        ctx = SimpleNamespace()
        result = asyncio.run(auth.login_with_token_interactive(ctx))
        assert self._declared_mcp_floor() in result
        no_session_save.save_token.assert_not_called()
