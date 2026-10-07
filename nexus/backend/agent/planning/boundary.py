"""Deterministic boundary gateway and user-safe fallback responses.

Two different things live here, and the difference is load-bearing.

**Interaction protocol** (``classify_local``): rules about *how a conversation
proceeds* rather than what the user wants. "确认" typed as chat text is never
an authorisation. "转人工" is an escalation. "你没听懂" is an error signal.
These are fixed strings, they are not paraphrasable, and keeping them in code
is what makes them unarguable. They run before the model on purpose.

**Scope classification**: deliberately *absent*. Whether a request is out of
scope, unsupported, or simply a greeting is the router model's job — it owns
the scene vocabulary, and Chinese is unbounded enough that a keyword table here
is a guess that will be wrong in both directions. Listing "写代码" but forgetting
"写个爬虫帮你抓一下" is the failure mode this layer used to have: it silently
answered a refusal template for a real banking question. The model may propose
a scene; the guard, grounder and confirmation layers keep the veto.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from ...core.models import Account, AgentTurn, Card, ContextEvent, Recipient
from ..security.guard import normalized


MISUNDERSTOOD = ("没听懂", "没明白", "不是这个意思", "还是不对", "答非所问", "你理解错了")
HUMAN_REQUEST = ("转人工", "人工客服", "找客服", "真人客服", "人工处理", "客服人员")
FRAUD_DISTRESS = ("盗刷", "不是我刷的", "不是我消费", "卡被刷", "可疑扣款", "陌生消费")

# Bare authorizations typed as chat text. Only the confirmation card's own
# button carries an already-verified intent, so these are intercepted before the
# model sees them rather than being reinterpreted as a new request.
TEXT_CONFIRMATION = ("确认", "确定", "好的", "好", "可以", "同意", "确认执行", "是的", "嗯")


def classify_local(message: str) -> dict | None:
    """Return a protocol decision for a fixed interaction signal, or None.

    This never decides *what business* the user meant. Anything that needs a
    judgement about the user's intent is left to the router model.
    """
    value = normalized(message).strip()
    if any(word in value for word in HUMAN_REQUEST):
        return {"category": "handoff", "reason": "user_requested_human"}
    if any(word in value for word in MISUNDERSTOOD):
        return {"category": "misunderstood", "reason": "user_reports_misunderstanding"}
    if any(word in value for word in FRAUD_DISTRESS):
        return {"category": "fraud_distress", "reason": "possible_unauthorized_card_use"}
    if value.rstrip("。！! ") in TEXT_CONFIRMATION:
        return {"category": "text_confirmation", "reason": "typed_confirmation_not_executable"}
    if _is_interrupted_transfer(value):
        return {"category": "interruption", "reason": "transfer_paused_for_balance"}
    return None


def _is_interrupted_transfer(value: str) -> bool:
    return (
        (
            any(word in value for word in ("转账", "转给", "打给", "汇给"))
            or bool(re.search(r"给.{1,20}(?:转|打|汇)\s*\d", value))
        )
        and any(word in value for word in ("等等", "等一下", "先查", "先看", "先查询"))
        and any(word in value for word in ("余额", "账户", "有多少钱"))
    )


def transfer_slots(message: str) -> dict:
    value = normalized(message)
    # Prefer the bounded "给某人转金额" form.  A greedy generic match would
    # otherwise treat "张三转100元" as the recipient.
    recipient_match = re.search(
        r"(?:给)\s*([^，,。.!！?？\s]{1,20}?)(?:转账|转|打|汇|发)(?=\s*(?:\d|[零一二两三四五六七八九十百千万]))",
        value,
    )
    if recipient_match is None:
        recipient_match = re.search(r"(?:转给|打给|汇给|发给)\s*([^，,。.!！?？\s]{1,20})", value)
    amount_match = re.search(r"(\d+(?:\.\d{1,2})?)\s*(?:元|块|块钱)", value)
    return {
        "intent": "transfer",
        "recipient": recipient_match.group(1) if recipient_match else None,
        "amount": amount_match.group(1) if amount_match else None,
    }


async def misunderstanding_count(session, session_id: int) -> int:
    latest = await session.scalar(
        select(AgentTurn).where(AgentTurn.session_id == session_id).order_by(AgentTurn.id.desc()).limit(1)
    )
    prior = latest.context if latest and isinstance(latest.context, dict) else {}
    return int(prior.get("misunderstanding_count", 0)) + 1


def _actions(*items: tuple[str, str, str]) -> list[dict]:
    return [{"label": label, "command": command, "tone": tone} for label, command, tone in items]


def out_of_scope_response(reason: str) -> tuple[dict, dict]:
    """Redirect a request we do not serve, without pretending to know more
    about it than we do.

    Scope is decided by the router model, which names a scene but not a topic
    code — so the wording must read naturally for *any* off-topic request.
    An earlier version interpolated a topic noun from a keyword table, and the
    model-driven path rendered "目前不直接处理这个问题", which is worse than
    saying nothing about the topic at all.
    """
    answer = {
        "type": "boundary", "category": "out_of_scope", "title": "这个我帮不上",
        "message": "我是你的 AI 金融管家，这类事情我确实做不了，我也不会给你编一个答案。"
                   "但账户和消费这块我能实打实地帮到你——要我先看看你现在的账单或者余额吗？",
        "actions": _actions(("分析本月账单", "分析我这个月的账单", "primary"), ("查看账户余额", "查看余额", "secondary")),
        "engine": "model", "trace": [{"label":"边界路由","detail":"识别为非金融请求，未调用账户写工具","status":"done"}],
    }
    return answer, {"boundary": "out_of_scope", "reason": reason}


# A greeting is a normal conversational turn, not a boundary violation. The
# model tells us the user made no request at all ("你好"), which is different
# from "写首诗" — a request we do not serve, which stays on the boundary card.
# The starters are read-only on purpose: a greeting must not put a
# money-moving action one click away.
def greeting_response() -> tuple[dict, dict]:
    answer = {
        "type": "chat", "category": "greeting", "title": "你好",
        "message": "你好，我是你的 AI 金融管家。直接说你要办的事就行——我会先取真实数据再回答，"
                   "数字不会是我编的；转账、锁卡这类会改状态的操作，我会先给你确认卡，你点了才执行。",
        "actions": _actions(
            ("查看账户余额", "查看余额", "primary"),
            ("分析本月账单", "分析我这个月的账单", "secondary"),
            ("你能办哪些事", "你能做什么", "secondary"),
        ),
        "engine": "model",
        "trace": [{"label": "意图理解", "detail": "识别为打招呼，没有业务诉求", "status": "done"}],
    }
    return answer, {"boundary": "greeting"}


def unsupported_response(reason: str) -> tuple[dict, dict]:
    labels = {
        "corporate_loan": "企业及对公贷款", "stock_trading": "股票委托交易",
        "social_security": "社保、医保或公积金业务", "regulated_advice": "收益保证或内幕交易相关请求",
    }
    capability = labels.get(reason, "这一类金融业务")
    answer = {
        "type": "boundary", "category": "unsupported_financial", "title": "当前能力暂不覆盖这项金融业务",
        "message": f"目前我不能直接办理{capability}，也不会编造政策、额度或办理结果。我可以帮你把诉求整理好转给人工客服；如果人工坐席不在线，也可以先登记，等他们上班后联系你。",
        "actions": _actions(("转接人工客服", "转接人工客服", "primary"), ("让客服稍后跟进", "创建客服工单", "secondary")),
        "engine": "boundary", "trace": [{"label":"能力边界","detail":f"unsupported_financial · {reason}","status":"done"}],
    }
    return answer, {"boundary": "unsupported_financial", "reason": reason}


async def handoff_response(session, session_id: int, message: str, reason: str, count: int = 0) -> tuple[dict, dict]:
    recent = list((await session.scalars(
        select(AgentTurn).where(AgentTurn.session_id == session_id).order_by(AgentTurn.id.desc()).limit(3)
    )).all())
    outcomes = []
    for turn in reversed(recent):
        response = turn.response or {}
        text = response.get("title") or response.get("message") or response.get("type")
        if text:
            outcomes.append(str(text)[:80])
    issue = re.sub(r"\s+", " ", message).strip()[:160]
    summary = f"用户当前诉求：{issue}。"
    if outcomes:
        summary += " 前序处理：" + "；".join(outcomes) + "。"
    open_now = 9 <= datetime.now().hour < 18
    status = "人工客服现在在线，可以直接接手" if open_now else "现在是非工作时间，人工坐席不在线"
    command = "创建人工接管工单" if open_now else "创建客服工单"
    answer = {
        "type": "support_handoff", "category": "handoff", "title": "我已经把你的情况整理好了",
        "message": f"{status}。不用担心重复描述，我已经把你刚才说的整理成摘要。"
                   + ("你可以现在直接转接，也可以先让我把诉求登记下来。" if open_now
                      else "我可以先帮你登记，等他们上班后联系你。")
                   + "转接前你还会看到一张确认卡。",
        "summary": summary, "reason": reason, "attempts": count,
        "actions": _actions((("转接人工客服" if open_now else "让客服稍后联系我"), command, "primary")),
        "engine": "boundary", "trace": [
            {"label":"升级判断","detail":reason,"status":"done"},
            {"label":"整理摘要","detail":"只保留业务诉求和前序处理结果","status":"done"},
            {"label":"接管状态","detail":status,"status":"ready"},
        ],
    }
    context = {"boundary":"handoff", "handoff":{"summary":summary,"reason":reason,"channel":"human" if open_now else "ticket"}}
    return answer, context


async def interruption_response(session, user_id: int, message: str) -> tuple[dict, dict]:
    slots = transfer_slots(message)
    accounts = list((await session.scalars(select(Account).where(Account.user_id == user_id))).all())
    available = sum((Decimal(row.available_balance) for row in accounts), Decimal("0"))
    action_items = []
    if slots.get("recipient") and slots.get("amount"):
        action_items.append(("继续生成转账确认", f"给{slots['recipient']}转账{slots['amount']}元", "primary"))
    answer = {
        "type":"interruption", "category":"task_interruption", "title":"已暂停转账，先完成余额查询",
        "message":f"当前可用余额为 ¥{available:,.2f}。原转账任务仍保留，没有生成付款或扣款。" + ("你可以继续原任务。" if action_items else "请继续补充收款人和金额。"),
        "paused_task":slots, "metrics":[{"label":"可用余额","value":f"¥{available:,.2f}"}],
        "actions":_actions(*action_items), "engine":"boundary",
        "trace":[{"label":"识别打断","detail":"暂停转账任务","status":"done"},{"label":"调用账户工具","detail":"只读查询当前可用余额","status":"done"},{"label":"保存任务槽位","detail":"等待用户继续，不创建资金操作","status":"ready"}],
    }
    return answer, {**slots, "paused":True, "boundary":"task_interruption"}


async def fraud_response(session, user_id: int) -> tuple[dict, dict]:
    event = await session.scalar(select(ContextEvent).where(
        ContextEvent.user_id == user_id, ContextEvent.event_type == "FRAUD_TRANSACTION", ContextEvent.status == "ACTIVE"
    ).order_by(ContextEvent.occurred_at.desc()).limit(1))
    accounts = list((await session.scalars(select(Account.id).where(Account.user_id == user_id))).all())
    cards = list((await session.scalars(select(Card).where(Card.account_id.in_(accounts)))).all())
    payload = event.payload if event else {}
    last4 = str(payload.get("card_last4") or "")
    matched = next((card for card in cards if card.last4 == last4), None)
    evidence = "尚未找到唯一的异常交易，请先选择卡片并核对近期流水。"
    actions = []
    if matched:
        evidence = f"检测到尾号 {last4} 在 {payload.get('city','异地')} 于 {payload.get('time','异常时段')} 发生 ¥{Decimal(str(payload.get('amount',0))):,.2f} 消费，且来自新设备。"
        actions.append((f"临时锁定尾号 {last4}", f"锁定尾号{last4}", "danger"))
    answer = {
        "type":"risk_assistance", "category":"urgent_financial", "title":"别着急，我先帮你隔离风险",
        "message":evidence + " 我不会直接挂失或报警；请先用确认卡临时锁卡，再核验是否本人交易。",
        "urgency":"urgent", "actions":_actions(*actions), "engine":"boundary",
        "trace":[{"label":"识别风险","detail":"疑似非本人卡片交易","status":"done"},{"label":"读取本地事件","detail":"交易时间、地点、设备与卡尾号","status":"done"},{"label":"最小影响处置","detail":"优先临时锁卡，写操作仍需确认","status":"ready"}],
    }
    return answer, {"boundary":"fraud_distress", "last4":last4 or None}
