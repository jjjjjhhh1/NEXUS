"""LangGraph orchestration layer for the Nexus banking agent.

    START -> guard -> boundary -> router -> <one node per business view> -> END

Layering, from the inside out:

    guard.py         may a message be processed at all?        (security veto)
    boundary.py      deterministic out-of-scope / handoff paths  (scope veto)
    model.understand what does the user actually want?         (LLM owns intent)
    routing.py       which handler owns a named scene           (policy, no keywords)
    plan_builder     write -> a confirmation card                (write authz)
    read_views       read  -> a verified payload                 (data truth)

Every read branch renders through exactly one read view, so the chat and the
side panel always agree. Every write branch ends in a persisted DemoAction that
only the UI button may execute, so "confirm-then-execute" survives the refactor.
The graph records an auditable trace on every branch, so the whole
"understand -> ground -> act" journey stays visible to the user.

The graph is intentionally state-lean: each node opens its own short-lived
SQLAlchemy transaction (the demo uses a single SQLite writer) rather than
holding a session inside LangGraph state, which must remain serializable.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from datetime import datetime
from typing import Callable
from uuid import uuid4

from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END

from ...core import database
from ...core.config import settings
from ...core.exceptions import BusinessRuleException
from ...core.models import AuditLog, DemoAction
from .conversation import lookup_identity, latest_turn, replay, save, message_answer
from ..integrations import model
from ..security.grounder import ground_understanding
from ..security.guard import guard
from ..contracts.responses import BLOCKED, FALLBACK, FAQ
from ..demo_agent import respond, HELP
from ..analysis.financial_analysis import build_financial_analysis
from ..analysis.bill_analysis import build_bill_analysis
from ..integrations.external_data import parse_fx_request, fetch_fx_quote
from ..integrations.market_data import parse_macro_request, fetch_macro_indicator, parse_sec_request, fetch_sec_filings
from ..planning.cross_scene import build_birthday_plan
from ..planning.universal_planner import build_universal_plan
from ..analysis.read_views import account_view, product_view, subscription_view, scheduled_view, aa_view
from ..analysis import risk_view
from .routing import BOUNDARY, BLOCKED as BLOCKED_BRANCH, CLARIFY, ESCALATE, EXTERNAL, HELP_BRANCH, LOCAL_FALLBACK, READ_AA, READ_ACCOUNT, READ_BILL, READ_BIRTHDAY, READ_FINANCIAL, READ_PLANS, READ_PRODUCTS, READ_RISK, READ_SUBSCRIPTIONS, READ_TOOLS, GREETING, WRITE, SLOT_LABELS, degraded_route, missing_write_slots, route
from ..planning.boundary import classify_local, out_of_scope_response, unsupported_response, handoff_response, interruption_response, fraud_response, misunderstanding_count, greeting_response
from ..contracts.understanding import Understanding
from ..planning.plan_builder import AlreadyDone, build_plan, Unresolvable
from .agent import strip_thinking


DEGRADED = "degraded"


class BankingState(TypedDict, total=False):
    """Serializable state flowing between LangGraph nodes.

    ``result`` is the final payload returned to the HTTP layer; it is written
    exactly once by the terminating node of each branch. ``trace`` accumulates
    the agent's auditable reasoning steps.
    """
    token: str | None
    message: str
    request_id: str
    result: dict
    engine: str
    trace: list[dict]
    route_reason: str
    understanding: dict | None
    understanding_context: dict | None
    model_status: str | None


# ---------------------------------------------------------------------------
# User-facing names for scenes and tools. Shown in the trace so a user can see
# why the agent did what it did and correct a misread intent immediately.
# ---------------------------------------------------------------------------
SCENE_LABELS = {
    "transfer": "转账",
    "scheduled_transfer": "定时转账",
    "aa_collection": "AA 收款",
    "bill_analysis": "账单分析",
    "financial_profile": "理财产品",
    "financial_planning": "个性化理财规划",
    "card": "卡片管理",
    "subscription": "订阅与代扣",
    "cross_scene": "跨场景任务",
    "account_query": "账户查询",
    "external_data": "公开数据查询",
    "capabilities": "能力说明",
    "greeting": "打招呼",
    "smalltalk": "非金融问题",
    "unsupported": "暂不支持的业务",
    "attack": "安全拦截",
}

TOOL_LABELS = {
    "account": "账户余额",
    "bills": "账单记录",
    "recipients": "收款人名录",
    "cards": "银行卡",
    "card_benefits": "卡片权益",
    "subscriptions": "订阅与代扣",
    "subscription_usage": "订阅使用情况",
    "products": "理财产品",
    "financial_profile": "理财画像",
    "events": "账户事件",
    "calendar": "日程",
    "social_context": "社交语境",
    "income_events": "收入事件",
    "market_events": "市场事件",
    "travel_context": "出行信息",
    "family_risk": "家庭风险",
    "fx": "实时汇率",
    "macro": "宏观指标",
    "sec": "公司披露",
}


def understanding_trace(state: BankingState, tools: list[str] | None = None) -> list[dict]:
    """Show the user how their words were understood. The model's one-line
    reasoning is surfaced verbatim so a wrong classification is visible and
    correctable, not hidden behind a confident-looking card."""
    understanding = state.get("understanding")
    if not understanding:
        return []
    rows = [{
        "label": "意图理解",
        "detail": understanding.get("reasoning") or SCENE_LABELS.get(understanding.get("scene"), understanding.get("scene", "已识别")),
        "status": "done",
    }]
    chosen = tools if tools is not None else understanding.get("read_tools") or []
    if chosen:
        rows.append({
            "label": "选择工具",
            "detail": "、".join(TOOL_LABELS.get(tool, tool) for tool in chosen),
            "status": "done",
        })
    return rows



# ---------------------------------------------------------------------------
# Node 1: security gate — runs before anything else, including the model
# ---------------------------------------------------------------------------
def guard_blocked(state: BankingState) -> str:
    return "done" if state.get("result") is not None else "boundary"


async def guard_node(state: BankingState) -> dict:
    refusal = guard(state["message"])
    if not refusal:
        return {"result": None, "engine": "policy"}
    digest = hashlib.sha256(state["message"].encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "policy", "trace": previous.get("trace", [])}
        if refusal.startswith("收到") and re.search(r"比较|分析|查看|查询", state["message"]) and "理财" in state["message"] and re.search(r"消费|账单", state["message"]):
            # Honor the negated write and still complete the explicit read goal.
            answer = await build_financial_analysis(session, who.user_id, state["message"], state.get("understanding"))
            from ..analysis.bill_analysis import build_bill_analysis
            if answer.get("type") == "financial_analysis":
                answer["supporting_bill"] = await build_bill_analysis(session, who.user_id, "month")
            answer["trace"] = [{"label":"只读比较", "detail":"已遵守不转账、不购买的约束，读取账单与财务资料完成比较", "status":"done"}]
            saved = await save(session, who, state["request_id"], digest, answer)
            return {"result": saved, "engine": "analysis", "trace": answer['trace']}
        answer = message_answer(refusal, "policy", boundary="malicious", reason_code="SECURITY_GATE")
        answer["trace"] = [{"label": "安全门卫", "detail": "请求已拦截；未调用模型或业务写工具", "status": "done"}]
        session.add(AuditLog(
            user_id=who.user_id, action="AGENT_SECURITY_BLOCK", target_type="agent_input",
            after_state="SECURITY_GATE", evidence_ids=digest[:16],
        ))
        saved = await save(session, who, state["request_id"], digest, answer, {"boundary": "malicious"})
    return {"result": saved, "engine": "policy", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node 2: deterministic boundary gateway — out of scope, handoff, pause, crisis
# ---------------------------------------------------------------------------
def boundary_done(state: BankingState) -> str:
    return "done" if state.get("result") is not None else "router"


async def boundary_node(state: BankingState) -> dict:
    """Handle deterministic boundary, escalation, interruption and crisis flows."""
    decision = classify_local(state["message"])
    if decision is None:
        return {"result": None, "engine": "boundary"}
    digest = hashlib.sha256(state["message"].encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": previous.get("engine", "boundary"), "trace": previous.get("trace", [])}
        category = decision["category"]
        reason = decision["reason"]
        if category == "handoff":
            answer, context = await handoff_response(session, who.id, state["message"], reason)
        elif category == "misunderstood":
            count = await misunderstanding_count(session, who.id)
            if count >= 2:
                answer, context = await handoff_response(session, who.id, state["message"], "repeated_misunderstanding", count)
            else:
                answer = {
                    "type": "boundary", "category": "clarification", "title": "我换一种方式确认",
                    "message": "抱歉，刚才没有理解准确。请用一句话告诉我你最终想完成的金融任务；如果再次无法解决，我会直接准备人工接管。",
                    "actions": [{"label": "转接人工客服", "command": "转接人工客服", "tone": "secondary"}],
                    "engine": "boundary", "trace": [{"label": "兜底计数", "detail": "首次理解失败，重新澄清", "status": "done"}],
                }
                context = {"boundary": "misunderstood", "misunderstanding_count": count}
        elif category == "interruption":
            answer, context = await interruption_response(session, who.user_id, state["message"])
        elif category == "text_confirmation":
            # Typed "确认" must never execute anything: only the card's own
            # button carries an authorized, already-verified intent.
            answer = {
                "type": "message", "title": "文字确认不会执行任何操作",
                "message": "为了避免确认错操作，我不会用聊天文字执行任何资金或状态变更。"
                           "请使用确认卡上的确认 / 取消按钮，我会严格按卡片上的收款人、金额和操作执行。",
                "needs_input": True,
                "engine": "policy",
                "trace": [{"label": "确认方式校验", "detail": "仅确认卡按钮可授权执行", "status": "done"}],
            }
            context = {"boundary": "text_confirmation"}
        else:
            answer, context = await fraud_response(session, who.user_id)
        saved = await save(session, who, state["request_id"], digest, answer, context)
        return {"result": saved, "engine": answer.get("engine", "boundary"), "trace": answer.get("trace", [])}


# ---------------------------------------------------------------------------
# Node 3: understand — the model reads the user's own words and decides
#           the scene, the operation, the slots, the read tools and the shape
# ---------------------------------------------------------------------------
# A turn that is nothing but one amount, e.g. "200元" / "200块".
_BARE_AMOUNT = re.compile(r"^\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块|块钱)\s*[。！!]?\s*$")


def _answer_our_own_amount_question(understanding, prior: dict, message: str) -> bool:
    """Fill the slot we just asked for, without re-deciding what the user wants.

    The previous turn was our own clarification that asked for 金额, so a reply
    that is exactly one number cannot mean anything else — it is the answer to
    the question we asked. The model gets this right about 19 times in 20; the
    remaining turns drop back to asking again, which is safe but reads as a
    broken conversation in a live demo.

    The decision is made from *our own record of what we asked*, not from the
    model's current reading, because the model's reading is the thing that
    wobbles. Scope stays tiny: it may only set ``amount`` and reuse a recipient
    that was grounded in an earlier turn. It never picks the scene, never picks
    a payee, and never executes — there is no guess here, only a blank we asked
    to be filled in.
    """
    if prior.get("awaiting") != [SLOT_LABELS["amount"]]:
        return False
    if not prior.get("recipient") and not prior.get("account_handle"):
        return False
    match = _BARE_AMOUNT.match(message)
    if not match:
        return False

    value = Decimal(match.group(1))
    understanding.scene = "transfer"
    understanding.operation = "transfer"
    understanding.write_intent = True
    understanding.amount = f"{value:f}"
    understanding.amount_evidence = match.group(0).strip()
    if not understanding.recipient and prior.get("recipient"):
        understanding.recipient = prior["recipient"]
    if not understanding.account_handle and prior.get("account_handle"):
        understanding.account_handle = prior["account_handle"]
    understanding.missing_information = []
    understanding.output = "confirmation"
    if understanding.confidence < 0.8:
        understanding.confidence = 0.95
    return True


async def router_node(state: BankingState) -> dict:
    message = state["message"]
    prior = await _prior_slots(state)

    if not model.is_configured():
        from ..planning.financial_orchestrator import fallback_plan
        if fallback_plan(message):
            return {"engine": READ_FINANCIAL, "route_reason":"无模型：只读显式目标测算，规则风控兜底", "understanding":None, "understanding_context":prior, "model_status":"not_configured"}
        decision = degraded_route(message)
        return {"engine": decision.branch, "route_reason": decision.reason,
                "understanding": None, "understanding_context": prior,
                "model_status": "not_configured"}
    try:
        understanding = await model.understand(message, prior)
    except model.ModelUnavailable as error:
        # A model outage must not be cached: the same request_id has to be
        # retryable, so this branch deliberately does not persist a turn.
        return {"engine": DEGRADED, "route_reason": f"模型不可用：{error.code}",
                "understanding": None, "understanding_context": prior,
                "model_status": error.code}
    canonical_birthday = re.fullmatch(r"创建生日计划\s*日期(20\d{2}-\d{2}-\d{2})\s*预算([0-9]+(?:\.[0-9]{1,2})?)元\s*方案([ABC])", message.strip())
    if canonical_birthday and understanding.scene == "birthday":
        event_date, amount, option = canonical_birthday.groups()
        understanding.operation, understanding.write_intent = "create_birthday_plan", True
        understanding.event_date, understanding.amount, understanding.amount_evidence, understanding.option = event_date, amount, amount + "元", option
    from ..analysis.scenarios import unused_days
    days = unused_days(message)
    if days and understanding.scene in {"subscription", "cross_scene"}:
        understanding.scene, understanding.write_intent, understanding.operation = "cross_scene", False, None
        understanding.unused_days = days
        understanding.read_tools = ["subscriptions", "subscription_usage"]
    from ..analysis.read_views import is_card_balance_question
    if (not understanding.write_intent and understanding.scene in {"account_query", "cross_scene"}
            and is_card_balance_question(message)):
        understanding.scene = "account_query"
        understanding.read_tools = ["cards", "account"]
    if understanding.consultation == "transfer_fee":
        understanding.scene, understanding.write_intent, understanding.operation = "account_query", False, None
    # Cross-domain read goals must keep every requested source in the tool plan.
    if not understanding.write_intent and understanding.scene in {"cross_scene", "financial_planning", "financial_profile", "bill_analysis", "account_query"}:
        domains = []
        for words, names in [(("余额", "可用资金"), ["account"]),
                             (("消费", "账单", "支出"), ["bills"]),
                             (("订阅", "会员"), ["subscriptions", "subscription_usage"]),
                             (("理财", "投资", "储蓄"), ["financial_profile", "products", "account"]),
                             (("银行卡权益", "卡片权益"), ["cards", "card_benefits"])]:
            if any(word in message for word in words):
                domains.extend(names)
        combined = list(dict.fromkeys(domains))
        if len(combined) >= 3 and any(word in message for word in ("综合", "结合", "同时", "并", "以及")):
            understanding.scene = "cross_scene"
            understanding.read_tools = list(dict.fromkeys(combined + understanding.read_tools))[:8]
    from ..planning.financial_orchestrator import fallback_plan
    if (understanding.financial_task or fallback_plan(message)) and not understanding.write_intent and understanding.scene != "attack":
        understanding.scene = "financial_planning"
        understanding.read_tools = ["account", "financial_profile", "products"]
        if understanding.confidence < 0.8:
            return {"engine": CLARIFY, "route_reason":"当前目标理解置信度不足，请确认金额、期限及净利润或储蓄口径", "understanding":understanding.model_dump(), "understanding_context":prior, "model_status":"ok"}
    if _answer_our_own_amount_question(understanding, prior, message):
        decision = route(understanding)
        return {
            "engine": decision.branch,
            "route_reason": f"补齐上一轮追问的槽位：{decision.reason}",
            "understanding": understanding.model_dump(),
            "understanding_context": prior,
        }
    decision = route(understanding)
    return {
        "engine": decision.branch,
        "route_reason": decision.reason,
        "understanding": understanding.model_dump(),
        "understanding_context": prior,
    }


async def _prior_slots(state: BankingState) -> dict:
    """What the previous turn established, so a follow-up can be read.

    Two things travel forward, and both are needed. The grounded slots answer
    "再给他转50元" by resolving the pronoun to a person. The previous question
    and answer shape answer "那我这个月结余够不够" by telling the model what
    "这个月" was being asked about — without it, a follow-up to a read-only
    turn carries no antecedent at all, because a read leaves no slots behind.
    """
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        latest = await latest_turn(session, who.id)
        from ..context.memory import context as memory_context
        memory = await memory_context(session, who.user_id)
    if latest and latest.id <= memory.pop("_forget_before_turn_id", 0):
        latest = None
    carried = dict(latest.context) if latest and isinstance(latest.context, dict) else {}
    response = latest.response or {} if latest else {}
    question = response.get("question")
    if question:
        carried["previous_question"] = question
        carried["previous_answer_type"] = response.get("type", "")
    if memory:
        carried["user_memory"] = memory
    return carried


# Branch -> the single node that owns it. This table is the one place where a
# routing branch is bound to a handler, so a new branch cannot be added to
# routing.py without also being bound here. An unbound branch falls through to
# END and raises at request time, which is why tests assert coverage.
BRANCH_NODES = {
    WRITE: "write",
    CLARIFY: "clarify",
    ESCALATE: "escalate",
    BLOCKED_BRANCH: "blocked",
    BOUNDARY: "scene_boundary",
    GREETING: "greeting",
    EXTERNAL: "external",
    READ_ACCOUNT: "account",
    READ_BILL: "bill",
    READ_FINANCIAL: "financial",
    READ_PRODUCTS: "products",
    READ_SUBSCRIPTIONS: "subscriptions",
    READ_PLANS: "plans",
    READ_AA: "aa",
    READ_TOOLS: "tools",
    READ_BIRTHDAY: "birthday",
    READ_RISK: "risk",
    LOCAL_FALLBACK: "local",
    HELP_BRANCH: "help",
    DEGRADED: "degraded",
}


def router_edges(state: BankingState) -> str:
    """Map the router's branch to the node that owns it.

    Every branch maps to exactly one node, and every node is named after the
    verified view it renders — never after a user phrasing.
    """
    return BRANCH_NODES.get(state.get("engine"), END)


# ---------------------------------------------------------------------------
# Node: degraded — the model was reachable but could not answer this turn
# ---------------------------------------------------------------------------
async def degraded_node(state: BankingState) -> dict:
    status = state.get("model_status") or "unavailable"
    answer = message_answer(FALLBACK, "fallback", model_status=status)
    answer["trace"] = [{"label": "模型降级", "detail": status, "status": "ready"}]
    # Intentionally not persisted: a retry with the same request_id must be
    # able to succeed once the provider recovers.
    return {"result": answer, "engine": "fallback", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: blocked — the model named an attack the local guard did not catch
# ---------------------------------------------------------------------------
async def blocked_node(state: BankingState) -> dict:
    digest = hashlib.sha256(state["message"].encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "policy", "trace": previous.get("trace", [])}
        answer = message_answer(
            BLOCKED, "policy", boundary="malicious", reason_code="UNDERSTANDING_ATTACK_SCENE",
        )
        answer["trace"] = [
            {"label": "意图理解", "detail": "识别为攻击请求", "status": "done"},
            {"label": "安全门卫", "detail": "已拦截，未调用任何业务工具", "status": "done"},
        ]
        session.add(AuditLog(
            user_id=who.user_id, action="AGENT_SECURITY_BLOCK", target_type="agent_understanding",
            after_state="UNDERSTANDING_ATTACK_SCENE", evidence_ids=digest[:16],
        ))
        saved = await save(session, who, state["request_id"], digest, answer, {"boundary": "malicious"})
    return {"result": saved, "engine": "policy", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: scene boundary — out of scope / unsupported, as classified by the model
# ---------------------------------------------------------------------------
async def scene_boundary_node(state: BankingState) -> dict:
    understanding = state.get("understanding") or {}
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    if understanding.get("scene") == "unsupported":
        answer, context = unsupported_response("model_classified")
    else:
        answer, context = out_of_scope_response("model_classified")
    answer["trace"] = [
        *understanding_trace(state),
        {"label": "能力边界", "detail": "未调用任何账户写工具", "status": "done"},
    ]
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "boundary", "trace": previous.get("trace", [])}
        saved = await save(session, who, state["request_id"], digest, answer, context)
    return {"result": saved, "engine": "boundary", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: greeting — "你好" is a normal turn, not a boundary violation.
# Kept separate from scene_boundary_node so that a greeting is never answered
# with an out-of-scope card, and so the trace does not claim a capability
# check ran when nothing was actually being checked.
# ---------------------------------------------------------------------------
async def greeting_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    answer, context = greeting_response()
    answer["trace"] = [
        *understanding_trace(state),
        {"label": "对话回应", "detail": "打招呼不需要取数，也没有待执行的操作", "status": "done"},
    ]
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "model", "trace": previous.get("trace", [])}
        saved = await save(session, who, state["request_id"], digest, answer, context)
    return {"result": saved, "engine": "model", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: escalate — the model was not confident enough to act on
# ---------------------------------------------------------------------------
async def escalate_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    reason = state.get("route_reason", "low_confidence")
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "boundary", "trace": previous.get("trace", [])}
        # Not being able to read a turn is only worth a person when there is
        # nothing to carry forward. If the customer has an established thread,
        # the cheaper and more useful move is to ask one precise question: they
        # can answer it in a second, whereas a handoff stalls the conversation
        # and hands over a problem the assistant had two turns of context for.
        recent = await latest_turn(session, who.id)
        prior_question = (getattr(recent, "response", None) or {}).get("question")
        if prior_question:
            topic = str(prior_question).strip()
            if len(topic) > 24:
                topic = topic[:24] + "…"
            ask = f"我这边没太接上你这句。你是想接着刚才「{topic}」继续问，还是换一件事？"
            answer = message_answer(ask, "clarify", category="slot_request", needs_input=True)
            answer["trace"] = [
                *understanding_trace(state),
                {"label": "需要澄清", "detail": f"上一轮主题：{str(prior_question)[:40]}；本轮未能确定指向", "status": "ready"},
            ]
            saved = await save(session, who, state["request_id"], digest, answer, {"boundary": "clarify_followup"})
            return {"result": saved, "engine": "clarify", "trace": answer["trace"]}
        answer, context = await handoff_response(session, who.id, message, reason)
        answer["trace"] = [
            *understanding_trace(state),
            *answer.get("trace", []),
        ]
        saved = await save(session, who, state["request_id"], digest, answer, context)
    return {"result": saved, "engine": "boundary", "trace": answer.get("trace", [])}


# ---------------------------------------------------------------------------
# Node: write — every state-changing request builds a confirmation plan here
# ---------------------------------------------------------------------------
def _grounded(state: BankingState) -> Understanding:
    """Re-validate the model's proposal against the user's own words.

    Grounding only ever clears slots, so a malformed reply degrades into a
    question rather than an exception.
    """
    understanding = Understanding.model_validate(state.get("understanding") or {})
    try:
        return ground_understanding(understanding, state["message"], state.get("understanding_context"))
    except (ValueError, TypeError):
        cleared = understanding.model_dump()
        for field in ("recipient", "amount", "amount_evidence", "last4", "merchant",
                      "account_handle", "product_code", "order_id", "participant_count",
                      "day_of_month"):
            cleared[field] = None
        return Understanding.model_validate(cleared)


def _carried_context(grounded: Understanding, plan: dict | None = None,
                     awaiting: list[str] | None = None) -> dict:
    """Slots to remember for the next turn.

    Persisting only *grounded* values is what lets a follow-up ("200元", "改成
    300") finish a task without making the user restate a payee. Slots are never
    carried across scenes — grounder drops them — so a card request cannot inherit
    a transfer's recipient.

    ``awaiting`` records which gaps this turn put to the user. The next turn can
    then recognise "the thing you just asked me for" from our own record instead
    of re-asking the model to infer it, which is the one place its reading
    wobbles.
    """
    payload = (plan or {}).get("payload", {})
    data = grounded.model_dump()
    carried = {
        "scene": data["scene"],
        "operation": (plan or {}).get("kind") or data.get("operation"),
    }
    for field, source in (
        ("recipient", payload.get("recipient") or data.get("recipient")),
        ("account_handle", data.get("account_handle")),
        ("amount", payload.get("amount") or data.get("amount")),
        ("last4", payload.get("last4") or data.get("last4")),
        ("merchant", payload.get("merchant") or data.get("merchant")),
        ("day_of_month", payload.get("day_of_month") or data.get("day_of_month")),
        ("participant_count", payload.get("participant_count") or data.get("participant_count")),
        ("product_code", data.get("product_code")),
        ("order_id", data.get("order_id")),
        ("event_date", data.get("event_date")),
    ):
        if source:
            carried[field] = source
    if awaiting:
        carried["awaiting"] = list(awaiting)
    return carried


async def write_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    grounded = _grounded(state)
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": previous.get("engine", "policy"),
                    "trace": previous.get("trace", [])}
        # A value that is present but invalid (zero, negative, too precise) is a
        # business-rule failure: retrying the same words cannot help, so it
        # surfaces as a 400. A *missing* or *unverifiable* slot is a question.
        try:
            plan = await build_plan(
                session, who.user_id, grounded, message, demo_session_id=who.id,
            )
        except (Unresolvable, AlreadyDone) as question:
            # A missing slot is a question; an already-satisfied request is a
            # statement. Neither produces a button the user could click blindly.
            ask = isinstance(question, Unresolvable)
            text = await _address(session, who, str(question)) if ask else str(question)
            # "要给李四转多少？" comes out of plan_builder, not out of a security
            # check. Labelling it policy made the UI print 服务范围与安全检查
            # under a perfectly ordinary question.
            answer = message_answer(
                text, "clarify" if ask else "rules",
                category="slot_request" if ask else "already_satisfied",
                needs_input=ask,
            )
            if ask:
                answer["awaiting"] = _awaiting_slots(grounded)
            answer["trace"] = [
                *understanding_trace(state),
                {"label": "需要补充" if ask else "当前状态", "detail": text, "status": "ready"},
            ]
            saved = await save(session, who, state["request_id"], digest, answer,
                               _carried_context(grounded, awaiting=_awaiting_slots(grounded) if ask else None))
            if not ask:
                answer.pop("awaiting", None)
            return {"result": saved, "engine": "clarify" if ask else "rules", "trace": answer["trace"]}
        payload = {**plan["payload"], "message": message}
        action = DemoAction(
            id=str(uuid4()), session_id=who.id, request_id=state["request_id"],
            kind=plan["kind"], payload=payload, status="PENDING", created_at=datetime.now(),
        )
        # A revision invalidates only the previous card in this conversation;
        # a new, independent request must not cancel other pending operations.
        recent = await latest_turn(session, who.id)
        old_id = (recent.response or {}).get("action_id") if recent else None
        if old_id and re.search(r"改成|改为|改到|修改|换成|更正", message):
            old = await session.get(DemoAction, old_id)
            if old and old.session_id == who.id and old.kind == action.kind and old.status in {"PENDING", "AWAITING_STEP_UP"}:
                old.status = "SUPERSEDED"
                old.result = {"superseded_by": action.id}
                session.add(AuditLog(user_id=who.user_id, action="SUPERSEDE_CONFIRMATION", target_type=old.kind, after_state="SUPERSEDED", evidence_ids=old.id))
        session.add(action)
        await session.flush()
        answer = _action_view(action)
        # Every answer carries the layer that produced it, including a pending
        # confirmation card, so the UI and the audit trail agree on one label.
        # A card built here was grounded and gated, not refused — saying
        # "服务范围与安全检查" under a card that is waiting for approval reads
        # as though the request had been blocked.
        answer["engine"] = "write"
        answer["trace"] = [
            *understanding_trace(state),
            {"label": "核验业务对象", "detail": "收款人、金额与状态均来自已登记数据", "status": "done"},
            {"label": "等待确认", "detail": "确认前不会移动任何资金", "status": "ready"},
        ]
        saved = await save(session, who, state["request_id"], digest, answer,
                           _carried_context(grounded, plan))
        session.add(AuditLog(
            user_id=who.user_id, action="CREATE_CONFIRMATION_PLAN",
            target_type=plan["kind"], after_state="PENDING", evidence_ids=digest[:16],
        ))
        return {"result": saved, "engine": "write", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: clarify — the model named the scene but a slot is missing
# ---------------------------------------------------------------------------
def _awaiting_slots(grounded: Understanding) -> list[str]:
    """What the next turn is expected to supply, as slot labels.

    The model states gaps in prose ("本次转账的具体金额"); the resume guard
    matches on the label ("金额"). Storing the sentence would silently turn
    that guard off, so the two are kept apart on purpose.
    """
    if grounded.write_intent:
        return missing_write_slots(grounded)
    return [str(item) for item in (grounded.missing_information or [])]


async def _address(session, who, question: str) -> str:
    """Greet a clarification by name.

    The identity is already resolved — the session knows the customer's name —
    so asking "要给李四转多少？" from an assistant that just greeted them by name
    reads like it forgot who it was talking to.
    """
    if not question.endswith(("？", "?", "。", "！", "!")):
        question += "？"
    from ...core.models import User

    user = await session.get(User, who.user_id)
    name = (getattr(user, "name", None) or "").strip()
    if not name:
        return question
    return f"{name}，{question}"


async def clarify_node(state: BankingState) -> dict:
    """The model asked for a slot to be confirmed before this can go through.

    The question is produced by plan_builder so it names the actual gap and the
    user's real records — a generic "请补充信息" makes the user guess what the
    agent already knows.

    The planner gets the last word. Reaching this node only means the *model*
    thought something was missing; if plan_builder can actually build the plan
    from the user's own records, the request is executable and asking would be
    asking for something they already said.
    """
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    grounded = _grounded(state)
    # Several operations in one sentence cannot be split by the planner, and
    # silently doing only the first would be worse than asking.
    multi_operation = bool(grounded.missing_information) and len(
        re.findall(r"(?:给|向)[^，,。]{1,20}?(?:转|打)", message)
    ) > 1
    async with database.session_scope() as probe:
        who = await lookup_identity(probe, state.get("token"))
        previous = await replay(probe, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "policy", "trace": previous.get("trace", [])}
        buildable = False
        question = None
        if multi_operation:
            question = "一句话包含多笔操作，请逐笔办理：" + "；".join(grounded.missing_information)
        else:
            try:
                await build_plan(probe, who.user_id, grounded, message, demo_session_id=who.id)
                buildable = True
            except (Unresolvable, AlreadyDone) as ask:
                question = str(ask)
    if buildable:
        # Outside the probe session on purpose: session_scope takes SQLite's
        # exclusive write lock, so write_node must open its own connection
        # rather than nest inside this one. build_plan is read-only, so the plan
        # it builds here is the same one write_node will build.
        return await write_node(state)
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        question = await _address(session, who, question)
        # engine is what the user reads as provenance. A missing slot is not a
        # security decision, so it must not wear the "服务范围与安全检查" label.
        answer = message_answer(question, "clarify", category="slot_request", needs_input=True)
        answer["awaiting"] = _awaiting_slots(grounded)
        answer["trace"] = [
            *understanding_trace(state),
            {"label": "需要补充", "detail": question, "status": "ready"},
        ]
        saved = await save(session, who, state["request_id"], digest, answer,
                           _carried_context(grounded, awaiting=_awaiting_slots(grounded)))
        return {"result": saved, "engine": "clarify", "trace": answer["trace"]}


def _action_view(action) -> dict:
    """Render a pending DemoAction as the confirmation card the UI renders."""
    from ..demo_agent import view
    return view(action)


# ---------------------------------------------------------------------------
# Read nodes — one per verified view
# ---------------------------------------------------------------------------
def read_node(view_builder: Callable, *, engine: str = "analysis", extra_trace: Callable | None = None):
    """Build a graph node that renders exactly one verified view."""
    async def node(state: BankingState) -> dict:
        message = state["message"]
        digest = hashlib.sha256(message.encode()).hexdigest()
        understanding = state.get("understanding") or {}
        tools = list(understanding.get("read_tools") or [])
        async with database.session_scope() as session:
            who = await lookup_identity(session, state.get("token"))
            previous = await replay(session, who, state["request_id"], digest)
            if previous:
                return {"result": previous, "engine": previous.get("engine", engine),
                        "trace": previous.get("trace", [])}
            answer = await view_builder(session, who.user_id, tools, message)
            answer["trace"] = [
                *understanding_trace(state, tools),
                *answer.get("trace", []),
            ]
            if extra_trace is not None:
                answer["trace"] = [*answer["trace"], *extra_trace(state)]
            saved = await save(session, who, state["request_id"], digest, answer)
        return {"result": saved, "engine": engine, "trace": answer["trace"]}
    return node


async def _account_view(session, user_id, tools, message):
    if re.search(r"手续费|费率", message) and re.search(r"转账|转.{0,12}元", message):
        return message_answer("本地演示账户转账手续费为 ¥0.00；这是演示规则，真实银行费用以对应渠道的费率为准。此查询不会发起转账。", "rules")
    return await account_view(session, user_id, tools=tools, message=message)


account_node = read_node(_account_view)
products_node = read_node(lambda s, u, t, m: product_view(s, u))
subscriptions_node = read_node(lambda s, u, t, m: subscription_view(s, u))
plans_node = read_node(lambda s, u, t, m: scheduled_view(s, u))
aa_node = read_node(lambda s, u, t, m: aa_view(s, u))


# ---------------------------------------------------------------------------
# Node: investor risk assessment (read; opens the questionnaire when missing)
# ---------------------------------------------------------------------------
async def risk_node(state: BankingState) -> dict:
    """Show the report when a valid grade exists, otherwise the questionnaire.

    A grade is a dated record, so this never reconstructs one from the message:
    either there is a live assessment or the customer has to answer the four
    questions.
    """
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "analysis", "trace": previous.get("trace", [])}
        record = await risk_view.current(session, who.user_id)
        if record:
            answer = risk_view.report(record)
        else:
            answer = await risk_view.questionnaire(session, who.user_id)
            if answer is None:
                # No financial profile yet: the objective half has nothing to
                # read, and guessing a grade from the message would be worse
                # than saying what is missing.
                answer = risk_view.needs_profile_message()
                answer["trace"] = [
                    *understanding_trace(state),
                    {"label": "需要补充", "detail": "财务档案尚未建立", "status": "ready"},
                ]
                saved = await save(session, who, state["request_id"], digest, answer)
                return {"result": saved, "engine": "analysis", "trace": answer["trace"]}
        answer.setdefault("trace", [
            *understanding_trace(state),
            {"label": "读取风险测评记录", "detail": "客观档案 + 主观问卷", "status": "done"},
        ])
        saved = await save(session, who, state["request_id"], digest, answer)
    return {"result": saved, "engine": "analysis", "trace": saved.get("trace", [])}


# ---------------------------------------------------------------------------
# Node: bill analysis
# ---------------------------------------------------------------------------
async def bill_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    understanding = state.get("understanding") or {}
    period = understanding.get("period") or "month"
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "analysis", "trace": previous.get("trace", [])}
        answer = await build_bill_analysis(session, who.user_id, period, message)
        answer["trace"] = [
            *understanding_trace(state),
            {"label": "读取本地账单", "detail": f"{period} 期间的交易与导入账单", "status": "done"},
            {"label": "运行分析", "detail": "分类、基线、异常与趋势聚合", "status": "done"},
        ]
        saved = await save(session, who, state["request_id"], digest, answer)
    return {"result": saved, "engine": "analysis", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: personalised financial planning (read; may open an intake form)
# ---------------------------------------------------------------------------
async def financial_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "analysis", "trace": previous.get("trace", [])}
        answer = await build_financial_analysis(session, who.user_id, state["message"], state.get("understanding"))
        from ..analysis.scenarios import apply_income_scenario
        answer = await apply_income_scenario(session, who.user_id, answer, message) if not answer.get("orchestration") else answer
        if answer["type"] != "financial_intake":
            answer["trace"] = [
                *understanding_trace(state),
                *answer.get("trace", []),
                {"label": "读取本地工具", "detail": "账户、订阅、持仓、目标与风险约束", "status": "done"},
                {"label": "生成方案", "detail": "本地金额计算", "status": "done"},
            ]
            saved = await save(session, who, state["request_id"], digest, {**answer, "engine": "analysis"})
            return {"result": saved, "engine": "analysis", "trace": answer["trace"]}
        # intake is stateful; persist the form without a narrative
        answer["trace"] = [
            *understanding_trace(state),
            {"label": "识别需求", "detail": "需先补齐理财约束", "status": "ready"},
        ]
        saved = await save(session, who, state["request_id"], digest, answer)
    return {"result": saved, "engine": "analysis", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: cross-scene / open-ended goals — run exactly the tools the model chose
# ---------------------------------------------------------------------------
async def tools_node(state: BankingState) -> dict:
    """Open-ended goals need several data domains crossed.

    With the optional agent loop enabled the model drives the tool loop itself;
    otherwise the tools already chosen by understanding are executed here in the
    same order, so the answer never depends on a second planning call.
    """
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    understanding = state.get("understanding") or {}
    tools = list(understanding.get("read_tools") or [])
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        latest = await latest_turn(session, who.id)
        revision = latest.id if latest else None

    if understanding.get("unused_days"):
        from ..analysis.scenarios import unused_subscription_view
        async with database.session_scope() as session:
            who = await lookup_identity(session, state.get("token"))
            previous = await replay(session, who, state["request_id"], digest)
            if previous:
                return {"result": previous, "engine": "analysis", "trace": previous.get("trace", [])}
            answer = await unused_subscription_view(session, who.user_id, understanding["unused_days"])
            answer["trace"] = [*understanding_trace(state, tools), *answer["trace"]]
            saved = await save(session, who, state["request_id"], digest, answer)
        return {"result": saved, "engine": "analysis", "trace": answer["trace"]}
    from ..analysis.scenarios import scenario_parameters, apply_income_scenario
    drop, target = scenario_parameters(message)
    if drop is not None and target is not None:
        # Scenario arithmetic must include living costs and debt, even when the
        # understanding model selected a cross-scene subscription/bill route.
        async with database.session_scope() as session:
            who = await lookup_identity(session, state.get("token"))
            previous = await replay(session, who, state["request_id"], digest)
            if previous:
                return {"result": previous, "engine": "analysis", "trace": previous.get("trace", [])}
            answer = await build_financial_analysis(session, who.user_id, state["message"], state.get("understanding"))
            answer = await apply_income_scenario(session, who.user_id, answer, message) if not answer.get("orchestration") else answer
            if answer.get("scenario"):
                from ..analysis.bill_analysis import build_bill_analysis
                answer["supporting_bill"] = await build_bill_analysis(session, who.user_id, "month")
                from ..analysis.scenarios import subscription_saving_options
                answer["saving_options"] = await subscription_saving_options(session, who.user_id)
                answer["recommendation"] = None  # baseline buying advice cannot describe a reduced-income case
                answer["trace"] = [*understanding_trace(state, tools),
                    {"label": "核验财务与订阅", "detail": answer['scenario']['basis'], "status": "done"},
                    {"label": "计算情景缺口", "detail": answer['summary'], "status": "done"},
                    {"label": "读取消费明细", "detail": "使用本月已导入账单，不用账单样本代替全部必要支出", "status": "done"}]
                saved = await save(session, who, state["request_id"], digest, answer)
                return {"result": saved, "engine": "analysis", "trace": answer['trace']}
    if settings.agent_loop:
        from .agent import run_agent
        answer = await run_agent(state.get("token"), message, state["request_id"], required_tools=tools)
        if answer is not None:
            async with database.session_scope() as session:
                who = await lookup_identity(session, state.get("token"))
                previous = await replay(session, who, state["request_id"], digest)
                if previous:
                    return {"result": previous, "engine": previous.get("engine", "agent"),
                            "trace": previous.get("trace", [])}
                current = await latest_turn(session, who.id)
                if (current.id if current else None) != revision:
                    raise BusinessRuleException("会话已有新消息，请重新发送，避免使用过期的业务上下文")
                answer["trace"] = [*understanding_trace(state, tools), *answer.get("trace", [])]
                saved = await save(session, who, state["request_id"], digest, answer)
            return {"result": saved, "engine": answer.get("engine", "agent"), "trace": answer.get("trace", [])}

    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": previous.get("engine", "analysis"),
                    "trace": previous.get("trace", [])}
        current = await latest_turn(session, who.id)
        if (current.id if current else None) != revision:
            raise BusinessRuleException("会话已有新消息，请重新发送，避免使用过期的业务上下文")
        plan = await build_universal_plan(session, who.user_id, message, tools=tools or None)
        plan["trace"] = [*understanding_trace(state, tools), *plan.get("trace", [])]
        saved = await save(session, who, state["request_id"], digest, plan)
    return {"result": saved, "engine": "analysis", "trace": plan["trace"]}


# ---------------------------------------------------------------------------
# Node: birthday cross-scene plan
# ---------------------------------------------------------------------------
async def birthday_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "analysis", "trace": previous.get("trace", [])}
        answer = await build_birthday_plan(session, who.user_id, message, state.get("understanding"))
        answer["trace"] = [*understanding_trace(state), *answer.get("trace", [])]
        saved = await save(session, who, state["request_id"], digest, answer)
    return {"result": saved, "engine": "analysis", "trace": answer["trace"]}


# ---------------------------------------------------------------------------
# Node: external data (fx / macro / sec) — network outside a DB transaction
# ---------------------------------------------------------------------------
async def external_node(state: BankingState) -> dict:
    from . import conversation as conversation_module
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        revision = await latest_turn(session, who.id)
        revision = revision.id if revision else None
    fx = parse_fx_request(message)
    macro = parse_macro_request(message)
    sec = parse_sec_request(message)
    if fx:
        answer = await conversation_module.fetch_fx_quote(*fx)
    elif macro:
        answer = await conversation_module.fetch_macro_indicator(macro)
    else:
        answer = await conversation_module.fetch_sec_filings(sec)
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "external-tool", "trace": previous.get("trace", [])}
        latest = await latest_turn(session, who.id)
        if (latest.id if latest else None) != revision:
            raise BusinessRuleException("会话已有新消息，请重新查询，避免返回过期结果")
        saved = await save(session, who, state["request_id"], digest, answer)
    return {"result": saved, "engine": "external-tool", "trace": answer.get("trace", [])}


# ---------------------------------------------------------------------------
# Node: help — capabilities, or the no-model capability list
# ---------------------------------------------------------------------------
async def help_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    understood = state.get("understanding") is not None
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "rules", "trace": previous.get("trace", [])}
        if understood:
            answer = message_answer(FAQ["capabilities"], "model")
            trace = [*understanding_trace(state), {"label": "生成结果", "detail": "说明可办理的业务与确认机制", "status": "done"}]
        else:
            answer = message_answer(HELP, "rules", model_status="not_configured")
            trace = [{"label": "识别需求", "detail": "模型未配置，回落到本地能力提示", "status": "done"}]
        answer["trace"] = trace
        saved = await save(session, who, state["request_id"], digest, answer)
    return {"result": saved, "engine": answer["engine"], "trace": trace}


# ---------------------------------------------------------------------------
# Node: local — deterministic responder used only when no model is configured
# ---------------------------------------------------------------------------
async def local_node(state: BankingState) -> dict:
    message = state["message"]
    digest = hashlib.sha256(message.encode()).hexdigest()
    async with database.session_scope() as session:
        who = await lookup_identity(session, state.get("token"))
        previous = await replay(session, who, state["request_id"], digest)
        if previous:
            return {"result": previous, "engine": "rules", "trace": previous.get("trace", [])}
        answer = await respond(session, who, message, state["request_id"])
        answer.setdefault("trace", [
            {"label": "识别需求", "detail": "本地精确指令", "status": "done"},
            {"label": "核验业务对象", "detail": "当前用户数据与状态规则", "status": "done"},
            {"label": "生成结果", "detail": "只读结果或待确认计划", "status": "done"},
        ])
        saved = await save(session, who, state["request_id"], digest, {**answer, "engine": "rules"}, answer.get("context"))
        saved.pop("context", None)
    return {"result": saved, "engine": "rules", "trace": answer.get("trace", [])}


# ---------------------------------------------------------------------------
# Build the StateGraph
# ---------------------------------------------------------------------------
READ_NODES = (
    "write", "clarify", "escalate", "blocked", "scene_boundary", "degraded",
    "account", "bill", "financial", "products", "subscriptions", "plans",
    "aa", "tools", "birthday", "external", "help", "local", "risk", "greeting",
)


def build_graph() -> StateGraph:
    graph = StateGraph(BankingState)

    graph.add_node("guard", guard_node)
    graph.add_node("boundary", boundary_node)
    graph.add_node("router", router_node)
    graph.add_node("degraded", degraded_node)
    graph.add_node("blocked", blocked_node)
    graph.add_node("scene_boundary", scene_boundary_node)
    graph.add_node("greeting", greeting_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("write", write_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("account", account_node)
    graph.add_node("bill", bill_node)
    graph.add_node("financial", financial_node)
    graph.add_node("risk", risk_node)
    graph.add_node("products", products_node)
    graph.add_node("subscriptions", subscriptions_node)
    graph.add_node("plans", plans_node)
    graph.add_node("aa", aa_node)
    graph.add_node("tools", tools_node)
    graph.add_node("birthday", birthday_node)
    graph.add_node("external", external_node)
    graph.add_node("help", help_node)
    graph.add_node("local", local_node)

    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", guard_blocked, {"done": END, "boundary": "boundary"})
    graph.add_conditional_edges("boundary", boundary_done, {"done": END, "router": "router"})
    graph.add_conditional_edges("router", router_edges, {
        node: node for node in READ_NODES
    })
    for node in READ_NODES:
        graph.add_edge(node, END)
    return graph


_graph = build_graph()
_compiled = _graph.compile()


async def run(token: str | None, message: str, request_id: str) -> dict:
    """Entry point for the HTTP layer. Returns the front-end payload.

    ``strip_thinking`` is a final defensive barrier applied to the entire
    payload before it leaves the server, so no provider reasoning wrapper can
    reach the browser regardless of which route produced the answer.
    """
    result = await _compiled.ainvoke({
        "token": token,
        "message": message,
        "request_id": request_id,
        "result": None,
        "engine": "graph",
        "trace": [],
        "route_reason": "",
    })
    payload = strip_thinking(result["result"])
    route_reason = result.get("route_reason")
    if route_reason and isinstance(payload, dict):
        payload["routing"] = {"reason": route_reason}
    return payload


__all__ = [
    "BankingState", "build_graph", "run", "router_edges", "router_node",
    "WRITE", "CLARIFY", "ESCALATE", "BLOCKED_BRANCH", "BOUNDARY", "EXTERNAL",
    "READ_ACCOUNT", "READ_BILL", "READ_FINANCIAL", "READ_PRODUCTS",
    "READ_SUBSCRIPTIONS", "READ_PLANS", "READ_AA", "READ_TOOLS", "READ_BIRTHDAY",
    "LOCAL_FALLBACK", "HELP_BRANCH", "DEGRADED",
    "SCENE_LABELS", "TOOL_LABELS", "understanding_trace", "READ_NODES",
]
