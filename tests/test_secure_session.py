"""Tests for keyring backend detection and secure session storage."""

import os
import sys
import textwrap
import types
from unittest.mock import MagicMock

import pytest

from monarch_mcp_server import secure_session as ss_module
from monarch_mcp_server.secure_session import _keyring_available

# Captured before the autouse fixture below stubs it out, so the cleanup test
# can still exercise the real implementation.
_REAL_CLEANUP = ss_module.SecureMonarchSession._cleanup_old_session_files


@pytest.fixture(autouse=True)
def isolate_real_home(tmp_path, monkeypatch):
    """Keep every test out of the developer's real home directory.

    Two ways this bites without it. _load_token_file reads the module-level
    _TOKEN_FILE, so a test asserting "no session" picks up the real token at
    ~/.monarch-mcp-server/token and fails on any machine that has logged in --
    while passing in CI, where no such file exists. And save_session_blob calls
    _cleanup_old_session_files, which os.remove()s paths under expanduser("~"),
    so merely running the suite deletes real files from the developer's home.

    Note this does NOT patch $HOME. The real macOS Keychain backend resolves the
    keychain through it, and the probe in _keyring_available() then blocks
    indefinitely in SecItemAdd waiting on a GUI unlock prompt. Redirect the two
    module constants and stub the one method that writes outside them instead.

    Tests that need their own layout still override _TOKEN_DIR/_TOKEN_FILE.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ss_module, "_TOKEN_DIR", home / ".monarch-mcp-server")
    monkeypatch.setattr(
        ss_module, "_TOKEN_FILE", home / ".monarch-mcp-server" / "token"
    )
    monkeypatch.setattr(
        ss_module.SecureMonarchSession,
        "_cleanup_old_session_files",
        lambda self: None,
    )


class _FakeKeyring:
    """Minimal stand-in for the `keyring` module used by detection tests."""

    def __init__(
        self,
        *,
        set_raises=None,
        get_returns=None,
        get_raises=None,
        delete_raises=None,
    ):
        self._set_raises = set_raises
        self._get_returns = get_returns
        self._get_raises = get_raises
        self._delete_raises = delete_raises
        self.set_calls = []
        self.get_calls = []
        self.delete_calls = []

    def set_password(self, service, username, value):
        self.set_calls.append((service, username, value))
        if self._set_raises:
            raise self._set_raises

    def get_password(self, service, username):
        self.get_calls.append((service, username))
        if self._get_raises:
            raise self._get_raises
        return self._get_returns

    def delete_password(self, service, username):
        self.delete_calls.append((service, username))
        if self._delete_raises:
            raise self._delete_raises


@pytest.fixture
def install_fake_keyring(monkeypatch):
    """Replace the importable `keyring` module with a controllable fake."""

    def _install(fake):
        module = types.ModuleType("keyring")
        module.set_password = fake.set_password
        module.get_password = fake.get_password
        module.delete_password = fake.delete_password
        monkeypatch.setitem(sys.modules, "keyring", module)
        return fake

    return _install


class TestKeyringAvailable:
    def test_returns_true_when_probe_round_trips(self, install_fake_keyring):
        """A real backend (set + get returns same value + delete) is accepted."""
        fake = install_fake_keyring(_FakeKeyring(get_returns="1"))
        assert _keyring_available() is True
        assert len(fake.set_calls) == 1
        assert len(fake.get_calls) == 1
        assert len(fake.delete_calls) == 1

    def test_macos_keychain_class_name_collision_is_handled(
        self, install_fake_keyring
    ):
        """The macOS Keychain and fail backends share the class name `Keyring`.

        Previously this caused real macOS keyrings to be rejected by name and
        tokens to be written to a plaintext file. The probe roundtrip ignores
        class names entirely and only trusts what the backend can actually do.
        """
        fake = install_fake_keyring(_FakeKeyring(get_returns="1"))
        # Simulate the macOS Keychain class name to prove name has no effect.
        fake.__class__.__name__ = "Keyring"
        assert _keyring_available() is True

    def test_returns_false_when_set_raises(self, install_fake_keyring):
        """The fail backend raises on set_password — we must NOT trust it."""
        install_fake_keyring(_FakeKeyring(set_raises=RuntimeError("no backend")))
        assert _keyring_available() is False

    def test_returns_false_when_get_returns_none(self, install_fake_keyring):
        """A backend that silently drops writes is not safe to use."""
        install_fake_keyring(_FakeKeyring(get_returns=None))
        assert _keyring_available() is False

    def test_returns_false_when_get_returns_wrong_value(self, install_fake_keyring):
        """A backend that corrupts the round-trip is not safe to use."""
        install_fake_keyring(_FakeKeyring(get_returns="not-the-probe-value"))
        assert _keyring_available() is False

    def test_returns_false_when_get_raises(self, install_fake_keyring):
        install_fake_keyring(
            _FakeKeyring(set_raises=None, get_raises=RuntimeError("read failed"))
        )
        assert _keyring_available() is False

    def test_returns_false_when_delete_raises(self, install_fake_keyring):
        """Delete failure means cleanup is broken; don't trust the backend."""
        install_fake_keyring(
            _FakeKeyring(get_returns="1", delete_raises=RuntimeError("rm failed"))
        )
        assert _keyring_available() is False

    def test_returns_false_when_keyring_not_installed(self, monkeypatch):
        """If the keyring package is absent, treat as unavailable, don't crash."""

        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict
        ) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "keyring":
                raise ImportError("no keyring installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        assert _keyring_available() is False

    def test_probe_uses_dedicated_username(self, install_fake_keyring):
        """The probe must not clobber the real token username."""
        fake = install_fake_keyring(_FakeKeyring(get_returns="1"))
        _keyring_available()
        for _service, username, _value in fake.set_calls:
            assert username != ss_module.KEYRING_USERNAME
        for _service, username in fake.get_calls:
            assert username != ss_module.KEYRING_USERNAME


class _StorageFakeKeyring:
    """In-memory keyring fake that round-trips set/get/delete.

    Lets save_session_blob/load_session roundtrip without touching the
    real Keychain or the host filesystem.
    """

    def __init__(self):
        self._store = {}

    def set_password(self, service, username, value):
        self._store[(service, username)] = value

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture
def storage_keyring(monkeypatch):
    """Install a roundtrip-capable fake keyring and return a fresh session."""
    fake = _StorageFakeKeyring()
    module = types.ModuleType("keyring")
    module.set_password = fake.set_password
    module.get_password = fake.get_password
    module.delete_password = fake.delete_password
    monkeypatch.setitem(sys.modules, "keyring", module)

    session = ss_module.SecureMonarchSession()
    # __init__ ran the probe and set _use_keyring=True via the fake.
    assert session._use_keyring is True
    return session, fake


class TestSessionStorageRoundTrip:
    """save_session_blob → load_session must round-trip every supported shape."""

    def test_token_mode_roundtrip(self, storage_keyring):
        session, _ = storage_keyring
        session.save_session_blob(
            token="tok-abc",
            device_uuid="dev-xyz",
            auth_mode="token",
        )

        loaded = session.load_session()
        assert loaded == {
            "token": "tok-abc",
            "device_uuid": "dev-xyz",
            "auth_mode": "token",
        }

    def test_cookie_mode_roundtrip_preserves_nested_dict(self, storage_keyring):
        """Cookies must come back as a nested dict, not flattened to strings."""
        session, _ = storage_keyring
        cookies = {
            "session_id": "session-value",
            "csrftoken": "csrf-value",
            "cf_clearance": "cf-value",
        }
        session.save_session_blob(
            cookies=cookies,
            device_uuid="dev-xyz",
            auth_mode="cookie",
        )

        loaded = session.load_session()
        assert loaded is not None
        assert loaded["auth_mode"] == "cookie"
        assert loaded["cookies"] == cookies
        assert loaded["device_uuid"] == "dev-xyz"

    def test_cookie_mode_with_token_fallback(self, storage_keyring):
        """When cookies and a token coexist, both must round-trip."""
        session, _ = storage_keyring
        session.save_session_blob(
            token="tok-abc",
            cookies={"session_id": "s", "csrftoken": "c"},
            device_uuid="dev",
            auth_mode="cookie",
        )

        loaded = session.load_session()
        assert loaded["auth_mode"] == "cookie"
        assert loaded["token"] == "tok-abc"
        assert loaded["cookies"] == {"session_id": "s", "csrftoken": "c"}

    def test_requires_token_or_cookies(self, storage_keyring):
        session, _ = storage_keyring
        with pytest.raises(ValueError):
            session.save_session_blob(auth_mode="token")


class TestBackwardCompatLoading:
    """Existing keyring entries must keep working after the cookie upgrade."""

    def test_legacy_bare_token_string(self, storage_keyring):
        """Very old installs stored the raw token as the keyring value."""
        session, fake = storage_keyring
        fake.set_password(
            ss_module.KEYRING_SERVICE,
            ss_module.KEYRING_USERNAME,
            "legacy-bare-token",
        )

        loaded = session.load_session()
        assert loaded == {"token": "legacy-bare-token", "auth_mode": "token"}

    def test_pre_cookie_json_blob(self, storage_keyring):
        """Pre-cookie entries had token + device_uuid but no auth_mode key."""
        session, fake = storage_keyring
        fake.set_password(
            ss_module.KEYRING_SERVICE,
            ss_module.KEYRING_USERNAME,
            '{"token": "t", "device_uuid": "d"}',
        )

        loaded = session.load_session()
        assert loaded["token"] == "t"
        assert loaded["device_uuid"] == "d"
        # Without an explicit auth_mode and no cookies, default to "token".
        assert loaded["auth_mode"] == "token"

    def test_blob_with_cookies_defaults_to_cookie_mode(self, storage_keyring):
        """If a blob has cookies but no auth_mode, infer cookie mode."""
        session, fake = storage_keyring
        fake.set_password(
            ss_module.KEYRING_SERVICE,
            ss_module.KEYRING_USERNAME,
            '{"cookies": {"session_id": "s", "csrftoken": "c"}}',
        )

        loaded = session.load_session()
        assert loaded["cookies"] == {"session_id": "s", "csrftoken": "c"}
        assert loaded["auth_mode"] == "cookie"

    def test_missing_token_and_cookies_returns_none(self, storage_keyring):
        """A blob with neither credential type is unusable."""
        session, fake = storage_keyring
        fake.set_password(
            ss_module.KEYRING_SERVICE,
            ss_module.KEYRING_USERNAME,
            '{"auth_mode": "token", "device_uuid": "d"}',
        )

        assert session.load_session() is None


class TestGetAuthenticatedClient:
    """get_authenticated_client must dispatch on the stored auth_mode."""

    def test_cookie_mode_calls_set_cookies_on_client(self, storage_keyring, monkeypatch):
        session, _ = storage_keyring
        session.save_session_blob(
            cookies={"session_id": "s", "csrftoken": "c"},
            auth_mode="cookie",
        )

        # Capture what set_cookies is invoked with.
        fake_client = MagicMock()
        monkeypatch.setattr(
            ss_module, "create_monarch_client", lambda **kwargs: fake_client
        )

        client = session.get_authenticated_client()
        assert client is fake_client
        fake_client.set_cookies.assert_called_once_with(
            {"session_id": "s", "csrftoken": "c"}
        )

    def test_token_mode_does_not_call_set_cookies(self, storage_keyring, monkeypatch):
        session, _ = storage_keyring
        session.save_session_blob(
            token="tok",
            device_uuid="dev",
            auth_mode="token",
        )

        fake_client = MagicMock()
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return fake_client

        monkeypatch.setattr(ss_module, "create_monarch_client", fake_create)

        client = session.get_authenticated_client()
        assert client is fake_client
        assert captured == {"token": "tok", "device_uuid": "dev"}
        fake_client.set_cookies.assert_not_called()

    def test_no_session_returns_none(self, storage_keyring):
        session, _ = storage_keyring
        assert session.get_authenticated_client() is None


class TestFileFallbackPermissions:
    """The plaintext file fallback must never expose the token to other users."""

    def test_token_file_created_locked_not_via_write_text(self, tmp_path, monkeypatch):
        """The token file must be created already locked to 0600, not written
        world-readable and chmod'd afterward (which leaves a race window)."""
        import stat as _stat

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", tmp_path / "store")
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", tmp_path / "store" / "token")

        # A revert to write_text()-then-chmod would trip this and fail loudly.
        def _boom(*_a, **_k):
            raise AssertionError("token file must not be created via write_text()")

        monkeypatch.setattr(ss_module.Path, "write_text", _boom)

        token_file = tmp_path / "store" / "token"

        # ss_module.os is the os module itself, so this patch is process-wide for
        # the duration of the test — record only opens of the token path, or an
        # unrelated os.open (e.g. a logging handler) would pollute the assertion.
        create_modes = []
        real_open = ss_module.os.open

        def _recording_open(path, flags, mode=0o777):
            if os.fspath(path) == os.fspath(token_file):
                create_modes.append(mode)
            return real_open(path, flags, mode)

        monkeypatch.setattr(ss_module.os, "open", _recording_open)

        session = ss_module.SecureMonarchSession()
        session._save_token_file("super-secret-token")

        assert token_file.read_text() == "super-secret-token"
        assert create_modes and all(m == 0o600 for m in create_modes)
        assert _stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_preexisting_file_locked_down_before_token_is_written(
        self, tmp_path, monkeypatch
    ):
        """O_CREAT's mode applies only when creating. A pre-existing token file
        with broader permissions must be narrowed to 0600 *before* the new token
        is written, not chmod'd afterward."""
        import stat as _stat

        store = tmp_path / "store"
        store.mkdir()
        token_file = store / "token"
        token_file.write_text("stale-token")
        token_file.chmod(0o644)

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", token_file)

        modes_at_write = []
        real_fdopen = ss_module.os.fdopen

        class _ModeRecordingFile:
            """Records the file's on-disk mode at the moment content lands."""

            def __init__(self, wrapped):
                self._wrapped = wrapped

            def write(self, data):
                modes_at_write.append(_stat.S_IMODE(token_file.stat().st_mode))
                return self._wrapped.write(data)

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *exc):
                return self._wrapped.__exit__(*exc)

        monkeypatch.setattr(
            ss_module.os,
            "fdopen",
            lambda fd, *a, **k: _ModeRecordingFile(real_fdopen(fd, *a, **k)),
        )

        session = ss_module.SecureMonarchSession()
        session._save_token_file("super-secret-token")

        assert token_file.read_text() == "super-secret-token"
        # Guard: an empty list would pass the all() below vacuously.
        assert modes_at_write, "token was never written"
        assert all(m == 0o600 for m in modes_at_write), (
            f"token written while file was {[oct(m) for m in modes_at_write]}"
        )
        assert _stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_symlink_at_token_path_is_refused(self, tmp_path, monkeypatch):
        """A symlink planted at the token path must not redirect the write. The
        open must fail rather than follow it to an attacker-chosen target."""
        store = tmp_path / "store"
        store.mkdir()
        token_file = store / "token"
        victim = tmp_path / "victim"
        victim.write_text("do-not-clobber")
        token_file.symlink_to(victim)

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", token_file)

        session = ss_module.SecureMonarchSession()
        with pytest.raises(OSError):
            session._save_token_file("super-secret-token")

        assert victim.read_text() == "do-not-clobber"

    def test_non_regular_file_at_token_path_is_refused(self, tmp_path, monkeypatch):
        """Even without a symlink, the fd must be verified to be a regular file
        before the token is written to it."""
        store = tmp_path / "store"
        store.mkdir()
        token_file = store / "token"

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", token_file)

        # Hand back an fd to a character device; nothing should be written to it.
        real_open = ss_module.os.open

        def _devnull_open(path, flags, mode=0o777):
            if os.fspath(path) == os.fspath(token_file):
                return real_open(os.devnull, os.O_WRONLY)
            return real_open(path, flags, mode)

        monkeypatch.setattr(ss_module.os, "open", _devnull_open)
        # fchmod on /dev/null raises EPERM for a non-root user. It runs *after*
        # the S_ISREG check, so it cannot preempt it today -- but if that check
        # were ever removed, fchmod would raise anyway and this test would stay
        # green while guarding nothing. Neutralise it so the regular-file check
        # is the only thing that can reject the fd.
        monkeypatch.setattr(ss_module.os, "fchmod", lambda *_a, **_k: None)

        written = []
        real_fdopen = ss_module.os.fdopen

        class _RecordingFile:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def write(self, data):
                written.append(data)
                return self._wrapped.write(data)

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *exc):
                return self._wrapped.__exit__(*exc)

        monkeypatch.setattr(
            ss_module.os,
            "fdopen",
            lambda fd, *a, **k: _RecordingFile(real_fdopen(fd, *a, **k)),
        )

        session = ss_module.SecureMonarchSession()
        with pytest.raises(OSError):
            session._save_token_file("super-secret-token")

        assert not written, "token was written to a non-regular file"

    def test_symlink_refused_without_o_nofollow(self, tmp_path, monkeypatch):
        """O_NOFOLLOW is Unix-only. On a platform without it there must still be
        a best-effort symlink check rather than silently following the link."""
        store = tmp_path / "store"
        store.mkdir()
        token_file = store / "token"
        victim = tmp_path / "victim"
        victim.write_text("do-not-clobber")
        token_file.symlink_to(victim)

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", token_file)
        monkeypatch.delattr(ss_module.os, "O_NOFOLLOW", raising=False)

        session = ss_module.SecureMonarchSession()
        with pytest.raises(OSError):
            session._save_token_file("super-secret-token")

        assert victim.read_text() == "do-not-clobber"

    def test_save_works_without_fchmod(self, tmp_path, monkeypatch):
        """os.fchmod is Unix-only; its absence must not break the fallback path.
        The file must still end up at 0600."""
        import stat as _stat

        store = tmp_path / "store"
        store.mkdir()
        token_file = store / "token"
        token_file.write_text("stale-token")
        token_file.chmod(0o644)

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", token_file)
        monkeypatch.delattr(ss_module.os, "fchmod", raising=False)

        session = ss_module.SecureMonarchSession()
        session._save_token_file("super-secret-token")

        assert token_file.read_text() == "super-secret-token"
        assert _stat.S_IMODE(token_file.stat().st_mode) == 0o600


class TestFileFallbackReadPath:
    """The read path must not be turned into an exfiltration primitive.

    _load_token_file's result is handed to callers that, when it isn't JSON,
    treat it as a bare token and send it as an Authorization header to
    api.monarch.com. So anything this returns is content we transmit to a third
    party, and it must come from a file we actually own.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        store.mkdir()
        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", store / "token")
        return store

    def test_reads_a_normal_token(self, store):
        (store / "token").write_text("  real-token\n")
        assert ss_module.SecureMonarchSession()._load_token_file() == "real-token"

    def test_returns_none_when_absent(self, store):
        assert ss_module.SecureMonarchSession()._load_token_file() is None

    def test_symlink_is_not_followed(self, store, tmp_path):
        secret = tmp_path / "id_rsa"
        secret.write_text("-----BEGIN PRIVATE KEY-----")
        (store / "token").symlink_to(secret)

        assert ss_module.SecureMonarchSession()._load_token_file() is None

    def test_symlink_is_not_followed_without_o_nofollow(
        self, store, tmp_path, monkeypatch
    ):
        secret = tmp_path / "id_rsa"
        secret.write_text("-----BEGIN PRIVATE KEY-----")
        (store / "token").symlink_to(secret)
        monkeypatch.delattr(ss_module.os, "O_NOFOLLOW", raising=False)

        assert ss_module.SecureMonarchSession()._load_token_file() is None

    def test_non_regular_file_is_refused(self, store, monkeypatch):
        token_file = store / "token"
        real_open = ss_module.os.open

        def _devnull_open(path, flags, *a):
            if os.fspath(path) == os.fspath(token_file):
                return real_open(os.devnull, os.O_RDONLY)
            return real_open(path, flags, *a)

        token_file.write_text("placeholder")
        monkeypatch.setattr(ss_module.os, "open", _devnull_open)

        assert ss_module.SecureMonarchSession()._load_token_file() is None

    def test_file_owned_by_another_user_is_refused(self, store, monkeypatch):
        (store / "token").write_text("planted-token")
        real_fstat = ss_module.os.fstat
        monkeypatch.setattr(
            ss_module.os, "fstat", lambda fd: _Foreign(real_fstat(fd))
        )

        assert ss_module.SecureMonarchSession()._load_token_file() is None

    def test_absurdly_large_file_is_refused(self, store):
        (store / "token").write_text("x" * (ss_module._MAX_TOKEN_BYTES + 1))
        assert ss_module.SecureMonarchSession()._load_token_file() is None


class _Foreign:
    """os.stat_result proxy reporting a uid that isn't the current user's."""

    def __init__(self, st):
        self._st = st

    def __getattr__(self, name):
        return getattr(self._st, name)

    @property
    def st_uid(self):
        return self._st.st_uid + 1


class TestTokenFileEncoding:
    """Session blobs must round-trip as UTF-8 regardless of the process locale.

    The file fallback exists for headless Linux, where a C/POSIX locale is
    common (bare Docker images, systemd units without LANG set). With encoding
    left to the locale, a blob carrying a non-ASCII cookie value raises
    UnicodeEncodeError on write and mojibake on read.
    """

    def test_round_trip_under_c_locale(self, tmp_path):
        import subprocess
        import sys

        script = textwrap.dedent(
            """
            import sys
            from pathlib import Path
            from monarch_mcp_server import secure_session as ss

            store = Path(sys.argv[1]) / "store"
            ss._TOKEN_DIR = store
            ss._TOKEN_FILE = store / "token"

            blob = '{"token": "caf\\u00e9-t\\u00f6ken", "auth_mode": "token"}'
            s = ss.SecureMonarchSession()
            s._save_token_file(blob)
            assert (store / "token").read_bytes() == blob.encode("utf-8"), "not utf-8 on disk"
            assert s._load_token_file() == blob, "did not round-trip"
            print("OK")
            """
        )
        env = {
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
        }
        proc = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            check=False,  # asserted on below, with the child's output attached
        )
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        assert "OK" in proc.stdout


class TestTokenDirIsNotFollowed:
    """O_NOFOLLOW on the token file guards only the final path component. If
    _TOKEN_DIR itself is a symlink, the token still lands in a directory the
    attacker controls — and chmod 0700 gets applied to their target."""

    def test_save_refuses_symlinked_token_dir(self, tmp_path, monkeypatch):
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        link = tmp_path / "store"
        link.symlink_to(attacker, target_is_directory=True)

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", link)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", link / "token")

        with pytest.raises(OSError):
            ss_module.SecureMonarchSession()._save_token_file("super-secret-token")

        assert not (attacker / "token").exists(), "token landed in attacker dir"

    def test_load_refuses_symlinked_token_dir(self, tmp_path, monkeypatch):
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        (attacker / "token").write_text("planted-token")
        link = tmp_path / "store"
        link.symlink_to(attacker, target_is_directory=True)

        monkeypatch.setattr(ss_module, "_TOKEN_DIR", link)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", link / "token")

        assert ss_module.SecureMonarchSession()._load_token_file() is None


class TestCleanupOldSessionFiles:
    """Upstream writes its pickled session to a CWD-relative .mm/ directory
    (monarchmoney.SESSION_DIR = ".mm"), so cleaning only ~/.mm never finds it."""

    def test_removes_cwd_relative_session_pickle(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # expanduser("~") is redirected so the real implementation cannot reach
        # the developer's home; $HOME itself stays put (see isolate_real_home).
        fake_home = tmp_path / "home"  # already created by isolate_real_home
        monkeypatch.setattr(
            ss_module.os.path, "expanduser", lambda p: str(fake_home)
        )

        mm = tmp_path / ".mm"
        mm.mkdir()
        pickle = mm / "mm_session.pickle"
        pickle.write_text("pickled-credentials")

        _REAL_CLEANUP(ss_module.SecureMonarchSession())

        assert not pickle.exists(), "CWD-relative session pickle was not cleaned up"
        assert not mm.exists(), "emptied .mm directory was not removed"


class TestTokenDirSurvivesDeletion:
    """Deleting the token must not delete the directory holding it.

    The directory is the 0700 container the token lives inside. Removing it on
    logout throws that boundary away and forces the next save to recreate it --
    and _assert_token_dir_safe()'s is_symlink() check runs *before* mkdir, so
    anything that can write to $HOME gets a fresh chance to win that race and
    pre-create the path as a symlink or a world-writable directory. Keeping the
    directory means the boundary is established once and persists.
    """

    def test_delete_token_file_keeps_the_directory(self, tmp_path, monkeypatch):
        import stat as _stat

        store = tmp_path / "store"
        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", store / "token")

        session = ss_module.SecureMonarchSession()
        session._save_token_file("a-token")
        assert (store / "token").exists()
        dir_inode = store.stat().st_ino

        session._delete_token_file()

        assert not (store / "token").exists(), "token file should be gone"
        assert store.is_dir(), "token directory was removed"
        assert store.stat().st_ino == dir_inode, "directory was recreated, not kept"
        assert _stat.S_IMODE(store.stat().st_mode) == 0o700

    def test_delete_token_is_idempotent(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", store / "token")

        session = ss_module.SecureMonarchSession()
        session._save_token_file("a-token")
        session._delete_token_file()
        session._delete_token_file()  # must not raise on an already-clean dir
        assert store.is_dir()


class TestEncryptionAtRest:
    """The file fallback should encrypt at rest where the platform can.

    On Windows the 0600/0700 mode bits this module sets are inert -- Windows
    honours only the read-only attribute -- and fchmod/O_NOFOLLOW do not exist,
    so the hardening in #1 degrades to almost nothing there. DPAPI is the
    primitive that platform actually provides.

    The win32crypt calls cannot run here, so these tests exercise the plumbing
    around them: the availability gate, the prefix, the round trip, and the
    transparent migration of an existing plaintext file.
    """

    @pytest.fixture
    def fake_dpapi(self, monkeypatch):
        """Stand in for CryptProtectData/CryptUnprotectData with a reversible
        transform, so the wiring is testable off-Windows."""
        monkeypatch.setattr(ss_module, "_dpapi_available", lambda: True)
        monkeypatch.setattr(
            ss_module,
            "_dpapi_encrypt",
            lambda s: ss_module._DPAPI_PREFIX + s[::-1],
        )
        monkeypatch.setattr(
            ss_module,
            "_dpapi_decrypt",
            lambda s: s[len(ss_module._DPAPI_PREFIX):][::-1],
        )

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        monkeypatch.setattr(ss_module, "_TOKEN_DIR", store)
        monkeypatch.setattr(ss_module, "_TOKEN_FILE", store / "token")
        return store

    def test_dpapi_is_unavailable_off_windows(self):
        import sys as _sys

        if _sys.platform != "win32":
            assert ss_module._dpapi_available() is False

    def test_token_is_not_plaintext_on_disk_when_available(self, store, fake_dpapi):
        session = ss_module.SecureMonarchSession()
        session._save_token_file("super-secret-token")

        on_disk = (store / "token").read_text(encoding="utf-8")
        assert "super-secret-token" not in on_disk
        assert on_disk.startswith(ss_module._DPAPI_PREFIX)

    def test_round_trip_through_encryption(self, store, fake_dpapi):
        session = ss_module.SecureMonarchSession()
        session._save_token_file("super-secret-token")
        assert session._load_token_file() == "super-secret-token"

    def test_plaintext_stays_plaintext_when_unavailable(self, store, monkeypatch):
        monkeypatch.setattr(ss_module, "_dpapi_available", lambda: False)
        session = ss_module.SecureMonarchSession()
        session._save_token_file("super-secret-token")
        assert (store / "token").read_text(encoding="utf-8") == "super-secret-token"
        assert session._load_token_file() == "super-secret-token"

    def test_existing_plaintext_file_still_loads_and_is_migrated(
        self, store, fake_dpapi
    ):
        store.mkdir()
        (store / "token").write_text("legacy-plaintext", encoding="utf-8")

        session = ss_module.SecureMonarchSession()
        assert session._load_token_file() == "legacy-plaintext"

        migrated = (store / "token").read_text(encoding="utf-8")
        assert migrated.startswith(ss_module._DPAPI_PREFIX)
        assert "legacy-plaintext" not in migrated

    def test_undecryptable_payload_returns_none(self, store, monkeypatch, fake_dpapi):
        def _boom(_s):
            raise OSError("wrong user or machine")

        session = ss_module.SecureMonarchSession()
        session._save_token_file("super-secret-token")
        monkeypatch.setattr(ss_module, "_dpapi_decrypt", _boom)

        assert session._load_token_file() is None
