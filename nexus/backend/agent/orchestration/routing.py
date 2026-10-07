"""Routing policy for the LangGraph orchestrator.

The model decides what the user wants (see understanding.py). This module only
decides *whether the request is allowed to proceed* and *which handler owns it
once the model has named a scene and an operation*. It never guesses intent from
keywords.

Rule layers kept here, and why:
  - Veto, not routing. A scene the model names is dispatched by name; rules
    only remove scenes that must never run (attacks, low confidence, out-of-scope,
    unsupported business, incomplete write slots).
  - Deterministic dispatch. Once a scene is known, the same handler must run
    every time, so routing stays auditable and testable.
  - Degradation. When no model is configured the agent must still refuse unsafe
    requests and give a bounded, useful answer rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts.understanding import WRITE_SCENES, Understanding, resolve_operation


@dataclass(frozen=True)
class RouteDecision:
    branch: str
    reason: str


# Branch identifiers. Each one maps to exactly one graph node that owns the
# business data for that shape. Read branches are named after the verified view
# they render, not after the user's phrasing.
WRITE = "write"
CLARIFY = "clarify"
ESCALATE = "escalate"
BLOCKED = "blocked"
BOUNDARY = "boundary"
GREETING = "greeting"
EXTERNAL = "external"

READ_ACCOUNT = "read_account"
READ_BILL = "read_bill"
READ_FINANCIAL = "read_financial"
READ_PRODUCTS = "read_products"
READ_SUBSCRIPTIONS = "read_subscriptions"
READ_PLANS = "read_plans"
READ_AA = "read_aa"
READ_TOOLS = "read_tools"
READ_BIRTHDAY = "read_birthday"
READ_RISK = "read_risk"

LOCAL_FALLBACK = "local_fallback"
HELP_BRANCH = "help"

# Scene -> read handler. A scene that can also write is routed here only when
# ``write_intent`` is false; otherwise it goes through the write path below.
SCENE_READ_BRANCH = {
    "account_query": READ_ACCOUNT,
    "card": READ_ACCOUNT,
    "bill_analysis": READ_BILL,
    "financial_planning": READ_FINANCIAL,
    "financial_profile": READ_PRODUCTS,
    "subscription": READ_SUBSCRIPTIONS,
    "scheduled_transfer": READ_PLANS,
    "aa_collection": READ_AA,
    "cross_scene": READ_TOOLS,
    "birthday": READ_BIRTHDAY,
    "risk_assessment": READ_RISK,
    "external_data": EXTERNAL,
    "capabilities": HELP_BRANCH,
}

# What each write operation needs before a confirmation card can be built.
# Keyed by operation, not by scene: "申请一张新卡" and "锁卡" share a scene but
# not a single required slot.
OPERATION_SLOTS = {
    "transfer": ("recipient", "amount"),
    "create_scheduled_transfer": ("recipient", "amount", "day_of_month"),
    "create_aa_collection": ("participant_count", "amount"),
    "lock_card": ("last4",),
    "unlock_card": ("last4",),
    "report_lost": ("last4",),
    "set_card_limit": ("last4", "amount"),
    "apply_card": (),
    "cancel_subscription": ("merchant",),
    "revoke_mandate": ("merchant",),
    "subscribe_product": ("product_code", "amount"),
    "redeem_product": ("order_id",),
    "create_birthday_plan": ("event_date", "amount"),
    "create_support_ticket": (),
    "create_human_handoff": (),
}

SLOT_LABELS = {
    "recipient": "收款人",
    "amount": "金额",
    "day_of_month": "执行日期",
    "participant_count": "参与人数",
    "last4": "银行卡尾号四位",
    "merchant": "商户名称",
    "product_code": "产品代码",
    "order_id": "订单号",
    "limit_type": "限额口径",
    "event_date": "日期",
}

# Below this the agent escalates instead of guessing a payee or an amount.
MIN_CONFIDENCE = 0.8


# A payee can arrive as a registered name or as a handle ("我妈"、"房东") that
# plan_builder._resolve_recipient resolves against the registry. Both satisfy the
# slot, so completeness must be judged by the same rule the resolver uses — the
# two disagreed here and the agent asked for a payee it was already holding.
_PAYEE_ALIASES = ("recipient", "account_handle")

# Same disagreement, one step over: a dated payment can arrive as a full date
# ("2026-10-10") or as a day of month ("10 号"), and agent.schedule reads both.
# Requiring only day_of_month made a perfectly specified one-off look like it
# was missing its execution date, and the customer was asked for something they
# had already said.
_TIMING_ALIASES = ("day_of_month", "run_date")


def payee_slot_filled(data: dict) -> bool:
    return any(str(data.get(name) or "").strip() for name in _PAYEE_ALIASES)


def timing_slot_filled(data: dict) -> bool:
    if any(str(data.get(name) or "").strip() for name in _TIMING_ALIASES):
        return True
    # "每周三" is a complete instruction. Asking a customer who already named
    # the weekday to also supply an execution date is the kind of question that
    # teaches people the assistant is not listening.
    return str(data.get("recurrence") or "").lower() == "weekly" and data.get("weekday") is not None


def missing_write_slots(understanding: Understanding, operation: str | None = None) -> list[str]:
    """Names of the slots a write operation still needs before it can be planned."""
    operation = operation or resolve_operation(understanding)
    if operation is None:
        return ["要执行的具体操作"]
    required = OPERATION_SLOTS.get(operation, ())
    data = understanding.model_dump()
    def filled(name: str) -> bool:
        if name == "recipient":
            return payee_slot_filled(data)
        if name == "day_of_month":
            return timing_slot_filled(data)
        return data.get(name) is not None
    return [SLOT_LABELS[name] for name in required if not filled(name)]


def route(understanding: Understanding) -> RouteDecision:
    """Map an already-understood request to a graph branch. No keyword matching."""
    scene = understanding.scene

    # 1. Security veto: an attack never reaches a business handler, and its
    #    wording is never passed through to the user or to a tool.
    if scene == "attack":
        return RouteDecision(BLOCKED, "识别到注入/角色伪装/密钥索取，直接拒绝")

    # 2. Scope veto, handled by the same deterministic boundary gateway that
    #    already owns user-initiated escalation and handoff.
    #    A greeting is deliberately NOT a veto: saying hello is a normal turn,
    #    and routing it through the boundary gateway answered a greeting with
    #    "out of scope". It gets its own branch so the two never share a card.
    #    smalltalk stays a veto — that scene means the user asked for something
    #    we do not do, which is exactly what the boundary card is for.
    if scene == "greeting":
        return RouteDecision(GREETING, "打招呼：没有业务诉求，正常寒暄")
    if scene == "smalltalk":
        return RouteDecision(BOUNDARY, "out_of_scope")
    if scene == "unsupported":
        return RouteDecision(BOUNDARY, "unsupported_financial")

    # 3. Write path. Only the model's explicit write_intent can reach it.
    if understanding.write_intent and scene in WRITE_SCENES:
        operation = resolve_operation(understanding)
        missing = missing_write_slots(understanding, operation)
        gaps = missing or list(understanding.missing_information)
        if gaps or understanding.output == "clarify":
            # Asking is always cheaper and safer than handing a human: the agent
            # already knows the business, it just needs one detail, and it can
            # name the user's real records to make the question answerable.
            return RouteDecision(CLARIFY, f"写操作缺少必要信息：{'、'.join(gaps or ['更多信息'])}")
        if understanding.confidence < MIN_CONFIDENCE:
            # Every slot is present but the reading itself is shaky. Moving money
            # or changing state on a shaky reading is the one thing we must not do.
            return RouteDecision(ESCALATE, f"low_confidence_{understanding.confidence:.2f}")
        return RouteDecision(WRITE, f"识别为 {operation} 写操作，先出确认卡")

    # 4. Read path. The scene owns the renderer; the model chose the scene.
    branch = SCENE_READ_BRANCH.get(scene)
    if branch is None:
        # The schema is closed, so this only fires if a future scene is added
        # without a handler. Degrade to a bounded answer instead of guessing.
        return RouteDecision(BOUNDARY, "未覆盖的业务范畴，转由边界处理")
    if understanding.confidence < MIN_CONFIDENCE:
        # Reading records back to a user on a shaky reading is misleading even
        # though it moves no money, so the turn is escalated instead.
        return RouteDecision(ESCALATE, f"low_confidence_{understanding.confidence:.2f}")
    if scene == "cross_scene" and understanding.goal == "birthday_plan":
        # A birthday plan needs a concrete date before it can reserve money, so
        # it gets the dedicated intake form instead of a generic multi-tool draft.
        return RouteDecision(READ_BIRTHDAY, "识别到生日跨场景目标，需要确认日期")
    return RouteDecision(branch, f"识别为 {scene}，按已选工具取数")


def degraded_route(message: str) -> RouteDecision:
    """Routing when no model is available.

    The agent must stay useful and safe without a model, but it must also never
    *guess* intent from keywords. So the degraded path does not classify the
    message at all: it hands the turn to the local responder, which only answers
    commands it can parse exactly, and shows the capability list for everything
    else. That keeps a model-less deployment honest rather than clever.
    """
    from ..planning.policy import exact_command

    if exact_command(message):
        return RouteDecision(LOCAL_FALLBACK, "无模型：仅办理可确定性解析的本地指令")
    return RouteDecision(HELP_BRANCH, "无模型：给出能力边界与可用指令，不猜测意图")


__all__ = [
    "RouteDecision", "route", "degraded_route", "missing_write_slots",
    "OPERATION_SLOTS", "SLOT_LABELS", "MIN_CONFIDENCE",
    "WRITE", "CLARIFY", "ESCALATE", "BLOCKED", "BOUNDARY", "GREETING", "EXTERNAL",
    "READ_ACCOUNT", "READ_BILL", "READ_FINANCIAL", "READ_PRODUCTS",
    "READ_SUBSCRIPTIONS", "READ_PLANS", "READ_AA", "READ_TOOLS",
    "READ_BIRTHDAY", "READ_RISK", "LOCAL_FALLBACK", "HELP_BRANCH",
]
