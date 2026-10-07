"""Literal user constraints evaluated against verified facts, without profile writes."""
import re
from datetime import date
from decimal import Decimal
from sqlalchemy import select
from ...core.models import FinancialProfile, FinancialSnapshot, ContextEvent
from ...services.subscription_service import SubscriptionService
from .financial_analysis import money


def scenario_parameters(message: str) -> tuple[Decimal | None, Decimal | None]:
    drop = None
    if re.search(r"收入.{0,12}(?:减半|少一半|减少一半|降低一半)", message):
        drop = Decimal('50')
    else:
        match = re.search(r"收入.{0,12}(?:减少|下降|降低|少|降)\s*(\d+(?:\.\d+)?)\s*[%％]", message)
        if match and Decimal(match.group(1)) <= 100:
            drop = Decimal(match.group(1))
    target = re.search(r"每月\s*(?:继续|再|能)?\s*(?:存|储蓄|攒|存下)\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)", message)
    return drop, Decimal(target.group(1)) if target else None


async def apply_income_scenario(session, user_id: int, report: dict, message: str) -> dict:
    drop, target = scenario_parameters(message)
    if drop is None or target is None or report.get('type') == 'financial_intake':
        return report
    profile = await session.scalar(select(FinancialProfile).where(FinancialProfile.user_id == user_id))
    snapshot = await session.scalar(select(FinancialSnapshot).where(FinancialSnapshot.user_id == user_id))
    month = date.today().month % 12  # next month's zero-based slot
    income = Decimal(profile.monthly_income)
    expense = Decimal(profile.essential_expenses)
    if snapshot and snapshot.seasonal_income and len(snapshot.seasonal_income) == 12:
        income = Decimal(str(snapshot.seasonal_income[month]))
    if snapshot and snapshot.seasonal_expenses and len(snapshot.seasonal_expenses) == 12:
        expense = Decimal(str(snapshot.seasonal_expenses[month]))
    subscriptions = await SubscriptionService(session).list_user_subscriptions(user_id)
    fees = sum((Decimal(str(row['amount'])) for row in subscriptions if row['status'] == 'ACTIVE'), Decimal(0))
    reduced = (income * (100 - drop) / 100).quantize(Decimal('.01'))
    outflow = expense + Decimal(profile.monthly_debt_payment) + fees
    surplus = reduced - outflow
    gap = max(target - surplus, Decimal(0))
    scenario = {'income_drop_pct': str(drop), 'baseline_income': str(income), 'income': str(reduced),
        'outflow': str(outflow), 'surplus': str(surplus), 'target': str(target), 'gap': str(gap),
        'affordable': surplus >= target, 'basis': '下月申报收入与生活支出；无季节资料时用月度档案；含还贷与已连接有效订阅'}
    verdict = '可以覆盖' if scenario['affordable'] else '无法覆盖'
    report['title'] = '下月收入变化与储蓄测算'
    report['summary'] = f"收入减少 {drop}% 后，下月预计收入 {money(reduced)}，必要流出 {money(outflow)}，结余 {money(surplus)}；{verdict}每月储蓄 {money(target)}，缺口 {money(gap)}。"
    report['scenario'] = scenario
    report['recommendation'] = None
    report['metrics'] = [{'label': label, 'value': money(value), 'basis': scenario['basis']} for label, value in
        [('下月减收后收入', reduced), ('下月必要流出', outflow), ('下月结余', surplus), ('储蓄目标', target), ('储蓄缺口', gap)]]
    report['decision'].update(goal_feasibility='ON_TRACK' if surplus >= target else 'GAP', monthly_required=money(target), monthly_surplus=money(surplus))
    report['allocation'] = {'method': '本次只测算用户提出的情景，不修改长期画像。', 'buckets': []}
    report['observations'] = [scenario['basis'], '估算不含未申报的临时支出，收入变化未写入长期资料。']
    return report


def unused_days(message: str) -> int | None:
    if not re.search(r"没用|未使用|不用", message):
        return None
    match = re.search(r"(\d+|三|两|二|一)\s*(?:个)?(月|天)", message)
    if not match:
        return None
    raw = match.group(1)
    count = int(raw) if raw.isdigit() else {'三': 3, '两': 2, '二': 2, '一': 1}[raw]
    days = count * (30 if match.group(2) == '月' else 1)
    return days if 1 <= days <= 3650 else None


async def unused_subscription_view(session, user_id: int, days: int) -> dict:
    connected = await SubscriptionService(session).list_user_subscriptions(user_id)
    events = (await session.scalars(select(ContextEvent).where(ContextEvent.user_id == user_id,
        ContextEvent.event_type == 'SUBSCRIPTION_USAGE', ContextEvent.status == 'ACTIVE'))).all()
    usage = {e.payload.get('merchant'): e.payload for e in events if isinstance(e.payload, dict)}
    candidates, unknown, actions = [], [], []
    for item in connected:
        if item['status'] != 'ACTIVE':
            continue
        merchant = item['merchant_name']
        payload = usage.get(merchant)
        if payload is None or not isinstance(payload.get('days_unused'), (int, float)):
            unknown.append(merchant)
        elif payload['days_unused'] >= days:
            candidates.append({'merchant': merchant, 'days_unused': payload['days_unused'], 'monthly_fee': str(item['amount'])})
            actions.append({'label': f'生成取消{merchant}确认', 'command': f'取消{merchant}订阅', 'tone': 'secondary'})
    summary = f"已按至少 {days} 天未使用筛选：找到 {len(candidates)} 项已连接订阅。"
    if unknown:
        summary += '以下订阅缺少使用记录，不能判断是否闲置：' + '、'.join(unknown) + '。'
    return {'type': 'message', 'message': summary, 'candidates': candidates, 'unknown_usage': unknown,
        'actions': actions, 'engine': 'analysis', 'trace': [{'label': '筛选订阅使用记录', 'detail': summary, 'status': 'done'}]}


async def subscription_saving_options(session, user_id: int) -> list[dict]:
    """Offer comparisons only for currently connected active merchants."""
    connected = await SubscriptionService(session).list_user_subscriptions(user_id)
    active = {row['merchant_name']: row for row in connected if row['status'] == 'ACTIVE'}
    events = (await session.scalars(select(ContextEvent).where(ContextEvent.user_id == user_id,
        ContextEvent.event_type == 'SUBSCRIPTION_OFFER', ContextEvent.status == 'ACTIVE'))).all()
    options = []
    for event in events:
        data = event.payload or {}
        row = active.get(data.get('current'))
        if not row or row['period'] != 'MONTHLY' or not data.get('alternative'):
            continue
        try:
            cost = Decimal(str(row['amount']))
            alternative = Decimal(str(data['alternative_price']))
        except (KeyError, ValueError, ArithmeticError):
            continue
        if not alternative.is_finite() or alternative < 0 or alternative >= cost:
            continue
        options.append({'merchant': row['merchant_name'], 'alternative': data['alternative'],
            'monthly_saving': money(cost-alternative), 'annual_saving': money((cost-alternative)*12),
            'note': '基于已授权优惠事件；先核实优惠资格与服务差异，再独立确认取消或更换。'})
    return options
