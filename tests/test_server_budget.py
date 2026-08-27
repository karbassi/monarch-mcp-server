import re

from monarch_mcp_server.tools.budgets import BUDGET_QUERY, format_budget_data


def test_budget_query_avoids_stale_category_group_fields():
    """categoryGroups must not request fields Monarch removed from it.

    Scoped to the categoryGroups selection rather than banning the tokens
    document-wide. budgetVariability is a *valid* field on
    monthlyAmountsForFlexExpense -- confirmed against the live API, where it
    returns "flexible" -- so a blanket substring check rejected a legitimate
    selection while claiming to guard category groups.

    gql 4.0 returns a GraphQLRequest wrapping the parsed DocumentNode; the
    source string lives on document.loc.source.body. Earlier gql 3.x exposed
    .loc directly on the gql() return value.
    """
    query_text = BUDGET_QUERY.document.loc.source.body

    opening = re.search(r"categoryGroups\s*\{", query_text)
    assert (
        opening is not None
    ), f"no categoryGroups selection found in the query:\n{query_text}"
    start = opening.start()
    depth, end = 0, None
    for i, char in enumerate(query_text[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end is not None, "could not find the end of the categoryGroups selection"
    category_groups_selection = query_text[start:end]

    for stale in ("budgetVariability", "rolloverPeriod"):
        assert stale not in category_groups_selection, (
            f"{stale} was removed from categoryGroups by Monarch; "
            f"selecting it fails the whole query"
        )


def test_format_budget_data_returns_current_month_category_rows():
    raw_budget_data = {
        "budgetData": {
            "monthlyAmountsByCategory": [
                {
                    "category": {"id": "cat-1"},
                    "monthlyAmounts": [
                        {
                            "month": "2026-06-01",
                            "plannedCashFlowAmount": -100,
                            "plannedSetAsideAmount": 0,
                            "actualAmount": -25,
                            "remainingAmount": -75,
                        }
                    ],
                }
            ]
        },
        "categoryGroups": [
            {
                "name": "Food",
                "categories": [{"id": "cat-1", "name": "Groceries"}],
            }
        ],
    }

    assert format_budget_data(raw_budget_data) == [
        {
            "id": "cat-1",
            "name": "Groceries",
            "planned": -100,
            "actual": -25,
            "remaining": -75,
            "category_group": "Food",
            "month": "2026-06-01",
        }
    ]


def test_budget_query_selects_everything_the_formatter_reads():
    """The query must request the fields get_budgets formats.

    This exists because of a mistake made while adding the flex support: the
    formatter was extended first and the tests passed against a hand-written
    fixture, while the live call returned nothing because BUDGET_QUERY never
    selected the fields. Fixture-driven tests cannot catch that -- they never
    see the query -- so assert the selection directly.
    """
    query_text = BUDGET_QUERY.document.loc.source.body

    required = [
        # budget-level data, none of it per-category
        "monthlyAmountsForFlexExpense",
        "budgetVariability",
        "totalsByMonth",
        "budgetSystem",
        # the five monthly buckets the formatter maps
        "totalIncome",
        "totalExpenses",
        "totalFixedExpenses",
        "totalFlexibleExpenses",
        "totalNonMonthlyExpenses",
        # the BudgetTotals scalars each bucket is unwrapped into
        "plannedAmount",
        "actualAmount",
        "remainingAmount",
        "previousMonthRolloverAmount",
    ]
    # Whole-token match: a plain substring check cannot tell
    # `monthlyAmountsForFlexExpense` from `monthlyAmountsForFlexExpenseXX`, which
    # is how a first version of this test passed against a renamed field.
    missing = [
        field
        for field in required
        if not re.search(rf"\b{re.escape(field)}\b(?![A-Za-z0-9_])", query_text)
    ]
    assert not missing, (
        f"BUDGET_QUERY does not select {missing}; get_budgets formats these, so "
        f"a live call would return them as null while fixture tests still pass"
    )
