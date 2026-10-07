"""Reports keep their visible totals and underlying records consistent."""
from datetime import date
from decimal import Decimal
import pytest
from nexus.backend.agent import model, graph
from nexus.backend.agent.graph import router_node
from nexus.backend.agent.understanding import Understanding
from nexus.backend.agent.bill_analysis import build_bill_analysis
from nexus.backend.core.models import ImportBatch, StatementTransaction


async def test_report_details_daily_and_categories_reconcile_and_are_user_scoped(db, seeded):
    today = date.today()
    async with db() as s:
        batch = ImportBatch(user_id=seeded['user'], file_name='report', file_type='DEMO', status='COMPLETED')
        s.add(batch)
        await s.flush()
        for amount, merchant in [('12.34', '街角餐厅'), ('56.78', '城市地铁')]:
            s.add(StatementTransaction(batch_id=batch.id, user_id=seeded['user'], txn_date=today,
                amount=Decimal(amount), merchant_name=merchant))
        await s.flush()
        report = await build_bill_analysis(s, seeded['user'], 'month')
        assert sum(Decimal(str(row['amount_value'])) for row in report['transactions']) == Decimal('69.12')
        assert sum(Decimal(str(row['amount_value'])) for row in report['daily_spending']) == Decimal('69.12')
        assert sum(Decimal(str(row['amount_value'])) for row in report['categories']) == Decimal('69.12')
        assert report['summary']['transaction_count'] == 2
        assert report['daily_spending'][-1]['date'] == today.isoformat()
        assert (await build_bill_analysis(s, seeded['user']+1000, 'month'))['empty'] is True


@pytest.mark.parametrize('scene', ['bill_analysis', 'financial_planning', 'cross_scene'])
async def test_compound_read_keeps_all_requested_domains(monkeypatch, scene):
    async def understand(*args):
        return Understanding(scene=scene, read_tools=['bills'])
    monkeypatch.setattr(model, 'understand', understand)
    async def prior(*args): return {}
    monkeypatch.setattr(graph, '_prior_slots', prior)
    result = await router_node({'message':'结合我的余额、消费账单和订阅，制定稳健理财计划', 'request_id':'r', 'token':None})
    data = result['understanding']
    assert data['scene'] == 'cross_scene'
    assert {'account','bills','subscriptions','subscription_usage','financial_profile','products'} <= set(data['read_tools'])
    assert data['write_intent'] is False


async def test_compound_keywords_never_promote_authorized_write(monkeypatch):
    async def understand(*args):
        return Understanding(scene='financial_profile', operation='subscribe_product', write_intent=True,
            amount='100', amount_evidence='100元', product_code='P001', read_tools=['products'])
    monkeypatch.setattr(model, 'understand', understand)
    async def prior(*args): return {}
    monkeypatch.setattr(graph, '_prior_slots', prior)
    result = await router_node({'message':'结合余额和账单，申购P001理财100元', 'request_id':'r', 'token':None})
    assert result['understanding']['scene'] == 'financial_profile'
    assert result['understanding']['write_intent'] is True


async def test_cross_scene_income_scenario_uses_debt_and_living_costs(client, monkeypatch):
    from uuid import uuid4
    async def understand(*args):
        return Understanding(scene='cross_scene', read_tools=['bills','subscriptions'])
    monkeypatch.setattr(model, 'understand', understand)
    r = await client.post('/api/messages', json={'message':'结合下个月收入减少一半和每月存3000元目标，分析消费与订阅', 'request_id':str(uuid4())})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['type'] == 'financial_analysis'
    assert data['scenario']['affordable'] is False
    assert Decimal(data['scenario']['gap']) == Decimal('3000')-Decimal(data['scenario']['surplus'])
    assert data['recommendation'] is None
    assert data['supporting_bill']['summary']['transaction_count'] > 0


async def test_negated_write_still_completes_explicit_read_comparison(client):
    from uuid import uuid4
    r = await client.post('/api/messages', json={'message':'我只想比较消费和理财，不要转账，也不要替我购买任何产品', 'request_id':str(uuid4())})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['type'] == 'financial_analysis'
    assert 'supporting_bill' in data and 'action_id' not in data
    assert data['trace'][0]['label'] == '只读比较'


async def test_event_tool_only_returns_requested_domain(client, db):
    from sqlalchemy import select
    from nexus.backend.core.models import ContextEvent
    from nexus.backend.agent.toolkit import get_events
    async with db() as s:
        uid = await s.scalar(select(ContextEvent.user_id).limit(1))
    result = await get_events(uid, ['SUBSCRIPTION_USAGE'])
    assert result['events'] and all(row['type'] == 'SUBSCRIPTION_USAGE' for row in result['events'])
    assert (await get_events(uid, []))['events'] == []


async def test_saving_options_only_cover_connected_offer_and_exact_savings(client, db):
    from sqlalchemy import select
    from nexus.backend.core.models import ContextEvent
    from nexus.backend.agent.scenarios import subscription_saving_options
    async with db() as s:
        uid = await s.scalar(select(ContextEvent.user_id).limit(1))
        items = await subscription_saving_options(s, uid)
    assert len(items) == 1
    assert items[0]['merchant'] == '视频会员'
    assert items[0]['monthly_saving'] == '¥10.00' and items[0]['annual_saving'] == '¥120.00'
