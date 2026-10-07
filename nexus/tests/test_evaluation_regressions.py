"""Regression checks for the independently observed evaluation failures."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from nexus.backend.core.models import Account, AuditLog, Transaction, PlanOrder, DemoAction, ContextEvent
from nexus.backend.core.exceptions import BusinessRuleException
from nexus.backend.services.cross_scene_service import CrossSceneService
from nexus.backend.services.scheduled_transfer_service import ScheduledTransferService
from nexus.backend.services.demo_execution import run_due
from nexus.backend.agent import model, presentation
from nexus.backend.agent.understanding import Understanding
from nexus.backend.agent.scenarios import scenario_parameters


async def say(client, text):
    r = await client.post('/api/messages', json={'message': text, 'request_id': str(uuid4())})
    assert r.status_code == 200, r.text
    return r.json()


async def test_budget_rejection_does_not_reserve_or_create_orders(db, seeded):
    with pytest.raises(BusinessRuleException, match='超过预算'):
        async with db() as s:
            await CrossSceneService(s).create_birthday_plan(seeded['user'], date.today()+timedelta(days=14), D('500'), 'A')
    async with db() as s:
        assert (await s.get(Account, seeded['account'])).available_balance == D('1000')
        assert await s.scalar(select(func.count()).select_from(PlanOrder)) == 0


async def test_birthday_due_simulates_once_and_preserves_reservation(db, seeded):
    at = date.today()+timedelta(days=14)
    async with db() as s:
        plan = await CrossSceneService(s).create_birthday_plan(seeded['user'], at, D('980'), 'A')
        pid = plan.id
    assert (await run_due(datetime.combine(at-timedelta(days=3), datetime.min.time()), seeded['user']))['birthday_orders'] == []
    result = await run_due(datetime.combine(at-timedelta(days=2), datetime.min.time()), seeded['user'])
    assert result['birthday_orders'][0]['order_total'] == '980.00'
    assert result['birthday_orders'][0]['simulation'] is True
    assert (await run_due(datetime.combine(at, datetime.min.time()), seeded['user']))['birthday_orders'] == []
    async with db() as s:
        a = await s.get(Account, seeded['account'])
        assert a.available_balance == D('20') and a.reserved_balance == D('980')
        items = (await s.scalars(select(PlanOrder).where(PlanOrder.plan_id == pid))).all()
        assert all(i.status == 'SIMULATED_PLACED' for i in items)


async def test_schedule_due_once_restarts_and_conserves_money(db, seeded):
    due = date.today()+timedelta(days=20)
    due = due.replace(day=min(due.day, 28))
    async with db() as s:
        p = await ScheduledTransferService(s).create_monthly(seeded['user'], seeded['recipient'], D('100'), due.day, '房租', due)
        pid = p.id
    now = datetime.combine(due, datetime.min.time())+timedelta(hours=9)
    assert (await run_due(now-timedelta(seconds=1), seeded['user']))['transfers'] == []
    first = await run_due(now, seeded['user'])
    assert first['transfers'][0]['status'] == 'COMPLETED'
    assert (await run_due(now, seeded['user']))['transfers'] == []
    async with db() as s:
        assert (await s.get(Account, seeded['account'])).balance == D('900')
        assert (await s.get(Account, seeded['destination'])).balance == D('200')
        assert await s.scalar(select(func.count()).select_from(Transaction)) == 1
        await ScheduledTransferService(s).pause(seeded['user'], pid)
    assert (await run_due(now+timedelta(days=62), seeded['user']))['transfers'] == []


async def test_schedule_insufficient_balance_pauses_without_partial_writes(db, seeded):
    due = date.today().replace(day=5)
    async with db() as s:
        await ScheduledTransferService(s).create_monthly(seeded['user'], seeded['recipient'], D('2000'), 5, '房租', due)
    result = await run_due(datetime.combine(due, datetime.min.time())+timedelta(hours=9), seeded['user'])
    assert result['transfers'][0]['status'] == 'PAUSED'
    async with db() as s:
        assert (await s.get(Account, seeded['account'])).balance == D('1000')
        assert await s.scalar(select(func.count()).select_from(Transaction)) == 0


async def test_legacy_schedule_without_new_consent_never_executes(db, seeded):
    due = date.today().replace(day=5)
    async with db() as s:
        p = await ScheduledTransferService(s).create_monthly(seeded['user'], seeded['recipient'], D('100'), 5, '旧计划', due)
        consent = await s.scalar(select(AuditLog).where(AuditLog.action == 'AUTHORIZE_SCHEDULED_EXECUTION', AuditLog.target_id == p.id))
        await s.delete(consent)
    assert (await run_due(datetime.combine(due, datetime.min.time())+timedelta(hours=9), seeded['user']))['transfers'] == []


async def test_explicit_year_is_honored_even_if_model_chooses_current_year(client, monkeypatch):
    async def understand(*args):
        return Understanding(scene='bill_analysis', period='year', read_tools=['bills'])
    monkeypatch.setattr(model, 'understand', understand)
    result = await say(client, '生成2025年年度账单报告')
    assert result['period']['label'] == '2025 年度'
    assert result['empty'] is True
    assert result['presentation']['order'] == ['head']


async def test_revision_invalidates_old_card_including_step_up(client, monkeypatch, db):
    async def understand(message, context=None):
        return Understanding(scene='transfer', operation='transfer', recipient='张三', amount='200' if '200' in message else '100', amount_evidence='200元' if '200' in message else '100元', write_intent=True, read_tools=['recipients'])
    monkeypatch.setattr(model, 'understand', understand)
    old = await say(client, '给张三转100元')
    await client.post(f"/api/actions/{old['action_id']}/confirm")
    new = await say(client, '改成200元')
    stale = await client.post(f"/api/actions/{old['action_id']}/step-up", json={'echoes': {'amount': '100'}, 'passcode': '2468'})
    assert stale.status_code == 200
    assert stale.json()['type'] == 'message'
    async with db() as s:
        assert (await s.get(DemoAction, old['action_id'])).status == 'SUPERSEDED'
        assert (await s.get(DemoAction, new['action_id'])).status == 'PENDING'
        assert await s.scalar(select(func.count()).select_from(Transaction)) == 0


async def test_income_scenario_uses_user_drop_target_without_profile_mutation(client, db, monkeypatch):
    async def understand(*args):
        return Understanding(scene='financial_planning', read_tools=['financial_profile'])
    monkeypatch.setattr(model, 'understand', understand)
    result = await say(client, '我下个月收入会少一半，能不能继续每月存3000元？')
    assert 'scenario' in result, result
    scenario = result['scenario']
    assert scenario['income_drop_pct'] == '50' and scenario['target'] == '3000'
    assert D(scenario['income']) == D(scenario['baseline_income'])/2
    assert D(scenario['gap']) == max(D('3000')-D(scenario['surplus']), D(0))
    assert '无法覆盖' in result['summary']


async def test_unused_subscription_filter_does_not_offer_unknown_usage(client, monkeypatch):
    async def understand(*args):
        return Understanding(scene='subscription', operation='cancel_subscription', write_intent=True)
    monkeypatch.setattr(model, 'understand', understand)
    result = await say(client, '帮我清理过去三个月没用过的订阅')
    assert result['candidates'] == []
    assert set(result['unknown_usage']) == {'云音乐', '视频会员'}
    assert not result['actions'] and '缺少使用记录' in result['message']


async def test_events_preserve_needed_benefits_and_filter_secrets(client, db):
    from nexus.backend.agent.toolkit import get_events
    async with db() as s:
        event = await s.scalar(select(ContextEvent).where(ContextEvent.event_type=='CARD_BENEFIT'))
        uid = event.user_id
        event.payload = {**event.payload, 'api_key': 'must-not-leak'}
    result = await get_events(uid)
    benefit = next(e for e in result['events'] if e['type']=='CARD_BENEFIT')
    assert benefit['payload']['lounge_visits'] == 2
    assert 'api_key' not in benefit['payload']


@pytest.mark.parametrize('message,drop,target', [('收入下降50%，每月存3000元', D(50), D(3000)), ('收入减半，每月存3000元', D(50), D(3000)), ('收入下降150%，每月存3000元', None, D(3000))])
def test_literal_scenario_bounds(message, drop, target):
    assert scenario_parameters(message) == (drop, target)
