"""Which tools this server is willing to expose.

The server holds credentials to a real personal-finance account and hands them to
an LLM. 24 of its 49 tools mutate that account -- create and delete transactions,
set budget amounts, rewrite auto-categorisation rules -- with no undo. Gating runs
at registration time, so a withheld tool is never advertised to the model and
cannot be invoked even by name.

**Allowlist, not denylist, and that is the point.** Classifying tools by name, or
by which client method they call, both fail -- in different places:

    split_transaction          reads as a query, calls update_transaction_splits
    categorize_transaction     reads as a query, calls update_transaction
    mark_transaction_reviewed  reads as a query, calls update_transaction
    review_recurring_stream    reads as a query and makes no write-shaped call --
                               a mutation only if you read the GraphQL operation
                               name (Web_ReviewStream)
    refresh_accounts           reads as a query, but calls
                               request_accounts_refresh, which triggers a sync at
                               the user's banks
    update_category            named as a write, reaches Monarch by raw GraphQL

A denylist inherits that unreliability forever: the next tool added upstream is
exposed by default, silently and unboundedly. An allowlist fails the other way --
an unlisted tool stays hidden, surfacing as a missing feature rather than as an
LLM writing to the account. That trade is deliberate, and it is why an unreadable
config falls back to reads-only rather than to "expose everything".

Which tools are enabled is data, not code: see ``tools.toml``. Flip a line to
``true`` and restart. When rebasing on upstream, the classification tests fail if
a tool appears that is in neither set -- read the tool's GraphQL operation, not
its name.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Point this at a path outside the checkout so toggling a tool is not a dirty file.
CONFIG_ENV_VAR = "MONARCH_TOOLS_CONFIG"

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "tools.toml"

#: Tools that only ever read. Verified from each tool's client call or GraphQL
#: operation, not from its name -- see the module docstring for why that matters.
READ_TOOLS = frozenset(
    {
        # local only, no network
        "check_auth_status",
        "debug_session_loading",
        "setup_authentication",
        # accounts
        "get_accounts",
        "get_account_holdings",
        "get_account_balance_history",
        # net worth / cashflow / summaries
        "get_net_worth",
        "get_net_worth_by_account_type",
        "get_cashflow",
        "get_cashflow_by_month",
        "get_spending_summary",
        "get_transactions_summary",
        # budgets
        "get_budgets",
        # categories
        "get_transaction_categories",
        "get_transaction_category_groups",
        "get_category_details",
        # merchants
        "get_merchant",
        # rules
        "get_transaction_rules",
        # tags
        "get_transaction_tags",
        # transactions
        "get_transactions",
        "get_transaction_details",
        "get_transactions_needing_review",
        "get_recurring_transactions",
        "search_transactions",
        "get_transaction_splits",
    }
)

#: Tools that mutate something. Financial data unless noted.
WRITE_TOOLS = frozenset(
    {
        # transactions
        "create_transaction",
        "delete_transaction",
        "update_transaction",
        "update_transaction_notes",
        "categorize_transaction",
        "bulk_categorize_transactions",
        "mark_transaction_reviewed",
        "split_transaction",
        # tags
        "create_transaction_tag",
        "add_transaction_tag",
        "set_transaction_tags",
        # categories
        "create_transaction_category",
        "update_category",
        # merchants
        "update_merchant",
        "review_recurring_stream",
        # rules
        "create_transaction_rule",
        "update_transaction_rule",
        "delete_transaction_rule",
        # budgets
        "set_budget_amount",
        # accounts
        "upload_account_balance_history",
        # No Monarch-side data change, but reaches out to the user's institutions
        # and can trip rate limits or an MFA prompt.
        "refresh_accounts",
        # Mutate locally stored credentials rather than account data. Withheld by
        # default because login_setup.py is the documented path and
        # setup_authentication (enabled) points at it.
        "monarch_login",
        "monarch_login_with_token",
        "monarch_logout",
    }
)

#: Used when the config is missing or unusable. Losing the config must never
#: silently enable a write.
FALLBACK_READ_TOOLS = READ_TOOLS


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG)


def _read_tools_table() -> dict[str, object] | None:
    path = config_path()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        logger.warning(
            "tool config not found at %s — falling back to the built-in read-only "
            "set. Set %s to point at one.",
            path,
            CONFIG_ENV_VAR,
        )
        return None
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.error(
            "tool config at %s is unreadable (%s) — falling back to the built-in "
            "read-only set. No write tool will be exposed until this is fixed.",
            path,
            exc,
        )
        return None

    tools = data.get("tools")
    if not isinstance(tools, dict):
        logger.error(
            "tool config at %s has no [tools] table — falling back to the "
            "built-in read-only set.",
            path,
        )
        return None
    return tools


def load_declared() -> set[str]:
    """Every tool named in the config, enabled or not."""
    return set(_read_tools_table() or {})


def load_enabled() -> set[str]:
    """The set of tool names permitted to register."""
    tools = _read_tools_table()
    if tools is None:
        return set(FALLBACK_READ_TOOLS)

    # `is True` on purpose: a stray "false", 0 or "" must never read as
    # permission, and neither should a truthy string.
    enabled = {name for name, value in tools.items() if value is True}
    logger.info(
        "tool config %s: %d of %d tool(s) enabled",
        config_path(),
        len(enabled),
        len(tools),
    )
    return enabled


def enforce(mcp: Any) -> tuple[list[str], list[str]]:
    """Wrap ``mcp.tool`` so only enabled tools register.

    Must run before the tool modules are imported. Returns
    ``(withheld, registered)``; log both -- a silent gate is indistinguishable
    from a gate that has stopped working, and a configured name that never
    registers usually means upstream renamed it.
    """
    enabled = load_enabled()
    original = mcp.tool
    withheld: list[str] = []
    registered: list[str] = []

    def gated(
        *args: Any, **kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        decorate = original(*args, **kwargs)

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            name = fn.__name__
            if name in enabled:
                registered.append(name)
                # mcp.tool is untyped upstream, so its return is Any; narrow it
                # rather than leaking Any through this signature.
                decorated: Callable[..., Any] = decorate(fn)
                return decorated
            withheld.append(name)
            # Returned undecorated: still importable and callable in-process,
            # never an MCP tool.
            return fn

        return register

    mcp.tool = gated
    return withheld, registered
