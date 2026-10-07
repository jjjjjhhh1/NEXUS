"""Execution boundary for a confirmed local birthday plan."""
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from ..core.exceptions import BusinessRuleException
from ..core.models import Account, AuditLog, Plan, PlanOrder, RiskReservation


OPTIONS = {
    "A": [("鲜花礼束", Decimal("520")), ("生日蛋糕", Decimal("460"))],
    "B": [("生日礼盒", Decimal("780")), ("手写卡片", Decimal("100"))],
    "C": [("浪漫晚餐预订", Decimal("699")), ("鲜花礼束", Decimal("300"))],
}


def quote_birthday(option: str, budget: Decimal) -> list[tuple[str, Decimal]]:
    from ..core.money import positive_amount
    budget = positive_amount(budget)
    if option not in OPTIONS:
        raise BusinessRuleException("未知的生日方案")
    items = OPTIONS[option]
    total = sum((price for _, price in items), Decimal("0"))
    if total > budget:
        raise BusinessRuleException(f"方案 {option} 总价 ¥{total:,.2f}，超过预算 ¥{budget:,.2f}，请增加预算或选择其他方案")
    return items


class CrossSceneService:
    def __init__(self, session):
        self.session = session

    async def create_birthday_plan(self, user_id: int, event_date: date, budget: Decimal, option: str) -> Plan:
        items = quote_birthday(option, budget)
        if event_date < date.today():
            raise BusinessRuleException("生日日期已经过去，请提供未来日期")
        account = await self.session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
        if account is None or Decimal(account.available_balance) < budget:
            raise BusinessRuleException("可用余额不足，无法预留这笔生日预算")
        account.available_balance = Decimal(account.available_balance) - budget
        account.reserved_balance = Decimal(account.reserved_balance) + budget
        plan = Plan(user_id=user_id, type="BIRTHDAY", title=f"生日惊喜方案 {option}", event_date=event_date, budget=budget, reserved_amount=budget, order_lead_days=2, categories='["鲜花","蛋糕","礼品"]', status="ACTIVE")
        self.session.add(plan)
        await self.session.flush()
        self.session.add(RiskReservation(account_id=account.id, plan_id=plan.id, amount=budget, status="ACTIVE"))
        for name, price in items:
            self.session.add(PlanOrder(plan_id=plan.id, product_name=name, product_price=price, quantity=1, status="DRAFT"))
        self.session.add(AuditLog(user_id=user_id, action="CREATE_BIRTHDAY_PLAN", target_type="plan", target_id=plan.id, after_state=f"日期{event_date.isoformat()}/预算{budget:.2f}/方案{option}"))
        self.session.add(AuditLog(user_id=user_id, action="AUTHORIZE_BIRTHDAY_SIMULATION",
            target_type="plan", target_id=plan.id, after_state="v1:confirmed-demo-order"))
        await self.session.flush()
        return plan
