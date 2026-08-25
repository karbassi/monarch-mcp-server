"""Loading a standalone script must not leave sys.path mutated.

login_setup.py does an unconditional sys.path.insert(0, .../src) at module
scope. A loader that does not restore sys.path grows it once per load, and a
polluted sys.path is how a suite quietly becomes order-dependent.
"""

import sys

import pytest

from tests.conftest import LOGIN_SETUP, load_script


def test_load_script_restores_sys_path():
    before = list(sys.path)

    with load_script(LOGIN_SETUP) as module:
        assert module._CANONICAL_LIMIT > 0  # actually used the module
        assert list(sys.path) != before, "expected the script to mutate sys.path"

    assert list(sys.path) == before


def test_repeated_loads_do_not_accumulate():
    before = list(sys.path)
    for _ in range(3):
        with load_script(LOGIN_SETUP):
            pass
    assert len(sys.path) == len(before)
    assert list(sys.path) == before


class TestOwnerOnlyModeGate:
    """assert_owner_only_mode must check mode bits on POSIX and skip on Windows.

    The gate exists because Windows does not enforce chmod bits -- DPAPI covers
    that platform instead -- so a hard assertion would fail on exactly the
    platform the encryption was added for.
    """

    def test_flags_a_wrong_mode_on_posix(self, tmp_path, monkeypatch):
        import tests.test_secure_session as mod

        monkeypatch.setattr(mod.sys, "platform", "darwin")
        f = tmp_path / "f"
        f.write_text("x")
        f.chmod(0o644)

        with pytest.raises(AssertionError, match="0o644"):
            mod.assert_owner_only_mode(f, 0o600)

    def test_accepts_the_expected_mode_on_posix(self, tmp_path, monkeypatch):
        import tests.test_secure_session as mod

        monkeypatch.setattr(mod.sys, "platform", "darwin")
        f = tmp_path / "f"
        f.write_text("x")
        f.chmod(0o600)

        mod.assert_owner_only_mode(f, 0o600)  # must not raise

    def test_skips_the_check_on_windows(self, tmp_path, monkeypatch):
        import tests.test_secure_session as mod

        monkeypatch.setattr(mod.sys, "platform", "win32")
        f = tmp_path / "f"
        f.write_text("x")
        f.chmod(0o644)  # wrong for POSIX, irrelevant on Windows

        mod.assert_owner_only_mode(f, 0o600)  # must not raise


class TestTokenOnDiskAssertion:
    """assert_token_on_disk must compare through encryption when it is in play.

    Tests that read the token file directly would otherwise hard-assert
    plaintext and fail on Windows+pywin32, where _save_token_file encrypts
    before writing -- correct behaviour reported as a failure.
    """

    def test_compares_plaintext_when_not_encrypted(self, tmp_path):
        import tests.test_secure_session as mod

        f = tmp_path / "token"
        f.write_text("a-token", encoding="utf-8")

        mod.assert_token_on_disk(f, "a-token")  # must not raise
        with pytest.raises(AssertionError):
            mod.assert_token_on_disk(f, "something-else")

    def test_compares_through_decryption_when_encrypted(self, tmp_path, monkeypatch):
        import monarch_mcp_server.secure_session as ss
        import tests.test_secure_session as mod

        monkeypatch.setattr(
            ss, "_dpapi_decrypt", lambda s: s[len(ss._DPAPI_PREFIX) :][::-1]
        )
        f = tmp_path / "token"
        f.write_text(ss._DPAPI_PREFIX + "a-token"[::-1], encoding="utf-8")

        # The plaintext is not on disk...
        assert "a-token" not in f.read_text(encoding="utf-8")
        # ...but the assertion still recognises it.
        mod.assert_token_on_disk(f, "a-token")
        with pytest.raises(AssertionError):
            mod.assert_token_on_disk(f, "something-else")


def test_load_script_raises_a_clear_error_for_an_unloadable_path(tmp_path):
    """spec_from_file_location returns None for a path it cannot handle (an
    unrecognised extension, for one). Reaching through that gives an opaque
    AttributeError on `spec.loader`; the helper should say what went wrong."""
    from tests.conftest import load_script

    bad = tmp_path / "not_python.txt"
    bad.write_text("x = 1", encoding="utf-8")

    with pytest.raises(ImportError, match="not_python.txt"):
        with load_script(bad):
            pass


def test_load_script_restores_sys_path_even_when_loading_fails(tmp_path):
    bad = tmp_path / "not_python.txt"
    bad.write_text("x = 1", encoding="utf-8")

    before = list(sys.path)
    with pytest.raises(ImportError):
        with load_script(bad):
            pass
    assert list(sys.path) == before
