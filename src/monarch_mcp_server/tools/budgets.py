"""Budget tools."""

import calendar
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from gql import gql
from monarchmoney import MonarchMoney

from monarch_mcp_server.app import mcp
from monarch_mcp_server.client import get_monarch_client
from monarch_mcp_server.helpers import json_error, json_success

logger = logging.getLogger(__name__)

# The upstream SDK's get_budgets() requests category-group fields (e.g.
# budgetVariability/rolloverPeriod) that Monarch's current API rejects for some
# accounts, so it can fail outright. This narrower query asks only for fields
# the current API still returns.
BUDGET_QUERY = gql(
    """
    fragment budgetTotals on BudgetTotals {
      plannedAmount
      actualAmount
      remainingAmount
      previousMonthRolloverAmount
      __typename
    }

    query MCPBudgetData($startDate: Date!, $endDate: Date!) {
      budgetData(startMonth: $startDate, endMonth: $endDate) {
        monthlyAmountsByCategory {
          category {
            id
            __typename
          }
          monthlyAmounts {
            month
            plannedCashFlowAmount
            plannedSetAsideAmount
            actualAmount
            remainingAmount
            __typename
          }
          __typename
        }
        monthlyAmountsForFlexExpense {
          budgetVariability
          monthlyAmounts {
            month
            plannedCashFlowAmount
            actualAmount
            remainingAmount
            previousMonthRolloverAmount
            __typename
          }
          __typename
        }
        totalsByMonth {
          month
          totalIncome { ...budgetTotals }
          totalExpenses { ...budgetTotals }
          totalFixedExpenses { ...budgetTotals }
          totalFlexibleExpenses { ...budgetTotals }
          totalNonMonthlyExpenses { ...budgetTotals }
          __typename
        }
        __typename
      }
      budgetSystem
      categoryGroups {
        id
        name
        type
        categories {
          id
          name
          __typename
        }
        __typename
      }
    }
    """
)


def current_month_range() -> tuple[str, str]:
    """Return the current month bounds as ISO date strings."""
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1).isoformat(), today.replace(day=last_day).isoformat()


async def get_budget_data(
    client: MonarchMoney,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch budget data using fields supported by Monarch's current API."""
    default_start, default_end = current_month_range()
    return await client.gql_call(
        operation="MCPBudgetData",
        graphql_query=BUDGET_QUERY,
        variables={
            "startDate": start_date or default_start,
            "endDate": end_date or default_end,
        },
    )


def format_budget_data(budget_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Format Monarch budget data into one row per category/month."""
    category_lookup: Dict[str, Dict[str, Optional[str]]] = {}
    for group in budget_data.get("categoryGroups", []):
        for category in group.get("categories", []):
            category_id = category.get("id")
            if category_id:
                category_lookup[category_id] = {
                    "name": category.get("name"),
                    "category_group": group.get("name"),
                }

    budget_rows = []
    monthly_by_category = budget_data.get("budgetData", {}).get(
        "monthlyAmountsByCategory", []
    )
    for category_budget in monthly_by_category:
        category_id = (category_budget.get("category") or {}).get("id")
        category_info = category_lookup.get(category_id, {})
        for monthly_amount in category_budget.get("monthlyAmounts", []):
            budget_rows.append(
                {
                    "id": category_id,
                    "name": category_info.get("name"),
                    "planned": monthly_amount.get("plannedCashFlowAmount"),
                    "actual": monthly_amount.get("actualAmount"),
                    "remaining": monthly_amount.get("remainingAmount"),
                    "category_group": category_info.get("category_group"),
                    "month": monthly_amount.get("month"),
                }
            )

    return budget_rows


def _format_month_amounts(
    monthly_amounts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalise a monthlyAmounts list to the same keys used for category rows."""
    return [
        {
            "month": amount.get("month"),
            "planned": amount.get("plannedCashFlowAmount"),
            "actual": amount.get("actualAmount"),
            "remaining": amount.get("remainingAmount"),
            "rollover": amount.get("previousMonthRolloverAmount"),
        }
        for amount in monthly_amounts or []
    ]


def format_flex_expense(budget_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The flexible-spending bucket, or None on a fixed-only budget.

    Monarch's fixed-and-flex system tracks a single flexible pool alongside the
    per-category budgets. It is not a category, so it has no row in
    format_budget_data -- which is why it was being dropped entirely.
    """
    flex = (budget_data.get("budgetData") or {}).get("monthlyAmountsForFlexExpense")
    if not flex:
        return None
    return {
        "budget_variability": flex.get("budgetVariability"),
        "months": _format_month_amounts(flex.get("monthlyAmounts")),
    }


#: Monarch returns each monthly total as a BudgetTotals object, not a scalar --
#: planned versus actual is the whole point of a budget total.
_TOTALS_FIELDS = {
    "income": "totalIncome",
    "expenses": "totalExpenses",
    "fixed_expenses": "totalFixedExpenses",
    "flexible_expenses": "totalFlexibleExpenses",
    "non_monthly_expenses": "totalNonMonthlyExpenses",
}


def _format_budget_totals(totals: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not totals:
        return None
    return {
        "planned": totals.get("plannedAmount"),
        "actual": totals.get("actualAmount"),
        "remaining": totals.get("remainingAmount"),
        "rollover": totals.get("previousMonthRolloverAmount"),
    }


def format_totals_by_month(budget_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-month income and the fixed / flexible / non-monthly expense split.

    The split is the point of a fixed-and-flex budget and cannot be
    reconstructed from the category rows, since non-monthly expenses and the
    flex pool are not categories.
    """
    totals = (budget_data.get("budgetData") or {}).get("totalsByMonth") or []
    return [
        {
            "month": total.get("month"),
            **{
                out_key: _format_budget_totals(total.get(api_key))
                for out_key, api_key in _TOTALS_FIELDS.items()
            },
        }
        for total in totals
    ]


@mcp.tool()
async def get_budgets(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Get budget information from Monarch Money.

    Args:
        start_date: Start month in YYYY-MM-DD format (defaults to the current month)
        end_date: End month in YYYY-MM-DD format (defaults to the current month)

    Returns:
        A JSON object with:

        ``categories``
            One row per budgeted category per month, each with ``id``, ``name``,
            ``planned`` (planned cash-flow amount), ``actual``, ``remaining``,
            ``category_group`` and ``month`` (YYYY-MM-DD).
        ``budget_system``
            Monarch's budgeting mode, e.g. ``fixed_and_flex`` or ``fixed``.
        ``flex_expense``
            The flexible-spending pool, or ``null`` on a fixed-only budget.
        ``totals_by_month``
            Per-month income and the fixed / flexible / non-monthly expense
            split, which cannot be derived from ``categories``.

        This replaced a bare list of category rows. The flex pool, the monthly
        totals and the budget system are all budget-level rather than
        per-category, so a flat list could not carry them -- and Monarch was
        returning all three already.
    """
    try:
        client = await get_monarch_client()
        budget_data = await get_budget_data(client, start_date, end_date)
        return json_success(
            {
                "categories": format_budget_data(budget_data),
                "budget_system": budget_data.get("budgetSystem"),
                "flex_expense": format_flex_expense(budget_data),
                "totals_by_month": format_totals_by_month(budget_data),
            }
        )
    except Exception as e:
        return json_error("get_budgets", e)


@mcp.tool()
async def set_budget_amount(
    amount: float,
    category_id: Optional[str] = None,
    category_group_id: Optional[str] = None,
    start_date: Optional[str] = None,
    apply_to_future: bool = False,
) -> str:
    """
    Set or update a budget amount for a category or category group.

    Use get_budgets() first to see current budgets and category IDs.
    Use get_categories() or get_category_groups() to find category/group IDs.

    Args:
        amount: The budget amount to set. Use 0 to clear/unset the budget.
        category_id: The ID of the category to budget (cannot use with category_group_id)
        category_group_id: The ID of the category group to budget (cannot use with category_id)
        start_date: The month to set budget for in YYYY-MM-DD format (defaults to current month)
        apply_to_future: Whether to apply this amount to all future months (default: False)

    Returns:
        Result of the budget update.

    Examples:
        Set grocery budget to $600 for current month:
            set_budget_amount(amount=600, category_id="cat_groceries_123")

        Set dining budget to $200 and apply to all future months:
            set_budget_amount(amount=200, category_id="cat_dining_456", apply_to_future=True)

        Clear a budget (set to 0):
            set_budget_amount(amount=0, category_id="cat_123")
    """
    try:
        if category_id and category_group_id:
            return json_success(
                {
                    "success": False,
                    "error": "Cannot specify both category_id and category_group_id. Choose one.",
                }
            )

        if not category_id and not category_group_id:
            return json_success(
                {
                    "success": False,
                    "error": "Must specify either category_id or category_group_id.",
                }
            )

        client = await get_monarch_client()

        params: Dict[str, Any] = {
            "amount": amount,
            "apply_to_future": apply_to_future,
        }

        if category_id:
            params["category_id"] = category_id
        if category_group_id:
            params["category_group_id"] = category_group_id
        if start_date:
            params["start_date"] = start_date

        result = await client.set_budget_amount(**params)

        return json_success(
            {
                "success": True,
                "message": f"Budget set to ${amount:.2f}"
                + (" for all future months" if apply_to_future else ""),
                "result": result,
            }
        )
    except Exception as e:
        return json_error("set_budget_amount", e)
