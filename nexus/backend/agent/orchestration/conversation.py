"""Conversation persistence and the public Agent entry points."""
from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select

from ...core import database
from ...core.exceptions import AuthException, BusinessRuleException
from ...core.models import AgentTurn, DemoAction, DemoSession
from ..integrations import model
from ..demo_agent import view
from ..analysis.financial_analysis import ProfileInput, build_financial_analysis, save_profile
# Public provider seams retained for deterministic tests and deployments that
# replace an external data adapter without importing graph internals.
from ..integrations.external_data import fetch_fx_quote
from ..integrations.market_data import fetch_macro_indicator, fetch_sec_filings


async def lookup_identity(session, token):
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        who = await session.scalar(select(DemoSession).where(
            DemoSession.token_hash == token_hash,
            DemoSession.expires_at > datetime.now(),
        ))
        if who:
            return who
    raise AuthException("会话已过期，请刷新页面重新进入")


async def latest_turn(session, session_id):
    return await session.scalar(
        select(AgentTurn).where(AgentTurn.session_id == session_id)
        .order_by(AgentTurn.id.desc()).limit(1)
    )


async def replay(session, who, request_id, digest):
    turn = await session.scalar(select(AgentTurn).where(
        AgentTurn.session_id == who.id, AgentTurn.request_id == request_id,
    ))
    if not turn:
        return None
    if turn.input_hash != digest:
        raise BusinessRuleException("请求编号已用于另一条消息")
    action_id = turn.response.get("action_id")
    if action_id:
        action = await session.get(DemoAction, action_id)
        if action and action.session_id == who.id:
            live = view(action)
            # The live view owns the current status; the stored response owns
            # everything that explains the decision (trace, presentation,
            # compliance). Re-rendering from the action alone drops the "why",
            # and a retry is exactly when the customer most needs to see it.
            # A terminal state (cancelled / expired / receipt) is a different
            # kind of answer, so it supersedes the card rather than merging.
            if live.get("type") == turn.response.get("type"):
                live = {**turn.response, **live}
            return {**live, "engine": turn.response.get("engine", "rules")}
    return turn.response


async def save(session, who, request_id, digest, answer, context=None):
    if answer.get("action_id"):
        action = await session.get(DemoAction, answer["action_id"])
        if action and action.session_id == who.id and action.status == "PENDING":
            from ..security.authorization import seal
            await seal(session, who.user_id, action)
    session.add(AgentTurn(
        session_id=who.id, request_id=request_id, input_hash=digest,
        response=answer, context=context,
    ))
    await session.flush()
    return answer


def message_answer(message, engine="policy", **extra):
    return {"type": "message", "message": message, "engine": engine, **extra}


async def annotate(token: str | None, request_id: str, answer: dict) -> None:
    """Write the layout decision back onto the stored turn.

    The turn is persisted by the graph node that produced it, before the layout
    pass has run — so the two things a rating is read against (the question, and
    the ordering that was chosen) only exist here. Reopening the turn to merge
    them is cheaper than threading both through every save site, and it keeps
    the graph free of layout concerns.
    """
    if not isinstance(answer, dict):
        return
    from sqlalchemy import select as _select
    try:
        async with database.session_scope() as session:
            who = await lookup_identity(session, token)
            turn = await session.scalar(
                _select(AgentTurn).where(
                    AgentTurn.session_id == who.id, AgentTurn.request_id == request_id)
            )
            if turn is None:
                return
            stored = dict(turn.response or {})
            stored["question"] = answer.get("question")
            if answer.get("presentation"):
                stored["presentation"] = answer["presentation"]
            turn.response = stored
            await session.flush()
    except Exception:
        # Annotating is bookkeeping for the rating loop. A turn that cannot be
        # annotated still has a perfectly good answer; it just is not a benchmark.
        return


async def enforce(token: str | None, answer: dict) -> dict:
    """Replace an answer the reviewer refused, and record why.

    Refusal is a last resort. Both layers are tuned to fail open, and the
    deterministic layer reserves its veto for things a customer must not be
    shown at all. When it does fire, the customer gets an answer to *something*
    rather than a stack trace, and the draft never reaches the screen.

    The draft is not deleted from the turn record: an auditor needs to be able
    to reconstruct what was caught and why.
    """
    verdict = answer.get("compliance") or {}
    if verdict.get("ok", True):
        return answer
    from ...core.models import AuditLog
    from sqlalchemy import select as _select
    codes = [f.get("code", "") for f in verdict.get("findings", [])]
    draft = {k: v for k, v in answer.items() if k not in {"trace", "question"}}
    try:
        async with database.session_scope() as session:
            who = await lookup_identity(session, token)
            session.add(AuditLog(
                user_id=who.user_id, action="OUTPUT_COMPLIANCE_BLOCK",
                target_type="agent_answer", after_state="BLOCKED",
                evidence_ids=",".join(codes)[:16] or "REVIEW",
            ))
            from ...services.evidence_service import append_evidence, digest
            await append_evidence(session, who.user_id, "OUTPUT_BLOCKED", {"codes":codes,"draft_hash":digest(draft)})
            turn = await session.scalar(
                _select(AgentTurn).where(
                    AgentTurn.session_id == who.id,
                    AgentTurn.request_id == answer.get("request_id", ""),
                )
            )
            if turn is not None:
                stored = dict(turn.response or {})
                stored["blocked_draft"] = draft
                turn.response = stored
            await session.flush()
    except Exception:
        # Recording the refusal is bookkeeping. Failing to write it must not
        # turn a caught violation into a served one, so the block still applies.
        pass

    answer.clear()
    answer.update(message_answer(
        "这段回答没有通过发送前的合规检查，我不能把它发给你。"
        "可以换个问法，或者告诉我你想解决的具体问题，我重新查一次。",
        engine="compliance",
        compliance={"ok": False, "layers": verdict.get("layers", []), "findings": verdict.get("findings", [])},
    ))
    return answer


async def handle(token: str | None, message: str, request_id: str):
    """Every customer answer passes through here, so this is where layout and
    compliance are decided.

    Retrieval answers "what happened"; the layout pass answers "how should this
    read"; the compliance pass answers "may this be sent at all". Both run on
    the finished, verified answer, so neither can introduce content of its own.
    """
    from .graph import run
    from .. import presentation
    from ..security import compliance
    from ..context.memory import memory_reply
    answer = await memory_reply(token,message,request_id)
    if answer is None:
        answer = await run(token, message, request_id)
    if not isinstance(answer, dict):
        return answer
    # Carried on the answer so a rating can be read back next to the question
    # that produced it — a score with no question attached is not a benchmark.
    answer["question"] = message
    answer["request_id"] = request_id
    await presentation.apply(answer, message)
    if answer.get("boundary") == "malicious" or answer.get("reason_code") == "SECURITY_GATE":
        verdict = compliance.check_local(answer)
        answer["compliance"] = {"ok": verdict.ok, "layers": ["local"], "findings": [f.model_dump() for f in verdict.findings], "summary": verdict.summary}
    else:
        await compliance.review(answer, message)
    answer = await enforce(token, answer)
    await annotate(token, request_id, answer)
    from ..context.memory import ingest
    await ingest(token, message, request_id, answer)
    return answer


async def update_financial_profile(token: str | None, data: ProfileInput):
    """Persist declared facts, calculate locally, then add bounded prose."""
    async with database.session_scope() as session:
        who = await lookup_identity(session, token)
        await save_profile(session, who.user_id, data)
        report = await build_financial_analysis(session, who.user_id)
    blackboard = report.pop("blackboard")
    try:
        narrative = await model.personalize_financial_plan(blackboard)
        report["narrative"] = narrative.model_dump()
        report["engine"] = "model-analysis"
    except model.ModelUnavailable as error:
        report["narrative_status"] = error.code
    return report
