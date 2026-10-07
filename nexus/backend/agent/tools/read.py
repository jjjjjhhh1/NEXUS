"""LangChain tool registry for the Nexus banking agent.

Each tool is *read-only* and *user-scoped*: it accepts only an explicit user
identifier and returns data computed from the local sandbox. No tool exposes a
bank-write primitive, so neither the model nor a prompt-injection can move money
or alter account/card/subscription state by calling a tool. Writes always go
through the confirmed DemoAction workflow in ``tools.banking.execute``.

Tools are async so a LangGraph/LLM agent loop can await them without blocking
the single-writer SQLite event loop.

The registry is exposed as LangChain ``BaseToolkit`` objects whose tools accept
``user_id`` through an injected argument (``InjectedToolArg``): the model never
sees or fabricates ``user_id`` — the orchestrating runtime supplies it from the
authenticated demo session.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Type

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...core import database
from ...core.models import Account, Card, Recipient, FinancialProfile, FinancialSnapshot, User, ContextEvent
from ...services.subscription_service import SubscriptionService
from ...services.product_service import ProductService
from ..integrations.external_data import fetch_fx_quote


# ---- input schemas (only model-controllable args; user_id is injected) ----

class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

class _BillsArgs(BaseModel):
    period: str = Field(default="month", description="month 或 year")


class _FxArgs(BaseModel):
    base: str = Field(description="基准币种 ISO 代码，如 CNY、USD、EUR")
    quote: str = Field(description="报价币种 ISO 代码，如 CNY、USD、EUR")
    amount: str = Field(default="1", description="要换算的金额字符串")


async def _user_accounts(user_id: int):
    async with database.session_scope() as session:
        return list((await session.scalars(select(Account).where(Account.user_id == user_id))).all())


async def _user_cards(user_id: int):
    async with database.session_scope() as session:
        account_ids = list((await session.scalars(select(Account.id).where(Account.user_id == user_id))).all())
        return list((await session.scalars(select(Card).where(Card.account_id.in_(account_ids)).order_by(Card.id))).all())


async def get_balance(user_id: int) -> dict:
    """返回用户的可用余额、计划预留与账户类型。"""
    accounts = await _user_accounts(user_id)
    available = sum((Decimal(a.available_balance) for a in accounts), Decimal("0"))
    reserved = sum((Decimal(a.reserved_balance) for a in accounts), Decimal("0"))
    return {
        "available": f"¥{available:,.2f}",
        "reserved": f"¥{reserved:,.2f}",
        "accounts": [{"type": a.type, "available": f"¥{a.available_balance:,.2f}"} for a in accounts],
    }


async def get_cards(user_id: int) -> dict:
    """返回用户卡片列表，含尾号、状态与单笔/每日限额。"""
    cards = await _user_cards(user_id)
    accounts = {a.id: a for a in await _user_accounts(user_id)}
    return {"balance_note": "卡片余额来自关联账户；相同账户的卡共用余额，不能重复相加；限额不是余额。", "cards": [
        {"last4": c.last4, "status": c.status, "bank": c.bank_name,
         "account_reference": str(c.account_id),
         "available": f"¥{accounts[c.account_id].available_balance:,.2f}",
         "shared_account": sum(other.account_id == c.account_id for other in cards) > 1,
         "single_limit": f"¥{c.single_limit:,.0f}" if c.single_limit is not None else "未设置",
         "daily_limit": f"¥{c.daily_limit:,.0f}" if c.daily_limit is not None else "未设置"}
        for c in cards
    ]}


async def get_recipients(user_id: int) -> dict:
    """返回已登记收款人的最小必要信息；手机号仅保留尾号。"""
    async with database.session_scope() as session:
        rows = list((await session.scalars(select(Recipient).where(Recipient.user_id == user_id))).all())
    return {"recipients": [
        {"name": r.name, "alias": r.alias, "phone_masked": f"***{str(r.phone)[-4:]}"}
        for r in rows
    ]}


async def get_subscriptions(user_id: int) -> dict:
    """返回用户订阅与代扣授权状态。"""
    async with database.session_scope() as session:
        rows = await SubscriptionService(session).list_user_subscriptions(user_id)
    monthly = sum((Decimal(str(r["amount"])) for r in rows if r["status"] == "ACTIVE"), Decimal("0"))
    return {
        "subscriptions": [{"merchant": r["merchant_name"], "amount": f"¥{Decimal(str(r['amount'])):,.2f}",
                           "contract": r["contract_status"], "mandate": r["mandate_status"]} for r in rows],
        "monthly_active": f"¥{monthly:,.2f}",
    }


async def get_bills(user_id: int, period: str = "month") -> dict:
    """返回用户账单的分类汇总、异常与趋势。period 为 month 或 year。"""
    async with database.session_scope() as session:
        from ..analysis.bill_analysis import build_bill_analysis
        report = await build_bill_analysis(session, user_id, period)
    return {
        "empty": report.get("empty", False),
        "total": report.get("summary", {}).get("total"),
        "period": report.get("period"), "chart": report.get("chart"),
        "summary": report.get("summary"),
        "categories": report.get("categories", []),
        "anomalies": len(report.get("anomalies", [])),
        "recurring": report.get("summary", {}).get("recurring"),
    }


async def get_financial_profile(user_id: int) -> dict:
    """返回用户理财画像：目标、期限、月收入、必要支出与负载。"""
    async with database.session_scope() as session:
        p = await session.scalar(select(FinancialProfile).where(FinancialProfile.user_id == user_id))
        if p is None:
            return {"has_profile": False}
        snapshot = await session.scalar(select(FinancialSnapshot).where(FinancialSnapshot.user_id == user_id))
        surplus = (Decimal(p.monthly_income) - Decimal(p.essential_expenses) - Decimal(p.monthly_debt_payment))
        from ..analysis.financial_analysis import build_financial_analysis
        plan = await build_financial_analysis(session, user_id)
    return {
        "has_profile": True,
        "user_preferences": plan.get("memory_context", {}),
        "verified_plan": {"monthly_income": plan['cashflow']['income'], "annual_outflow": plan['cashflow']['annual_outflow'],
            "eligible_products":plan.get("product_matches", []), "excluded_products":plan.get("product_rejected", []),
            "monthly_surplus": plan['cashflow']['surplus'], "investable": plan['recommendation']['investable'],
            "risk_cap": plan['recommendation']['risk_cap'], "verdict": plan['recommendation']['verdict'],
            "allocation": plan['allocation'], "basis": "以全年季节性收支及还贷、订阅计算月均结余；基础结余未扣订阅，不用于投资额度"},
        "goal": p.goal_name, "goal_amount": f"¥{p.goal_amount:,.2f}",
        "horizon_months": p.horizon_months, "monthly_income": f"¥{p.monthly_income:,.2f}",
        "monthly_surplus": f"¥{surplus:,.2f}",
        "debt_balance": f"¥{p.debt_balance:,.2f}",
        "investments": f"¥{Decimal(snapshot.investment_assets):,.2f}" if snapshot else "¥0.00",
    }


async def get_products(user_id: int) -> dict:
    """返回候选理财产品的代码、风险等级、参考收益率与锁定期。"""
    async with database.session_scope() as session:
        user = await session.get(User, user_id)
        rows = await ProductService(session).list_products(user_id)
        orders = await ProductService(session).list_user_orders(user_id)
    return {
        "risk_score": user.risk_score or "未测评",
        "products": [{"code": r["code"], "name": r["name"], "risk": r["risk_level"],
                      "rate": f"{r['yield_rate']:.2f}%", "lock_days": r["lock_days"],
                      "minimum": f"¥{r['min_purchase']:,.0f}"} for r in rows],
        "orders": [{"id": o["id"], "product": o.get("product_name", ""), "shares": o.get("remaining_shares")} for o in orders],
    }


async def get_events(user_id: int, event_types: list[str] | None = None) -> dict:
    """返回用户已授权的本地事件（生日、差旅、家庭、市场等）。"""
    async with database.session_scope() as session:
        rows = list((await session.scalars(select(ContextEvent).where(
            ContextEvent.user_id == user_id, ContextEvent.status == "ACTIVE"
        ).order_by(ContextEvent.occurred_at.desc()))).all())
    # Event payloads may later gain internal or sensitive fields. Only this
    # explicit allowlist can cross the model-tool boundary.
    safe_fields = {
        "date", "city", "destination", "days", "relationship", "budget",
        "merchant", "amount", "category", "risk_level", "currency",
        "company", "ticker", "preference", "note",
        "card_last4", "lounge_visits", "fast_track", "valid_until", "hotel_benefit",
        "temporary_limit_needed", "preferred_transport", "departure_date",
        "days_unused", "next_fee", "next_charge", "current", "current_price",
        "alternative", "alternative_price", "saving_year", "contact", "recipient",
        "total", "participants", "payer", "user_share", "day", "suggested", "recent_average",
    }
    return {"events": [
        {
            "type": e.event_type,
            "title": e.title,
            "payload": {k: v for k, v in (e.payload or {}).items() if k in safe_fields},
        }
        for e in rows if event_types is None or e.event_type in event_types
    ]}


async def get_fx(base: str, quote: str, amount: str = "1") -> dict:
    """查询两个币种之间的参考汇率并换算。base/quote 用 ISO 代码，如 CNY、USD、EUR。"""
    from decimal import Decimal as D
    result = await fetch_fx_quote(D(amount), base, quote)
    return {"base": base, "quote": quote, "rate": result["rate"], "converted": result["converted"], "as_of": result["as_of"]}


# ---- LangChain tool wrapping ----

def _make_tool(name: str, description: str, args_schema: Type[BaseModel] | None, func) -> StructuredTool:
    """Build a StructuredTool; the injected user_id is provided by the runtime."""
    return StructuredTool.from_function(
        name=name,
        description=description,
        args_schema=args_schema,
        coroutine=func,
        infer_schema=True,
    )


def build_read_toolkit(user_id: int | None = None, event_types: list[str] | None = None) -> List[StructuredTool]:
    """Return read-only tools, optionally bound to an authenticated user.

    The AgentExecutor always uses the bound form. Identity is then supplied by
    the runtime and cannot be selected or changed by the model.
    """
    if user_id is not None:
        async def balance(): return await get_balance(user_id)
        async def cards(): return await get_cards(user_id)
        async def recipients(): return await get_recipients(user_id)
        async def subscriptions(): return await get_subscriptions(user_id)
        async def bills(period: str = "month"): return await get_bills(user_id, period)
        async def financial_profile(): return await get_financial_profile(user_id)
        async def products(): return await get_products(user_id)
        async def events(): return await get_events(user_id, event_types)
        scoped = {
            "get_balance": balance, "get_cards": cards,
            "get_recipients": recipients, "get_subscriptions": subscriptions,
            "get_bills": bills, "get_financial_profile": financial_profile,
            "get_products": products, "get_events": events,
        }
    else:
        scoped = {
            "get_balance": get_balance, "get_cards": get_cards,
            "get_recipients": get_recipients, "get_subscriptions": get_subscriptions,
            "get_bills": get_bills, "get_financial_profile": get_financial_profile,
            "get_products": get_products, "get_events": get_events,
        }
    tools = [
        _make_tool("get_balance", "读取用户可用余额与账户列表。", _NoArgs if user_id is not None else None, scoped["get_balance"]),
        _make_tool("get_cards", "读取用户银行卡名称、尾号、关联账户余额、共享账户关系及交易限额；卡余额不可重复相加。", _NoArgs if user_id is not None else None, scoped["get_cards"]),
        _make_tool("get_recipients", "读取已登记收款人列表。", _NoArgs if user_id is not None else None, scoped["get_recipients"]),
        _make_tool("get_subscriptions", "读取用户订阅与代扣授权状态。", _NoArgs if user_id is not None else None, scoped["get_subscriptions"]),
        _make_tool("get_bills", "读取用户账单分类、异常与趋势。period 为 month 或 year。", _BillsArgs, scoped["get_bills"]),
        _make_tool("get_financial_profile", "读取用户理财画像。", _NoArgs if user_id is not None else None, scoped["get_financial_profile"]),
        _make_tool("get_products", "读取候选理财产品与风险等级。", _NoArgs if user_id is not None else None, scoped["get_products"]),
        _make_tool("get_events", "读取用户已授权的本地事件。", _NoArgs if user_id is not None else None, scoped["get_events"]),
        _make_tool("get_fx", "查询两种货币的参考汇率。", _FxArgs, get_fx),
    ]
    return tools
