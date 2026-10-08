"""Conversation requests must not silently inherit the seeded housing goal."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nexus.backend.agent.analysis import financial_analysis as financial


@pytest.mark.parametrize("message, expected", [
    ("帮我做个资产配置", "allocation"),
    ("根据我的账户数据做一份个性化理财分析", "allocation"),
    ("不考虑购房，分析资产配置", "allocation"),
    ("帮我为买车制定储蓄计划", "new_goal"),
    ("规划孩子教育金", "new_goal"),
    ("两年攒十万元", "new_goal"),
    ("买房期限改成五年", "new_goal"),
    ("分析我的购房目标", "saved_goal"),
    ("", "saved_goal"),
])
def test_request_focus(message, expected):
    assert financial.planning_focus(message, "三年购房首付") == expected


@pytest.mark.asyncio
async def test_allocation_answers_request_and_discloses_saved_constraint(monkeypatch):
    profile = SimpleNamespace(goal_name="三年购房首付")
    monkeypatch.setattr(financial, "get_profile", AsyncMock(return_value=profile))
    report = {"type": "financial_analysis", "balance_sheet": {"cash": "¥100", "investments": "¥20", "net_worth": "¥120"}, "cashflow": {"surplus": "¥10"}, "allocation": {}, "observations": [], "profile": {}, "recommendation": {"actions": ["每月为三年购房首付存钱"]}}
    monkeypatch.setattr(financial, "_build_financial_analysis", AsyncMock(return_value=deepcopy(report)))
    answer = await financial.build_financial_analysis(None, 1, "帮我做资产配置")
    assert "整体资产配置" in answer["title"]
    assert "首付" not in answer["summary"]
    assert "资金预留约束" in answer["observations"][0]
    assert profile.goal_name == "三年购房首付"
    assert all("每月为三年购房首付" not in a for a in answer["recommendation"]["actions"])


@pytest.mark.asyncio
async def test_new_goal_clears_previous_goal_inputs(monkeypatch):
    monkeypatch.setattr(financial, "get_profile", AsyncMock(return_value=SimpleNamespace(goal_name="三年购房首付")))
    monkeypatch.setattr(financial, "get_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(financial, "get_declared_subscriptions", AsyncMock(return_value=[]))
    monkeypatch.setattr(financial, "intake", lambda *args: {"type": "financial_intake", "values": {"monthly_income": "15000", "goal_name": "三年购房首付", "goal_amount": "450000", "goal_saved": "96000", "horizon_months": 36}})
    calculate = AsyncMock()
    monkeypatch.setattr(financial, "_build_financial_analysis", calculate)
    answer = await financial.build_financial_analysis(None, 1, "为买车制定计划")
    assert answer["values"] == {"monthly_income": "15000"}
    assert answer["templates"] == []
    calculate.assert_not_awaited()
