"""Staying usable without becoming careless.

Three controls, all of which exist to make the assistant harder to abuse or
harder to get stuck talking to:

- A rate limit that is invisible to a person and only engages automation.
- A follow-up that inherits the previous turn, so a pronoun has something to
  refer to.
- An unclear turn that asks one question instead of handing the customer to a
  person, when there is a conversation to continue.

The first two are additions. The third changes an existing behaviour, so it is
pinned here: the handoff must still happen when there is no thread to continue,
because silently asking questions forever is its own failure mode.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from uuid import uuid4

from nexus.backend.api import app as api_app
from nexus.backend.api.app import app
from nexus.backend.agent import graph, model as model_module
from nexus.backend.agent.understanding import Understanding

HEADERS = {"X-Nexus-Demo": "1"}


@pytest_asyncio.fixture
async def client(db):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", headers=HEADERS) as client:
            assert (await client.post("/api/session")).status_code == 200
            yield client


# ------------------------------------------------------------- rate limiting

class _FakeRequest:
    """Enough of a Request for the limiter; running the whole agent 45 times
    to test a counter would test the graph instead."""

    def __init__(self, path, cookie="c1", host="127.0.0.1"):
        self.url = type("U", (), {"path": path})()
        self.cookies = ({"nexus_session": cookie} if cookie else {})
        self.headers = {}
        self.client = type("C", (), {"host": host})()


def test_messages_endpoint_is_rate_limited():
    """The endpoint that costs a model call is the one worth metering."""
    api_app._rate_buckets.clear()
    limit, _ = api_app.RATE_LIMITS["/api/messages"]
    request = _FakeRequest("/api/messages")
    assert not any(api_app._rate_exceeded(request) for _ in range(limit))
    assert api_app._rate_exceeded(request), "limit never engaged"


def test_limit_is_per_session_not_global():
    """一个人把额度用完，不该把别人一起限掉。

    换过三种键，每一种都有具体的失败方式：按会话——会话可以自己无限申请，
    等于没有限额；按 IP——评审现场几十人共用一个出口 IP，第一个人手快就把额度
    花光了。现在按会话键（登录后即等价于操作账号），既是攻击者无法免费续期的，
    也不会连带影响同一出口 IP 的其他人。
    """
    api_app._rate_buckets.clear()
    limit, _ = api_app.RATE_LIMITS["/api/messages"]
    noisy = _FakeRequest("/api/messages", cookie="noisy")
    for _ in range(limit + 3):
        api_app._rate_exceeded(noisy)
    assert api_app._rate_exceeded(_FakeRequest("/api/messages", cookie="noisy"))
    assert not api_app._rate_exceeded(_FakeRequest("/api/messages", cookie="quiet"))
    # 同一个会话换一个来源 IP，额度依然跟着会话走——换个出口绕不过去。
    assert api_app._rate_exceeded(_FakeRequest("/api/messages", cookie="noisy", host="10.0.0.9"))


def test_a_saneless_caller_is_metered_by_address():
    """还没登录的请求（扫描、撞库）按来源地址计量。

    这类请求没有会话可用，地址是唯一能拿到的、且攻击者无法免费续期的标识。
    """
    api_app._rate_buckets.clear()
    limit, _ = api_app.RATE_LIMITS["/api/messages"]
    attacker = _FakeRequest("/api/messages", cookie="")
    for _ in range(limit + 3):
        api_app._rate_exceeded(attacker)
    assert api_app._rate_exceeded(_FakeRequest("/api/messages", cookie=""))
    assert not api_app._rate_exceeded(
        _FakeRequest("/api/messages", cookie="", host="10.0.0.9"))


def test_read_only_endpoints_are_not_metered():
    """Throttling a page refresh would look like an outage, not a safeguard."""
    api_app._rate_buckets.clear()
    for path in ("/api/overview", "/api/capabilities", "/api/health"):
        assert not any(api_app._rate_exceeded(_FakeRequest(path)) for _ in range(80))


def test_bypassed_paths_are_excluded_even_under_pressure():
    api_app._rate_buckets.clear()
    limit, _ = api_app.RATE_LIMITS["/api/messages"]
    for _ in range(limit + 5):
        api_app._rate_exceeded(_FakeRequest("/api/messages"))
    assert not any(api_app._rate_exceeded(_FakeRequest("/api/session")) for _ in range(50))


def test_window_expires_so_a_burst_is_not_a_lifetime_ban():
    api_app._rate_buckets.clear()
    limit, window = api_app.RATE_LIMITS["/api/messages"]
    request = _FakeRequest("/api/messages")
    for _ in range(limit + 1):
        api_app._rate_exceeded(request)
    assert api_app._rate_exceeded(request)
    # Pretend the recorded hits aged past the window.
    key = f"{api_app._rate_key(request)}|/api/messages"
    api_app._rate_buckets[key] = [t - window - 1 for t in api_app._rate_buckets[key]]
    assert not api_app._rate_exceeded(request)


def test_buckets_are_pruned_so_the_map_cannot_grow_forever():
    """A limiter that leaks memory is a denial-of-service vector of its own."""
    api_app._rate_buckets.clear()
    for index in range(600):
        api_app._rate_buckets[f"stale-key-{index}"] = [0.0]
    api_app._rate_exceeded(_FakeRequest("/api/messages"))
    assert len(api_app._rate_buckets) < 600


async def test_middleware_answers_429_with_a_usable_body(client, monkeypatch):
    """One real request, to prove the limiter is actually wired to the route.

    The configured ceiling is a production number; driving it here would run
    the whole agent dozens of times to test a counter, so it is lowered to the
    smallest value that still exercises the path.
    """
    api_app._rate_buckets.clear()
    monkeypatch.setitem(api_app.RATE_LIMITS, "/api/messages", (2, 60.0))
    for _ in range(4):
        response = await client.post("/api/messages", json={
            "message": "查看我的账户", "request_id": str(uuid4())})
    assert response.status_code == 429
    assert response.headers.get("Retry-After")
    body = response.json()
    assert body["code"] == "RATE_LIMITED"
    # The message is read by a customer, so it is written as one.
    assert body["message"] and "频繁" in body["message"]


# ------------------------------------------------------- following the thread

async def _say(client, text):
    response = await client.post("/api/messages", json={"message": text, "request_id": str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


async def test_previous_question_is_carried_forward(client):
    """A read-only turn leaves no slots, so the question is the only anchor."""
    await _say(client, "我账户里现在有多少钱")
    from nexus.backend.core import database
    from nexus.backend.core.models import AgentTurn
    from sqlalchemy import select
    async with database.session_scope() as session:
        turn = (await session.scalars(
            select(AgentTurn).order_by(AgentTurn.id.desc()).limit(1))).one()
        assert (turn.response or {}).get("question") == "我账户里现在有多少钱"


async def test_follow_up_after_a_read_is_not_handed_off(client, monkeypatch):
    """The regression this guards: a pronoun follow-up used to become a handoff.

    "那我这个月结余够不够" carries no antecedent on its own. Before the previous
    question was carried forward it read as low confidence and went to a person,
    which stalled a conversation the assistant already had context for.
    """
    await _say(client, "我账户里现在有多少钱")

    async def _unclear(message, context=None):
        return Understanding(
            scene="account_query", write_intent=False,
            read_tools=["account"], confidence=0.4, output="text",
            reasoning="这一句本身信息不足", missing_information=[],
        )

    monkeypatch.setattr(model_module, "understand", _unclear)
    answer = await _say(client, "那我这个月结余够不够")
    assert answer["type"] != "support_handoff", "unclear follow-up was escalated"
    assert answer.get("needs_input") is True
    # A follow-up question is not a security decision, so it must not borrow the
    # engine value that renders as 服务范围与安全检查.
    assert answer.get("engine") == "clarify"


async def test_unclear_first_turn_still_hands_off(client, monkeypatch):
    """With nothing to continue, asking again forever is its own failure.

    The handoff must remain reachable, or the assistant becomes a loop.
    """
    async def _unclear(message, context=None):
        return Understanding(
            scene="account_query", write_intent=False, output="text",
            read_tools=["account"], confidence=0.4,
            reasoning="完全无法判断", missing_information=[],
        )

    monkeypatch.setattr(model_module, "understand", _unclear)
    answer = await _say(client, "嗯嗯嗯那个那个")
    assert answer["type"] in {"support_handoff", "boundary"}


async def test_clarifying_question_refers_to_the_previous_topic(client, monkeypatch):
    """A question the customer cannot act on is not a clarifying question."""
    await _say(client, "帮我转2000给妈妈")

    async def _unclear(message, context=None):
        return Understanding(
            scene="transfer", write_intent=True, operation="transfer", output="text",
            read_tools=["recipients"], confidence=0.4, amount="2000", recipient="妈妈",
            reasoning="不确定", missing_information=[],
        )

    monkeypatch.setattr(model_module, "understand", _unclear)
    answer = await _say(client, "嗯嗯嗯那个那个")
    assert answer["type"] != "support_handoff"
    # The customer has to be able to act on this, and "接着刚才那个问题"
    # is not actionable unless the assistant says which question.
    assert "帮我转2000给妈妈" in answer["message"], \
        "the clarifying question does not name the previous topic"
    trace = answer.get("trace") or []
    assert any("上一轮主题" in str(step.get("detail", "")) for step in trace), \
        "the reasoning does not record the previous topic"
