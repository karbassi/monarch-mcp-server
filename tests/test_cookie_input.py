"""Reading the browser cookie header must not depend on the tty line discipline.

getpass()/input() read in canonical mode, where the terminal buffer is capped at
MAX_CANON (1024 bytes on macOS). A raw `cookie:` header from app.monarch.com
routinely exceeds that once Cloudflare and analytics cookies are included, so
the paste is silently truncated and login fails with no useful explanation.
"""

import pytest


@pytest.fixture
def no_prompt(monkeypatch, login_setup):
    """Fail loudly if the code falls back to the tty when it shouldn't."""

    def _boom(*_a, **_k):
        raise AssertionError("fell back to a tty prompt")

    monkeypatch.setattr(login_setup.getpass, "getpass", _boom)


def test_reads_cookie_from_environment(monkeypatch, login_setup, no_prompt):
    monkeypatch.setenv("MONARCH_COOKIE", "  session=abc; csrftoken=def  ")
    assert login_setup._read_cookie_string() == "session=abc; csrftoken=def"


def test_reads_cookie_from_a_file(monkeypatch, login_setup, no_prompt, tmp_path):
    f = tmp_path / "cookie.txt"
    f.write_text("session=fromfile; csrftoken=x\n", encoding="utf-8")
    monkeypatch.delenv("MONARCH_COOKIE", raising=False)
    monkeypatch.setenv("MONARCH_COOKIE_FILE", str(f))
    assert login_setup._read_cookie_string() == "session=fromfile; csrftoken=x"


def test_env_wins_over_file(monkeypatch, login_setup, no_prompt, tmp_path):
    f = tmp_path / "cookie.txt"
    f.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("MONARCH_COOKIE", "from-env")
    monkeypatch.setenv("MONARCH_COOKIE_FILE", str(f))
    assert login_setup._read_cookie_string() == "from-env"


def test_falls_back_to_the_prompt(monkeypatch, login_setup):
    monkeypatch.delenv("MONARCH_COOKIE", raising=False)
    monkeypatch.delenv("MONARCH_COOKIE_FILE", raising=False)
    monkeypatch.setattr(login_setup.getpass, "getpass", lambda *_a: "typed=value")
    assert login_setup._read_cookie_string() == "typed=value"


def test_flags_a_value_truncated_at_the_canonical_limit(monkeypatch, login_setup):
    """A paste that lands exactly on the terminal's limit was almost certainly
    cut off. Say so, instead of letting login fail for no visible reason."""
    monkeypatch.delenv("MONARCH_COOKIE", raising=False)
    monkeypatch.delenv("MONARCH_COOKIE_FILE", raising=False)
    truncated = "x" * login_setup._CANONICAL_LIMIT
    monkeypatch.setattr(login_setup.getpass, "getpass", lambda *_a: truncated)

    with pytest.raises(login_setup.CookieInputTruncated) as excinfo:
        login_setup._read_cookie_string()

    assert "MONARCH_COOKIE_FILE" in str(excinfo.value)


def test_non_utf8_cookie_file_is_reported_not_raised(
    monkeypatch, login_setup, tmp_path
):
    """A cookie file that is not valid UTF-8 must produce a user-facing error.

    _read_cookie_string reads with encoding="utf-8", which raises
    UnicodeDecodeError -- a ValueError, not an OSError -- so catching only
    OSError lets it escape as a traceback out of a user-facing script.
    """
    import asyncio

    f = tmp_path / "cookie.bin"
    f.write_bytes(b"session=\xff\xfe not utf-8")
    monkeypatch.delenv("MONARCH_COOKIE", raising=False)
    monkeypatch.setenv("MONARCH_COOKIE_FILE", str(f))

    # Must not raise; the script reports and returns None.
    assert asyncio.run(login_setup._login_with_cookies()) is None
