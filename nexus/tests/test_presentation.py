"""The layout pass and the rating loop that make answers comparable over time.

Two claims are worth locking down here, because both are easy to break silently:

1. A layout decision may only ever *rearrange* an answer. It must not invent a
   block, drop one, fold the block the customer actually asked for, or see an
   amount. Everything else — which order reads best — is a judgement call, and
   the tests deliberately do not pin it.
2. A rating is only useful if it can be traced back to the turn that earned it.
   So the score has to travel with the question and the layout, and re-rating the
   same turn has to replace rather than accumulate.
"""
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from nexus.backend.agent import presentation
from nexus.backend.api.app import app

HEADERS = {"X-Nexus-Demo": "1"}


@pytest.fixture(autouse=True)
def layout_transport(monkeypatch):
    class Response:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(presentation.httpx, "AsyncClient", Client)


# ---------------------------------------------------------------- layout layer

def _snapshot(answer_type="account_snapshot", **extra):
    base = {
        "type": answer_type,
        "title": "账户快照",
        "hero": {"key": "income", "label": "本月收入", "value": "¥36,000.00"},
        "transactions": [{"merchant_name": "京东", "amount": 100.0}] * 6,
        "cards": [{"status": "正常"}] * 2,
        "subscriptions": [{"name": "视频会员"}],
        "accounts": [{"name": "活期"}] * 3,
    }
    base.update(extra)
    return base


def test_fallback_puts_the_asked_for_number_first():
    plan = presentation.fallback(_snapshot())
    assert plan["order"][0] == "hero"
    assert plan["source"] == "fallback"


def test_fallback_never_drops_a_block():
    answer = _snapshot()
    plan = presentation.fallback(answer)
    assert set(plan["order"]) == set(presentation.allowed_blocks(answer))


def test_shape_hides_amounts_from_the_model():
    """The model may know there are six transactions. It may not know what they cost."""
    answer = _snapshot()
    digest = presentation._shape_of(answer, presentation.allowed_blocks(answer))
    rendered = json.dumps(digest, ensure_ascii=False)
    assert digest["transactions"] == 6
    for leak in ("36000", "36,000.00", "京东"):
        assert leak not in rendered


async def test_plan_falls_back_when_no_model(monkeypatch):
    monkeypatch.setattr(presentation.model_module, "is_configured", lambda: False)
    plan = await presentation.plan(_snapshot(), "这个月收入多少")
    assert plan["source"] == "fallback"


async def test_layout_never_fails_the_answer(monkeypatch):
    """The contract is stated as 'the layout pass never fails', so it is tested at
    the boundary that guarantees it: whatever the model layer does, the answer
    still comes back with a usable layout attached."""
    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise RuntimeError("network is having a day")

    monkeypatch.setattr(presentation.model_module, "is_configured", lambda: True)
    monkeypatch.setattr(presentation.httpx, "AsyncClient", _Client)
    answer = await presentation.apply(_snapshot(), "这个月收入多少")
    assert answer["presentation"]["source"] == "fallback"
    assert answer["presentation"]["order"][0] == "hero"


async def test_plan_falls_back_on_a_non_200(monkeypatch):
    class _Response:
        status_code = 502

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(presentation.model_module, "is_configured", lambda: True)
    monkeypatch.setattr(presentation.httpx, "AsyncClient", _Client)
    assert (await presentation.plan(_snapshot(), "这个月收入多少"))["source"] == "fallback"


async def test_plan_drops_blocks_the_answer_does_not_have(monkeypatch):
    """A hallucinated block name is discarded, not rendered as an empty section."""
    answer = _snapshot()
    blocks = presentation.allowed_blocks(answer)
    choice = presentation.LayoutPlan(
        order=["hero", "not_a_block", "cards"], fold=["accounts"],
        emphasis="先看收入", rationale="收入是问的那个数", source="model",
    )
    monkeypatch.setattr(presentation.model_module, "is_configured", lambda: True)
    monkeypatch.setattr(presentation.model_module, "_json_object", lambda data: choice.model_dump())

    plan = await presentation.plan(answer, "这个月收入多少")
    assert plan["source"] == "model"
    assert "not_a_block" not in plan["order"]
    # Nothing may be lost: every real block still renders somewhere.
    assert set(plan["order"]) == set(blocks)


async def test_plan_never_folds_the_block_that_answers_the_question(monkeypatch):
    answer = _snapshot()
    choice = presentation.LayoutPlan(
        order=["hero", "cards", "transactions"], fold=["hero", "accounts"],
        emphasis="先看收入", rationale="收入优先", source="model",
    )
    monkeypatch.setattr(presentation.model_module, "is_configured", lambda: True)
    monkeypatch.setattr(presentation.model_module, "_json_object", lambda data: choice.model_dump())

    plan = await presentation.plan(answer, "这个月收入多少")
    assert "hero" not in plan["fold"]


async def test_plan_caps_the_fold_list(monkeypatch):
    answer = _snapshot()
    choice = presentation.LayoutPlan(
        order=["hero", "cards", "transactions", "accounts", "subscriptions"],
        fold=["cards", "transactions", "accounts", "subscriptions"],
        emphasis="", rationale="", source="model",
    )
    monkeypatch.setattr(presentation.model_module, "is_configured", lambda: True)
    monkeypatch.setattr(presentation.model_module, "_json_object", lambda data: choice.model_dump())

    plan = await presentation.plan(answer, "这个月收入多少")
    assert len(plan["fold"]) <= 3


async def test_apply_is_idempotent(monkeypatch):
    """A replayed turn keeps the layout it was answered with."""
    calls = []

    async def _fake_plan(answer, question):
        calls.append(question)
        return presentation.fallback(answer)

    monkeypatch.setattr(presentation, "plan", _fake_plan)
    answer = await presentation.apply(_snapshot(), "这个月收入多少")
    again = await presentation.apply(answer, "这个月收入多少")
    assert again["presentation"] == answer["presentation"]
    assert len(calls) == 1


@pytest.mark.parametrize("answer_type", ["account_snapshot", "bill_analysis"])
def test_every_layout_answer_starts_with_a_readable_number(answer_type):
    """Contract, not routing: whichever card an income question lands on, it opens with the number.

    Intent routing is the model's call, so an income question may legitimately
    produce either card. Both must satisfy the same rule — the asked-for figure
    is the first thing on screen — or the promise depends on routing luck.
    """
    blocks = presentation.BLOCKS_BY_TYPE[answer_type]
    assert "hero" in blocks
    assert presentation.DEFAULT_ORDER[answer_type][0] == "hero"


def test_layout_layer_covers_the_cards_the_demo_shows():
    """A type with no block map silently falls back to a single 'head' block."""
    for answer_type in ("account_snapshot", "bill_analysis", "risk_report", "recurring_detection"):
        assert len(presentation.BLOCKS_BY_TYPE[answer_type]) >= 2, answer_type


# ----------------------------------------------------------------- rating loop

@pytest_asyncio.fixture
async def client(db):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", headers=HEADERS) as client:
            assert (await client.post("/api/session")).status_code == 200
            yield client


async def _answer(client, text):
    response = await client.post("/api/messages", json={"message": text, "request_id": str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


async def test_answer_echoes_the_request_id(client):
    """Without it the renderer cannot attach a score to the turn on screen."""
    request_id = str(uuid4())
    response = await client.post("/api/messages", json={"message": "这个月收入多少", "request_id": request_id})
    assert response.json()["request_id"] == request_id


async def test_answer_carries_a_layout_plan(client):
    answer = await _answer(client, "这个月收入多少")
    plan = answer.get("presentation")
    assert plan, "no layout decision was attached"
    assert plan["order"] and plan["order"][0] == "hero"
    assert set(plan["order"]) <= set(presentation.allowed_blocks(answer))


async def test_rating_is_stored_against_its_turn(client):
    answer = await _answer(client, "这个月收入多少")
    response = await client.post("/api/ratings", json={"request_id": answer["request_id"], "stars": 5})
    assert response.status_code == 200
    assert response.json()["stars"] == 5


async def test_rerating_replaces_rather_than_accumulates(client):
    answer = await _answer(client, "这个月收入多少")
    request_id = answer["request_id"]
    await client.post("/api/ratings", json={"request_id": request_id, "stars": 5})
    await client.post("/api/ratings", json={"request_id": request_id, "stars": 2})
    listed = (await client.get("/api/ratings", params={"min_stars": 5})).json()
    assert not [row for row in listed["items"] if row["request_id"] == request_id]
    low = (await client.get("/api/ratings", params={"min_stars": 2})).json()
    assert [row["stars"] for row in low["items"] if row["request_id"] == request_id] == [2]


async def test_benchmark_carries_question_and_layout(client):
    """A bare score is noise; the reusable artefact is the whole pairing."""
    answer = await _answer(client, "这个月收入多少")
    await client.post("/api/ratings", json={"request_id": answer["request_id"], "stars": 5})
    listed = (await client.get("/api/ratings", params={"min_stars": 5})).json()
    row = next(r for r in listed["items"] if r["request_id"] == answer["request_id"])
    assert listed["average"] is not None
    assert row["question"] == "这个月收入多少"
    assert row["answer_type"] == answer["type"]
    assert row["layout"]["order"] == answer["presentation"]["order"]


async def test_rating_rejects_an_unknown_turn(client):
    response = await client.post("/api/ratings", json={"request_id": str(uuid4()), "stars": 5})
    assert response.status_code == 404


async def test_rating_rejects_an_out_of_range_score(client):
    answer = await _answer(client, "这个月收入多少")
    for stars in (0, 6):
        response = await client.post("/api/ratings", json={"request_id": answer["request_id"], "stars": stars})
        assert response.status_code == 422
