"""
Secure session management for Monarch Money MCP Server.

Uses the system keyring when available, with an automatic file-based
fallback for environments without a keyring backend (e.g. WSL, headless Linux).
"""

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from monarchmoney import MonarchMoney

from monarch_mcp_server.monarch_auth import (
    cookies_from_client,
    create_monarch_client,
)

logger = logging.getLogger(__name__)

# Keyring service identifiers
KEYRING_SERVICE = "com.mcp.monarch-mcp-server"
KEYRING_USERNAME = "monarch-token"

# File-based fallback location
_TOKEN_DIR = Path.home() / ".monarch-mcp-server"
_TOKEN_FILE = _TOKEN_DIR / "token"
# A stored session is a small JSON blob (token, device uuid, a few cookies).
# Anything substantially larger did not come from us; cap the read rather than
# pulling an arbitrarily large planted file into memory.
_MAX_TOKEN_BYTES = 64 * 1024


_PROBE_USERNAME = "__keyring_probe__"


# The plaintext file fallback is POSIX-only: it depends on these to store the
# token with owner-only permissions and to refuse a symlink planted at the path.
# The keyring path has no such requirement -- on Windows, Credential Manager
# works and this fallback is only reached when a keyring is absent or throws --
# so the right response to a missing primitive is a clear refusal, not an
# AttributeError from deep inside a save.
# Open flags must be non-zero to do anything: OR-ing 0 into os.open() enables
# nothing, which is why the earlier code wrote getattr(os, "O_NOFOLLOW", 0) and
# treated 0 as absent. With the best-effort prechecks gone there is no backstop,
# so a falsy flag has to count as missing rather than silently disabling the
# hardening.
_POSIX_FLAGS = ("O_NOFOLLOW", "O_NONBLOCK")
_POSIX_CALLS = ("fchmod", "getuid")
_POSIX_PRIMITIVES = _POSIX_FLAGS + _POSIX_CALLS


def _require_posix_primitives() -> None:
    missing = []
    for name in _POSIX_PRIMITIVES:
        value = getattr(os, name, None)
        if name in _POSIX_FLAGS:
            # A flag of 0 is a no-op: OR-ing it into os.open() enables nothing.
            usable = isinstance(value, int) and value != 0
        else:
            usable = callable(value)
        if not usable:
            missing.append(name)
    if missing:
        raise OSError(
            "The file-based token fallback needs POSIX primitives that are "
            f"unavailable here (missing: {', '.join(missing)}), so the token "
            "cannot be stored with owner-only permissions, protected against "
            "symlink redirection, or read and written without risking a hang on "
            "a special file left at the path. Configure a working keyring "
            "backend instead."
        )


def _keyring_available() -> bool:
    """Probe whether the active keyring backend can actually round-trip a value.

    Class-name sniffing is unreliable: the macOS Keychain backend
    (`keyring.backends.macOS.Keyring`) and the no-op fail backend
    (`keyring.backends.fail.Keyring`) share the class name `Keyring`, so a
    name-based check rejects real macOS keyrings and silently falls back to
    plaintext file storage. We instead set + get + delete a sentinel value
    and trust the backend only if every step succeeds.
    """
    try:
        import keyring
    except ImportError:
        return False

    try:
        keyring.set_password(KEYRING_SERVICE, _PROBE_USERNAME, "1")
        stored = keyring.get_password(KEYRING_SERVICE, _PROBE_USERNAME)
        keyring.delete_password(KEYRING_SERVICE, _PROBE_USERNAME)
    except Exception:
        return False

    return stored == "1"


class SecureMonarchSession:
    """Manages Monarch Money sessions securely using the system keyring,
    falling back to a file-based store when no keyring backend is available."""

    def __init__(self) -> None:
        self._use_keyring = _keyring_available()
        if self._use_keyring:
            logger.info("🔐 Using system keyring for token storage")
        else:
            logger.info("🔐 Keyring unavailable — using file-based token storage")

    # -- file-based helpers --------------------------------------------------

    @staticmethod
    def _assert_token_dir_safe() -> None:
        """Refuse to use _TOKEN_DIR when it is a symlink.

        O_NOFOLLOW on the token file guards only the final path component. A
        symlinked _TOKEN_DIR redirects both the read and the write into a
        directory someone else controls, and _save_token_file's chmod 0700 then
        lands on their target.

        This one is genuinely best-effort, unlike the O_NOFOLLOW guards on the
        file itself: mkdir and open are separate syscalls, so the link could be
        swapped in between. Closing it properly needs openat(2) against a
        directory fd, which Python exposes only patchily.
        """
        if _TOKEN_DIR.is_symlink():
            raise OSError(f"{_TOKEN_DIR} is a symlink; refusing to use it")

    def _save_token_file(self, token: str) -> None:
        _require_posix_primitives()
        self._assert_token_dir_safe()
        _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        _TOKEN_DIR.chmod(stat.S_IRWXU)  # 700
        # Create the file already locked to owner-only (0600) instead of
        # write_text()-then-chmod, which leaves a window where the token is
        # world-readable under a default umask.
        mode = stat.S_IRUSR | stat.S_IWUSR  # 600
        # O_NOFOLLOW refuses a symlink planted at the token path outright, rather
        # than following it and writing the token to an attacker-chosen target.
        # Atomic with the open, so there is no window to race.
        # O_NONBLOCK so a FIFO planted at the path fails the S_ISREG check below
        # instead of blocking the open until a reader appears -- a hang is worse
        # than an error, since nothing is logged and the server simply stops. It
        # is a no-op for regular files. The read path always passed it; the write
        # path did not, which was the asymmetry.
        fd = os.open(
            _TOKEN_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
            mode,
        )
        try:
            # O_NOFOLLOW only rejects symlinks, so confirm we really hold a plain
            # file before writing a credential into it — not a device or socket
            # someone left at the path.
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"{_TOKEN_FILE} is not a regular file; refusing to write")
            # O_CREAT honors the mode only when creating, so a pre-existing file
            # keeps its old (possibly 0644) mode. Narrow it before any token bytes
            # land. fchmod targets the open file, so unlike a path-based chmod it
            # cannot be redirected by a symlink swapped in after the open.
            os.fchmod(fd, mode)
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        # fdopen took ownership of fd; closing f closes it exactly once.
        with f:
            f.write(token)
        logger.info(f"✅ Token saved to {_TOKEN_FILE}")

    def _load_token_file(self) -> Optional[str]:
        # Whatever this returns is credential material: load_session_blob treats
        # a non-JSON value as a bare token and sends it to api.monarch.com as an
        # Authorization header. A symlink planted here would therefore exfiltrate
        # whatever it points at, so the read path needs the same guarantees as
        # the write path — the file must be a regular file that we own.
        try:
            _require_posix_primitives()
            self._assert_token_dir_safe()
        except OSError as e:
            # Callers already treat None as "no session", so refuse rather than
            # crash -- the user gets a re-login prompt and a logged reason.
            logger.warning(f"⚠️  Refusing to read the token file: {e}")
            return None
        try:
            # O_NONBLOCK so a FIFO left at the path fails the regular-file check
            # below instead of hanging the open forever. It is a no-op for
            # regular files.
            fd = os.open(_TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as e:
            # ELOOP from O_NOFOLLOW lands here, as does anything unreadable.
            logger.warning(f"⚠️  Refusing to read {_TOKEN_FILE}: {e}")
            return None
        try:
            f = os.fdopen(fd, "r", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                logger.warning(
                    f"⚠️  {_TOKEN_FILE} is not a regular file; refusing to read"
                )
                return None
            if st.st_uid != os.getuid():
                logger.warning(
                    f"⚠️  {_TOKEN_FILE} is owned by another user; refusing to read"
                )
                return None
            if st.st_size > _MAX_TOKEN_BYTES:
                logger.warning(
                    f"⚠️  {_TOKEN_FILE} is larger than a session blob; refusing to read"
                )
                return None
            # Bounded independently of st_size, which can understate the content.
            token = f.read(_MAX_TOKEN_BYTES + 1)
        if len(token) > _MAX_TOKEN_BYTES:
            logger.warning(
                f"⚠️  {_TOKEN_FILE} is larger than a session blob; refusing to read"
            )
            return None
        token = token.strip()
        if not token:
            return None

        logger.info(f"✅ Token loaded from {_TOKEN_FILE}")
        return token

    def _delete_token_file(self) -> None:
        # Catch FileNotFoundError rather than passing missing_ok=True: both
        # tolerate an absent file, but this way the success branch only runs
        # when something was actually removed, so the log cannot claim a
        # deletion that did not happen. unlink() on a symlink removes the link
        # rather than whatever it points at.
        try:
            _TOKEN_FILE.unlink()
        except FileNotFoundError:
            logger.debug(f"No token file to delete at {_TOKEN_FILE}")
        except OSError as e:
            logger.warning(f"⚠️  Could not delete {_TOKEN_FILE}: {e}")
        else:
            logger.info(f"🗑️ Token file deleted: {_TOKEN_FILE}")
        # The directory is deliberately left in place. It is the 0700 container
        # the token lives in; removing it discards that boundary and makes the
        # next save recreate the path, which hands anything able to write to
        # $HOME another chance to win the race between _assert_token_dir_safe()
        # and mkdir() by pre-creating it as a symlink or a permissive directory.

    # -- public API ----------------------------------------------------------

    def save_session_blob(
        self,
        *,
        token: Optional[str] = None,
        device_uuid: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
        auth_mode: str = "token",
    ) -> None:
        """Persist a Monarch session (token + device_uuid, or cookies).

        Stored as a JSON blob so we can represent either auth mode:

        - Token mode: ``{"token": "...", "device_uuid": "...",
          "auth_mode": "token"}``. The device_uuid must be the same UUID
          presented during login or Monarch rejects the token.
        - Cookie mode: ``{"cookies": {"session_id": "...",
          "csrftoken": "..."}, "auth_mode": "cookie"}``. May also carry
          ``token`` and ``device_uuid`` from the same session, which the
          upstream library preserves as a fallback.
        """
        if not token and not cookies:
            raise ValueError("save_session_blob requires either a token or cookies")

        session_data: Dict[str, Any] = {"auth_mode": auth_mode}
        if token:
            session_data["token"] = token
        if device_uuid:
            session_data["device_uuid"] = device_uuid
        if cookies:
            session_data["cookies"] = dict(cookies)
        blob = json.dumps(session_data)

        if self._use_keyring:
            try:
                import keyring

                keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, blob)
                logger.info(
                    "✅ Session saved securely to keyring (auth_mode=%s)",
                    auth_mode,
                )
                self._cleanup_old_session_files()
                return
            except Exception as e:
                logger.warning(f"⚠️  Keyring save failed, falling back to file: {e}")

        self._save_token_file(blob)
        self._cleanup_old_session_files()

    def save_token(self, token: str, *, device_uuid: Optional[str] = None) -> None:
        """Save a token-mode session. Kept for backward compatibility."""
        self.save_session_blob(token=token, device_uuid=device_uuid, auth_mode="token")

    def load_token(self) -> Optional[str]:
        """Load just the authentication token from keyring or file fallback."""
        session = self.load_session()
        if not session:
            return None
        token = session.get("token")
        return token if isinstance(token, str) else None

    def load_session(self) -> Optional[Dict[str, Any]]:
        """Load the stored Monarch session as a dict.

        Returns a dict that may carry any of ``token``, ``device_uuid``,
        ``cookies`` (a nested dict), and ``auth_mode``. Accepts three
        legacy formats:

        1. Bare token string (very old installs).
        2. JSON blob with token and optional device_uuid, no auth_mode.
        3. Current JSON blob with explicit auth_mode and optional cookies.

        Cookies are returned as a nested ``dict`` to preserve their
        original key/value pairs (legacy ``load_session`` flattened
        everything to ``str(value)`` which corrupted nested dicts).
        """
        raw_session = None
        if self._use_keyring:
            try:
                import keyring

                raw_session = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
            except Exception as e:
                logger.warning(f"⚠️  Keyring load failed, trying file fallback: {e}")

        if raw_session is None:
            raw_session = self._load_token_file()

        if not raw_session:
            logger.info("🔍 No session found")
            return None

        logger.info("✅ Session loaded from secure storage")
        try:
            parsed = json.loads(raw_session)
        except json.JSONDecodeError:
            # Legacy entry: the stored value is the bare token string.
            return {"token": raw_session, "auth_mode": "token"}

        if not isinstance(parsed, dict):
            return None
        if not parsed.get("token") and not parsed.get("cookies"):
            return None

        result: Dict[str, Any] = {}
        if isinstance(parsed.get("token"), str):
            result["token"] = parsed["token"]
        if isinstance(parsed.get("device_uuid"), str):
            result["device_uuid"] = parsed["device_uuid"]
        cookies = parsed.get("cookies")
        if isinstance(cookies, dict) and cookies:
            result["cookies"] = {str(k): str(v) for k, v in cookies.items()}
        # Default to "cookie" only when cookies are present and no explicit
        # auth_mode says otherwise; otherwise default to "token".
        result["auth_mode"] = parsed.get(
            "auth_mode", "cookie" if result.get("cookies") else "token"
        )
        return result

    def delete_token(self) -> None:
        """Delete the authentication token from all storage backends."""
        # Try keyring
        if self._use_keyring:
            try:
                import keyring

                keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
                logger.info("🗑️ Token deleted from keyring")
            except Exception:
                pass

        # Always try file cleanup too
        self._delete_token_file()
        self._cleanup_old_session_files()

    def get_authenticated_client(self) -> Optional[MonarchMoney]:
        """Get an authenticated MonarchMoney client.

        Prefers cookie auth when cookies are present, falling back to the
        token + device_uuid path otherwise. Returns None if no usable
        session is stored.
        """
        session = self.load_session()
        if not session:
            return None

        auth_mode = session.get("auth_mode", "token")
        cookies = session.get("cookies")
        token = session.get("token")

        try:
            if auth_mode == "cookie" and isinstance(cookies, dict) and cookies:
                client = create_monarch_client(
                    token=token, device_uuid=session.get("device_uuid")
                )
                # set_cookies pops Authorization and sets the cookie-mode
                # web headers (Origin, Referer, monarch-client, X-Csrftoken).
                client.set_cookies(cookies)
                logger.info("✅ MonarchMoney client created with stored cookies")
                return client

            if not token:
                logger.warning(
                    "⚠️  Session has no token and no cookies; treating as missing"
                )
                return None

            client = create_monarch_client(
                token=token, device_uuid=session.get("device_uuid")
            )
            logger.info("✅ MonarchMoney client created with stored token")
            return client
        except Exception as e:
            logger.error(f"❌ Failed to create MonarchMoney client: {e}")
            return None

    def save_authenticated_session(self, mm: MonarchMoney) -> None:
        """Save the session from an authenticated MonarchMoney instance.

        Inspects ``mm._auth_mode`` to decide whether to persist cookies or
        the token + device_uuid pair. The upstream library exposes both as
        documented internals (``_auth_mode``, ``_cookies``, ``token``).
        """
        cookies = cookies_from_client(mm)
        device_uuid = mm._headers.get("device-uuid")

        if cookies:
            self.save_session_blob(
                token=mm.token,
                device_uuid=device_uuid,
                cookies=cookies,
                auth_mode="cookie",
            )
            return

        if mm.token:
            self.save_session_blob(
                token=mm.token,
                device_uuid=device_uuid,
                auth_mode="token",
            )
            return

        logger.warning("⚠️  MonarchMoney instance has no token or cookies to save")

    def _cleanup_old_session_files(self) -> None:
        """Clean up old insecure session files."""
        home = os.path.expanduser("~")
        # monarchmoney.SESSION_DIR is the bare relative path ".mm", so the
        # library writes its pickled session under the *current directory*, not
        # under $HOME. Cleaning only ~/.mm never found the file it was written
        # to remove. Sweep both. Files first, so the directories are empty by
        # the time their turn comes.
        cwd = os.getcwd()
        cleanup_paths = [
            os.path.join(home, ".mm", "mm_session.pickle"),
            os.path.join(cwd, ".mm", "mm_session.pickle"),
            os.path.join(home, "monarch_session.json"),
            os.path.join(home, ".mm"),  # Remove the entire directory if empty
            os.path.join(cwd, ".mm"),
        ]

        for path in cleanup_paths:
            try:
                if os.path.exists(path):
                    if os.path.isfile(path):
                        os.remove(path)
                        logger.info(f"🗑️ Cleaned up old insecure session file: {path}")
                    elif os.path.isdir(path) and not os.listdir(path):
                        os.rmdir(path)
                        logger.info(f"🗑️ Cleaned up empty session directory: {path}")
            except Exception as e:
                logger.warning(f"⚠️  Could not clean up {path}: {e}")


# Global session manager instance
secure_session = SecureMonarchSession()
