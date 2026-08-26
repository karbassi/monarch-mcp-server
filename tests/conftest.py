"""Shared test fixtures for Monarch MCP Server tests."""

import contextlib
import importlib.util
import json
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Mock the monarchmoney module before any monarch_mcp_server imports
mm_mock = MagicMock()
mm_mock.MonarchMoney = MagicMock
mm_mock.RequireMFAException = Exception
sys.modules.setdefault("monarchmoney", mm_mock)
sys.modules.setdefault("monarchmoney.monarchmoney", MagicMock())

import pytest

# Shapes below mirror real API responses, reduced to what the wrappers read.
_INSTITUTIONS_RESPONSE = {
    "accounts": [
        {
            "id": "acc-1",
            "displayName": "Checking Account",
            "mask": "4321",
            "subtype": {"name": "checking", "display": "Checking"},
            "credential": {"id": "cred-1"},
            "deletedAt": None,
        },
        {
            "id": "acc-deleted",
            "displayName": "Old Card",
            "mask": "9999",
            "subtype": {"name": "credit_card", "display": "Credit Card"},
            "credential": {"id": "cred-1"},
            "deletedAt": "2026-01-01",
        },
        {
            "id": "acc-manual",
            "displayName": "Cash",
            "mask": None,
            "subtype": {"name": "cash", "display": "Cash"},
            "credential": None,
            "deletedAt": None,
        },
    ],
    "credentials": [
        {
            "id": "cred-1",
            "institution": {"name": "Test Bank"},
            "dataProvider": "PLAID",
            "displayLastUpdatedAt": "2026-08-25",
            "updateRequired": False,
            "disconnectedFromDataProviderAt": None,
        },
        {
            "id": "cred-2",
            "institution": {"name": "Broken Bank"},
            "dataProvider": "MX",
            "displayLastUpdatedAt": "2026-06-01",
            "updateRequired": True,
            "disconnectedFromDataProviderAt": None,
        },
        {
            "id": "cred-3",
            "institution": {"name": "Gone Bank"},
            "dataProvider": "PLAID",
            "displayLastUpdatedAt": "2026-02-01",
            "updateRequired": False,
            "disconnectedFromDataProviderAt": "2026-03-01",
        },
    ],
}

_CREDIT_HISTORY_RESPONSE = {
    "creditScoreSnapshots": [
        {"reportedDate": "2026-08-01", "score": 760},
        {"reportedDate": "2026-06-01", "score": 740},
        {"reportedDate": "2026-07-01", "score": None},
    ],
    "spinwheelUser": {
        "creditScoreTrackingStatus": "ACTIVE",
        "onboardingStatus": "COMPLETE",
        "onboardingErrorMessage": None,
    },
}

_RECENT_BALANCES_RESPONSE = {
    "accounts": [
        {"id": "acc-1", "recentBalances": [100.0, 110.0, 120.0]},
        {"id": "acc-2", "recentBalances": []},
    ]
}

_DUPLICATES_RESPONSE = [
    {
        "date": "2026-08-10",
        "amount": -42.5,
        "account_id": "acc-1",
        "account_name": "Checking Account",
        "plaidName": "COFFEE SHOP",
        "transactions": [{"id": "txn-a"}, {"id": "txn-b"}],
    }
]


@pytest.fixture
def mock_monarch_client():
    """Create a mock MonarchMoney client with default responses."""
    client = AsyncMock()

    client.get_accounts.return_value = {
        "accounts": [
            {
                "id": "acc-1",
                "displayName": "Checking Account",
                "name": "Checking",
                "type": {"name": "checking"},
                "currentBalance": 1500.00,
                "displayBalance": 500.00,
                "institution": {"name": "Test Bank"},
                "deactivatedAt": None,
                "isHidden": False,
                "subtype": {"name": "roth_ira", "display": "Roth IRA"},
                "mask": "4321",
                "hideFromList": True,
                "includeInNetWorth": False,
            },
            {
                "id": "acc-2",
                "displayName": "Savings Account",
                "name": "Savings",
                "type": {"name": "savings"},
                "currentBalance": 10000.00,
                "displayBalance": 1000.00,
                "institution": {"name": "Test Bank"},
                "deactivatedAt": None,
                "isHidden": True,
            },
        ]
    }

    client.get_transactions.return_value = {
        "allTransactions": {
            "results": [
                {
                    "id": "txn-1",
                    "date": "2026-03-01",
                    "amount": -42.50,
                    "currencyCode": "CAD",
                    "description": "Grocery Store",
                    "plaidName": "WHOLE FOODS MARKET 10234",
                    "category": {
                        "id": "cat-1",
                        "name": "Groceries",
                        "group": {"id": "grp-1", "name": "Food", "type": "expense"},
                    },
                    "account": {"id": "acc-1", "displayName": "Checking Account"},
                    "merchant": {"name": "Whole Foods"},
                    "isPending": False,
                    "needsReview": True,
                    "notes": "weekly groceries",
                    "isRecurring": False,
                    "reviewStatus": "needs_review",
                    "isSplitTransaction": False,
                    "hideFromReports": False,
                    "tags": [{"id": "tag-1", "name": "business"}],
                },
                {
                    "id": "txn-2",
                    "date": "2026-03-02",
                    "amount": 3000.00,
                    "description": "Paycheck",
                    "category": {"id": "cat-3", "name": "Income"},
                    "account": {"id": "acc-1", "displayName": "Wise USD Balance"},
                    "merchant": None,
                    "isPending": False,
                    "needsReview": False,
                    "notes": None,
                    "isRecurring": True,
                    "reviewStatus": "reviewed",
                    "isSplitTransaction": False,
                    "hideFromReports": False,
                    "tags": [],
                },
            ]
        }
    }

    # get_budgets now fetches via a custom GraphQL query (client.gql_call),
    # matching the MCPBudgetData query in tools/budgets.py.
    client.gql_call.return_value = {
        "budgetData": {
            "monthlyAmountsByCategory": [
                {
                    "category": {"id": "cat-1"},
                    "monthlyAmounts": [
                        {
                            "month": "2026-03-01",
                            "plannedCashFlowAmount": 500.00,
                            "plannedSetAsideAmount": 0.00,
                            "actualAmount": 320.00,
                            "remainingAmount": 180.00,
                        }
                    ],
                },
                {
                    "category": {"id": "cat-2"},
                    "monthlyAmounts": [
                        {
                            "month": "2026-03-01",
                            "plannedCashFlowAmount": 200.00,
                            "plannedSetAsideAmount": 0.00,
                            "actualAmount": 185.00,
                            "remainingAmount": 15.00,
                        }
                    ],
                },
            ]
        },
        "categoryGroups": [
            {
                "id": "grp-1",
                "name": "Food",
                "type": "expense",
                "categories": [
                    {"id": "cat-1", "name": "Groceries"},
                    {"id": "cat-2", "name": "Dining Out"},
                ],
            }
        ],
    }

    client.get_cashflow.return_value = {
        "cashflow": {
            "income": 5000.00,
            "expenses": -3200.00,
            "savings": 1800.00,
        }
    }

    client.get_account_holdings.return_value = {
        "holdings": [
            {
                "id": "hold-1",
                "name": "VTI",
                "quantity": 100,
                "value": 25000.00,
            }
        ]
    }

    client.create_transaction.return_value = {
        "createTransaction": {"transaction": {"id": "txn-new"}}
    }

    client.update_transaction.return_value = {
        "updateTransaction": {"transaction": {"id": "txn-1"}}
    }

    client.request_accounts_refresh.return_value = {
        "requestAccountsRefresh": {"success": True}
    }

    client.get_transaction_categories.return_value = {
        "categories": [
            {
                "id": "cat-1",
                "name": "Groceries",
                "icon": "🛒",
                "group": {"id": "grp-1", "name": "Food", "type": "expense"},
            },
            {
                "id": "cat-2",
                "name": "Dining Out",
                "icon": "🍽️",
                "group": {"id": "grp-1", "name": "Food", "type": "expense"},
            },
        ]
    }

    client.get_transaction_tags.return_value = {
        "householdTransactionTags": [
            {"id": "tag-1", "name": "business", "color": "#ff0000"},
            {"id": "tag-2", "name": "vacation", "color": "#00ff00"},
        ]
    }

    client.get_transaction_details.return_value = {
        "getTransaction": {
            "id": "txn-1",
            "tags": [{"id": "tag-1", "name": "business"}],
        }
    }

    client.set_transaction_tags.return_value = {
        "setTransactionTags": {"transaction": {"id": "txn-1"}}
    }

    client.get_transaction_category_groups.return_value = {
        "categoryGroups": [
            {"id": "grp-1", "name": "Food", "type": "expense"},
            {"id": "grp-2", "name": "Income", "type": "income"},
        ]
    }

    client.create_transaction_category.return_value = {
        "createCategory": {"category": {"id": "cat-new", "name": "Coffee"}}
    }

    client.create_transaction_tag.return_value = {
        "createTransactionTag": {
            "tag": {"id": "tag-new", "name": "new", "color": "#0000ff"}
        }
    }

    client.get_account_history.return_value = [
        {
            "date": "2026-04-20",
            "signedBalance": 1000.0,
            "accountId": "acc-1",
            "accountName": "Checking Account",
        },
        {
            "date": "2026-04-21",
            "signedBalance": 1200.0,
            "accountId": "acc-1",
            "accountName": "Checking Account",
        },
        {
            "date": "2026-04-22",
            "signedBalance": 1100.0,
            "accountId": "acc-1",
            "accountName": "Checking Account",
        },
    ]

    client.upload_account_balance_history.return_value = True

    client.get_institutions.return_value = _INSTITUTIONS_RESPONSE
    client.get_credit_history.return_value = _CREDIT_HISTORY_RESPONSE
    client.get_recent_account_balances.return_value = _RECENT_BALANCES_RESPONSE
    client.find_duplicate_transactions.return_value = _DUPLICATES_RESPONSE

    return client


_TOOL_MODULES = [
    "monarch_mcp_server.client",
    "monarch_mcp_server.tools.auth",
    "monarch_mcp_server.tools.accounts",
    "monarch_mcp_server.tools.transactions",
    "monarch_mcp_server.tools.summaries",
    "monarch_mcp_server.tools.splits",
    "monarch_mcp_server.tools.tags",
    "monarch_mcp_server.tools.rules",
    "monarch_mcp_server.tools.categories",
    "monarch_mcp_server.tools.budgets",
    "monarch_mcp_server.tools.financial",
    "monarch_mcp_server.tools.merchants",
]


@pytest.fixture(autouse=True)
def patch_monarch_client(mock_monarch_client):
    """Automatically patch get_monarch_client wherever it's imported."""
    patchers = []
    for module_path in _TOOL_MODULES:
        try:
            p = patch(
                f"{module_path}.get_monarch_client",
                new_callable=AsyncMock,
                return_value=mock_monarch_client,
            )
            p.start()
            patchers.append(p)
        except (AttributeError, ModuleNotFoundError):
            pass
    try:
        yield mock_monarch_client
    finally:
        for p in patchers:
            p.stop()


@contextlib.contextmanager
def load_script(path):
    """Import a standalone script by path, leaving sys.path as we found it.

    login_setup.py is a script, not a package member, so it has to be loaded
    this way. Executing it runs `sys.path.insert(0, .../src)` at module scope,
    and that insert is unconditional -- so loading it once per test grows
    sys.path without bound and can make unrelated tests order-dependent.
    """
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(pathlib.Path(path).stem, path)
        if spec is None or spec.loader is None:
            # spec_from_file_location returns None for a path it has no loader
            # for -- an unrecognised extension, say. Reaching through that gives
            # an opaque "NoneType has no attribute loader" instead of naming the
            # file that could not be loaded.
            raise ImportError(f"cannot load {path} as a Python module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = saved


LOGIN_SETUP = pathlib.Path(__file__).resolve().parent.parent / "login_setup.py"


@pytest.fixture
def login_setup():
    with load_script(LOGIN_SETUP) as module:
        yield module
