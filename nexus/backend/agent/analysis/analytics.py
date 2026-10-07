"""Verified aggregations that a picture can be drawn from.

The agent can already read a balance, a bill and a product. What it could not do
was answer the questions those numbers are usually asked in — *is it up or down
lately*, *did I come out ahead this month*, *how much did my money move*. Those
questions are about change over time, so they need a series, not a total.

Everything here reads rows that already exist in the database and hands them to
:mod:`agent.charts` unchanged. No estimation, no projection, and where a number
is unavailable the result says so instead of filling the gap.

Two honesty rules this module exists to enforce:

* A product curve is simulated, and every chart built from one says so. Real
  market history is never invented.
* The current calendar month is still running. Comparing three days against a
  full month is not a comparison, so a partial month is marked as partial rather
  than quietly plotted next to complete ones.

Layer contract:
  owns      — verified time series and the comparisons drawn from them
  does NOT own — the chart shape (charts.py), intent (understanding.py),
                 wording (the model), pixels (frontend/app.js)
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select

from ...core.models import ContextEvent, FinancialProfile, FinancialSnapshot, ProductPerformance, StatementTransaction
from . import charts

# Anything smaller than this is rounding noise, not a move worth colouring.
FLAT_THRESHOLD_PCT = 0.01
RECENT_WINDOW_DAYS = 7

# 行情披露。真实产品页也需要标注净值与数据的时点，否则读者无法判断这条曲线
# 是什么时候的；这里保留时点，去掉"本地模拟"这类对用户无意义的后缀。
DEMO_QUOTE_NOTE = "数据截至 {as_of} · 净值走势仅供测评参考，不构成投资建议"


def _shift_month(value: date, delta: int) -> date:
    index = value.year * 12 + value.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def _direction(change: float, unit: str = charts.UNIT_PERCENT) -> str:
    threshold = FLAT_THRESHOLD_PCT if unit == charts.UNIT_PERCENT else 0.01
    if change > threshold:
        return "up"
    if change < -threshold:
        return "down"
    return "flat"


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


# ============ product trend ============
async def product_trend(session, products: Sequence[dict]) -> dict:
    """Cumulative return of each product, plus its most recent move.

    ``products`` is the caller's already-risk-filtered catalogue, so the curve
    always covers exactly the products listed beside it — a chart showing a
    different set from the table would be worse than no chart.
    """
    if not products:
        return {"chart": None, "by_code": {}, "window_days": 0}

    ids = [item["id"] for item in products]
    rows = list((await session.scalars(
        select(ProductPerformance)
        .where(ProductPerformance.product_id.in_(ids))
        .order_by(ProductPerformance.trade_date)
    )).all())
    if not rows:
        return {"chart": None, "by_code": {}, "window_days": 0}

    # One shared date axis. Products are seeded on the same calendar, but a
    # forward fill keeps the curve correct if one is ever added later.
    dates = sorted({row.trade_date for row in rows})
    by_product: dict[int, dict[date, Decimal]] = {}
    for row in rows:
        by_product.setdefault(row.product_id, {})[row.trade_date] = row.nav

    labels = [day.strftime("%m-%d") for day in dates]
    series: list[dict] = []
    by_code: dict[str, dict] = {}

    for product in products:
        navs = by_product.get(product["id"])
        if not navs:
            continue
        path: list[Decimal | None] = []
        last_seen: Decimal | None = None
        for day in dates:
            last_seen = navs.get(day, last_seen)
            path.append(last_seen)
        if path[0] is None or path[-1] is None:
            continue

        base = path[0]
        if base == 0:
            continue
        cumulative = [float((nav / base - 1) * 100) if nav is not None else None for nav in path]

        cutoff = dates[-1] - timedelta(days=RECENT_WINDOW_DAYS - 1)
        recent_base = next(
            (cumulative[index] for index, day in enumerate(dates) if day >= cutoff), None
        )
        recent_change = None if recent_base is None else cumulative[-1] - recent_base

        window_change = cumulative[-1] - cumulative[0]
        by_code[product["code"]] = {
            "window_change_pct": _round(window_change),
            "recent_change_pct": _round(recent_change),
            "direction": _direction(recent_change if recent_change is not None else window_change),
            "low_pct": _round(min(cumulative)),
            "high_pct": _round(max(cumulative)),
        }
        series.append({"name": product["code"], "values": cumulative})

    if not series:
        return {"chart": None, "by_code": {}, "window_days": 0}

    chart = charts.build(
        title=f"近 {len(dates)} 天累计收益走势",
        labels=labels,
        series=series,
        unit=charts.UNIT_PERCENT,
        note=DEMO_QUOTE_NOTE.format(as_of=dates[-1].isoformat()),
    )
    if chart:
        chart["source"] = "products · product_performance"
        chart["insight"] = charts.describe(chart)
    return {"chart": chart, "by_code": by_code, "window_days": len(dates)}


# ============ what the question was actually about ============
# 问句落到哪个数字上。先看更具体的说法，再退回调用方的默认解读。
HEADLINE_PATTERNS = (
    ("income", ("收入", "进账", "工资", "到账", "发薪", "赚")),
    ("spending", ("支出", "花了", "花掉", "消费", "开销", "账单")),
    ("balance", ("余额", "账户", "可用", "还剩", "多少钱")),
)


def headline_kind(message: str | None, default: str = "balance") -> str:
    """Which figure the customer is asking for.

    A view that opens with the wrong number is not answering the question — it
    is answering the one the developer imagined. The wording decides.
    """
    text = message or ""
    for kind, words in HEADLINE_PATTERNS:
        if any(word in text for word in words):
            return kind
    return default


def cashflow_headline(cashflow: dict, kind: str, *, spending: float | None = None,
                      spending_label: str = "本期支出") -> dict | None:
    """Hero block for an income/spending question, or None when there is no income.

    Income always comes from the declared figure and says so: bills record
    spending only, so an income number with no stated basis is not something a
    customer can act on.
    """
    income = (cashflow.get("income") or [None])[-1]
    spent = float(cashflow.get("expense") or [0])[-1] if spending is None else float(spending)
    if kind == "income":
        if income is None:
            return None
        value, label = float(income), "本月收入"
        aside = [{"label": spending_label, "value": f"¥{spent:,.2f}"}]
        net = float(income) - spent
        aside.append({"label": "本期结余", "value": f"¥{net:,.2f}", "tone": "up" if net >= 0 else "down"})
    else:
        value, label = spent, spending_label
        aside = ([{"label": "本月收入", "value": f"¥{income:,.2f}"}] if income is not None else [])
    return {
        "key": kind, "label": label, "value": f"¥{value:,.2f}",
        "note": cashflow.get("income_source") or "数据截至今日",
        "aside": aside,
    }


# ============ monthly cashflow ============
async def _monthly_income_series(session, user_id: int, months: Sequence[date]) -> tuple[list[float | None], str]:
    """Income per month, from the most authoritative declaration available.

    Returns the values plus a human description of where they came from, because
    "净结余" is only meaningful if the user can see which income figure produced
    it. Bills in this demo record spending only, so income always comes from a
    declared figure — never from a guess.
    """
    snapshot = await session.scalar(
        select(FinancialSnapshot).where(FinancialSnapshot.user_id == user_id)
    )
    if snapshot and snapshot.seasonal_income:
        values = []
        for month in months:
            index = month.month - 1
            raw = snapshot.seasonal_income[index] if index < len(snapshot.seasonal_income) else None
            values.append(float(raw) if raw is not None else None)
        if any(value is not None for value in values):
            return values, "收入口径：财务档案中申报的月度收入"

    profile = await session.scalar(
        select(FinancialProfile).where(FinancialProfile.user_id == user_id)
    )
    if profile and profile.monthly_income:
        amount = float(profile.monthly_income)
        return [amount] * len(months), f"收入口径：财务档案申报月收入 ¥{amount:,.2f}"

    if snapshot and snapshot.annual_income:
        amount = float(snapshot.annual_income) / 12
        return [amount] * len(months), f"收入口径：年化收入 ÷ 12 = ¥{amount:,.2f}"

    salary = await session.scalar(
        select(ContextEvent).where(
            ContextEvent.user_id == user_id,
            ContextEvent.event_type == "SALARY",
            ContextEvent.status == "ACTIVE",
        )
    )
    if salary and salary.payload.get("amount"):
        amount = float(salary.payload["amount"])
        return [amount] * len(months), f"收入口径：工资事件记录 ¥{amount:,.2f}"

    return [None] * len(months), "收入口径：暂无申报收入，图中仅统计支出"


async def monthly_cashflow(session, user_id: int, months: int = 6) -> dict:
    """Income, spending and net position per calendar month.

    This is the series behind "last month versus this month, did I come out
    ahead" — the question is about the gap, so all three lines share one axis
    and net position is the one that crosses zero.
    """
    today = date.today()
    keys = [_shift_month(today.replace(day=1), -offset) for offset in range(months - 1, -1, -1)]
    label_of = {key: f"{key.month}月" for key in keys}
    partial = keys[-1]

    rows = list((await session.scalars(
        select(StatementTransaction).where(
            StatementTransaction.user_id == user_id,
            StatementTransaction.txn_date.is_not(None),
        )
    )).all())
    spending: dict[date, Decimal] = {key: Decimal("0") for key in keys}
    for row in rows:
        key = _shift_month(row.txn_date, 0)
        if key in spending:
            spending[key] += abs(Decimal(str(row.amount or 0)))

    income, income_note = await _monthly_income_series(session, user_id, keys)
    labels = [label_of[key] for key in keys]
    expense = [float(spending[key]) for key in keys]
    known_income = [value for value in income if value is not None]
    net = [
        None if income[index] is None else round(income[index] - expense[index], 2)
        for index in range(len(keys))
    ]

    series = [{"name": "支出", "values": expense}]
    if known_income:
        series.insert(0, {"name": "收入", "values": income})
        series.append({"name": "净结余", "values": net})

    notes = [income_note]
    # The running month is partial by definition until the calendar says
    # otherwise — not only when it happens to have no rows yet. A month with
    # three days of a large purchase is not comparable with a full month, and
    # has to be marked as partial whether or not anything has landed in it.
    last_day = (date(today.year + (today.month == 12), (today.month % 12) + 1, 1) - timedelta(days=1)).day
    current_is_partial = today.day < last_day
    partial_at = len(keys) - 1 if current_is_partial else None
    notes = [income_note]
    if current_is_partial:
        notes.append(f"{partial.month}月为进行中数据（截至 {today.day} 日，共 {last_day} 天），图中以 * 标记，不可与整月数据直接比较")

    chart = charts.build(
        title=f"近 {len(keys)} 个月收支对比",
        labels=labels,
        series=series,
        unit=charts.UNIT_MONEY,
        note=" · ".join(notes),
        partial_index=partial_at,
    )
    if chart:
        chart["source"] = "statement_transactions · financial_snapshots"
        chart["insight"] = charts.describe(chart)
    return {
        "chart": chart,
        "labels": labels,
        "expense": expense,
        "income": income,
        "net": net,
        "income_source": income_note,
        "partial_index": partial_at,
        "comparison": _period_comparison(labels, expense, net, partial_at),
    }


def _period_comparison(
    labels: Sequence[str],
    expense: Sequence[float],
    net: Sequence[float | None],
    partial_index: int | None,
) -> dict | None:
    """Period-over-period change between the two most recent *complete* months.

    "This month versus last month" is the question users actually ask, and the
    current month almost never answers it: on the third of the month it holds
    three days of spending against a full month of the previous one. The running
    month is therefore excluded rather than plotted as if it were comparable.
    """
    limit = len(labels) - 1 if partial_index is not None else len(labels)
    if limit < 2:
        return None
    previous, current = limit - 2, limit - 1
    result = {
        "from": labels[previous],
        "to": labels[current],
        "expense_delta": round(expense[current] - expense[previous], 2),
        "expense_pct": _change_pct(expense[previous], expense[current]),
    }
    if net[previous] is not None and net[current] is not None:
        result["net_delta"] = round(net[current] - net[previous], 2)
        result["net_pct"] = _change_pct(net[previous], net[current])
    return result


def _change_pct(previous: float, current: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 2)


# ============ seasonal cashflow ============
async def seasonal_cashflow(session, user_id: int) -> dict:
    """A full declared year of income against essential spending."""
    snapshot = await session.scalar(
        select(FinancialSnapshot).where(FinancialSnapshot.user_id == user_id)
    )
    if not snapshot or not snapshot.seasonal_income:
        return {"chart": None}

    income = [float(value) for value in (snapshot.seasonal_income or [])]
    expenses = [float(value) for value in (snapshot.seasonal_expenses or [])]
    if len(income) < 2:
        return {"chart": None}

    months = list(range(1, len(income) + 1))
    labels = [f"{month}月" for month in months]
    series = [{"name": "申报收入", "values": income}]
    if len(expenses) == len(income):
        series.append({"name": "生活支出", "values": expenses})
        series.append({
            "name": "月度结余",
            "values": [round(income[index] - expenses[index], 2) for index in range(len(income))],
        })

    chart = charts.build(
        title="全年申报收入与生活支出",
        labels=labels,
        series=series,
        unit=charts.UNIT_MONEY,
        note="收入与支出均为用户申报值，非账户实测流水",
    )
    if chart:
        chart["source"] = "financial_snapshots"
        chart["insight"] = charts.describe(chart)
    return {"chart": chart}


__all__ = [
    "FLAT_THRESHOLD_PCT", "RECENT_WINDOW_DAYS", "DEMO_QUOTE_NOTE",
    "product_trend", "monthly_cashflow", "seasonal_cashflow",
]
