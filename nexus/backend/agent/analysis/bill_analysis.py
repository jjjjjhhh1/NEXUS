"""Deterministic, user-scoped bill analysis for the local banking sandbox."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median, mean, pstdev
import re

from sqlalchemy import select

from ...core.models import StatementTransaction
from . import analytics


CATEGORY_RULES = (
    ("居住", ("公寓", "房租", "物业")),
    ("餐饮", ("盒马", "便利店", "餐", "咖啡", "小馆", "超市")),
    ("交通", ("地铁", "公交", "滴滴", "铁路", "加油")),
    ("订阅", ("会员", "云音乐", "视频", "订阅")),
    ("医疗", ("医院", "药房", "诊所")),
    ("健康", ("健身", "运动")),
    ("购物", ("京东", "淘宝", "商店", "数码", "商城")),
)


# 付款附言里的用途词。真实账单里附言比商户名更能说明钱花在什么地方，
# 所以只要写了附言就以它为准，商户名只作为没有附言时的兜底。
NOTE_RULES = (
    ("居住", ("房租", "物业", "公寓", "按揭")),
    ("餐饮", ("买菜", "菜", "水果", "吃饭", "外卖", "咖啡", "聚餐", "餐厅", "盒马", "超市", "早餐", "下午茶")),
    ("交通", ("地铁", "公交", "打车", "加油", "停车", "高铁", "机票", "滴滴")),
    ("医疗", ("医院", "药", "体检", "诊所", "挂号")),
    ("健康", ("健身", "运动", "游泳")),
    ("教育", ("学费", "培训", "课程", "教材", "教育")),
    ("购物", ("日用", "数码", "衣服", "衣物", "服饰", "鞋", "家电", "商城", "购物", "母婴")),
    ("人情", ("生日", "礼物", "红包", "份子", "随礼")),
    ("旅行", ("旅行", "酒店", "门票", "旅游", "民宿")),
    ("订阅", ("会员", "订阅", "续费", "云音乐", "视频")),
)


def classify_note(note: str | None) -> str | None:
    """Category from the payer-supplied memo, or None when there is no usable memo."""
    if not note:
        return None
    for category, words in NOTE_RULES:
        if any(word in note for word in words):
            return category
    return None


def classify(merchant: str | None, stored: str | None = None) -> str:
    """Prefer a trusted imported category, otherwise use explainable merchant rules."""
    if stored:
        return stored
    value = merchant or "未知商户"
    for category, keywords in CATEGORY_RULES:
        if any(keyword in value for keyword in keywords):
            return category
    return "其他"


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _shift_month(value: date, delta: int) -> date:
    index = value.year * 12 + value.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def _money(value: Decimal) -> str:
    return f"¥{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


# Each period kind resolves to a concrete window. The model picks the kind from a
# closed vocabulary, so "上个月" is a decision rather than something re-parsed out
# of the sentence here — which is what made "那上个月呢" silently re-render the
# current month.
PERIOD_MONTH_OFFSET = {
    "month": 0,
    "last_month": -1,
    "last_3_months": -2,
}


def _elapsed_days(today: date, start: date, end: date) -> int:
    """Days actually covered by the window that have happened.

    A completed past month has to divide by its own length — dividing last
    month's spend by the days elapsed *this* month is how a daily average
    silently comes out too high.
    """
    last_day = min(end - timedelta(days=1), today)
    return max((last_day - start).days + 1, 1)


def resolve_period(period: str | None, today: date) -> tuple[date, date, str, str]:
    """Return (start, end_exclusive, label, headline_label) for a period kind.

    ``last_3_months`` is anchored so that the window always *ends* with the
    current month, matching how people say "最近三个月".
    """
    period = period if period in {"month", "last_month", "last_3_months", "year", "last_year"} else "month"
    current_month = _month_start(today)
    if period == "year":
        start, end = date(today.year, 1, 1), _shift_month(current_month, 1)
        return start, end, f"{today.year} 年度", "本期支出"
    if period == "last_year":
        start, end = date(today.year - 1, 1, 1), date(today.year, 1, 1)
        return start, end, f"{today.year - 1} 年度", "本期支出"
    anchor = _shift_month(current_month, PERIOD_MONTH_OFFSET[period])
    if period == "last_3_months":
        return anchor, _shift_month(current_month, 1), \
            f"{anchor.year} 年 {anchor.month} - {current_month.month} 月", "本期支出"
    return anchor, _shift_month(anchor, 1), \
        f"{anchor.year} 年 {anchor.month} 月", "本月支出" if period == "month" else "上月支出"


async def build_bill_analysis(session, user_id: int, period: str = "month",
                              message: str | None = None) -> dict:
    today = date.today()
    current_month = _month_start(today)
    start, end, label, headline_label = resolve_period(period, today)
    explicit_year = re.search(r"(?<!\d)(20\d{2})年(?:度)?", message or "")
    if explicit_year:
        year = int(explicit_year.group(1))
        start, end = date(year, 1, 1), date(year + 1, 1, 1)
        label, headline_label, period = f"{year} 年度", "本期支出", "year"
    rows = (await session.scalars(
        select(StatementTransaction)
        .where(
            StatementTransaction.user_id == user_id,
            StatementTransaction.txn_date >= start,
            StatementTransaction.txn_date < end,
        )
        .order_by(StatementTransaction.txn_date.desc(), StatementTransaction.id.desc())
    )).all()
    if not rows:
        return {
            "type": "bill_analysis",
            "title": "还没有可分析的账单",
            "period": {"kind": period, "label": label},
            "empty": True,
            "message": "当前账户没有这一期间的账单数据。导入账单后，我可以生成分类、异常与趋势报告。",
            "engine": "analysis",
        }

    enriched = []
    values_by_category: dict[str, list[Decimal]] = defaultdict(list)
    for row in rows:
        amount = abs(Decimal(row.amount or 0))
        # 附言优先：同一家商户，"买菜"和"给同事生日礼物"不该算成同一项消费。
        category = classify_note(row.note) or classify(row.merchant_name, row.category)
        values_by_category[category].append(amount)
        enriched.append((row, category, amount))

    category_totals: dict[str, Decimal] = defaultdict(Decimal)
    category_counts: dict[str, int] = defaultdict(int)
    monthly_totals: dict[str, Decimal] = defaultdict(Decimal)
    merchant_totals: dict[str, Decimal] = defaultdict(Decimal)
    merchant_counts: dict[str, int] = defaultdict(int)
    weekday_totals: dict[int, Decimal] = defaultdict(Decimal)
    weekday_counts: dict[int, int] = defaultdict(int)
    anomalies = []
    recurring_total = Decimal("0")
    total = Decimal("0")
    for row, category, amount in enriched:
        total += amount
        category_totals[category] += amount
        category_counts[category] += 1
        if row.txn_date:
            monthly_totals[row.txn_date.strftime("%Y-%m")] += amount
            weekday_totals[row.txn_date.weekday()] += amount
            weekday_counts[row.txn_date.weekday()] += 1
        merchant = row.merchant_name or "未知商户"
        merchant_totals[merchant] += amount
        merchant_counts[merchant] += 1
        if row.is_recurring:
            recurring_total += amount
        baseline = Decimal(str(median(values_by_category[category])))
        computed = amount >= Decimal("500") and amount > baseline * Decimal("2.5")
        if row.is_anomaly or computed:
            reason = row.anomaly_reason or f"金额是同类交易中位数的 {(amount / baseline):.1f} 倍"
            anomalies.append({
                "date": row.txn_date.isoformat() if row.txn_date else "未知日期",
                "merchant": row.merchant_name or "未知商户",
                "category": category,
                "amount": _money(amount),
                "amount_value": float(amount),
                "reason": reason,
                "severity": "HIGH" if amount >= Decimal("3000") else "MEDIUM",
            })

    categories = [
        {
            "name": name,
            "amount": _money(amount),
            "amount_value": float(amount),
            "share_pct": float((amount / total * 100).quantize(Decimal("0.1"))) if total else 0,
            "count": category_counts[name],
        }
        for name, amount in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    trend_start = date(today.year, 1, 1) if period in {"year", "last_year"} else _shift_month(current_month, -5)
    trend_rows = (await session.scalars(
        select(StatementTransaction).where(
            StatementTransaction.user_id == user_id,
            StatementTransaction.txn_date >= trend_start,
            StatementTransaction.txn_date < end,
        )
    )).all()
    trend_values: dict[str, Decimal] = defaultdict(Decimal)
    for row in trend_rows:
        if row.txn_date:
            trend_values[row.txn_date.strftime("%Y-%m")] += abs(Decimal(row.amount or 0))
    months = []
    cursor = _month_start(trend_start)
    while cursor < end:
        key = cursor.strftime("%Y-%m")
        months.append({"label": key, "amount": _money(trend_values[key]), "amount_value": float(trend_values[key])})
        cursor = _shift_month(cursor, 1)

    merchant_ranking = [
        {"name": name, "amount": _money(amount), "amount_value": float(amount), "count": merchant_counts[name], "share_pct": float((amount / total * 100).quantize(Decimal('0.1'))) if total else 0}
        for name, amount in sorted(merchant_totals.items(), key=lambda item: item[1], reverse=True)[:6]
    ]
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday_pattern = [
        {"label": weekday_names[index], "amount": _money(weekday_totals[index]), "amount_value": float(weekday_totals[index]), "count": weekday_counts[index]}
        for index in range(7)
    ]
    trend_numbers = [item["amount_value"] for item in months]
    # The last month on the axis is still running, so measuring month-over-month
    # against it would report a collapse that is only the calendar. Every
    # period comparison here is taken between the two most recent *complete*
    # months, and the running month is labelled as partial in the chart note.
    running_month = today.strftime("%Y-%m")
    complete_months = months[:-1] if months and months[-1]["label"] == running_month else months
    complete_numbers = [item["amount_value"] for item in complete_months]
    current_value = complete_numbers[-1] if complete_numbers else 0
    previous_value = complete_numbers[-2] if len(complete_numbers) > 1 else 0
    change_pct = ((current_value - previous_value) / previous_value * 100) if previous_value else None
    average_value = mean(complete_numbers) if complete_numbers else 0
    volatility_pct = (pstdev(complete_numbers) / average_value * 100) if average_value else 0
    # The peak is a statement about a finished month. Crediting a three-day-old
    # month with being the highest spender would read as a spending crisis.
    highest = max(complete_months, key=lambda item: item["amount_value"]) if complete_months else (months[0] if months else {"label": "—", "amount": _money(Decimal('0'))})
    top_three = sum((Decimal(str(item["amount_value"])) for item in merchant_ranking[:3]), Decimal("0"))
    cashflow = await analytics.monthly_cashflow(session, user_id, months=6)

    top = categories[0]
    insights = [f"{top['name']}是最大支出类别，占本期消费 {top['share_pct']:.1f}%。"]
    if recurring_total:
        insights.append(f"固定订阅与周期扣费共 {_money(recurring_total)}，可结合订阅页逐项核对。")
    if anomalies:
        insights.append(f"识别到 {len(anomalies)} 笔需关注交易；这是规则提示，不等同于欺诈结论。")
    else:
        insights.append("未发现明显偏离同类金额基线的交易。")

    # 问收入就答收入。账单分析这张卡默认讲的是"花了多少"，所以被问到收入时
    # 必须把收入放到最上面，否则用户翻半屏才看到自己要的那个数。
    hero = analytics.cashflow_headline(
        cashflow, analytics.headline_kind(message, "spending"),
        spending=float(total), spending_label=headline_label,
    )
    return {
        "type": "bill_analysis",
        "title": f"{label}账单洞察",
        "hero": hero,
        "headline_label": headline_label,
        "period": {"kind": period, "label": label, "start": start.isoformat(), "end_exclusive": end.isoformat()},
        "empty": False,
        "summary": {
            "total": _money(total),
            "transaction_count": len(rows),
            "daily_average": _money(total / Decimal(max(_elapsed_days(today, start, end), 1))),
            "recurring": _money(recurring_total),
        },
        "transactions": [{"id": row.id, "date": row.txn_date.isoformat() if row.txn_date else "未知日期",
            "merchant": row.merchant_name or "未知商户", "category": category,
            "amount": _money(amount), "amount_value": float(amount), "note": row.note or ""}
            for row, category, amount in enriched],
        "daily_spending": [{"date": (start + timedelta(days=i)).isoformat(), "amount_value": float(sum(
            amount for row, _, amount in enriched if row.txn_date == start + timedelta(days=i)))}
            for i in range(min((end - start).days, max((today - start).days + 1, 0)))],
        "categories": categories,
        "anomalies": sorted(anomalies, key=lambda item: item["amount_value"], reverse=True),
        "monthly_trend": months,
        "chart": cashflow.get("chart"),
        "period_comparison": cashflow.get("comparison"),
        "merchant_ranking": merchant_ranking,
        "weekday_pattern": weekday_pattern,
        "trend_stats": {"change_pct": None if change_pct is None else round(change_pct, 1), "average": _money(Decimal(str(average_value))), "volatility_pct": round(volatility_pct, 1), "highest_month": highest["label"], "highest_amount": highest["amount"]},
        "concentration": {"top_three_merchant_pct": float((top_three / total * 100).quantize(Decimal('0.1'))) if total else 0, "recurring_pct": float((recurring_total / total * 100).quantize(Decimal('0.1'))) if total else 0},
        "insights": insights,
        "method": "基于当前用户已导入账单，按商户规则分类；异常由同类金额中位数与导入标记共同识别。",
        "engine": "analysis",
    }
