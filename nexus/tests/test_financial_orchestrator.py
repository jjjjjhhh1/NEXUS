from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from nexus.backend.agent.planning.financial_orchestrator import fallback_plan, resolve_plan, calculate_goal, aggregate
from nexus.backend.agent.analysis import financial_analysis as financial

@pytest.fixture
def baseline():
    return {'type':'financial_analysis', 'balance_sheet':{'cash':'¥150,000.00','investments':'¥20,000.00','assets':'¥170,000.00','net_worth':'¥100,000.00','liabilities':'¥70,000.00'}, 'allocation':{'buckets':[{'target':'¥60,000.00'}]}, 'cashflow':{'surplus':'¥2,190.33'}, 'profile':{'risk_score':'C4','style':'稳健'}, 'decision':{'max_product_risk':'R3'}, 'product_matches':[{'code':'r3','risk_level':'R3','lock_days':0,'min_purchase':'¥100.00'},{'code':'r1','risk_level':'R1','lock_days':0,'min_purchase':'¥100.00'}]}

@pytest.mark.parametrize('text,amount,months', [('一个月靠理财赚100万','1000000',1),('一个月赚一百万','1000000',1),('两年攒十万','100000',24),('12个月赚10万元','100000',12)])
def test_explicit_goal(text,amount,months):
    plan=fallback_plan(text)
    assert Decimal(plan.amount)==Decimal(amount)
    assert plan.months==months


def test_extreme_math_and_no_stale_goal(baseline):
    before=deepcopy(baseline)
    plan=fallback_plan('我要制定理财计划，首要目标是一个月靠理财赚100万')
    report=aggregate(plan,baseline,'三年购房首付',plan.objective)
    result=report['orchestration']['results']['goal_calculation']['data']
    assert result['principal']=='¥90,000.00'
    assert result['period_return_pct']=='1111.11'
    assert report['decision']['monthly_required']=='¥1,000,000.00'
    assert report['decision']['goal_feasibility']=='EXTREME'
    assert report['health']['components'][0]['score']==0
    assert report['recommendation']['buy']==[]
    assert report['product_matches']==[]
    assert '首付' not in report['summary']
    assert '9,833.33' not in str(report)
    assert report['orchestration']['goal_conflict']
    assert len(report['orchestration']['results'])==6
    assert baseline==before


def test_zero_principal_is_not_division_error(baseline):
    baseline['balance_sheet']['cash']='¥0.00'
    goal=calculate_goal(fallback_plan('一个月赚100万'),baseline)
    assert goal['status']=='EXTREME'
    assert goal['period_return_pct'] is None


@pytest.mark.parametrize('suffix,flag',[('借钱投资','BORROW_TO_INVEST'),('保证收益','RETURN_PROMISE'),('推荐R3产品','HIGH_RISK_PRODUCT')])
def test_red_lines(baseline,suffix,flag):
    message='一个月赚100万，'+suffix
    report=aggregate(fallback_plan(message),baseline,'三年购房首付',message)
    assert flag in report['orchestration']['results']['risk_compliance']['data']['flags']
    assert report['product_matches']==[]


def test_model_cannot_invent_amount_or_deadline():
    text='一个月赚100万'
    proposed=fallback_plan(text).model_dump()
    proposed['amount']='100'
    assert resolve_plan(text,proposed) is None
    proposed=fallback_plan(text).model_dump();proposed['months']=36
    assert resolve_plan(text,proposed) is None
    proposed=fallback_plan(text).model_dump();proposed['evidence']='旧资料目标'
    assert resolve_plan(text,proposed) is None


def test_multiple_goals_are_clarified():
    assert fallback_plan('一个月赚100万，同时一个月攒200万').confidence<0.8


def test_negated_goal_not_executed():
    assert fallback_plan('不要一个月赚100万') is None


@pytest.mark.asyncio
async def test_missing_period_asks_without_old_plan(monkeypatch):
    monkeypatch.setattr(financial,'get_profile',AsyncMock(return_value=SimpleNamespace(goal_name='三年购房首付')))
    calculate=AsyncMock();monkeypatch.setattr(financial,'_build_financial_analysis',calculate)
    report=await financial.build_financial_analysis(None,1,'靠理财赚100万')
    assert report['needs_input']
    calculate.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_target_integration(monkeypatch,baseline):
    profile=SimpleNamespace(goal_name='三年购房首付')
    monkeypatch.setattr(financial,'get_profile',AsyncMock(return_value=profile))
    monkeypatch.setattr(financial,'_build_financial_analysis',AsyncMock(return_value=baseline))
    report=await financial.build_financial_analysis(None,1,'一个月赚100万')
    assert report['request_focus']=='current_goal'
    assert profile.goal_name=='三年购房首付'


@pytest.mark.asyncio
async def test_low_confidence_asks(monkeypatch):
    monkeypatch.setattr(financial,'get_profile',AsyncMock(return_value=None))
    proposed=fallback_plan('一个月赚100万').model_dump();proposed['confidence']=0.2
    report=await financial.build_financial_analysis(None,1,'一个月赚100万',{'financial_task':proposed})
    assert report['needs_input']
    assert report['type']=='message'

@pytest.mark.asyncio
async def test_ai_plan_routes_to_financial_dispatcher(monkeypatch):
    from nexus.backend.agent.orchestration import graph
    from nexus.backend.agent.contracts.understanding import Understanding
    message='一个月赚100万'
    plan=fallback_plan(message).model_dump();plan['source']='model';plan['modules']=['goal_calculation']
    understood=Understanding(scene='cross_scene',financial_task=plan,write_intent=False,confidence=0.95)
    monkeypatch.setattr(graph,'_prior_slots',AsyncMock(return_value={}))
    monkeypatch.setattr(graph.model,'is_configured',lambda:True)
    model=AsyncMock(return_value=understood);monkeypatch.setattr(graph.model,'understand',model)
    result=await graph.router_node({'message':message})
    assert result['engine']==graph.READ_FINANCIAL
    assert result['understanding']['financial_task']['modules']==['goal_calculation']
    model.assert_awaited_once()


@pytest.mark.asyncio
async def test_financial_goal_never_overrides_security_veto(monkeypatch):
    from nexus.backend.agent.orchestration import graph
    from nexus.backend.agent.contracts.understanding import Understanding
    monkeypatch.setattr(graph,'_prior_slots',AsyncMock(return_value={}))
    monkeypatch.setattr(graph.model,'is_configured',lambda:True)
    monkeypatch.setattr(graph.model,'understand',AsyncMock(return_value=Understanding(scene='attack',confidence=1.0)))
    result=await graph.router_node({'message':'一个月赚100万'})
    assert result['engine']==graph.BLOCKED_BRANCH


def test_old_deadline_does_not_override_current(baseline):
    plan=fallback_plan('原来三年购房，现在一个月赚100万')
    assert plan.months==1


def test_mandatory_modules_cannot_be_omitted(baseline):
    plan=fallback_plan('一个月赚100万');plan.modules=['goal_calculation']
    report=aggregate(plan,baseline,'购房','一个月赚100万')
    assert 'risk_compliance' in report['orchestration']['results']
    assert report['product_matches']==[]
