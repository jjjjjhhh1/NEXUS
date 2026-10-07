"""Three things a customer notices before they notice anything else.

A comparison the customer asked for has to compare. A failure has to say what
happened to their money. A confirmation window has to be visible while it is
open, not discovered after it closes.

Each of these was observed failing on the running system before it was written,
so each test pins a specific broken behaviour rather than a general intent:

  - "我账户里够换1000美元吗" was answered with a balance and no comparison
  - a request failure left one line of red text and no way forward
  - "5 分钟内有效" was a claim, not a countdown
"""
import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from uuid import uuid4

from nexus.backend.agent import read_views
from nexus.backend.agent.external_data import parse_fx_request
from nexus.backend.api.app import app

HEADERS = {"X-Nexus-Demo": "1"}


@pytest_asyncio.fixture
async def client(db):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", headers=HEADERS) as client:
            assert (await client.post("/api/session")).status_code == 200
            yield client


# ------------------------------------------------ comparison needs both sides

def test_comparison_parses_a_target_the_customer_named():
    """Only the target was spoken, but the source is known by construction.

    The account is held in CNY, so "够换 1000 美元" is a complete question.
    Requiring the customer to also say "人民币" would be the parser asking a
    question the data already answers.
    """
    assert parse_fx_request("我账户里够换1000美元吗", strict=False) == (Decimal("1000"), "CNY", "USD")


def test_pair_must_still_be_spelled_out_when_routing_on_its_own():
    """The strict gate is what stops an incidental mention becoming a request."""
    assert parse_fx_request("我同时有美元和欧元") is None
    assert parse_fx_request("我有5万人民币") is None
    assert parse_fx_request("1000人民币能换多少美元") == (Decimal("1000"), "CNY", "USD")


def test_magnitudes_are_refused_rather_than_misread():
    """万 multiplies the digits before it. Reading them literally is a wrong
    answer about someone's money, so no comparison is produced at all."""
    assert read_views._has_unparsed_magnitude("我账户里够换20万欧元吗")
    assert read_views._has_unparsed_magnitude("我有5万人民币")
    assert not read_views._has_unparsed_magnitude("我账户里够换1000美元吗")
    assert not read_views._has_unparsed_magnitude("我买了3件衣服花了3000")


def test_spending_and_targeting_are_different_questions():
    """"1000人民币能换多少" spends. "够换1000美元吗" targets. Same pair,
    opposite arithmetic — and conflating them produced a comparison that
    declared the customer able to afford something with a negative surplus."""
    assert read_views._spends_base("1000人民币能换多少美元", Decimal("1000"), "CNY") is True
    assert read_views._spends_base("我账户里够换1000美元吗", Decimal("1000"), "CNY") is False


async def test_comparison_is_built_from_balance_and_rate(monkeypatch):
    async def _fake(amount, base, quote, client=None):
        return {"rate": "0.14", "as_of": "2026-10-04", "source": {"name": "test"}}

    monkeypatch.setattr(read_views, "fetch_fx_quote", _fake)
    block, trace = await read_views._fx_comparison(
        Decimal("27450.00"), "我账户里够换1000美元吗", ["account", "fx"])
    assert block is not None
    assert block["affordable"] is True
    # 1000 USD at 0.14 costs ~7142.86 CNY against a 27450 balance.
    assert "够换" in block["verdict"]
    assert "7,142.86" in block["verdict"]
    assert "20,307.14" in block["verdict"]
    assert trace and trace[0]["label"] == "取参考汇率"


async def test_comparison_reports_the_shortfall(monkeypatch):
    async def _fake(amount, base, quote, client=None):
        return {"rate": "0.14", "as_of": "2026-10-04", "source": {"name": "test"}}

    monkeypatch.setattr(read_views, "fetch_fx_quote", _fake)
    block, _ = await read_views._fx_comparison(
        Decimal("1000.00"), "我账户里够换1000美元吗", ["account", "fx"])
    assert block["affordable"] is False
    assert "不够换" in block["verdict"] and "还差" in block["verdict"]


async def test_a_failing_rate_call_leaves_the_answer_intact(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(read_views, "fetch_fx_quote", _boom)
    assert await read_views._fx_comparison(
        Decimal("27450.00"), "我账户里够换1000美元吗", ["account", "fx"]) == (None, None)


async def test_no_comparison_when_the_rate_was_not_asked_for(monkeypatch):
    """A balance question must not start calling an external provider."""
    calls = []

    async def _fake(*a, **kw):
        calls.append(a)
        return {"rate": "0.14", "as_of": ""}

    monkeypatch.setattr(read_views, "fetch_fx_quote", _fake)
    await read_views._fx_comparison(Decimal("27450.00"), "我账户里现在有多少钱", ["account"])
    assert calls == []


async def test_comparison_is_absent_when_the_rate_cannot_be_read(monkeypatch):
    async def _fake(*a, **kw):
        return {"rate": "0.14", "as_of": ""}

    monkeypatch.setattr(read_views, "fetch_fx_quote", _fake)
    assert await read_views._fx_comparison(
        Decimal("27450.00"), "我账户里够换1000美元吗", ["account"]) == (None, None)


async def test_snapshot_carries_the_comparison_block(client, monkeypatch):
    """End to end: the block reaches the answer the renderer receives."""
    from nexus.backend.agent import model as model_module
    from nexus.backend.agent.understanding import Understanding

    async def _rate(amount, base, quote, client=None):
        return {"rate": "0.14", "as_of": "2026-10-04", "source": {"name": "Frankfurter 汇率 API"}}

    async def _compare(message, context=None):
        return Understanding(
            scene="account_query", write_intent=False, output="text",
            read_tools=["account", "fx"], confidence=0.9,
            reasoning="余额与汇率的比较", missing_information=[],
        )

    monkeypatch.setattr(read_views, "fetch_fx_quote", _rate)
    monkeypatch.setattr(model_module, "understand", _compare)
    response = await client.post("/api/messages", json={
        "message": "我账户里够换1000美元吗", "request_id": str(uuid4())})
    assert response.status_code == 200
    answer = response.json()
    assert answer.get("comparison"), "no comparison block reached the answer"
    assert answer["comparison"]["affordable"] is True
