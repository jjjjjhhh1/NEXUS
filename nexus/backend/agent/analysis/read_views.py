"""Verified read views.

Every read-only answer the agent can give is rendered here, from database
records, with no model prose in it. The model decides *which* view the user
asked for (scene + read_tools) and *what shape* the answer takes; this module
owns *what is true*, so the numbers on screen can never be invented.

One function per business view. Nothing here writes: a view can be reached only
from a read branch, and any state change must go through plan_builder.

Layer contract:
  owns      — verified read payloads and their trace
  does NOT own — intent (understanding.py), dispatch (routing.py),
                 writes (plan_builder.py), safety (guard/boundary)
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import or_, select

from ..integrations.external_data import fetch_fx_quote, parse_fx_request

from ...core.models import Account, Card, Transaction, User
from ...services.aa_collection_service import AACollectionService
from ...services.product_service import ProductService
from ...services.scheduled_transfer_service import ScheduledTransferService
from ...services.subscription_service import SubscriptionService
from . import analytics


# The model chooses tools; the focus label follows so the user sees *why* this
# view was rendered rather than an arbitrary default.
FOCUS_BY_TOOL = {
    "cards": "卡片",
    "card_benefits": "卡片",
    "subscriptions": "订阅",
    "subscription_usage": "订阅",
    "bills": "流水",
    "recipients": "收款人",
}


def _pct(value: float | None) -> str | None:
    """Signed percentage for display; ``None`` when the product has no curve."""
    if value is None:
        return None
    return f"{value:+.2f}%"


def _trace(recognised: str, fetched: str, checked: str) -> list[dict]:
    return [
        {"label": "意图理解", "detail": recognised, "status": "done"},
        {"label": "调用本地工具", "detail": fetched, "status": "done"},
        {"label": "权限核验", "detail": checked, "status": "done"},
    ]


def is_card_balance_question(message: str | None) -> bool:
    text = message or ""
    return ("卡" in text and bool(re.search(r"余额|多少钱|多少[钱元]|有多少|剩多少", text))
            and not bool(re.search(r"转账|汇款|锁定|解锁|挂失|限额|额度|申购|赎回|消费|账单|订阅", text)))


async def card_balance_view(session, user_id: int, message: str) -> dict:
    accounts = {a.id: a for a in (await session.scalars(
        select(Account).where(Account.user_id == user_id))).all()}
    cards = list((await session.scalars(select(Card).where(
        Card.account_id.in_(accounts)).order_by(Card.id))).all())
    selected = [c for c in cards if c.last4 in message or c.bank_name in message
                or re.sub(r"^Nexus\s*", "", c.bank_name, flags=re.I) in message]
    if not selected and any(word in message for word in ("银行卡", "卡片", "两张卡", "每张卡", "我的卡")):
        selected = cards
    if not selected:
        return {"type": "message", "engine": "clarify",
                "message": "没有匹配到这张卡，请告诉我卡片名称或尾号。我会按它关联的账户查询余额。"}
    shared = len(selected) > 1 and len({c.account_id for c in selected}) == 1
    return {"type": "card_balances", "title": "卡片余额",
            "summary": "这些卡片关联同一个账户，共用余额，不是各自独立的一笔钱，不能重复相加。" if shared
                       else "按各卡片关联的账户查询；可用余额与单笔、每日交易限额分别展示。",
            "cards": [{"name": c.bank_name, "last4": c.last4, "status": c.status,
                       "available": f"¥{accounts[c.account_id].available_balance:,.2f}",
                       "reserved": f"¥{accounts[c.account_id].reserved_balance:,.2f}",
                       "shared_account": sum(other.account_id == c.account_id for other in cards) > 1,
                       "single_limit": f"¥{c.single_limit:,.2f}" if c.single_limit is not None else "未设置",
                       "daily_limit": f"¥{c.daily_limit:,.2f}" if c.daily_limit is not None else "未设置"}
                      for c in selected],
            "trace": _trace("按卡片查询余额", "卡片关联账户的可用余额与限额", "仅查询本人持有的卡片")}


async def account_view(session, user_id: int, *, tools: list[str] | None = None,
                        message: str | None = None) -> dict:
    """One verified snapshot, led by the number the question was actually about.

    Still returns every section — the side panel and the chat expect the same
    canonical snapshot. What changes is the *headline*: asking "这个月收入多少"
    and being shown "流水数据已核验" answers a question nobody asked. So the view
    leads with the figure the customer asked for, and the verification wording
    moves down to the provenance line where it belongs.
    """
    if is_card_balance_question(message):
        return await card_balance_view(session, user_id, message or "")
    accounts = list((await session.scalars(
        select(Account).where(Account.user_id == user_id)
    )).all())
    account_ids = [row.id for row in accounts]
    cards = list((await session.scalars(
        select(Card).where(Card.account_id.in_(account_ids)).order_by(Card.id)
    )).all())
    subscriptions = await SubscriptionService(session).list_user_subscriptions(user_id)
    transactions = list((await session.scalars(
        select(Transaction).where(
            or_(Transaction.from_account_id.in_(account_ids),
                Transaction.to_account_id.in_(account_ids)),
            Transaction.idempotency_key.is_not(None),
        ).order_by(Transaction.id.desc()).limit(5)
    )).all())
    available = sum((row.available_balance for row in accounts), start=Decimal("0"))
    reserved = sum((row.reserved_balance for row in accounts), start=Decimal("0"))
    chosen = tools or ["account"]
    focus = next((FOCUS_BY_TOOL[tool] for tool in chosen if tool in FOCUS_BY_TOOL), "账户")
    hero = await _headline(session, user_id, message=message, tools=chosen,
                           available=available, reserved=reserved)
    # A comparison question needs both sides in one answer. The model asked for
    # the rate as well as the balance, so fetch it and put the two next to each
    # other — otherwise "够换1000美元吗" gets answered with a balance alone,
    # which is a fact about the wrong half of the question.
    comparison, fx_trace = await _fx_comparison(available, message, chosen)
    trace = [
        {"label": "识别需求", "detail": f"读取{focus}", "status": "done"},
        {"label": "调用本地工具", "detail": "账户、卡片、订阅与流水数据库", "status": "done"},
        {"label": "权限核验", "detail": "仅返回你本人的数据", "status": "done"},
        *(fx_trace or []),
    ]
    answer = {
        "type": "account_snapshot",
        "title": hero["label"],
        "hero": hero,
        "summary": hero["note"],
        "accounts": [{"type": row.type, "available": f"¥{row.available_balance:,.2f}",
                      "reserved": f"¥{row.reserved_balance:,.2f}"} for row in accounts],
        "cards": [{"name": row.bank_name, "last4": row.last4, "status": row.status,
                   "single_limit": f"¥{row.single_limit:,.2f}" if row.single_limit is not None else "未设置",
                   "daily_limit": f"¥{row.daily_limit:,.2f}" if row.daily_limit is not None else "未设置"}
                  for row in cards],
        "subscriptions": [{"merchant": row["merchant_name"], "amount": f"¥{Decimal(row['amount']):,.2f}",
                           "contract": row["contract_status"], "mandate": row["mandate_status"]}
                          for row in subscriptions],
        "transactions": [{"id": row.id, "amount": f"¥{row.amount:,.2f}", "status": row.status,
                          "direction": "转出" if row.from_account_id in account_ids else "转入"}
                         for row in transactions],
        "trace": trace,
    }
    if comparison:
        answer["comparison"] = comparison
    return answer


async def _fx_comparison(available, message, tools) -> tuple[dict | None, list[dict] | None]:
    """Answer "can I afford this conversion" with both sides, or with neither.

    Only fires when the model asked for the rate and the question names a
    currency to convert into. A live-rate call that fails leaves the answer
    exactly as it was — a comparison the customer cannot make is better shown
    as absent than shown as a guess.
    """
    if "fx" not in tools or not message:
        return None, None
    if _has_unparsed_magnitude(message):
        # "20 万欧元" is not a number this parser reads. Producing a comparison
        # from the "20" it did see would state a confidently wrong answer about
        # the customer's money, so the block is withheld instead.
        return None, None
    # Intent already came from the model; this only has to find the pair.
    request = parse_fx_request(message, strict=False)
    if not request:
        return None, None
    amount, base, quote = request
    try:
        rate = Decimal((await fetch_fx_quote(Decimal("1"), base, quote))["rate"])
        as_of = (await fetch_fx_quote(Decimal("1"), base, quote)).get("as_of", "")
    except Exception:
        return None, None
    balance = Decimal(str(available))

    # "1000 人民币能换多少美元" states how much to spend. "我够换 1000 美元吗"
    # states a target and asks what it costs. Reading the first as the second
    # yields not a wrong number but an impossible one — a negative surplus
    # while declaring the customer able to afford it.
    spend = (amount * rate if _spends_base(message, amount, base)
             else amount / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    affordable = balance >= spend
    gap = (balance - spend) if affordable else (spend - balance)
    if affordable:
        verdict = (f"够换。按参考汇率 {rate}，换到 {quote} {amount:,.2f} 约需 {base} {spend:,.2f}，"
                   f"可用余额 ¥{balance:,.2f} 能覆盖，还余 {base} {gap:,.2f}。")
    else:
        verdict = (f"不够换。按参考汇率 {rate}，换到 {quote} {amount:,.2f} 约需 {base} {spend:,.2f}，"
                   f"可用余额 ¥{balance:,.2f} 还差 {base} {gap:,.2f}。")
    return {
        "label": f"换汇测算 · {base} → {quote}",
        "verdict": verdict,
        "affordable": affordable,
        "rate": f"1 {base} ≈ {rate} {quote}",
        "as_of": as_of,
        "source": {"name": "Frankfurter 汇率 API", "url": "https://frankfurter.dev/"},
    }, [{
        "label": "取参考汇率",
        "detail": f"Frankfurter /v2/rate（{base}→{quote}）· 金额在本地换算",
        "status": "done",
    }]


def _has_unparsed_magnitude(message: str) -> bool:
    """Detect Chinese magnitude words the currency parser does not apply.

    万, 千 and 亿 multiply the digits that precede them. The pair parser reads
    those digits literally, so a question phrased in magnitudes must not be
    answered from them.
    """
    return bool(re.search(r"[0-9][0-9,.]*\s*[万千亿]|[万千亿]\s*[0-9]", message))


def _spends_base(message: str, amount, base: str) -> bool:
    """True when the customer attached the amount to the currency being spent."""
    aliases = ("人民币", "元", "RMB", "CNY") if base == "CNY" else (base, base.lower())
    spoken = re.search(rf"\d[\d,.]*\s*(?:{'|'.join(aliases)})", message, re.IGNORECASE)
    return bool(spoken)
# 问句落到哪个数字上。顺序有意义：先看更具体的说法，再退回工具选择。
_HERO_PATTERNS = (
    ("income", ("收入", "进账", "工资", "到账", "赚", "发薪")),
    ("spending", ("支出", "花了", "花掉", "消费", "开销")),
    ("balance", ("余额", "账户", "可用", "还剩", "多少钱")),
)


async def _headline(session, user_id: int, *, message: str | None, tools: list[str],
                    available: Decimal, reserved: Decimal) -> dict:
    """The one figure to put at the top, plus what the rest of the card is about."""
    text = message or ""
    kind = next((key for key, words in _HERO_PATTERNS if any(word in text for word in words)), None)
    if kind is None and "bills" in tools:
        kind = "income"
    if kind is None:
        kind = "balance"

    if kind == "balance":
        return {
            "key": "balance", "label": "可用余额", "value": f"¥{available:,.2f}",
            "note": "已核验 · 仅统计你本人的账户",
            "aside": [{"label": "计划预留", "value": f"¥{reserved:,.2f}"}],
        }

    cashflow = await analytics.monthly_cashflow(session, user_id, months=1)
    income = (cashflow.get("income") or [None])[-1]
    spending = (cashflow.get("expense") or [0])[-1]
    note = cashflow.get("income_source") or "数据截至今日"
    label = "本月收入" if kind == "income" else "本月支出"
    value = income if kind == "income" else spending
    if value is None:
        return {
            "key": kind, "label": label, "value": "暂无申报",
            "note": "账单只记录支出；收入需要你在财务档案里申报后才会显示。",
            "aside": [],
        }
    net = float(income) - float(spending)
    aside = [
        {"label": "本月支出" if kind == "income" else "本月收入", "value": f"¥{spending:,.2f}" if kind == "income" else f"¥{income:,.2f}"},
        {"label": "本月结余", "value": f"¥{net:,.2f}", "tone": "up" if net >= 0 else "down"},
    ]
    return {
        "key": kind, "label": label, "value": f"¥{value:,.2f}",
        "note": note,
        "aside": aside,
    }


async def product_view(session, user_id: int) -> dict:
    """Investment catalogue checked against the user's risk score, plus holdings."""
    service = ProductService(session)
    user = await session.get(User, user_id)
    products = await service.list_products(user_id)
    orders = await service.list_user_orders(user_id)
    trend = await analytics.product_trend(session, products)
    movements = trend["by_code"]
    return {
        "type": "product_catalog", "title": "理财产品与持仓",
        "risk_score": user.risk_score or "未测评",
        # The curve covers exactly the products in this table; a chart of a
        # different set would contradict the cards right below it.
        "chart": trend["chart"],
        "quote_note": analytics.DEMO_QUOTE_NOTE,
        "products": [{
            "code": row["code"], "name": row["name"], "risk": row["risk_level"],
            "reference_rate": f"{row['yield_rate']:.2f}%", "lock_days": row["lock_days"],
            "minimum": f"¥{row['min_purchase']:,.2f}", "fictional": row["is_fictional"],
            "window_change_pct": _pct(movements.get(row["code"], {}).get("window_change_pct")),
            "recent_change_pct": _pct(movements.get(row["code"], {}).get("recent_change_pct")),
            "direction": movements.get(row["code"], {}).get("direction", "flat"),
        } for row in products],
        "orders": orders,
        "trace": _trace(
            "理财产品与持仓查询",
            "产品库、风险等级、净值走势与投资订单",
            f"按当前风险等级 {user.risk_score or '未测评'} 展示；申购时再次校验",
        ),
    }


async def subscription_view(session, user_id: int) -> dict:
    """Detected recurring charges plus the contracts and mandates behind them."""
    rows = await SubscriptionService(session).detect_recurring_charges(user_id)
    connected = await SubscriptionService(session).list_user_subscriptions(user_id)
    return {
        "type": "recurring_detection", "title": "订阅与周期扣费",
        "items": rows,
        "subscriptions": [{
            "merchant": row["merchant_name"], "amount": f"¥{Decimal(row['amount']):,.2f}",
            "contract": row["contract_status"], "mandate": row["mandate_status"],
        } for row in connected],
        "summary": f"从近月账单中识别出 {len(rows)} 个规律扣费候选，当前有 {len(connected)} 项订阅连接。",
        "trace": _trace(
            "识别周期扣费与订阅状态",
            "按商户、金额、时间间隔聚合账单，并读取订阅合同与代扣授权",
            "只读取当前用户的导入账单，不发起任何扣款",
        ),
    }


async def scheduled_view(session, user_id: int) -> dict:
    plans = await ScheduledTransferService(session).list_user_plans(user_id)
    return {
        "type": "scheduled_transfer_list", "title": "定时转账计划", "plans": plans,
        "summary": f"当前共有 {len(plans)} 个周期性转账计划。确认前不会自动扣款，创建后也可随时暂停。",
        "trace": _trace(
            "查询周期性资金任务",
            "scheduled_transfer_plans · 当前用户",
            "仅展示计划，不自动移动资金",
        ),
    }


async def aa_view(session, user_id: int) -> dict:
    rows = await AACollectionService(session).list_user_collections(user_id)
    total = sum(Decimal(item["total"]) for item in rows)
    return {
        "type": "aa_collection_list", "title": "AA 收款任务", "items": rows,
        "summary": f"共有 {len(rows)} 个 AA 收款任务，总额 ¥{total:,.2f}。",
        "chart": {
            "type": "aa_overview",
            "labels": [item["purpose"] for item in rows[:6]],
            "values": [float(item["total"]) for item in rows[:6]],
        },
        "trace": _trace(
            "查询拆分收款",
            "aa_collections · 当前用户",
            "不向真实联系人发送请求",
        ),
    }


__all__ = [
    "account_view", "product_view", "subscription_view",
    "scheduled_view", "aa_view",
]
