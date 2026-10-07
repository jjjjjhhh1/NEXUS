"""Auditable multi-tool planning for the hackathon birthday scenario."""
from datetime import date
from decimal import Decimal
from ...services.cross_scene_service import OPTIONS
import re

from sqlalchemy import select

from ...core.models import Account
from ..analysis.bill_analysis import build_bill_analysis


def extract_budget(text: str) -> Decimal:
    match = re.search(r"预算\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块)?", text)
    return Decimal(match.group(1)) if match else Decimal("1000")


def extract_event_date(text: str) -> date | None:
    exact = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?", text)
    if exact:
        try:
            return date(*map(int, exact.groups()))
        except ValueError:
            return None
    short = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if short:
        month, day = map(int, short.groups())
        year = date.today().year + (1 if month < date.today().month else 0)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _slot_amount(understanding, text: str) -> Decimal:
    """Budget the model already read, falling back to the literal form.

    The model puts the budget in ``amount`` and the date in ``event_date`` as
    grounded slots. Re-deriving them from the sentence with a regex meant the
    two could disagree — "预算一千" or a budget mentioned without the word
    预算 at all was invisible here, and the plan silently used the 1000 default.
    """
    if understanding is not None and understanding.get("amount"):
        try:
            value = Decimal(str(understanding["amount"]))
        except Exception:
            return extract_budget(text)
        if value > 0:
            return value
    return extract_budget(text)


def _slot_date(understanding, text: str) -> date | None:
    if understanding is not None and understanding.get("event_date"):
        try:
            return date.fromisoformat(str(understanding["event_date"]))
        except ValueError:
            return extract_event_date(text)
    return extract_event_date(text)


async def build_birthday_plan(session, user_id: int, text: str, understanding=None) -> dict:
    budget = _slot_amount(understanding, text)
    event_date = _slot_date(understanding, text)
    if event_date is None:
        return {
            "type": "birthday_intake", "title": "还差一个关键日期",
            "message": "我已识别到生日惊喜目标和预算。请补充生日日期，我才能安排资金预留时间与提前两天的下单节点。",
            "budget": str(budget), "min_date": date.today().isoformat(), "engine": "analysis",
        }
    if event_date < date.today():
        return {"type": "message", "message": "生日日期已经过去，请提供未来日期。", "engine": "policy"}
    accounts = list((await session.scalars(select(Account).where(Account.user_id == user_id))).all())
    available = sum((Decimal(row.available_balance) for row in accounts), Decimal("0"))
    bills = await build_bill_analysis(session, user_id, "month")
    top_category = bills.get("categories", [{}])[0].get("name", "暂无") if not bills.get("empty") else "暂无"
    top_share = bills.get("categories", [{}])[0].get("share_pct", 0) if not bills.get("empty") else 0
    anomaly_count = len(bills.get("anomalies", []))
    affordable = available >= budget
    options = [
        {"code": "A", "name": "鲜花 + 蛋糕", "amount": sum((price for _, price in OPTIONS["A"]), Decimal("0")), "description": "生日前两天下单，与资金预留和商户订单联动"},
        {"code": "B", "name": "礼盒 + 手写卡", "amount": sum((price for _, price in OPTIONS["B"]), Decimal("0")), "description": "保留一部分预算作为当天机动资金"},
        {"code": "C", "name": "晚餐 + 鲜花", "amount": sum((price for _, price in OPTIONS["C"]), Decimal("0")), "description": "预算利用率高，需提前确认时间和地点"},
    ]
    return {
        "type": "cross_scene_plan", "title": "生日惊喜跨场景计划", "event_date": event_date.isoformat(),
        "budget": f"¥{budget:,.2f}", "budget_value": float(budget), "available": f"¥{available:,.2f}",
        "affordable": affordable, "top_category": top_category, "top_share_pct": top_share, "anomaly_count": anomaly_count,
        "options": [{**item, "within_budget": item["amount"] <= budget, "amount": f"¥{item['amount']:,.2f}", "amount_value": float(item["amount"]), "command": f"创建生日计划 日期{event_date.isoformat()} 预算{budget:.2f}元 方案{item['code']}"} for item in options],
        "plan_steps": [
            {"tool": "账户工具", "action": "核验可用余额", "observation": f"可用余额 ¥{available:,.2f}"},
            {"tool": "账单分析", "action": "检查预算压力", "observation": f"本月最大支出类别为{top_category}，占比 {top_share:.1f}%；需关注交易 {anomaly_count} 笔"},
            {"tool": "计划编排", "action": "安排资金和节点", "observation": f"确认后预留预算；生日前两天生成礼品订单草稿"},
            {"tool": "安全确认", "action": "等待用户选择方案", "observation": "未确认前不预留资金、不创建订单"},
        ],
        "trace": [
            {"label": "拆解复合目标", "detail": "生日日期、预算、资金、礼品与执行节点", "status": "done"},
            {"label": "调用账户与账单工具", "detail": "余额核验、消费结构和异常提示", "status": "done"},
            {"label": "生成可执行计划", "detail": "选择方案后进入独立确认卡", "status": "done"},
        ],
        "engine": "analysis",
    }
