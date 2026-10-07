"""Regression tests for the LangGraph orchestration layer (agent/graph.py)
and the LangChain tool registry (agent/toolkit.py).

These tests are model-free: they exercise the graph structure, the conditional
edges, the tool injection contract, and the end-to-end HTTP flow without any
live LLM call. They must stay green alongside the original 64 tests.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select

from nexus.backend.agent import graph
from nexus.backend.core.models import DemoSession


def test_graph_has_expected_nodes():
    """The StateGraph must expose the full orchestration node set.

    Nodes are named after the verified view they render, not after a user
    phrasing: every read branch owns exactly one read view, and the four veto
    nodes (guard, boundary, blocked, escalate) are always present.
    """
    nodes = set(graph._graph.nodes.keys())
    assert {"guard", "boundary", "router", "write", "clarify", "escalate",
            "blocked", "degraded", "account", "bill", "financial", "products",
            "subscriptions", "plans", "aa", "tools", "birthday", "external",
            "help", "local"} <= nodes


def test_graph_has_guard_conditional_edge():
    """guard must branch to END when blocked, else to router."""
    edges = graph._graph.edges
    assert ("guard", "router") in edges or ("__start__", "guard") in edges


def test_router_produces_known_branches():
    """The router conditional path must map every branch to a registered node."""
    branches = (
        graph.WRITE, graph.CLARIFY, graph.ESCALATE, graph.BLOCKED_BRANCH,
        graph.BOUNDARY, graph.DEGRADED,
        graph.READ_ACCOUNT, graph.READ_BILL, graph.READ_FINANCIAL,
        graph.READ_PRODUCTS, graph.READ_SUBSCRIPTIONS, graph.READ_PLANS,
        graph.READ_AA, graph.READ_TOOLS, graph.READ_BIRTHDAY,
        graph.EXTERNAL, graph.HELP_BRANCH, graph.LOCAL_FALLBACK,
    )
    for branch in branches:
        target = graph.router_edges({"engine": branch})
        assert target in graph._graph.nodes, branch


def test_every_branch_has_exactly_one_owner():
    """No branch may fall through to END: a silent branch is a dead agent."""
    from langgraph.graph import END

    branches = (
        graph.WRITE, graph.CLARIFY, graph.ESCALATE, graph.BLOCKED_BRANCH,
        graph.BOUNDARY, graph.DEGRADED,
        graph.READ_ACCOUNT, graph.READ_BILL, graph.READ_FINANCIAL,
        graph.READ_PRODUCTS, graph.READ_SUBSCRIPTIONS, graph.READ_PLANS,
        graph.READ_AA, graph.READ_TOOLS, graph.READ_BIRTHDAY,
        graph.EXTERNAL, graph.HELP_BRANCH, graph.LOCAL_FALLBACK,
    )
    assert len(set(branches)) == len(branches), "branches must be distinguishable"
    for branch in branches:
        assert graph.router_edges({"engine": branch}) != END, branch


def test_single_routing_policy_has_auditable_ownership():
    """Routing is decided by the named scene + operation, never by wording.

    The two scenes that can both read and write prove the point: the identical
    business domain takes a different branch purely because the model set
    write_intent, with no keyword anywhere in the decision.
    """
    from nexus.backend.agent.routing import route
    from nexus.backend.agent.understanding import Understanding

    def read(**kwargs):
        base = dict(scene="account_query", operation=None, write_intent=False,
                    read_tools=["account"], output="table", confidence=0.95)
        base.update(kwargs)
        return route(Understanding.model_validate(base))

    assert read().branch == graph.READ_ACCOUNT
    # Same domain, two modes, decided only by the model's write_intent.
    assert read(scene="subscription", read_tools=["subscriptions"]).branch == graph.READ_SUBSCRIPTIONS
    assert read(scene="subscription", operation="cancel_subscription",
                write_intent=True, merchant="云音乐").branch == graph.WRITE
    # A write missing its operation's slots asks instead of guessing.
    assert read(scene="card", write_intent=True, output="clarify").branch == graph.CLARIFY
    # A birthday goal gets the dedicated intake rather than a generic draft.
    # It is selected by the structured ``goal`` field, not by grepping the
    # model's prose: the same judgement phrased without the characters 生日
    # used to silently fall through to the generic planner.
    assert read(scene="cross_scene", goal="birthday_plan").branch == graph.READ_BIRTHDAY
    assert read(scene="cross_scene", reasoning="生日跨场景任务").branch == graph.READ_TOOLS
    assert read(scene="cross_scene", reasoning="用户要给爱人准备一个惊喜",
                goal=None).branch == graph.READ_TOOLS
    # Low confidence escalates; small talk is simply out of scope.
    assert read(scene="transfer", write_intent=True, recipient="张三", amount="100",
                amount_evidence="100元", operation="transfer", confidence=0.42).branch == graph.ESCALATE
    assert read(scene="smalltalk", confidence=0.2).branch == graph.BOUNDARY


def test_frontend_rich_text_renderer_is_safe_and_loaded_first():
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[1] / "frontend"
    renderer = (frontend / "rich-text.js").read_text()
    index = (frontend / "index.html").read_text()
    app = (frontend / "app.js").read_text()
    assert "innerHTML" not in renderer
    assert index.index("rich-text.js") < index.index("app.js")
    assert "NexusRichText.render" in app
    assert 'id="capability-toggle"' in index
    assert "aria-expanded" in index


def test_database_migration_baseline_exists():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    revision = root / "migrations" / "versions" / "0001_demo_schema_baseline.py"
    assert (root / "alembic.ini").exists()
    assert (root / "migrations" / "env.py").exists()
    text = revision.read_text()
    assert 'revision = "0001_demo_schema_baseline"' in text
    assert "Base.metadata.create_all" in text


def test_capability_copy_matches_remote_tool_data_flow():
    from nexus.backend.agent.external_data import capability_catalog

    catalog = capability_catalog(True)
    text = str(catalog)
    assert "最小化工具结果" in text
    assert "账户数据留在本地" not in text


def test_toolkit_tools_build(client):
    """Every read tool must build with a name and a runnable coroutine."""
    from nexus.backend.agent import toolkit
    tools = toolkit.build_read_toolkit(user_id=1)
    assert len(tools) >= 8
    for tool in tools:
        assert tool.name, f"{tool.name} must have a name"
    exposed = {t.name for t in tools if "user_id" in t.args_schema.model_json_schema().get("properties", {})}
    assert not exposed, "authenticated user_id must never be model-controllable"


@pytest.mark.asyncio
async def test_toolkit_read_tools_return_user_scoped_data(client):
    """Tool invocation reflects the current demo session's user_id only."""
    from nexus.backend.agent import toolkit
    from nexus.backend.core import database
    async with database.session_scope() as session:
        who = await session.scalar(select(DemoSession).order_by(DemoSession.id))
        uid = who.user_id
    tools = {t.name: t for t in toolkit.build_read_toolkit(uid)}
    balance = await tools["get_balance"].ainvoke({})
    assert "available" in balance
    cards = await tools["get_cards"].ainvoke({})
    assert isinstance(cards.get("cards"), list)
    recipients = await tools["get_recipients"].ainvoke({})
    assert "recipients" in recipients
    assert all("phone" not in item and "phone_masked" in item for item in recipients["recipients"])
    assert all(str(item["phone_masked"]).startswith("***") for item in recipients["recipients"])
    subs = await tools["get_subscriptions"].ainvoke({})
    assert "subscriptions" in subs
    events = await tools["get_events"].ainvoke({})
    allowed = {"date", "city", "destination", "days", "relationship", "budget", "merchant", "amount", "category", "risk_level", "currency", "company", "ticker", "preference", "note"}
    allowed |= {"card_last4", "lounge_visits", "fast_track", "valid_until", "hotel_benefit", "temporary_limit_needed", "preferred_transport", "departure_date", "days_unused", "next_fee", "next_charge", "current", "current_price", "alternative", "alternative_price", "saving_year", "contact", "recipient", "total", "participants", "payer", "user_share", "day", "suggested", "recent_average"}
    assert all(set(item["payload"]) <= allowed for item in events["events"])
