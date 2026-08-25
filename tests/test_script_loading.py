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
