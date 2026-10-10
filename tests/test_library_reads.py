"""Read-only tools wrapping library capabilities the server did not expose.

Each corresponds to a monarchmoneycommunity method that was already available
and already paid for. Shapes in the fixtures mirror real API responses; the
assertions here cover the shaping the wrappers do on top.
"""

import json
from unittest.mock import patch

import pytest

from monarch_mcp_server.tools.accounts import (
    get_institutions,
    get_recent_account_balances,
)
from monarch_mcp_server.tools.financial import get_credit_history
from monarch_mcp_server.tools.transactions import find_duplicate_transactions


class TestGetInstitutions:
    """The point of this tool is spotting connections that stopped refreshing."""

    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_flags_connections_needing_attention(
        self, mock_get, mock_monarch_client
    ):
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_institutions())

        by_institution = {c["institution"]: c for c in data["connections"]}
        assert by_institution["Test Bank"]["needs_attention"] is False
        # updateRequired and a disconnect are different signals; both must count.
        assert by_institution["Broken Bank"]["needs_attention"] is True
        assert by_institution["Gone Bank"]["needs_attention"] is True
        assert data["connections_needing_attention"] == 2

    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_groups_accounts_under_their_credential(
        self, mock_get, mock_monarch_client
    ):
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_institutions())

        test_bank = next(
            c for c in data["connections"] if c["institution"] == "Test Bank"
        )
        names = [a["name"] for a in test_bank["accounts"]]
        assert names == ["Checking Account"], "deleted accounts must be excluded"

    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_manual_accounts_are_reported_separately(
        self, mock_get, mock_monarch_client
    ):
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_institutions())

        assert [a["name"] for a in data["manual_accounts"]] == ["Cash"]

    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_errors_are_reported_not_raised(self, mock_get):
        mock_get.side_effect = RuntimeError("API down")
        data = json.loads(await get_institutions())
        assert data["error"] is True
        assert "API down" in data["message"]


class TestGetCreditHistory:
    @patch("monarch_mcp_server.tools.financial.get_monarch_client")
    async def test_snapshots_are_ordered_and_summarised(
        self, mock_get, mock_monarch_client
    ):
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_credit_history())

        # Fixture order is 08, 06, 07 -- output must be chronological.
        assert [s["date"] for s in data["snapshots"]] == ["2026-06-01", "2026-08-01"]
        assert data["latest_score"] == 760
        assert data["change_over_series"] == 20

    @patch("monarch_mcp_server.tools.financial.get_monarch_client")
    async def test_scoreless_snapshots_are_dropped(self, mock_get, mock_monarch_client):
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_credit_history())
        assert all(s["score"] is not None for s in data["snapshots"])

    @patch("monarch_mcp_server.tools.financial.get_monarch_client")
    async def test_tracking_status_is_surfaced(self, mock_get, mock_monarch_client):
        """An empty series is usually a tracking problem, not a scoreless user."""
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_credit_history())
        assert data["tracking_status"] == "ACTIVE"

    @patch("monarch_mcp_server.tools.financial.get_monarch_client")
    async def test_single_snapshot_has_no_change(self, mock_get, mock_monarch_client):
        mock_monarch_client.get_credit_history.return_value = {
            "creditScoreSnapshots": [{"reportedDate": "2026-08-01", "score": 700}],
            "spinwheelUser": {},
        }
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_credit_history())
        assert data["latest_score"] == 700
        assert data["change_over_series"] is None


class TestGetRecentAccountBalances:
    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_returns_a_series_per_account(self, mock_get, mock_monarch_client):
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_recent_account_balances(start_date="2026-08-01"))

        assert data["start_date"] == "2026-08-01"
        by_id = {a["id"]: a for a in data["accounts"]}
        assert by_id["acc-1"]["balance_count"] == 3
        assert by_id["acc-2"]["balance_count"] == 0

    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_start_date_is_forwarded(self, mock_get, mock_monarch_client):
        mock_get.return_value = mock_monarch_client
        await get_recent_account_balances(start_date="2026-01-15")
        kwargs = mock_monarch_client.get_recent_account_balances.call_args.kwargs
        assert kwargs["start_date"] == "2026-01-15"


class TestFindDuplicateTransactions:
    @patch("monarch_mcp_server.tools.transactions.get_monarch_client")
    async def test_reports_groups_with_member_ids(self, mock_get, mock_monarch_client):
        mock_get.return_value = mock_monarch_client
        data = json.loads(
            await find_duplicate_transactions(
                start_date="2026-08-01", end_date="2026-08-26"
            )
        )

        assert data["group_count"] == 1
        group = data["duplicate_groups"][0]
        assert group["count"] == 2
        assert group["transaction_ids"] == ["txn-a", "txn-b"]
        assert group["statement_name"] == "COFFEE SHOP"

    @patch("monarch_mcp_server.tools.transactions.get_monarch_client")
    async def test_dates_are_forwarded(self, mock_get, mock_monarch_client):
        mock_get.return_value = mock_monarch_client
        await find_duplicate_transactions(
            start_date="2026-01-01", end_date="2026-02-01"
        )
        kwargs = mock_monarch_client.find_duplicate_transactions.call_args.kwargs
        assert kwargs == {"start_date": "2026-01-01", "end_date": "2026-02-01"}

    @patch("monarch_mcp_server.tools.transactions.get_monarch_client")
    async def test_no_duplicates_is_not_an_error(self, mock_get, mock_monarch_client):
        mock_monarch_client.find_duplicate_transactions.return_value = []
        mock_get.return_value = mock_monarch_client
        data = json.loads(await find_duplicate_transactions())
        assert data["group_count"] == 0
        assert data["duplicate_groups"] == []


class TestInstitutionAccountNaming:
    """Account names must not come back null when displayName is absent.

    get_accounts in the same module falls back to `name`; get_institutions did
    not. The fallback is defensive rather than currently load-bearing -- the
    institutions query does not select `name` at all today (verified against the
    live API: the account keys are __typename, credential, deletedAt,
    displayName, id, mask, subtype) -- but the cost is one `or` and it stops a
    selection-set change from silently producing nulls.
    """

    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_falls_back_to_name(self, mock_get, mock_monarch_client):
        mock_monarch_client.get_institutions.return_value = {
            "accounts": [
                {
                    "id": "acc-1",
                    "name": "Fallback Name",
                    "subtype": {"name": "checking", "display": "Checking"},
                    "credential": {"id": "cred-1"},
                    "deletedAt": None,
                }
            ],
            "credentials": [
                {
                    "id": "cred-1",
                    "institution": {"name": "Test Bank"},
                    "dataProvider": "PLAID",
                    "displayLastUpdatedAt": "2026-08-25",
                    "updateRequired": False,
                    "disconnectedFromDataProviderAt": None,
                }
            ],
        }
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_institutions())

        names = [a["name"] for c in data["connections"] for a in c["accounts"]]
        assert names == ["Fallback Name"]

    @patch("monarch_mcp_server.tools.accounts.get_monarch_client")
    async def test_display_name_still_wins(self, mock_get, mock_monarch_client):
        mock_monarch_client.get_institutions.return_value["accounts"][0]["name"] = "Raw"
        mock_get.return_value = mock_monarch_client
        data = json.loads(await get_institutions())

        test_bank = next(
            c for c in data["connections"] if c["institution"] == "Test Bank"
        )
        assert [a["name"] for a in test_bank["accounts"]] == ["Checking Account"]
