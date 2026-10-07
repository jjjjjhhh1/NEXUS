"""Local sandbox API. Intentionally refuses production mode and non-SQLite DBs."""
from contextlib import asynccontextmanager, suppress
import asyncio
from datetime import datetime, timedelta
import hashlib
import secrets
import time
from pathlib import Path
from typing import Literal, Optional
from uuid import UUID
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, Depends, HTTPException, Response, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .request_limits import RATE_LIMITS, RATE_BYPASS_PATHS, _rate_buckets, _rate_key, _rate_exceeded, client_address
from .schemas import LoginInput, Message, RatingInput, RiskAnswers, StepUpPasscode, ActionEdit, StepUpInput, DemoClockInput
from sqlalchemy import select, or_
from ..core import database
from ..core.config import settings
from ..core.exceptions import NexusException, BusinessRuleException, PermissionDeniedException, AuthFailedException, AuthLockedException
from ..core.models import DemoSession, DemoAction, User, Account, Card, Transaction, AuditLog, Recipient, FinancialProfile, UserMemory, AgentTurn, OperatorAccount
from ..core.logging import logger
from ..simulation.seed import seed_demo
from ..services.subscription_service import SubscriptionService
from ..services.product_service import ProductService
from ..services.scheduled_transfer_service import ScheduledTransferService
from ..services.aa_collection_service import AACollectionService
from ..agent.demo_agent import respond, resolve_action, view, begin_step_up, complete_step_up, edit_action, step_up_states
from ..agent.security import step_up
from ..agent.security.step_up import StepUpState
from ..agent.orchestration.conversation import handle, update_financial_profile
from ..agent.analysis.financial_analysis import ProfileInput, get_declared_subscriptions, get_profile, get_snapshot, intake
from ..agent.analysis.bill_analysis import build_bill_analysis
from ..agent.analysis import risk_view
from ..agent.integrations.model import is_configured
from ..agent.integrations.external_data import capability_catalog


@asynccontextmanager
async def lifespan(app):
    ready, problems = settings.public_demo_ready()
    if not ready:
        # Refuse to start rather than start insecurely. A public address reached
        # with sandbox defaults still open is the exact accident worth failing on.
        raise RuntimeError("公网演示部署配置不完整：\n- " + "\n- ".join(problems))
    if not settings.public_demo and (
        not settings.demo_mode or settings.environment == "production" or database._engine.dialect.name != "sqlite"
    ):
        raise RuntimeError("此入口仅用于本地 SQLite 演示，请勿作为生产银行服务运行")
    await database.init_db()
    async with database.session_scope() as session:
        app.state.demo_user_id = await seed_demo(session)
        if settings.public_demo:
            from ..core.models import OperatorAccount
            from sqlalchemy import func as _func
            count = await session.scalar(select(_func.count(OperatorAccount.id)))
            if not count:
                # An empty account table means the login screen nobody can pass.
                # Better to refuse than to serve a door with no key behind it.
                raise RuntimeError(
                    "公网演示模式下没有任何登录账号。请先执行：python -m nexus.scripts.create_operator"
                )
    from ..services.demo_execution import worker
    task = asyncio.create_task(worker()) if settings.scheduler_enabled and settings.environment != "test" else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await database.close_db()


app = FastAPI(title="Nexus · 金融助手", lifespan=lifespan)
# A hard-coded localhost list silently 400s every request on a public
# address. Derived from configuration instead, and left permissive locally
# so development is unaffected.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=(settings.allowed_host_list + ["localhost", "127.0.0.1", "[::1]", "testserver"]),
)


# Rate limits exist to stop enumeration and scripted red-team sweeps, not to
# pace a person thinking. The windows below are therefore set well above what a
# real customer produces in a minute, so the control is invisible in normal
# use and only engages under automation.






# Endpoints a browser may reach before it has logged in. Anything not listed
# here is behind the gate — and the list is deliberately short: a health probe
# and the three auth calls, nothing that reads or moves anything.
OPEN_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/state"})


def _bearer(request: Request) -> str | None:
    """The session token from whichever cookie carries it.

    Both names are accepted so a session minted by an older build still works
    after an upgrade — but on a public address ``_session_for`` additionally
    requires that it came from a real login.
    """
    return request.cookies.get("nexus_session") or request.cookies.get("nexus_demo")




@app.middleware("http")
async def require_login(request: Request, call_next):
    """Refuse the whole API surface to anyone without a logged-in session.

    Enforced as middleware rather than per-route so that a route added later is
    covered by default. A new endpoint that forgets ``Depends(identity)`` would
    otherwise be a public endpoint the moment it is written.
    """
    path = request.url.path
    if settings.public_demo and path.startswith("/api/") and path not in OPEN_PATHS:
        async with database.session_scope() as session:
            if await _session_for(request, session) is None:
                return JSONResponse(
                    {"code": "UNAUTHENTICATED", "message": "请先登录", "extra": {}},
                    status_code=401,
                )
    return await call_next(request)


@app.middleware("http")
async def local_request_boundary(request: Request, call_next):
    if request.url.path not in RATE_BYPASS_PATHS and _rate_exceeded(request):
        return JSONResponse(
            {"code": "RATE_LIMITED", "message": "请求太频繁，请稍等一下再试。", "extra": {}},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if request.headers.get("x-nexus-demo") != "1" or (origin and urlsplit(origin).netloc != request.headers.get("host")):
            return JSONResponse({"message": "请求来源校验失败"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.exception_handler(NexusException)
async def domain_error(request, error):
    if request.url.path.startswith("/api/actions/"):
        from ..agent.orchestration.conversation import lookup_identity
        from ..services.evidence_service import append_evidence
        try:
            async with database.session_scope() as session:
                who = await lookup_identity(session, _bearer(request))
                await append_evidence(session,who.user_id,"ACTION_REJECTED",{
                    "action_id":request.url.path.split("/")[3],"error_code":error.code,
                    "reason":str(error)[:160]})
        except NexusException:
            pass
    return JSONResponse(error.to_dict(), status_code=error.status_code)


@app.get("/api/health")
async def health():
    """Liveness for the process manager and the load balancer.

    Deliberately thin on a public address. It used to name the model provider
    and answer ``mode: local-demo`` even when serving strangers — both wrong:
    one hands a scanner the identity of your model vendor, the other makes an
    operator reading the probe think a sandbox is exposed.
    """
    if settings.public_demo:
        return {"status": "ok", "mode": "public-demo"}
    return {"status": "ok", "mode": "local-demo", "ui_contract": "memory-v1",
            "agent": "model-with-confirmation" if is_configured() else "deterministic-commands",
            "model": settings.llm_model if is_configured() else None,
            "external_data": ["frankfurter-fx", "world-bank-indicators", "sec-edgar"]}


@app.get("/api/capabilities")
async def capabilities():
    return capability_catalog(is_configured())


async def identity(request: Request, session=Depends(database.get_session)):
    resolved = await _session_for(request, session)
    if resolved is not None:
        return resolved["session"]
    raise HTTPException(401, "会话已过期，请重新登录")


@app.get("/api/memory")
async def memory_view(who=Depends(identity), session=Depends(database.get_session)):
    from ..agent.context.memory import context, active_facts, LABELS
    data = await context(session,who.user_id)
    row = await session.get(UserMemory,who.user_id)
    verified = data.get("status") != "untrusted"
    return {"enabled":settings.memory_enabled,"summary":data.get("summary", ""),
        "version":data.get("version",0),"compression_count":row.compression_count if row else 0,
        "status":"ready" if verified else "untrusted",
        "facts":[{"key":key,"label":LABELS[key],**fact} for key,fact in active_facts(row).items()] if verified else [],
        "recent_count":len(data.get("recent_dialogue",[])),"window_hours":24,
        "window_turns":settings.memory_window_turns,"window_chars":settings.memory_window_chars,
        "policy":data.get("policy", "偏好记忆不替代财务资料、正式风险测评或操作授权。")}


@app.post("/api/memory/reset")
async def reset_memory(who=Depends(identity), session=Depends(database.get_session)):
    from ..agent.context.memory import sign_memory
    from ..services.evidence_service import append_evidence
    await session.scalar(select(User).where(User.id==who.user_id).with_for_update())
    row = await session.get(UserMemory,who.user_id)
    latest = await session.scalar(select(AgentTurn).join(DemoSession).where(DemoSession.user_id==who.user_id).order_by(AgentTurn.id.desc()).limit(1))
    if row is None:
        row = UserMemory(user_id=who.user_id,version=0,compression_count=0)
        session.add(row)
    row.facts, row.recent, row.summary = {}, [], ""
    row.version += 1
    row.forget_before_turn_id = latest.id if latest else 0
    row.updated_at = datetime.now()
    sign_memory(session,row)
    await append_evidence(session,who.user_id,"MEMORY_CLEARED",{"memory_version":row.version,"forget_before_turn_id":row.forget_before_turn_id})
    return {"ok":True,"message":"偏好和近期对话记忆已清除。财务档案与业务审计记录仍按各自规则保存。"}


@app.get("/api/audit/verify")
async def verify_audit(who=Depends(identity), session=Depends(database.get_session)):
    from ..services.evidence_service import verify_chain
    return await verify_chain(session,who.user_id)




@app.post("/api/auth/login")
async def login(body: LoginInput, request: Request, response: Response,
                 session=Depends(database.get_session)):
    """Exchange a username and password for a session.

    Every rejection below returns the same message and takes the same amount of
    work. An endpoint that distinguishes "no such user" from "wrong password" is
    a username oracle, and an oracle is how a list of candidate accounts gets
    narrowed to the one worth attacking.
    """
    from ..core import auth as auth_module

    address = client_address(request)
    username = (body.username or "").strip()

    wait = auth_module.login_guard.check(address, username.lower())
    if wait:
        # Say *why* here — a locked account is not a guess, and pretending
        # otherwise just makes a legitimate user retry into a longer lock.
        raise AuthLockedException(f"登录尝试过多，请在 {wait} 秒后重试。")

    row = await session.scalar(
        select(OperatorAccount).where(OperatorAccount.username == username))

    # Always run a real bcrypt comparison, even for an unknown username.
    matched = auth_module.verify_password(
        body.password, row.password_hash if row is not None else None)
    usable = matched and row is not None and row.is_active

    if not usable:
        lock = auth_module.login_guard.record_failure(address, username.lower())
        message = auth_module.LOGIN_FAILED_MESSAGE
        if lock:
            message = f"登录失败次数过多，该账号已锁定 {min(lock, 3600)} 秒。"
        # A failed login is exactly the event an operator reviewing the log
        # needs to see, and exactly the one that must not carry the password.
        logger.warning("login_failed", extra={
            "address": address, "username": username[:64], "locked_for": lock,
        })
        raise AuthLockedException(message) if lock else AuthFailedException(message)

    if not settings.public_demo:
        # Local sandbox: a visitor cannot be expected to know a username, so
        # fall through to the self-issued demo session below.
        return await _issue_session(request, response, session, row)

    token = auth_module.new_session_token()
    session_row = DemoSession(
        token_hash=auth_module.token_fingerprint(token),
        user_id=app.state.demo_user_id,
        operator_id=row.id,
        auth_time=datetime.now(),
        expires_at=datetime.now() + timedelta(seconds=settings.session_max_age_seconds),
        ip=address,
        user_agent=(request.headers.get("user-agent") or "")[:200],
    )
    session.add(session_row)
    row.last_login_at = datetime.now()
    row.last_login_ip = address
    await session.flush()
    auth_module.login_guard.record_success(address, username.lower())
    auth_module.set_cookie(
        response, token,
        secure=settings.session_cookie_secure, max_age=settings.session_max_age_seconds,
    )
    logger.info("login_ok", extra={"address": address, "operator": row.username})
    return {"ok": True, "name": row.display_name or row.username, "mode": "public-demo"}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, session=Depends(database.get_session)):
    """Revoke the session server-side, then ask the browser to drop the cookie.

    It *revokes* rather than deletes the row, and the reason is a bug this
    method used to have. Deleting looked cleaner and was a trap: a session that
    has done anything owns transfer records and conversation turns that point
    at it, so the delete violates a foreign key, the transaction rolls back,
    and the token survives — logout returns 500 and the browser stays logged
    in. It passed its tests only because those sessions had never done
    anything.

    Rotating the stored hash destroys the token just as completely, keeps the
    history that refers to the session intact, and cannot fail halfway.
    """
    from ..core import auth as auth_module

    token = _bearer(request)
    if token:
        row = await session.scalar(
            select(DemoSession).where(
                DemoSession.token_hash == auth_module.token_fingerprint(token)))
        if row:
            row.token_hash = auth_module.token_fingerprint(auth_module.new_session_token())
            row.expires_at = datetime.now()
    auth_module.clear_cookie(response)
    return {"ok": True}


@app.get("/api/auth/state")
async def auth_state(request: Request, session=Depends(database.get_session)):
    """Whether this browser is allowed in, and as whom.

    Deliberately does not create anything. A page load must not be able to mint
    a session, or the login in front of it is decoration.
    """
    if not settings.public_demo:
        return {"required": False, "authenticated": False}
    who = await _session_for(request, session)
    return {
        "required": True,
        "authenticated": who is not None,
        "name": (who or {}).get("name"),
    }


async def _session_for(request: Request, session) -> dict | None:
    """Resolve the session cookie to a logged-in operator, or None."""
    from ..core import auth as auth_module

    token = _bearer(request)
    if not token:
        return None
    row = await session.scalar(
        select(DemoSession).where(
            DemoSession.token_hash == auth_module.token_fingerprint(token),
            DemoSession.expires_at > datetime.now(),
        ))
    if row is None:
        return None
    if settings.public_demo and row.operator_id is None:
        # A session issued before the gate existed must not survive it.
        return None
    operator = None
    if row.operator_id is not None:
        operator = await session.get(OperatorAccount, row.operator_id)
        if operator is None or not operator.is_active:
            return None
    user = await session.get(User, row.user_id)
    return {
        "session": row,
        "operator": operator,
        "name": (operator.display_name or operator.username) if operator else (user.name if user else None),
    }


async def _issue_session(request: Request, response: Response, session, operator=None):
    from ..core import auth as auth_module

    token = auth_module.new_session_token()
    max_age = settings.session_max_age_seconds
    row = DemoSession(
        token_hash=auth_module.token_fingerprint(token),
        user_id=app.state.demo_user_id,
        operator_id=operator.id if operator is not None else None,
        auth_time=datetime.now() if operator is not None else None,
        expires_at=datetime.now() + timedelta(seconds=max_age),
        ip=client_address(request),
        user_agent=(request.headers.get("user-agent") or "")[:200],
    )
    session.add(row)
    await session.flush()
    auth_module.set_cookie(
        response, token,
        secure=settings.session_cookie_secure, max_age=max_age,
    )
    return row


@app.post("/api/session")
async def enter(request: Request, response: Response, session=Depends(database.get_session)):
    # On a public address this must never be a way in. The login endpoint is
    # the only door; reaching this without a session gets you nothing.
    if settings.public_demo and await _session_for(request, session) is None:
        raise HTTPException(401, "请先登录")
    from ..core import auth as auth_module
    existing = await session.scalar(
        select(DemoSession).where(
            DemoSession.token_hash == auth_module.token_fingerprint(_bearer(request) or ""),
            DemoSession.expires_at > datetime.now()))
    if not existing:
        existing = await _issue_session(request, response, session)
    user = await session.get(User, existing.user_id)
    # 演示口令只在本地沙箱下发；公网默认也不下发——任何登进来的人都能看到执行
    # 资金操作所需的第二因子，这一层核验就形同虚设。关掉之后首次写操作会提示
    # 用户自己设一个 4 位码，设完的体验和本地完全一致。
    # NEXUS_DEMO_PASSCODE_PUBLIC=true 可以让公网行为和本地一模一样，风险自担。
    disclose = (not settings.public_demo) or settings.demo_passcode_on_public
    payload = {"name": user.name, "mode": "public-demo" if settings.public_demo else "local-demo"}
    if disclose:
        from ..agent.demo_agent import ensure_demo_passcode
        payload["demo_passcode"] = ensure_demo_passcode(existing.id)
    return payload


@app.get("/api/overview")
async def overview(who=Depends(identity), session=Depends(database.get_session)):
    user = await session.get(User, who.user_id)
    accounts = (await session.scalars(select(Account).where(Account.user_id == who.user_id))).all()
    ids = [a.id for a in accounts]
    accounts_by_id = {a.id: a for a in accounts}
    cards = (await session.scalars(select(Card).where(Card.account_id.in_(ids)).order_by(Card.id))).all()
    txs = (await session.scalars(select(Transaction).where(or_(Transaction.from_account_id.in_(ids), Transaction.to_account_id.in_(ids)), Transaction.idempotency_key.is_not(None)).order_by(Transaction.id.desc()).limit(12))).all()
    audits = (await session.scalars(select(AuditLog).where(AuditLog.user_id == who.user_id).order_by(AuditLog.id.desc()).limit(12))).all()
    actions = (await session.scalars(select(DemoAction).where(DemoAction.session_id == who.id).order_by(DemoAction.created_at.desc()).limit(30))).all()
    for action in actions:
        if action.status == "PENDING" and datetime.now() - action.created_at > timedelta(minutes=5):
            action.status = "EXPIRED"
    recipients = (await session.scalars(select(Recipient).where(Recipient.user_id == who.user_id))).all()
    profile = await session.scalar(select(FinancialProfile).where(FinancialProfile.user_id == who.user_id))
    return {
        "name": user.name,
        "accounts": [{"id": a.id, "balance": str(a.balance), "available": str(a.available_balance), "reserved": str(a.reserved_balance)} for a in accounts],
        "cards": [{"id": c.id, "name": c.bank_name, "last4": c.last4, "status": c.status, "available": str(accounts_by_id[c.account_id].available_balance), "shared_account": sum(other.account_id == c.account_id for other in cards) > 1, "single_limit": str(c.single_limit) if c.single_limit is not None else None, "daily_limit": str(c.daily_limit) if c.daily_limit is not None else None} for c in cards],
        "subscriptions": await SubscriptionService(session).list_user_subscriptions(who.user_id),
        "recipients": [{"name": r.name} for r in recipients],
        "investment_orders": await ProductService(session).list_user_orders(who.user_id),
        "scheduled_transfers": await ScheduledTransferService(session).list_user_plans(who.user_id),
        "aa_collections": await AACollectionService(session).list_user_collections(who.user_id),
        "goal": None if not profile else {
            "name": profile.goal_name,
            "amount": str(profile.goal_amount),
            "saved": str(profile.goal_saved),
            "horizon_months": profile.horizon_months,
            "progress_pct": min(100, round(float(profile.goal_saved / profile.goal_amount * 100), 1)) if profile.goal_amount else 0,
        },
        "transactions": [{"id": t.id, "amount": str(t.amount), "status": t.status, "direction": "out" if t.from_account_id in ids else "in", "time": t.created_at.isoformat(), "remark": t.remark} for t in txs],
        "audit": [{"id": a.id, "action": a.action, "target": a.target_type, "time": a.created_at.isoformat()} for a in audits],
        "actions": [view(a) for a in reversed(actions)],
    }


@app.get("/api/bill-analysis")
async def bill_analysis(period: str = "month", who=Depends(identity), session=Depends(database.get_session)):
    if period not in {"month", "year"}:
        raise HTTPException(422, "账单周期仅支持 month 或 year")
    return await build_bill_analysis(session, who.user_id, period)




@app.post("/api/messages")
async def message(body: Message, request: Request):
    answer = await handle(_bearer(request), body.message, str(body.request_id))
    # Echoed so the renderer can attach a rating to exactly the turn the customer
    # is looking at. Without it a score cannot be traced back to its question.
    if isinstance(answer, dict):
        answer["request_id"] = str(body.request_id)
    return answer




@app.post("/api/ratings")
async def rate(body: RatingInput, who=Depends(identity), session=Depends(database.get_session)):
    """Record how this answer landed, together with the layout that produced it.

    Re-rating the same turn overwrites the previous score: the number is "how
    good is this answer right now", not a tally.
    """
    from ..core.models import AgentTurn, AnswerRating
    from sqlalchemy import select as _select
    turn = await session.scalar(
        _select(AgentTurn).where(AgentTurn.session_id == who.id, AgentTurn.request_id == body.request_id)
    )
    if not turn:
        raise HTTPException(404, "没有找到这一轮对话")
    layout = (turn.response or {}).get("presentation") or {}
    existing = await session.scalar(
        _select(AnswerRating).where(
            AnswerRating.session_id == who.id, AnswerRating.request_id == body.request_id)
    )
    if existing:
        existing.stars = body.stars
        existing.answer_type = (turn.response or {}).get("type", "message")
        existing.layout = layout
    else:
        session.add(AnswerRating(
            session_id=who.id, user_id=who.user_id, request_id=body.request_id,
            answer_type=(turn.response or {}).get("type", "message"),
            stars=body.stars, layout=layout,
        ))
    await session.flush()
    return {"ok": True, "stars": body.stars, "average": await average_rating(session, who.user_id)}


async def average_rating(session, user_id: int) -> float | None:
    from ..core.models import AnswerRating
    from sqlalchemy import func, select as _select
    return await session.scalar(
        _select(func.avg(AnswerRating.stars)).where(AnswerRating.user_id == user_id)
    )


@app.get("/api/ratings")
async def list_ratings(min_stars: int = Query(5, ge=1, le=5), who=Depends(identity),
                       session=Depends(database.get_session)):
    """The benchmark set: questions whose answers were judged worth keeping.

    Joined with the turn that produced them so each entry is reproducible — the
    question, the layout and the score travel together.
    """
    from ..core.models import AgentTurn, AnswerRating
    from sqlalchemy import select as _select
    rows = (await session.scalars(
        _select(AnswerRating)
        .where(AnswerRating.user_id == who.user_id, AnswerRating.stars >= min_stars)
        .order_by(AnswerRating.stars.desc(), AnswerRating.id.desc())
    )).all()
    out = []
    for row in rows:
        turn = await session.scalar(
            _select(AgentTurn).where(AgentTurn.session_id == row.session_id, AgentTurn.request_id == row.request_id)
        )
        if not turn:
            continue
        out.append({
            "request_id": row.request_id, "stars": row.stars, "answer_type": row.answer_type,
            "question": (turn.response or {}).get("question") or None,
            "layout": row.layout or {},
            "created_at": row.created_at.isoformat(),
        })
    return {"count": len(out), "average": await average_rating(session, who.user_id), "items": out}


@app.post("/api/financial-profile")
async def financial_profile(body: ProfileInput, request: Request):
    return await update_financial_profile(_bearer(request), body)




@app.get("/api/risk-assessment")
async def read_risk_assessment(who=Depends(identity), session=Depends(database.get_session)):
    record = await risk_view.current(session, who.user_id)
    if record:
        return risk_view.report(record)
    intake = await risk_view.questionnaire(session, who.user_id)
    return intake if intake is not None else risk_view.needs_profile_message()


@app.post("/api/risk-assessment")
async def write_risk_assessment(body: RiskAnswers, who=Depends(identity), session=Depends(database.get_session)):
    try:
        return await risk_view.submit(session, who.user_id, body.model_dump())
    except ValueError as error:
        # A half-finished questionnaire is a question, not a failure: the
        # customer is told which one is missing and nothing is recorded.
        raise HTTPException(400, str(error)) from None


@app.get("/api/financial-profile")
async def read_financial_profile(who=Depends(identity), session=Depends(database.get_session)):
    return intake(await get_profile(session, who.user_id), await get_snapshot(session, who.user_id), await get_declared_subscriptions(session, who.user_id))






@app.patch("/api/actions/{action_id}")
async def amend_action(action_id: UUID, body: ActionEdit, who=Depends(identity),
                       session=Depends(database.get_session)):
    """Change a pending plan's terms before it is authorised.

    A PATCH rather than a second POST confirm: it is not a second decision, it
    is the correction that has to happen *before* the first one. Business rules
    are re-run and the authorisation contract re-sealed inside ``edit_action``,
    and the action returns to ``PENDING`` so the identity check runs against the
    numbers the customer will actually be shown.
    """
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    # edit_action already returns the card view; wrapping it again would try to
    # read .status off a dict.
    return await edit_action(session, who, str(action_id), changes)




@app.post("/api/actions/{action_id}/step-up")
async def verify_action(action_id: UUID, body: StepUpInput, who=Depends(identity), session=Depends(database.get_session)):
    """Second factor for a money-moving write.

    Deliberately a separate endpoint from ``/confirm``: the first records that
    the user agreed to the summary, the second records that they are who they
    say they are. Collapsing them would make the identity check just a second
    click.

    Declared before the generic ``/{decision}`` route so the literal path is
    matched as a step-up and never as a decision verb.

    No local error handling on purpose: ``complete_step_up`` already rolls back a
    write that fails, and every remaining failure is a ``NexusException`` whose
    class carries the right status code. Re-wrapping them here would only hide
    风控拦截 (a 200 business result) behind a blanket 400.
    """
    action = await complete_step_up(session, who, str(action_id), body.echoes, body.passcode)
    return view(action)


@app.post("/api/actions/{action_id}/{decision}")
async def decide(action_id: UUID, decision: str, who=Depends(identity), session=Depends(database.get_session)):
    if decision not in {"confirm", "cancel"}:
        raise HTTPException(404, "未知操作")
    if decision == "confirm":
        action = await session.get(DemoAction, str(action_id))
        if not action or action.session_id != who.id:
            raise HTTPException(403, "无法访问此确认请求")
        if action.status == "PENDING" and step_up.requires_passcode(action.kind):
            # 资金类写操作不能只靠"确认"这一个动作就执行：读完摘要不等于
            # 证明是本人在操作。所以这里只把它推进到等待核验，不落地任何资金。
            #
            # 只针对仍在等待的单：重复点"确认"应该拿回原来的回执，而不是把一
            # 笔已完成的转账重新变成一道待核验的题目；过期单也没有可核验的东西。
            awaiting = await begin_step_up(session, who, str(action_id))
            if awaiting is not None:
                return view(awaiting)
    return await resolve_action(session, who, str(action_id), decision == "confirm")


@app.post("/api/step-up/passcode")
async def set_passcode(body: StepUpPasscode, who=Depends(identity)):
    """Set or replace this browser session's verification passcode.

    The plaintext is hashed immediately and never stored, logged or returned.
    """
    try:
        step_up.set_passcode(step_up_states.setdefault(who.id, StepUpState()), body.passcode)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {"ok": True, "length": step_up.PASSCODE_LENGTH, "message": "验证密码已在本机生效。"}


FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/login", include_in_schema=False)
async def login_page():
    """登录是一个独立页面，不是盖在主界面上的蒙版。

    之前登录做在 app.js 里，作为 body 上的一个 fixed 浮层：登录成功后浮层被
    remove 掉，但 body 上的 login-locked 类忘了摘，前端样式里给 .app-shell 的
    blur(6px) 就一直生效——登录之后看到的整个主界面是虚的，而且浮层里那份
    表单和主界面共用一个 JS 运行时，任何一次 401 都会在已经渲染好的界面上
    再盖一层。拆成两个页面之后，这两种状态都不存在了：登录页里没有主界面，
    登录成功整页 replace 过去。
    """
    return FileResponse(FRONTEND / "login.html")




@app.post("/api/demo/run-due")
async def demo_run_due(data: DemoClockInput, request: Request):
    from ..agent.orchestration.conversation import lookup_identity
    async with database.session_scope() as session:
        who = await lookup_identity(session, _bearer(request))
        user_id = who.user_id
    """One-shot simulated clock; affects only this authenticated demo user."""
    from ..services.demo_execution import run_due
    # UTC+8 is the schedule convention; never compare aware/naive values.
    from datetime import timezone
    at = data.now.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None) if data.now.tzinfo else data.now
    return await run_due(at, user_id)


@app.post("/api/scheduled-transfers/{plan_id}/pause")
async def pause_schedule(plan_id: int, who=Depends(identity), session=Depends(database.get_session)):
    await ScheduledTransferService(session).pause(who.user_id, plan_id)
    return {"status": "PAUSED", "plan_id": plan_id}
