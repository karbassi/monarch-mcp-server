"""Tests for budget-related MCP tools."""

import json

from monarch_mcp_server.tools.budgets import get_budgets


class TestGetBudgets:
    async def test_returns_formatted_category_rows(self):
        """Category rows moved under `categories` when budget-level data was
        added; the rows themselves are unchanged. See
        TestFlexibleBudgetIsSurfaced for why the shape had to change."""
        result = json.loads(await get_budgets())
        assert isinstance(result, dict)
        assert len(result["categories"]) == 2
        groceries = next(row for row in result["categories"] if row["id"] == "cat-1")
        assert groceries == {
            "id": "cat-1",
            "name": "Groceries",
            "planned": 500.00,
            "actual": 320.00,
            "remaining": 180.00,
            "category_group": "Food",
            "month": "2026-03-01",
        }

    async def test_passes_explicit_date_params(self, mock_monarch_client):
        await get_budgets(start_date="2026-03-01", end_date="2026-03-31")
        _, kwargs = mock_monarch_client.gql_call.call_args
        assert kwargs["variables"] == {
            "startDate": "2026-03-01",
            "endDate": "2026-03-31",
        }

    async def test_defaults_to_current_month(self, mock_monarch_client):
        from monarch_mcp_server.tools.budgets import current_month_range

        start, end = current_month_range()
        await get_budgets()
        _, kwargs = mock_monarch_client.gql_call.call_args
        assert kwargs["variables"] == {"startDate": start, "endDate": end}

    async def test_handles_api_error(self, mock_monarch_client):
        mock_monarch_client.gql_call.side_effect = Exception("Budget error")
        result = await get_budgets()
        assert "get_budgets" in result


class TestFlexibleBudgetIsSurfaced:
    """get_budgets discarded everything that is not per-category.

    The response already carries the flex bucket
    (``monthlyAmountsForFlexExpense``), the per-month fixed/flexible/non-monthly
    splits (``totalsByMonth``) and the account's ``budgetSystem``. A flat list of
    category rows structurally cannot express any of them, since none are
    per-category -- so the response shape had to change.

    Verified against the live API: this account's budgetSystem is
    ``fixed_and_flex`` and the payload contains ``totalFlexibleExpenses``, so
    this was real data being dropped rather than a hypothetical gap.
    """

    async def test_response_carries_budget_level_data(self):
        result = json.loads(await get_budgets())

        assert isinstance(result, dict), "flat list cannot carry non-category data"
        assert set(result) >= {
            "categories",
            "budget_system",
            "flex_expense",
            "totals_by_month",
        }

    async def test_category_rows_are_unchanged(self):
        """The previous return value is preserved verbatim under `categories`."""
        result = json.loads(await get_budgets())

        assert isinstance(result["categories"], list)
        assert len(result["categories"]) == 2
        groceries = next(r for r in result["categories"] if r["id"] == "cat-1")
        assert groceries == {
            "id": "cat-1",
            "name": "Groceries",
            "planned": 500.00,
            "actual": 320.00,
            "remaining": 180.00,
            "category_group": "Food",
            "month": "2026-03-01",
        }

    async def test_flex_bucket_is_surfaced(self):
        result = json.loads(await get_budgets())

        flex = result["flex_expense"]
        assert flex["budget_variability"] == "flexible"
        assert flex["months"] == [
            {
                "month": "2026-03-01",
                "planned": 700.00,
                "actual": 505.00,
                "remaining": 195.00,
                "rollover": 0.00,
            }
        ]

    async def test_monthly_totals_split_fixed_from_flexible(self):
        result = json.loads(await get_budgets())

        totals = result["totals_by_month"]
        assert len(totals) == 1
        assert totals[0]["month"] == "2026-03-01"
        # Each bucket is planned/actual/remaining/rollover, not a bare number.
        assert totals[0]["flexible_expenses"] == {
            "planned": 700.00,
            "actual": 505.00,
            "remaining": 195.00,
            "rollover": 0.00,
        }
        assert totals[0]["fixed_expenses"]["planned"] == 900.00
        assert totals[0]["income"]["actual"] == 4800.00
        assert totals[0]["non_monthly_expenses"]["remaining"] == 85.00

    async def test_budget_system_is_reported(self):
        result = json.loads(await get_budgets())
        assert result["budget_system"] == "fixed_and_flex"

    async def test_degrades_when_the_account_has_no_flex_budget(
        self, mock_monarch_client
    ):
        """An account on a fixed-only budget has none of this. It must come back
        empty rather than raising -- the fallback the issue asked for."""
        mock_monarch_client.gql_call.return_value = {
            "budgetData": {
                "monthlyAmountsByCategory": [],
                # No monthlyAmountsForFlexExpense, no totalsByMonth.
            },
            "categoryGroups": [],
            "budgetSystem": "fixed",
        }

        result = json.loads(await get_budgets())

        assert result["budget_system"] == "fixed"
        assert result["flex_expense"] is None
        assert result["totals_by_month"] == []
        assert result["categories"] == []
