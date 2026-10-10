"""login_setup.main() must not destroy a working session before it has a new one.

main() previously called delete_token() up front, then had five separate return
paths before the new session was saved: unrecognised menu choice, login helper
returning None, an unexpected accounts payload, a failed connection test, and a
failed save. Hitting any of them left the user with no session at all.
"""

import asyncio

import pytest


@pytest.fixture
def spy_session(monkeypatch, login_setup):
    """Record destructive calls on secure_session without performing them."""
    calls = []
    monkeypatch.setattr(
        login_setup.secure_session,
        "delete_token",
        lambda: calls.append("delete_token"),
    )
    monkeypatch.setattr(
        login_setup.secure_session,
        "save_authenticated_session",
        lambda mm: calls.append("save"),
    )
    return calls


@pytest.mark.parametrize(
    "choice, cookie_result, why",
    [
        ("9", None, "unrecognised menu choice"),
        ("1", None, "login helper returned None"),
    ],
)
def test_existing_session_survives_an_abandoned_login(
    monkeypatch, login_setup, spy_session, choice, cookie_result, why
):
    monkeypatch.setattr("builtins.input", lambda *_a: choice)
    monkeypatch.setattr(
        login_setup, "_login_with_cookies", lambda: _async(cookie_result)
    )

    asyncio.run(login_setup.main())

    assert (
        "delete_token" not in spy_session
    ), f"session was cleared after {why}, leaving the user with nothing"
    assert "save" not in spy_session


def test_successful_login_still_saves(monkeypatch, login_setup, spy_session):
    class _Client:
        async def get_accounts(self):
            return {"accounts": [{"id": "a"}]}

    monkeypatch.setattr("builtins.input", lambda *_a: "1")
    monkeypatch.setattr(login_setup, "_login_with_cookies", lambda: _async(_Client()))

    asyncio.run(login_setup.main())

    assert "save" in spy_session


async def _async(value):
    return value
