"""理财建议必须先回答“买什么、不买什么”，而且每个字都要有算过的数支撑。

这一组测试守的是信息架构，不只是数值：卡片可以调整排版，但不允许退回到
“把数据列一遍就当建议”的状态。build_recommendation 是纯函数，所以这里不
需要数据库，也不需要模型。
"""
from decimal import Decimal

import pytest

from nexus.backend.agent.financial_analysis import build_recommendation, _risk_cap_detail, _product_reasons, _buy_reasons


def cap(user_risk="C4", drawdown="12", horizon=36):
    return _risk_cap_detail(user_risk, Decimal(drawdown), horizon)


def card(code, level, lock_days, yield_pct, min_purchase):
    return {"code": code, "name": f"产品{code}", "risk_level": level, "lock_days": lock_days,
            "reference_yield": f"{yield_pct}%", "min_purchase": f"¥{min_purchase:,.2f}"}


def build(**overrides):
    base = dict(
        investable=Decimal("1300.00"), matched=[], rejected=[], risk_detail=cap(),
        emergency_gap=Decimal("0"), emergency_months=6, debt_balance=Decimal("320000"),
        debt_rate=Decimal("3.9"), monthly_surplus=Decimal("2208.33"),
        required_monthly=Decimal("9833.33"), goal_gap=Decimal("354000"),
        goal_name="三年购房首付", feasibility="TIGHT", horizon_months=36,
    )
    return build_recommendation(**{**base, **overrides})


# ---------- 风险上限：必须说清是谁在卡 ----------

def test_risk_cap_names_the_binding_constraint():
    """R2 是三条约束里最小的那条。只回一个 R2，用户不知道该改什么。"""
    detail = cap()
    assert detail["cap"] == 2
    assert detail["binding"] == "目标期限"
    assert "36 个月" in detail["binding_reason"]
    assert {c["name"] for c in detail["constraints"]} == {"可接受回撤", "风险测评", "目标期限"}


def test_risk_cap_binding_follows_the_tightest_input():
    assert cap(drawdown="3")["binding"] == "可接受回撤"
    assert cap(user_risk="C2")["binding"] == "风险测评"
    assert cap(horizon=120)["cap"] == 3 and cap(horizon=120)["binding"] == "可接受回撤"


def test_untested_risk_profile_does_not_invent_a_grade():
    detail = _risk_cap_detail("", Decimal("12"), 36)
    assert detail["cap"] == 1
    assert "尚未做风险测评" in detail["binding_reason"]


# ---------- 排除理由：逐条，不笼统 ----------

class FakeProduct:
    def __init__(self, code, level, lock_days, min_purchase):
        self.code, self.name = code, f"产品{code}"
        self.risk_level, self.lock_days = level, lock_days
        self.min_purchase = Decimal(min_purchase)


class FakeProfile:
    horizon_months = 36


def selectable(code, level, lock_days, yield_pct, min_purchase):
    """按产品线真正的组装方式造一张会进 buy 的卡，包含它必须带的理由。"""
    product = FakeProduct(code, level, lock_days, min_purchase)
    return {**card(code, level, lock_days, yield_pct, min_purchase),
            "why": _buy_reasons(product, cap(), FakeProfile(), Decimal("1300.00"))}


def test_each_rejection_carries_its_own_reason():
    detail, investable = cap(), Decimal("1300")
    horizon = FakeProfile()
    too_risky = _product_reasons(FakeProduct("A", "R3", 30, "1000"), detail, horizon, investable)
    assert too_risky and "R2" in too_risky[0] and "目标期限 36 个月" in too_risky[0]

    too_long = _product_reasons(FakeProduct("B", "R1", 1200, "100"), detail, horizon, investable)
    assert any("锁定 1200 天" in reason for reason in too_long)

    too_expensive = _product_reasons(FakeProduct("C", "R1", 0, "5000"), detail, horizon, investable)
    assert any("起购" in reason and "¥5,000.00" in reason for reason in too_expensive)

    assert _product_reasons(FakeProduct("D", "R1", 0, "100"), detail, horizon, investable) == []


# ---------- 结论：三种处境各说各的话 ----------

def test_verdict_pulls_the_emergency_cushion_first():
    rec = build(emergency_gap=Decimal("8000"), investable=Decimal("0"))
    assert "先别买" in rec["verdict"]
    assert "¥8,000.00" in rec["verdict"]
    assert rec["buy"] == [] and rec["investable"]["amount"] == "¥0.00"


def test_verdict_says_debt_first_when_borrowing_costs_more():
    """负债 3.9% 高于产品 2.8% 时，替用户省下的是钱：先还债，别买债券。"""
    rec = build(matched=[selectable("NX-CASH", "R1", 0, 1.85, 100), selectable("NX-BOND", "R2", 30, 2.80, 1000)])
    assert "还债比买理财更划算" in rec["verdict"]
    assert "3.90%" in rec["verdict"] and "2.80%" in rec["verdict"]
    assert rec["debt_comparison"]["conclusion"] == "还债更划算"
    assert any("优先偿还高息负债" in action for action in rec["actions"])


def test_verdict_does_not_compare_when_the_debt_rate_is_unknown():
    """利率没填就没有比较对象。宁可不给结论，也不给一个错的。"""
    rec = build(debt_rate=Decimal("0"), matched=[card("NX-CASH", "R1", 0, 1.85, 100)])
    assert "还债比买理财更划算" not in rec["verdict"]
    assert rec["debt_comparison"]["conclusion"] == "本报告不作比较"
    assert any("补充你的负债利率" in action for action in rec["actions"])


def test_verdict_admits_when_the_goal_cannot_be_met():
    rec = build(matched=[selectable("NX-CASH", "R1", 0, 1.85, 100)], debt_balance=Decimal("0"), debt_rate=Decimal("0"))
    assert any("¥2,208.33" in action and "¥9,833.33" in action for action in rec["actions"])


# ---------- 结构：卡片第一屏要能独立回答问题 ----------

def test_recommendation_answers_before_it_shows_data():
    rec = build(matched=[selectable("NX-CASH", "R1", 0, 1.85, 100)])
    assert rec["verdict"] and rec["investable"]["amount"] and rec["risk_cap"]["level"] == "R2"
    assert rec["actions"] and rec["risk_cap"]["reason"]


def test_every_buy_recommendation_explains_itself():
    """产品卡必须自己说明“为什么是它”，否则用户拿到产品仍然不知道该不该买。"""
    rec = build(matched=[selectable("NX-CASH", "R1", 0, 1.85, 100)])
    assert rec["buy"][0]["why"] and all(line.strip() for line in rec["buy"][0]["why"])


def test_avoid_list_keeps_the_rejected_products_visible():
    """“不买什么”是用户明确问的第二个问题，不能因为没被选中就当作不存在。"""
    rejected = [{**card("NX-BAL", "R3", 90, 4.2, 1000), "blocked_by": ["风险等级 R3 超过你的上限 R2"]}]
    rec = build(rejected=rejected)
    assert rec["avoid"][0]["subject"].endswith("（NX-BAL）")
    assert "R3" in rec["avoid"][0]["reason"]


def test_borrowing_to_invest_is_always_refused_when_debt_costs_more():
    rec = build(matched=[selectable("NX-CASH", "R1", 0, 1.85, 100)])
    assert any(item["subject"] == "靠借钱做投资" for item in rec["avoid"])


def test_zero_surplus_refuses_consumer_loan_for_the_goal():
    rec = build(monthly_surplus=Decimal("-500"), feasibility="GAP", debt_balance=Decimal("0"), debt_rate=Decimal("0"))
    assert any("消费贷" in item["subject"] for item in rec["avoid"])
    assert any("没有可持续投入的钱" in action for action in rec["actions"])


def test_no_debt_means_no_debt_comparison_block():
    assert build(debt_balance=Decimal("0"))["debt_comparison"] is None


# ---------- 边界：空结果也不能崩 ----------

def test_nothing_investable_and_nothing_avoided_is_still_a_valid_card():
    rec = build(investable=Decimal("0"), emergency_gap=Decimal("0"), matched=[], rejected=[],
                debt_balance=Decimal("0"), debt_rate=Decimal("0"))
    assert rec["verdict"] and rec["avoid"] == [] and rec["buy"] == []
    assert all(str(action).strip() for action in rec["actions"])


@pytest.mark.parametrize("investable", ["0", "0.01", "999999.99"])
def test_money_formatting_never_leaks_a_raw_decimal(investable):
    rec = build(investable=Decimal(investable))
    assert rec["investable"]["amount"].startswith("¥")
    assert "Decimal" not in rec["investable"]["amount"]
