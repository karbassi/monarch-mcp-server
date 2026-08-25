#!/usr/bin/env python3
"""
Standalone script to perform interactive Monarch Money login.

Supports three auth paths in order of recommendation:

1. Session cookies pasted from a logged-in browser. Long-lived, works
   for all account types including SSO, sidesteps Cloudflare CAPTCHA.
2. Email and password (with optional email OTP and MFA prompts). Now
   requests a long-lived session token from Monarch.
3. Legacy session token paste. Kept for users with a working token
   captured under the old auth model.
"""

import asyncio
import getpass
import os
import sys
from pathlib import Path

src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from monarchmoney import CaptchaRequiredException, RequireMFAException
from monarch_mcp_server.monarch_auth import (
    EmailOtpRequiredException,
    create_monarch_client,
    login_with_browser_cookies,
    login_with_current_auth,
)
from monarch_mcp_server.secure_session import secure_session


# Terminals read in canonical mode, where the line buffer is capped at
# MAX_CANON -- 1024 bytes on macOS. A raw `cookie:` header from app.monarch.com
# routinely exceeds that once Cloudflare and analytics cookies are in play, so
# the paste is silently truncated and the login fails with nothing to explain
# why. Query the tty when we can; 1024 is the POSIX floor otherwise.
def _canonical_limit() -> int:
    try:
        return int(os.pathconf("/dev/tty", "PC_MAX_CANON"))
    except (OSError, ValueError, AttributeError):
        return 1024


_CANONICAL_LIMIT = _canonical_limit()


class CookieInputTruncated(RuntimeError):
    """The pasted cookie hit the terminal's line limit and was cut off."""


def _read_cookie_string() -> str:
    """Read the browser cookie header, preferring paths that bypass the tty.

    MONARCH_COOKIE, then MONARCH_COOKIE_FILE, then an interactive prompt. The
    first two exist so a header longer than MAX_CANON can be supplied at all.
    """
    from_env = os.environ.get("MONARCH_COOKIE")
    if from_env and from_env.strip():
        return from_env.strip()

    path = os.environ.get("MONARCH_COOKIE_FILE")
    if path and path.strip():
        return Path(path.strip()).read_text(encoding="utf-8").strip()

    value = getpass.getpass("Paste the Cookie header value: ").strip()
    if len(value) >= _CANONICAL_LIMIT:
        raise CookieInputTruncated(
            f"The pasted value is {len(value)} bytes, at or over this "
            f"terminal's {_CANONICAL_LIMIT}-byte line limit, so it was almost "
            "certainly cut off. Write the header to a file and set "
            "MONARCH_COOKIE_FILE=/path/to/file (or MONARCH_COOKIE=...) instead."
        )
    return value


async def _login_with_cookies():
    print("\n📋 To copy the right cookie string:")
    print("  1. Log in to https://app.monarch.com in Chrome or Firefox")
    print("  2. Open DevTools (F12) → Network tab")
    print("  3. Click any request whose Name starts with 'graphql'")
    print("     (or any request to api.monarch.com)")
    print("  4. Scroll to 'Request Headers' and find the 'cookie:' header")
    print("  5. Copy the full value (a long string of key=value; pairs)")
    print()
    print("  (If the header is long, save it to a file and set")
    print("   MONARCH_COOKIE_FILE=/path/to/file -- terminals truncate")
    print("   pastes at the MAX_CANON line limit.)")
    print()
    try:
        cookie_string = _read_cookie_string()
    except CookieInputTruncated as e:
        print(f"❌ {e}")
        return None
    except OSError as e:
        print(f"❌ Could not read the cookie: {e}")
        return None
    if not cookie_string:
        print("❌ No cookie string provided. Exiting.")
        return None
    try:
        mm = await login_with_browser_cookies(cookie_string)
        print("✅ Cookie login successful")
        return mm
    except Exception as e:
        print(f"❌ Cookie login failed: {e}")
        return None


async def _login_with_password():
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    try:
        mm = await login_with_current_auth(email, password)
        print("✅ Login successful")
        return mm
    except CaptchaRequiredException as e:
        print(f"❌ {e}")
        print("Re-run this script and choose option 1 (session cookies).")
        return None
    except EmailOtpRequiredException:
        print("📧 Monarch sent a verification code to your email.")
        code = input("Email verification code: ").strip()
        if not code:
            print("❌ No code provided. Exiting.")
            return None
        try:
            mm = await login_with_current_auth(email, password, email_otp=code)
        except RequireMFAException:
            mfa_code = input("Two Factor Code: ").strip()
            mm = await login_with_current_auth(
                email, password, email_otp=code, mfa_code=mfa_code
            )
        print("✅ Email verification successful")
        return mm
    except RequireMFAException:
        mfa_code = input("Two Factor Code: ").strip()
        if not mfa_code:
            print("❌ No MFA code provided. Exiting.")
            return None
        mm = await login_with_current_auth(email, password, mfa_code=mfa_code)
        print("✅ MFA authentication successful")
        return mm


def _login_with_legacy_token():
    print("\n📋 To get a legacy session token:")
    print("  1. Log in to https://app.monarch.com in Chrome or Firefox")
    print("  2. DevTools (F12) → Application tab → Local Storage")
    print("     → https://app.monarch.com → key 'token'")
    print("  3. Copy the value")
    print()
    print(
        "⚠️  Monarch may no longer accept Authorization: Token auth on the "
        "GraphQL endpoint. If the test call below fails with 401, re-run "
        "this script and choose option 1 (cookies) instead."
    )
    token = getpass.getpass("Paste your session token: ").strip()
    if not token:
        print("❌ No token provided. Exiting.")
        return None
    mm = create_monarch_client(token=token)
    print("✅ Token configured")
    return mm


async def main():
    print("\n🏦 Monarch Money - Claude Desktop Setup")
    print("=" * 45)
    print("This will authenticate you once and save a session")
    print("for seamless access through Claude Desktop.\n")

    try:
        import monarchmoney
        print(
            f"📦 MonarchMoney version: "
            f"{getattr(monarchmoney, '__version__', 'unknown')}"
        )
    except Exception as e:
        print(f"⚠️  Could not check version: {e}")

    try:
        # Deliberately NOT clearing the stored session here. There are five
        # return paths between this point and save_authenticated_session below
        # (bad menu choice, login helper returning None, unexpected accounts
        # payload, failed connection test, failed save) and clearing up front
        # meant hitting any of them left the user with no session at all.
        # The clear was redundant anyway: a save replaces the keyring entry
        # outright and opens the fallback file O_TRUNC, so nothing stale
        # survives it.
        print("\nHow do you sign in to Monarch Money?")
        print(
            "  1) Session cookies from browser   "
            "(recommended: long-lived, supports SSO)"
        )
        print("  2) Email and password")
        print("  3) Legacy session token paste")
        choice = input("Choice [1]: ").strip() or "1"

        mm = None
        if choice == "1":
            mm = await _login_with_cookies()
        elif choice == "2":
            mm = await _login_with_password()
        elif choice == "3":
            mm = _login_with_legacy_token()
        else:
            print(f"❌ Unrecognized choice: {choice!r}. Exiting.")
            return

        if mm is None:
            return

        print("\nTesting connection...")
        try:
            accounts = await mm.get_accounts()
            if accounts and isinstance(accounts, dict):
                account_count = len(accounts.get("accounts", []))
                print(f"✅ Found {account_count} accounts")
            else:
                print(f"❌ Unexpected accounts response: {type(accounts)}")
                return
        except Exception as test_error:
            print(f"❌ Connection test failed: {test_error}")
            print(f"Error type: {type(test_error).__name__}")
            print(
                "\nIf this looks like 401 Unauthorized, the cookie or token "
                "is invalid. Re-run this script."
            )
            return

        try:
            print("\n🔐 Saving session securely to system keyring...")
            secure_session.save_authenticated_session(mm)
            print("✅ Session saved")
        except Exception as save_error:
            print(f"❌ Could not save session: {save_error}")
            return

        print("\n🎉 Setup complete. Restart Claude Desktop to pick up the session.")
        print("\n💡 Useful tools in Claude:")
        print("   • get_accounts - View all your accounts")
        print("   • get_transactions - Recent transactions")
        print("   • get_budgets - Budget information")
        print("   • get_cashflow - Income/expense analysis")

    except Exception as e:
        print(f"\n❌ Login failed: {e}")
        print(f"Error type: {type(e).__name__}")


if __name__ == "__main__":
    asyncio.run(main())
