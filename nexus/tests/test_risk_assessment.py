"""The investor risk assessment: two legs, one conservative grade.

These tests exist because a suitability grade is a sales promise. A wrong one
does not crash anything — it quietly puts an R5 product in front of a customer
who said they cannot lose 5% — so the rules get checked directly rather than only
through the UI.
"""
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from nexus.backend.agent import risk_assessment as rules
from nexus.backend.api.app import app
from nexus.backend.core.models import FinancialProfile, RiskAssessment, User

HEADERS = {'X-Nexus-Demo': '1'}

ALL_ANSWERS = {
    "experience": "1-3年", "max_loss": "15%以内",
    "horizon": "1-3年", "purpose": "稳健增值",
}


# ============ 客观财务 BMI ============
def strong_finances(**overrides):
    """A customer who can genuinely absorb a drawdown."""
    inputs = {
        "annual_income": Decimal("600000"),
        "annual_expenses": Decimal("300000"),
        "monthly_income": Decimal("50000"),
        "essential_expenses": Decimal("18000"),
        "monthly_debt_payment": Decimal("3000"),
        "liquid_assets": Decimal("600000"),
        "total_assets": Decimal("2000000"),
        "total_debt": Decimal("600000"),
        "monthly_net": [Decimal("30000")] * 12,
    }
    inputs.update(overrides)
    return inputs


def test_objective_rewards_savings_cushion_and_low_debt():
    strong = rules.score_objective(**strong_finances())
    thin = rules.score_objective(**strong_finances(
        annual_expenses=Decimal("570000"), liquid_assets=Decimal("24000"),
        total_assets=Decimal("90000"), total_debt=Decimal("600000"),
        monthly_debt_payment=Decimal("26000"),
    ))
    assert strong["score"] > thin["score"]
    assert all(0 <= item["score"] <= item["max"] for item in strong["components"])
    assert 0 <= thin["score"] <= 100


def test_objective_components_report_value_rating_and_basis():
    result = rules.score_objective(**strong_finances())
    names = [item["name"] for item in result["components"]]
    assert names == ["年度结余率", "现金流安全垫", "负债收入比", "收入稳定性", "资产厚度"]
    for item in result["components"]:
        # 体检报告的三要素：数值、评价、算法依据。一个都不能少。
        assert item["value"] and item["rating"] and item["basis"]
    assert sum(item["max"] for item in result["components"]) == 100


def test_seasonal_income_shows_up_as_volatility_not_as_a_smaller_score():
    steady = rules.score_objective(**strong_finances())
    # 同样的年收入，淡旺季差额很大：一年结余率一样，稳定性得分必须更低。
    seasonal = [Decimal("60000")] * 6 + [Decimal("20000")] * 6
    swung = rules.score_objective(**strong_finances(monthly_net=seasonal))
    volatility = next(i for i in steady["components"] if i["name"] == "收入稳定性")
    volatility_swung = next(i for i in swung["components"] if i["name"] == "收入稳定性")
    assert volatility_swung["score"] < volatility["score"]
    assert "波动" in volatility_swung["rating"] or "季节性" in volatility_swung["rating"]


def test_zero_income_does_not_divide_by_zero():
    result = rules.score_objective(**strong_finances(
        annual_income=Decimal("0"), monthly_income=Decimal("0"), monthly_net=[Decimal("0")] * 12,
    ))
    assert 0 <= result["score"] <= 100


# ============ 主观问卷 ============
def test_subjective_requires_every_answer_and_names_the_gap():
    partial = rules.score_subjective({"experience": "1-3年", "max_loss": "不能亏"})
    assert partial["missing"] == ["这笔钱多久不用", "这笔钱的主要目的"]
    # 只累加答了的题；`submit` 看到 missing 就整份拒收，所以这个分数不会被用上。
    assert partial["raw"] == 14.0
    full = rules.score_subjective(ALL_ANSWERS)
    assert full["missing"] == []
    ceiling = rules.score_subjective(
        {"experience": "3年以上", "max_loss": "30%以上", "horizon": "5年以上", "purpose": "长期增值"})
    assert ceiling["score"] == 100
    assert 0 < full["score"] < ceiling["score"]


def test_loss_tolerance_carries_the_most_weight():
    # 亏损容忍度不是偏好，是"跌到那个幅度还会不会持有"的前提，所以它最重。
    loss = rules.score_subjective({**ALL_ANSWERS, "max_loss": "不能亏"})
    purpose = rules.score_subjective({**ALL_ANSWERS, "purpose": "保本为主"})
    assert loss["score"] < purpose["score"]


def test_unknown_answer_is_treated_as_unanswered():
    result = rules.score_subjective({**ALL_ANSWERS, "horizon": "下辈子"})
    assert result["missing"] == ["这笔钱多久不用"]


# ============ 审慎原则 ============
def test_the_more_conservative_side_wins_regardless_of_who_scored_higher():
    down = rules.combine(objective_score=88, subjective_score=20)
    assert down["objective_grade"] == "C5" and down["subjective_grade"] == "C2"
    assert down["grade"] == "C2" and down["binding"] == "SUBJECTIVE"
    # 分数被压到绑定等级的区间上限，数字和字母不会在同一页上自相矛盾。
    assert down["downgraded"] and down["final"] <= rules.band_max("C2")
    assert rules.grade_of(down["final"])[0] == "C2"

    up = rules.combine(objective_score=12, subjective_score=90)
    assert up["grade"] == "C1" and up["binding"] == "OBJECTIVE"
    assert up["downgraded"] and rules.grade_of(up["final"])[0] == "C1"


def test_matching_halves_are_not_downgraded():
    merged = rules.combine(objective_score=52, subjective_score=50)
    assert merged["binding"] == "MATCH"
    assert not merged["downgraded"]
    assert merged["final"] == merged["blended"]
    assert merged["grade"] == "C3"


def test_grade_and_score_never_disagree():
    for objective in range(0, 101, 5):
        for subjective in range(0, 101, 5):
            merged = rules.combine(objective, subjective)
            assert merged["grade"] == rules.grade_of(merged["final"])[0], (objective, subjective)
            assert merged["grade"] in {"C1", "C2", "C3", "C4", "C5"}


# ============ 输出：配置与禁忌 ============
def test_allocation_shrinks_risk_when_the_cushion_is_thin():
    thin = rules.score_objective(**strong_finances(liquid_assets=Decimal("20000")))
    thin_buckets = rules.allocation("C4", thin)
    thick_buckets = rules.allocation("C4", rules.score_objective(**strong_finances()))
    assert next(b for b in thin_buckets if b["name"] == "现金与货币基金")["weight"] > \
           next(b for b in thick_buckets if b["name"] == "现金与货币基金")["weight"]
    assert sum(b["weight"] for b in thin_buckets) == 100


def test_conservative_grades_never_allocate_to_equities():
    for grade in ("C1", "C2"):
        buckets = rules.allocation(grade, rules.score_objective(**strong_finances()))
        assert not any("权益" in bucket["name"] for bucket in buckets)
        assert rules.GRADE_ADVICE[grade]["forbid"]


def test_warnings_name_the_weak_link_instead_of_a_generic_boilerplate():
    weak = rules.score_objective(**strong_finances(
        annual_expenses=Decimal("590000"), liquid_assets=Decimal("18000"),
        monthly_debt_payment=Decimal("24000"),
    ))
    titles = [item["title"] for item in rules.warnings(weak, "C1")]
    assert "应急资金短缺" in titles
    assert "负债压力偏高" in titles
    healthy = rules.warnings(rules.score_objective(**strong_finances()), "C4")
    assert healthy[0]["title"] == "未发现明显短板"


def test_expiry_is_twelve_months_out():
    from datetime import date
    assert rules.expiry(date(2026, 1, 31)) == date(2027, 1, 31)
    assert rules.expiry(date(2026, 12, 15)) == date(2027, 12, 15)


# ============ 端到端：档案 → 问卷 → 报告 ============
async def message(client, text, request_id=None):
    from httpx import AsyncClient as _C
    response = await client.post('/api/messages', json={'message': text, 'request_id': request_id or str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


async def test_report_is_rebuilt_from_the_stored_record_on_a_later_request(client, db):
    """The JSON columns come back as strings after a restart.

    Scoring has to survive that: the very first request after a restart reads the
    seasonal income/expense rows out of the database rather than out of the
    session that wrote them, and a calculator that only ever saw Decimals in
    memory would blow up exactly there.
    """
    async with db() as session:
        user_id = (await session.scalar(select(User))).id

    intake = (await client.get('/api/risk-assessment')).json()
    assert intake['type'] == 'risk_intake'
    assert len(intake['questions']) == 4
    # 问卷之前先把已经掌握的客观半边亮出来，客户知道自己是在补哪一半。
    assert intake['objective_preview']['grade'].startswith('C')
    assert intake['objective_preview']['metrics']['safety_months'] > 0

    answered = await client.post('/api/risk-assessment', json=ALL_ANSWERS)
    assert answered.status_code == 200, answered.text
    report = answered.json()
    assert report['type'] == 'risk_report'
    assert report['grade'] in {'C1', 'C2', 'C3', 'C4', 'C5'}
    assert report['max_product_risk'] == rules.GRADE_MAX_RISK[report['grade']]
    assert len(report['checklist']) == 5
    assert report['allocation'] and report['warnings'] and report['advice']['forbid']
    assert [step['label'] for step in report['trace']] == [
        '读取财务档案', '客观承受能力', '主观风险偏好', '审慎原则']

    # 换一个全新的数据库会话重算一次，模拟进程重启后的首次读取。
    async with db() as session:
        record = await session.scalar(
            select(RiskAssessment).where(RiskAssessment.user_id == user_id))
        assert record is not None
    again = (await client.get('/api/risk-assessment')).json()
    assert again['type'] == 'risk_report'
    assert again['grade'] == report['grade']
    assert again['score'] == report['score']


async def test_grade_is_written_back_to_the_customer_profile(client, db):
    async with db() as session:
        user_id = (await session.scalar(select(User))).id
    report = (await client.post('/api/risk-assessment', json=ALL_ANSWERS)).json()
    async with db() as session:
        user = await session.get(User, user_id)
    # 等级是对外承诺，产品适配校验读的是同一个值，所以必须落到档案上。
    assert user.risk_score == report['grade']
    assert user.investment_style == report['grade_label']


async def test_a_half_finished_questionnaire_records_nothing(client, db):
    incomplete = {**ALL_ANSWERS, "max_loss": ""}
    response = await client.post('/api/risk-assessment', json=incomplete)
    assert response.status_code == 400
    # 告诉客户漏了哪一题，而不是丢一个字段校验清单过去。
    assert '最大亏损' in response.json()['detail']
    async with db() as session:
        assert await session.scalar(
            select(RiskAssessment).limit(1)) is None
    # 拒绝之后仍然是问卷，不是半成品报告。
    assert (await client.get('/api/risk-assessment')).json()['type'] == 'risk_intake'


async def test_answers_the_model_cannot_invent_are_rejected(client):
    for bad in ({"experience": "1-3年"}, ALL_ANSWERS | {"horizon": "下辈子"}):
        response = await client.post('/api/risk-assessment', json=bad)
        assert response.status_code == 400, bad
    assert (await client.post('/api/risk-assessment', json=ALL_ANSWERS | {"grade": "C5"})).status_code == 422
    assert (await client.get('/api/risk-assessment')).json()['type'] == 'risk_intake'


async def test_without_a_financial_profile_the_questionnaire_refuses_to_start(client, db):
    async with db() as session:
        await session.execute(delete(FinancialProfile))
        await session.commit()
    intake = (await client.get('/api/risk-assessment')).json()
    # 没有档案就没有客观半边，也就无从谈审慎原则，只能请客户先补资料。
    assert intake['type'] == 'message'
    assert intake['needs_input'] and '财务档案' in intake['message']
    assert (await client.post('/api/risk-assessment', json=ALL_ANSWERS)).status_code == 400


async def test_an_expired_grade_is_not_offered_for_sales(client, db):
    from datetime import date, timedelta
    await client.post('/api/risk-assessment', json=ALL_ANSWERS)
    async with db() as session:
        record = await session.scalar(select(RiskAssessment).limit(1))
        record.valid_until = date.today() - timedelta(days=1)
        await session.commit()
    # 过期的等级不能再拿来当结论，客户必须重新答一遍。
    assert (await client.get('/api/risk-assessment')).json()['type'] == 'risk_intake'


async def test_risk_question_reaches_the_intake_card(client):
    answer = await message(client, '我要做个风险测评')
    assert answer['type'] == 'risk_intake'
    assert len(answer['questions']) == 4
