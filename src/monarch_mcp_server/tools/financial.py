"""Financial analytics tools (cashflow, net worth)."""

import logging
from datetime import datetime as dt
from typing import Any, Dict, Optional

from monarch_mcp_server.app import mcp
from monarch_mcp_server.client import get_monarch_client
from monarch_mcp_server.helpers import json_error, json_success

logger = logging.getLogger(__name__)


@mcp.tool()
async def get_cashflow(
    start_date: Optional[str] = None, end_date: Optional[str] = None
) -> str:
    """
    Get cashflow analysis from Monarch Money.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
    """
    try:
        client = await get_monarch_client()

        filters: Dict[str, Any] = {}
        if start_date:
            filters["start_date"] = start_date
        if end_date:
            filters["end_date"] = end_date

        cashflow = await client.get_cashflow(**filters)
        return json_success(cashflow)
    except Exception as e:
        return json_error("get_cashflow", e)


@mcp.tool()
async def get_net_worth(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_type: Optional[str] = None,
) -> str:
    """
    Get net worth history over time.

    Returns daily snapshots of total net worth, useful for tracking wealth trends.

    Args:
        start_date: Start date in YYYY-MM-DD format (defaults to account history start)
        end_date: End date in YYYY-MM-DD format (defaults to today)
        account_type: Filter by account type (e.g., "brokerage", "depository", "credit")

    Returns:
        Daily net worth snapshots with dates and values.

    Examples:
        Get net worth for the past year:
            get_net_worth(start_date="2024-01-01")

        Get only investment account net worth:
            get_net_worth(account_type="brokerage")
    """
    try:
        client = await get_monarch_client()

        params: Dict[str, Any] = {}
        # Pass ISO strings directly; upstream serializes via gql JSON and
        # cannot handle datetime.date objects in GraphQL variables.
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if account_type:
            params["account_type"] = account_type

        result = await client.get_aggregate_snapshots(**params)

        snapshots = result.get("aggregateSnapshots", [])

        formatted: Dict[str, Any] = {"snapshot_count": len(snapshots), "snapshots": []}

        if snapshots:
            values = [
                s.get("balance", 0) for s in snapshots if s.get("balance") is not None
            ]
            if values:
                formatted["current_net_worth"] = values[-1] if values else 0
                formatted["earliest_net_worth"] = values[0] if values else 0
                formatted["change"] = values[-1] - values[0] if len(values) > 1 else 0
                formatted["change_percent"] = (
                    ((values[-1] - values[0]) / values[0] * 100)
                    if values[0] != 0 and len(values) > 1
                    else 0
                )
                formatted["highest"] = max(values)
                formatted["lowest"] = min(values)

        for snapshot in snapshots[-365:]:
            formatted["snapshots"].append(
                {
                    "date": snapshot.get("date"),
                    "net_worth": snapshot.get("balance"),
                }
            )

        return json_success(formatted)
    except Exception as e:
        return json_error("get_net_worth", e)


@mcp.tool()
async def get_net_worth_by_account_type(
    start_date: str,
    timeframe: str = "month",
) -> str:
    """
    Get net worth breakdown by account type over time.

    Shows how net worth is distributed across different account types
    (checking, savings, investments, credit cards, etc.) with monthly or yearly granularity.

    Args:
        start_date: Start date in YYYY-MM-DD format
        timeframe: Granularity - "month" or "year" (default: "month")

    Returns:
        Net worth snapshots grouped by account type.

    Examples:
        Get monthly breakdown for the past year:
            get_net_worth_by_account_type(start_date="2024-01-01", timeframe="month")

        Get yearly breakdown:
            get_net_worth_by_account_type(start_date="2020-01-01", timeframe="year")
    """
    try:
        if timeframe not in ("month", "year"):
            return json_success(
                {"success": False, "error": "timeframe must be 'month' or 'year'"}
            )

        client = await get_monarch_client()
        result = await client.get_account_snapshots_by_type(
            start_date=start_date,
            timeframe=timeframe,
        )

        # Upstream returns a flat list under key "snapshotsByAccountType"
        # with shape [{"accountType": str, "month": "YYYY-MM" or "YYYY", "balance": float}, ...]
        rows = result.get("snapshotsByAccountType", [])

        formatted: Dict[str, Any] = {
            "timeframe": timeframe,
            "start_date": start_date,
            "account_types": [],
        }

        # Group flat rows by accountType, preserving order of first appearance.
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            atype = row.get("accountType")
            if atype is None:
                continue
            entry = grouped.setdefault(atype, {"type": atype, "snapshots": []})
            entry["snapshots"].append(
                {
                    "month": row.get("month"),
                    "balance": row.get("balance"),
                }
            )

        for type_info in grouped.values():
            if type_info["snapshots"]:
                type_info["current_balance"] = type_info["snapshots"][-1].get(
                    "balance", 0
                )
            formatted["account_types"].append(type_info)

        total = sum(
            t.get("current_balance", 0)
            for t in formatted["account_types"]
            if t.get("current_balance") is not None
        )
        formatted["total_net_worth"] = total

        return json_success(formatted)
    except Exception as e:
        return json_error("get_net_worth_by_account_type", e)


@mcp.tool()
async def get_credit_history() -> str:
    """
    Get credit score history as reported to Monarch.

    Returns:
        Score snapshots oldest-first, plus the change across the series and the
        tracking status (which explains an empty series).
    """
    try:
        client = await get_monarch_client()
        data = await client.get_credit_history()

        snapshots = sorted(
            (
                {"date": snap.get("reportedDate"), "score": snap.get("score")}
                for snap in (data.get("creditScoreSnapshots") or [])
                if snap.get("score") is not None
            ),
            key=lambda s: s["date"] or "",
        )

        change = None
        if len(snapshots) >= 2:
            change = snapshots[-1]["score"] - snapshots[0]["score"]

        spinwheel = data.get("spinwheelUser") or {}
        return json_success(
            {
                "snapshots": snapshots,
                "latest_score": snapshots[-1]["score"] if snapshots else None,
                "change_over_series": change,
                # Explains an empty series rather than leaving the caller guessing.
                "tracking_status": spinwheel.get("creditScoreTrackingStatus"),
                "onboarding_status": spinwheel.get("onboardingStatus"),
                "onboarding_error": spinwheel.get("onboardingErrorMessage"),
            }
        )
    except Exception as e:
        return json_error("get_credit_history", e)
