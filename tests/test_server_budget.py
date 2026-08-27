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

    start = query_text.index("categoryGroups {")
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
