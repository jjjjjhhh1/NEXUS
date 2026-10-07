"""Deterministic Chinese command adapter for the first demo; no LLM is called.

Persisted confirmation plans are the boundary between understanding and execution.
Future LLM parsers must produce the same validated plan, never call writes directly.
"""
import re
import secrets
from decimal import Decimal
from datetime import date, datetime, timedelta
from uuid import uuid4
from sqlalchemy import select, or_
from ..core.models import DemoAction, Recipient, Account, Card, Transaction, User, InvestmentOrder, Product, OrchestratedPlan, AgentTurn
from ..core.money import positive_amount
from ..core.exceptions import BusinessRuleException, NexusException, PermissionDeniedException
from ..services.card_service import CardService
from ..services.subscription_service import SubscriptionService
from ..services.product_service import ProductService
from ..services.scheduled_transfer_service import ScheduledTransferService
from ..services.aa_collection_service import AACollectionService
from .tools.banking import execute
from .security import step_up
from .security.step_up import StepUpState

HELP = "可以试试：完善我的经济资料、分析本月消费、查看定时转账、每月5号给张三转账1000元备注房租、对比理财产品、申购 NX-CASH 100元。所有资金与状态变更都需要你确认。"


def view(action):
    if action.status == "COMPLETED":
        return {"type": "receipt", "action_id": action.id, **action.result}
    if action.status == "AWAITING_STEP_UP":
        # Re-render the card with its second factor rather than a bare shell, so
        # a page refresh mid-verification still shows what is being authorised.
        return step_up_view(action, step_up.challenge_payload(
            action.id, action.kind,
            last4=action.payload.get("card_last4") or action.payload.get("last4"),
            amount=action.payload.get("amount"),
        ))
    if action.status == "SUPERSEDED":
        return {"type": "message", "action_id": action.id, "message": "此确认已被修改后的新请求替代，未执行。"}
    if action.status in {"CANCELLED", "EXPIRED"}:
        return {"type": "message", "action_id": action.id, "message": "操作已取消，未执行。" if action.status == "CANCELLED" else "确认已过期，请重新发起。"}
    return {"type": "confirmation", "action_id": action.id, "title": action.payload['title'], "detail": action.payload['detail'], "expires_at": (action.created_at + timedelta(minutes=5)).isoformat(), "kind": action.kind, **_edit_surface(action)}


#: The only kinds whose terms a customer may still change before authorising.
#: Everything else (锁卡/挂失/申购/赎回) is a single decision with no terms to
#: vary — offering an editor there would be decoration.
EDITABLE_KINDS = frozenset({"transfer", "create_scheduled_transfer"})


def _edit_surface(action_or_kind, payload: dict | None = None) -> dict:
    """What the card may offer to change, derived from the sealed plan.

    The payee is never editable. It was resolved from the user's own words and
    checked against their records; letting a pending card be re-pointed at a
    different account turns "我确认" into "我确认了另一笔", which is the one
    mistake a confirmation screen exists to prevent. The card says so instead
    of hiding the limitation.
    """
    if isinstance(action_or_kind, str):
        kind, payload = action_or_kind, payload or {}
    else:
        kind, payload = action_or_kind.kind, action_or_kind.payload or {}
    if kind not in EDITABLE_KINDS:
        return {"editable": []}
    return {
        "editable": ["amount", "purpose"] + (
            ["run_date", "recurrence", "occurrences"]
            if kind == "create_scheduled_transfer" else []
        ),
        "payee_locked": True,
        "edit_notice": "金额、执行时间、扣款次数和用途都可以在确认前改。收款人按你原话确定，需要换人请取消后重新发起。",
        "terms": {
            "amount": payload.get("amount"),
            "purpose": payload.get("purpose") or payload.get("remark"),
            "first_run_on": payload.get("first_run_on"),
            "day_of_month": payload.get("day_of_month"),
            "frequency": payload.get("frequency") or "once",
            "occurrences": payload.get("occurrences"),
        },
    }


async def resolve_action(session, identity, action_id: str, confirm: bool):
    action = await session.get(DemoAction, action_id)
    if not action or action.session_id != identity.id:
        raise PermissionDeniedException("无法访问此确认请求")
    if not confirm:
        # 取消在"确认后、核验中"也必须生效。
        #
        # 曾经这里只处理 PENDING，于是从二次核验卡点取消会原样把核验卡再画一遍——
        # 客户看着自己点的按钮毫无反应，只能回到更早那张确认卡去取消，于是以为
        # 应用坏了。钱还没动的时候，任何一处的"取消"都该真的取消。
        if action.status in {"AWAITING_STEP_UP", "PENDING"}:
            if datetime.now() - action.created_at > timedelta(minutes=5):
                action.status = "EXPIRED"
            else:
                action.status = "CANCELLED"
                action.result = None
                from .security.authorization import record_result
                await record_result(session, identity.user_id, action, "PLAN_CANCELLED")
            await session.flush()
            return view(action)
        return view(action)
    if action.status != "PENDING":
        return view(action)
    if datetime.now() - action.created_at > timedelta(minutes=5):
        action.status = "EXPIRED"
        return view(action)
    from .security.authorization import validate, record_result
    await validate(session,identity.user_id,action)
    await record_result(session,identity.user_id,action,"PLAN_CONFIRMED")
    action.result = await execute(session, identity.user_id, action)
    action.status = "COMPLETED"
    await record_result(session,identity.user_id,action,"PLAN_EXECUTED")
    await session.flush()
    return view(action)


async def edit_action(session, identity, action_id: str, changes: dict):
    """Change the terms of a plan the customer has not yet authorised.

    A confirmation screen that cannot be corrected is a receipt with a button
    on it. The last stop before money moves has to be where a misread date or a
    fat-fingered amount gets fixed, because after execution none of that is
    cheap any more.

    What this deliberately does *not* allow:

    * **Changing the payee.** It was grounded in the user's own words. Re-pointing
      a live confirmation at another account is exactly the confusion the card
      exists to prevent, and a re-read of the same sentence is free.
    * **Editing after authorisation.** Once money has moved, the audit trail
      records what was agreed; the customer cancels or corrects forward.
    * **Resetting the identity check.** An edit returns the action to
      ``PENDING`` and the customer starts the second factor again, but the
      attempt budget in :data:`step_up_states` is left alone — otherwise
      "edit, guess, edit, guess" is an unlimited passcode oracle wearing a
      customer-facing form.

    Every edit re-runs the business rules and re-seals the authorisation
    contract, and lands in the evidence chain with its before/after, so a
    changed plan is as auditable as the one it replaced.
    """
    from ..core import scheduling as schedule_module
    from .security.authorization import record_result, seal, validate

    action = await session.get(DemoAction, action_id)
    if not action or action.session_id != identity.id:
        raise PermissionDeniedException("无法访问此确认请求")
    if action.status in {"COMPLETED", "CANCELLED", "SUPERSEDED", "EXPIRED"}:
        raise BusinessRuleException("这笔操作已经结束，无法再修改。需要重新发起一笔。")
    if action.kind not in EDITABLE_KINDS:
        raise BusinessRuleException("这笔操作没有可修改的条款。")
    if datetime.now() - action.created_at > timedelta(minutes=5):
        action.status = "EXPIRED"
        await session.flush()
        raise BusinessRuleException("确认已过期，请重新发起。")

    payload = dict(action.payload or {})
    before = {key: payload.get(key) for key in ("amount", "purpose", "remark", "frequency",
                                                "first_run_on", "day_of_month", "occurrences")}

    if changes.get("amount") is not None:
        schedule_module.validate_amount(changes["amount"])
        payload["amount"] = f"{Decimal(str(changes['amount'])):.2f}"
    if changes.get("purpose") is not None:
        clean = schedule_module.validate_purpose(str(changes["purpose"]))
        if "purpose" in payload:
            payload["purpose"] = clean
        if "remark" in payload:
            payload["remark"] = clean

    if action.kind == "create_scheduled_transfer":
        current = schedule_module.resolve(
            frequency=payload.get("frequency") or "once",
            run_date=payload.get("first_run_on"),
            day_of_month=payload.get("day_of_month"),
            occurrences=payload.get("occurrences"),
        )
        # Starting from the *current* resolved schedule means the customer can
        # change one thing at a time: switch 仅此一次 → 每月 and the date they
        # already picked stays picked.
        #
        # Switching cadence also clears the inherited period count. A one-off
        # carries occurrences=1, and reusing it for 每月 would hand the customer
        # "每月，共 1 期" — the exact standing order they just said they did not
        # want, now behind a button they pressed to ask for one. Asking 每月
        # without a count means 每月, indefinitely, which is what the label says.
        cadence_changed = changes.get("recurrence") not in (None, current.frequency.lower())
        updated = schedule_module.resolve(
            frequency=changes.get("recurrence") or (current.frequency.lower()),
            run_date=changes.get("run_date") or (None if changes.get("recurrence") == "weekly" else current.first_run_on.isoformat()),
            day_of_month=(
                changes.get("day_of_month") if changes.get("day_of_month") is not None
                else (current.day_of_month or current.first_run_on.day)
            ),
            occurrences=(
                changes.get("occurrences") if changes.get("occurrences") is not None
                else (None if cadence_changed else current.occurrences)
            ),
            weekday=(None if changes.get("recurrence") else current.weekday),
        )
        payload.update({
            "frequency": updated.frequency,
            "first_run_on": updated.first_run_on.isoformat(),
            "day_of_month": updated.day_of_month or updated.first_run_on.day,
            "occurrences": updated.occurrences,
            "weekday": updated.weekday,
        })

    amount = Decimal(payload["amount"])
    if action.kind == "create_scheduled_transfer":
        updated_schedule = schedule_module.resolve(
            frequency=payload.get("frequency") or "once",
            run_date=payload.get("first_run_on"),
            day_of_month=payload.get("day_of_month"),
            occurrences=payload.get("occurrences"),
        ).with_amount(amount)
        payload["title"] = _retitle(payload, updated_schedule)
        payload["detail"] = _redetail(payload, updated_schedule)
    else:
        # An immediate transfer has no date to re-title against. Running it
        # through the schedule copy turned "确认转账给张三" into "确认 10 月 5 日
        # 向张三转账" after a plain amount edit — a card promising a future date
        # for money that leaves now.
        payload["title"] = f"确认转账给{payload.get('recipient') or '对方'}"
        payload["detail"] = _redetail_transfer(payload, amount)
    payload["editable"] = _edit_surface(action.kind, payload)["editable"]

    after = {key: payload.get(key) for key in before}
    if after == before:
        return view(action)

    action.payload = payload
    # A new payload invalidates the signed contract — deliberately. The seal is
    # re-issued below, and only after the new terms have passed the same rules
    # the original did, so the signature still means "this customer agreed to
    # *these* terms".
    action.authorization_contract = None
    action.contract_signature = None
    action.result = None
    # Back to PENDING: the identity check was for *those* numbers. Re-arming it
    # makes the customer confirm the terms they just changed, which is the whole
    # point of letting them change them.
    action.status = "PENDING"
    action.created_at = datetime.now()
    await seal(session, identity.user_id, action)
    await validate(session, identity.user_id, action)
    from ..services.evidence_service import append_evidence
    await append_evidence(session, identity.user_id, "PLAN_EDITED", {
        "action_id": action.id, "kind": action.kind,
        "before": {k: v for k, v in before.items() if v is not None},
        "after": {k: v for k, v in after.items() if v is not None},
    })
    await record_result(session, identity.user_id, action, "PLAN_CONFIRMED")
    await session.flush()
    return view(action)


def _retitle(payload: dict, sched) -> str:
    """Rebuild the headline from the terms, never from the model's prose.

    The title is the one line a customer scans, so it has to move with the
    edit: a card that still reads "确认每月 10 日转给张三" after the customer
    switched it to a single payment has told them the opposite of the truth.
    """
    who = payload.get("recipient") or "对方"
    if sched.is_one_off:
        return f"确认 {sched.first_run_on.month} 月 {sched.first_run_on.day} 日向{who}转账"
    return f"确认{sched.cadence_label()}向{who}转账"


def _redetail_transfer(payload: dict, amount: Decimal) -> str:
    """Immediate-transfer copy. Says now, because it is now."""
    purpose = payload.get("remark") or payload.get("purpose") or "转账"
    who = payload.get("recipient") or "对方"
    tail = payload.get("recipient_tail4")
    return (
        f"付款账户：日常账户\n"
        f"收款人：{who}{f'（尾号 {tail}）' if tail else ''}\n"
        f"金额：¥{amount:,.2f}\n"
        f"用途：{purpose}\n"
        f"手续费：¥0.00\n"
        f"确认后立即扣款；余额不足会告诉你差额，不会自动改金额。"
    )


def _redetail(payload: dict, sched) -> str:
    amount = Decimal(payload["amount"])
    purpose = payload.get("purpose") or payload.get("remark") or "转账"
    who = payload.get("recipient") or "对方"
    tail = payload.get("recipient_tail4")
    return (
        f"收款人：{who}{f'（尾号 {tail}）' if tail else ''}\n"
        f"{sched.amount_label()}：¥{amount:,.2f}\n"
        f"执行时间：{sched.window_label()}\n"
        f"用途：{purpose}\n"
        f"扣款范围：{sched.scope_label()}\n"
        f"确认前不会扣款；执行时若余额不足会暂停并告诉你差额，不会自动改金额。"
    )


def step_up_view(action, challenge: dict):
    """Confirmation card plus the second factor, rendered as one block.

    The summary the user already agreed to stays on screen, so the second
    factor is answering "is this still what you want to authorise" rather than
    appearing as an unexplained extra hurdle.
    """
    return {
        "type": "step_up", "action_id": action.id, "kind": action.kind,
        "title": action.payload["title"], "detail": action.payload["detail"],
        "expires_at": (action.created_at + timedelta(minutes=5)).isoformat(),
        "challenge": challenge,
        **_edit_surface(action),
    }


async def begin_step_up(session, identity, action_id: str):
    """Move a confirmed money action into the awaiting-verification state.

    Returns the challenge for the UI. Nothing is executed here — the write
    still has to survive the second factor.

    Returns ``None`` when the action is not something we can verify: already
    finished, or past its confirmation window. An expired confirmation has
    nothing left to verify, so the caller falls through and renders the plain
    "已过期" outcome rather than raising a second, confusing error.
    """
    action = await session.get(DemoAction, action_id)
    if not action or action.session_id != identity.id:
        raise PermissionDeniedException("无法访问此确认请求")
    if action.status == "AWAITING_STEP_UP":
        from .security.authorization import validate
        await validate(session,identity.user_id,action)
        return action
    if action.status != "PENDING":
        return None
    if datetime.now() - action.created_at > timedelta(minutes=5):
        action.status = "EXPIRED"
        await session.flush()
        return None
    from .security.authorization import validate, record_result
    await validate(session,identity.user_id,action)
    action.status = "AWAITING_STEP_UP"
    await record_result(session,identity.user_id,action,"PLAN_CONFIRMED")
    await session.flush()
    return action


async def complete_step_up(session, identity, action_id: str, echoes: dict, passcode: str | None):
    """Verify the second factor, then execute.

    The echo-back and the passcode are checked *before* anything is written, so
    a wrong code cannot leave a half-finished transfer behind.
    """
    action = await session.get(DemoAction, action_id)
    if not action or action.session_id != identity.id:
        raise PermissionDeniedException("无法访问此确认请求")
    if action.status != "AWAITING_STEP_UP":
        # 已经落到某个终态：重复提交只回放既有结果，绝不重跑一次写入。用户连点
        # 两下、或页面重试，都应该拿到同一张回执而不是一个错误。
        return action
    if datetime.now() - action.created_at > timedelta(minutes=5):
        action.status = "EXPIRED"
        await session.flush()
        raise BusinessRuleException("确认已过期，请重新发起")

    from .security.authorization import validate, record_result
    await validate(session,identity.user_id,action)
    state = step_up_states.setdefault(identity.id, StepUpState())
    expected = step_up.expected_values(action.id, action.payload or {})
    for name, want in expected.items():
        if not want:
            continue
        got = echoes.get(name)
        if got is None or not step_up.check_echo(action.id, str(got), want, numeric=name == "amount"):
            raise BusinessRuleException("核验内容与确认卡不一致，请重新发起")
    if step_up.requires_passcode(action.kind):
        if not state.has_passcode():
            raise BusinessRuleException("请先设置本机验证密码")
        try:
            step_up.check_passcode(state, passcode or "")
        except step_up.StepUpLocked as locked:
            action.status = "EXPIRED"
            await session.flush()
            raise BusinessRuleException(str(locked)) from None
        except ValueError as wrong:
            raise BusinessRuleException(str(wrong)) from None

    try:
        action.result = await execute(session, identity.user_id, action)
    except NexusException:
        # 身份已经核验通过，是这笔操作本身不成立（余额不足、额度超限、风控拦截
        # 等）。先把这次执行整体回滚，再把单子退回确认态：用户可以改金额或直接
        # 取消。留在"等待核验"只会把人困在一张再也过不去的卡上，而重发一遍同样
        # 的措辞也换不来结果。与不走二次核验的确认路径保持同一套语义。
        await session.rollback()
        action = await session.get(DemoAction, action_id)
        action.status = "PENDING"
        await session.commit()
        raise
    action.status = "COMPLETED"
    await record_result(session,identity.user_id,action,"PLAN_EXECUTED")
    await session.flush()
    return action


# 二次核验状态只活在内存里：不入库、不落审计、不回传客户端。
step_up_states: dict[int, StepUpState] = {}


def ensure_demo_passcode(session_id: int) -> str:
    """Give a fresh sandbox session a passcode it can actually use.

    In the local demo there is no real account holder to register one, so the
    old flow stranded every write behind "首次使用请先设置" — a control the
    user can neither satisfy nor see working. Each session gets a random code
    that the session endpoint hands to the browser as a labelled demo
    credential.

    What this deliberately does *not* do is put the code in
    ``challenge_payload``: that payload is the challenge, and shipping its own
    answer there would turn the control into theatre. The code is disclosed
    once, at session start, the way a sandbox test account's password is.
    """
    state = step_up_states.setdefault(session_id, StepUpState())
    if state.has_passcode():
        return state.demo_passcode or ""
    code = f"{secrets.randbelow(10 ** 4):04d}"
    step_up.set_passcode(state, code)
    state.demo_passcode = code
    return code


# Chinese third-person pronouns are the only way users refer to a payee that was
# mentioned in an earlier turn. Resolve them from that turn's own grounded
# recipient — never guess, and never carry a slot across different intents.
_PRONOUNS = ("他", "她", "它", "对方", "那个人")


async def _resolve_pronoun_recipient(session, identity, text: str) -> str:
    """Replace 他/她 in a payee slot with the recipient from the previous turn."""
    if not any(word in text for word in _PRONOUNS):
        return text
    if not re.search(r"(?:转账|转钱|打款|发红包)\s*(?:给|向)", text):
        return text
    latest = await session.scalar(
        select(AgentTurn)
        .where(AgentTurn.session_id == identity.id)
        .order_by(AgentTurn.id.desc())
        .limit(1)
    )
    context = latest.context if latest and isinstance(latest.context, dict) else {}
    if context.get("intent") != "transfer":
        return text
    recipient = context.get("recipient")
    if not recipient or not isinstance(recipient, str):
        return text
    for pronoun in _PRONOUNS:
        if pronoun in text:
            return text.replace(pronoun, recipient, 1)
    return text


async def respond(session, identity, message: str, request_id: str, *, command: str | None = None):
    previous = await session.scalar(select(DemoAction).where(DemoAction.session_id == identity.id, DemoAction.request_id == request_id))
    if previous:
        if previous.payload['message'] != message:
            raise BusinessRuleException("请求编号已用于另一条消息")
        return view(previous)
    text = (command if command is not None else message).strip().rstrip("。！!")
    if text in {"确认", "取消"}:
        return {"type": "message", "message": "请使用对应确认卡上的按钮，避免确认错误的操作。"}
    text = await _resolve_pronoun_recipient(session, identity, text)
    # Read commands render through the shared verified views so the model-driven
    # and model-less paths can never disagree about the same business data.
    from .analysis.read_views import aa_view, account_view, product_view, scheduled_view, subscription_view

    if text in {"余额", "查看余额", "我的余额", "查看账户", "查看我的账户", "账户概览", "查看卡片", "我的卡片", "查看订阅", "我的订阅", "查看流水"}:
        tools = ["subscriptions"] if "订阅" in text else ["cards"] if "卡片" in text else ["bills"] if "流水" in text else ["account"]
        return await account_view(session, identity.user_id, tools=tools)

    if text in {"对比理财产品", "查看理财产品", "有哪些理财产品", "看看产品", "产品对比",
                "查看投资订单", "查看我的投资", "查看持仓", "我的持仓"}:
        return await product_view(session, identity.user_id)

    if text in {"识别周期扣费", "检测周期扣费", "分析周期扣费", "识别我的订阅", "检测我的订阅"}:
        return await subscription_view(session, identity.user_id)

    if text in {"查看定时转账", "我的定时转账", "查看转账计划", "定时转账计划"}:
        return await scheduled_view(session, identity.user_id)

    if text in {"查看AA收款", "我的AA收款"}:
        return await aa_view(session, identity.user_id)

    kind, payload = None, None
    if text in {"创建客服工单", "创建人工接管工单"}:
        latest = await session.scalar(select(AgentTurn).where(
            AgentTurn.session_id == identity.id
        ).order_by(AgentTurn.id.desc()).limit(1))
        handoff = latest.context.get("handoff") if latest and isinstance(latest.context, dict) else None
        if not handoff:
            return {"type":"message", "message":"当前没有待接管的业务摘要。请先说明具体问题，或发送“转接人工客服”。"}
        mode = "HUMAN_HANDOFF" if text == "创建人工接管工单" else "SUPPORT_TICKET"
        kind = "create_support_ticket"
        payload = {
            "mode": mode, "summary": handoff["summary"], "reason": handoff.get("reason", "user_request"),
            "title": "确认提交人工接管" if mode == "HUMAN_HANDOFF" else "确认创建客服工单",
            "detail": "将脱敏业务摘要提交到人工坐席队列，确认后生成可追踪工单号。",
        }
    activate_plan = re.fullmatch(r"启用方案#(\d+)", text)
    if activate_plan:
        plan = await session.scalar(select(OrchestratedPlan).where(OrchestratedPlan.id==int(activate_plan.group(1)),OrchestratedPlan.user_id==identity.user_id,OrchestratedPlan.status=="DRAFT"))
        if not plan:
            return {"type":"message","message":"未找到可启用的方案，可能已经处理或不属于当前用户。"}
        kind="activate_orchestrated_plan"
        payload={"plan_id":plan.id,"title":"确认启用多工具方案","detail":f"启用“{plan.objective}”。确认后保存任务状态、下一步节点和审计记录；涉及资金的具体动作仍需单独确认。"}
    birthday_create = re.fullmatch(r"创建生日计划\s*日期(20\d{2}-\d{2}-\d{2})\s*预算([0-9]+(?:\.[0-9]{1,2})?)元\s*方案([ABC])", text)
    if birthday_create:
        raw_date, raw_budget, option = birthday_create.groups()
        event_date = date.fromisoformat(raw_date)
        budget = positive_amount(raw_budget)
        kind = "create_birthday_plan"
        payload = {"event_date": raw_date, "budget": f"{budget:.2f}", "option": option, "title": "确认创建生日惊喜计划", "detail": f"预留 ¥{budget:,.2f} 作为生日资金，采用方案 {option}；生日前两天生成礼品订单。确认后可用余额将转为计划预留。"}

    card_apply = re.fullmatch(r"(?:请)?申请一张(.{1,20}?)(?:信用卡|银行卡|卡)", text)
    if card_apply:
        product_name = card_apply.group(1).strip()
        kind = "apply_card"
        payload = {"product_name": product_name, "title": "确认提交办卡申请", "detail": f"申请“{product_name}”卡。确认后申请提交后进入审核，不会查询征信或上传身份材料。"}

    aa_match = re.fullmatch(r"(?:请|帮我|帮忙)?(?:发起|创建|设置)\s*(\d{1,2})\s*人\s*AA(?:收款|分摊)\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?(?:[，,\s]*(?:备注|用于)\s*(.{1,100}))?", text, re.I)
    if aa_match:
        count, raw_total, purpose = aa_match.groups();count=int(count);total=positive_amount(raw_total);purpose=(purpose or "AA收款").strip()
        if not 2 <= count <= 50: raise BusinessRuleException("AA 人数需在 2 至 50 人之间")
        per_person=(total/Decimal(count)).quantize(Decimal('0.01'))
        kind="create_aa_collection";payload={"participant_count":count,"total":f"{total:.2f}","purpose":purpose,"title":"确认发起 AA 收款","detail":f"向 {count} 人发起总额 ¥{total:,.2f} 的收款请求，每人约 ¥{per_person:,.2f}，备注“{purpose}”。不会通知真实联系人。"}

    schedule_match = re.fullmatch(r"(?:请)?(?:创建|设置|安排)?(?:一个)?每月\s*(\d{1,2})\s*[号日]\s*给(.{1,30}?)转账\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?(?:[，,\s]*(?:备注|用于|用途是?)\s*(.{1,100}))?", text)
    if schedule_match:
        raw_day, name, raw_amount, purpose = schedule_match.groups()
        day = int(raw_day)
        if not 1 <= day <= 28:
            raise BusinessRuleException("为了避免月底日期缺失，Demo 支持每月 1 至 28 日")
        amount = positive_amount(raw_amount)
        recipients = (await session.scalars(select(Recipient).where(Recipient.user_id == identity.user_id, Recipient.name == name.strip()))).all()
        if len(recipients) != 1:
            return {"type": "message", "message": "未找到唯一收款人，请使用已登记的完整姓名。"}
        today = date.today()
        if day > today.day:
            first_run = date(today.year, today.month, day)
        else:
            year = today.year + (1 if today.month == 12 else 0)
            month = 1 if today.month == 12 else today.month + 1
            first_run = date(year, month, day)
        purpose = (purpose or "定期转账").strip()
        kind = "create_scheduled_transfer"
        payload = {
            "recipient_id": recipients[0].id, "recipient": recipients[0].name,
            "amount": f"{amount:.2f}", "day_of_month": day, "first_run_on": first_run.isoformat(), "purpose": purpose,
            "title": "确认创建定时转账计划",
            "detail": f"每月 {day} 日向 {recipients[0].name} 计划转账 ¥{amount:,.2f}，用途“{purpose}”，首次日期 {first_run.isoformat()}。Demo 只创建计划，不会自动扣款。",
        }

    transfer = re.fullmatch(r"(?:请)?给(.{1,30}?)转账\s*([0-9]+(?:\.[0-9]+)?)\s*元?(?:[，,\s]*备注\s*(.{1,100}))?", text)
    is_red_packet = False
    # 口语化红包：发个红包 / 发红包 / 给X发N元红包
    if not transfer:
        transfer = re.fullmatch(r"(?:请)?(?:帮|替|为|给)?(.{1,30}?)\s*发个红包\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?(?:[，,\s]*(?:备注|用于|作为)\s*(.{1,100}))?", text)
        is_red_packet = bool(transfer)
    if not transfer:
        transfer = re.fullmatch(r"(?:请)?给(.{1,30}?)\s*发红包\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?(?:[，,\s]*(?:备注|用于|作为)\s*(.{1,100}))?", text)
        is_red_packet = bool(transfer)
    if not transfer:
        transfer = re.fullmatch(r"(?:请)?(?:向|给)?(.{1,30}?)\s*(?:发|转)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?\s*红包(?:[，,\s]*(?:备注|用于|作为)\s*(.{1,100}))?", text)
        is_red_packet = bool(transfer)
    # 动词前置："转账给张三100元" / "转账aa给张三80元" / "AA转账给他30元"
    if not transfer:
        transfer = re.fullmatch(r"(?:请)?(?:帮我|帮忙|替我|给我|我要|我想)?\s*(?:AA\s*)?(?:转账|转钱|打款|发红包)\s*(?:AA\s*)?(?:给|向)?\s*([一-龥A-Za-z]{1,20}?)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?(?:[，,\s]*(?:备注|用于|作为|用途是?)\s*(.{1,100}))?", text, re.I)
        is_red_packet = bool(transfer) and "红包" in text
        # "AA" 是分摊方式提示，不是收款人名字的一部分。
        if transfer:
            transfer = (re.sub(r"(?i)aa", "", transfer.group(1)).strip(), transfer.group(2), transfer.group(3))
    if transfer:
        identifier, raw_amount, remark = transfer if isinstance(transfer, tuple) else transfer.groups()
        amount = positive_amount(raw_amount)
        identifier = identifier.strip()
        # 去掉口语化前导："我帮"/"我替"/"我给" → 提取出"张三"
        for prefix in ("我帮", "我替", "我为", "帮我", "替我", "为给", "我给"):
            if identifier.startswith(prefix):
                identifier = identifier[len(prefix):]
                break
        # "他"/"她" 指代上一轮已登记收款人时无法唯一解析，交由上层追问
        identifier = identifier.strip()
        if not identifier:
            return {"type": "message", "message": "请告诉我要转给谁。可以使用已登记的姓名、备注名或手机号，例如张三、房东。"}
        recipients = (await session.scalars(select(Recipient).where(Recipient.user_id == identity.user_id, or_(Recipient.name == identifier, Recipient.phone == identifier, Recipient.alias == identifier)))).all()
        if len(recipients) != 1:
            return {"type": "message", "message": "未找到唯一收款人。可以使用姓名、已登记手机号或备注名，例如张三、13800001333、房东。"}
        kind = "transfer"
        remark = (remark or "").strip() or ("红包" if is_red_packet else "转账")
        payload = {
            "recipient_id": recipients[0].id,
            "recipient": recipients[0].name,
            "amount": f"{amount:.2f}",
            "remark": remark,
            "title": f"确认这笔{remark}给{recipients[0].name}" if is_red_packet else f"确认转账给{recipients[0].name}",
            "detail": f"从日常账户向 {recipients[0].name} 转账 ¥{amount:,.2f}，备注“{remark}”。手续费 ¥0.00。",
        }

    card_match = re.fullmatch(r"(?:请)?(锁定|锁卡|解锁|挂失)(?:尾号)?\s*(\d{4})(?:的)?(?:银行卡|卡片|卡)?", text)
    if card_match:
        verb, last4 = card_match.groups()
        cards = [c for c in await CardService(session).list_user_cards(identity.user_id) if c.last4 == last4]
        if len(cards) != 1:
            return {"type": "message", "message": "未找到唯一匹配的银行卡，请核对尾号。"}
        kind = {"锁定": "lock_card", "锁卡": "lock_card", "解锁": "unlock_card", "挂失": "report_lost"}[verb]
        note = "挂失后无法普通解锁，将创建补卡工单。" if kind == "report_lost" else "临时锁定后可再次解锁。"
        payload = {"card_id": cards[0].id, "last4": last4, "title": f"确认{verb}卡片", "detail": f"{cards[0].bank_name} · 尾号 {last4}。{note}"}

    limit_match = re.fullmatch(r"(?:请)?(?:把|将)?(?:尾号)?\s*(\d{4})(?:的)?(?:银行卡|卡片|卡)?(?:的)?(单笔|每日|日)(?:交易)?限额(?:调整|调|设|设置)?到?\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?", text)
    if limit_match:
        last4, limit_type, raw_amount = limit_match.groups()
        amount = positive_amount(raw_amount)
        if amount > Decimal("1000000"):
            raise BusinessRuleException("单笔限额不能超过 100 万元")
        cards = [c for c in await CardService(session).list_user_cards(identity.user_id) if c.last4 == last4]
        if len(cards) != 1:
            return {"type": "message", "message": "未找到唯一匹配的银行卡，请核对尾号。"}
        field = "single" if limit_type == "单笔" else "daily"
        old = cards[0].single_limit if field == "single" else cards[0].daily_limit
        kind = "set_card_limit"
        payload = {
            "card_id": cards[0].id, "last4": last4, "limit_type": field, "amount": f"{amount:.2f}",
            "title": "确认调整卡片限额",
            "detail": f"{cards[0].bank_name} · 尾号 {last4}，{limit_type}限额从 ¥{old:,.2f} 调整为 ¥{amount:,.2f}。变更立即生效。",
        }

    subscribe_match = re.fullmatch(r"(?:请)?申购\s*([A-Za-z0-9-]{2,20})\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?", text, re.I)
    if subscribe_match:
        code, raw_amount = subscribe_match.groups()
        amount = positive_amount(raw_amount)
        product_service = ProductService(session)
        product = await product_service.get_product_by_code(code.upper())
        if not product:
            return {"type": "message", "message": "未找到这个产品。请先发送“对比理财产品”查看产品代码。"}
        eligibility = await product_service.check_eligibility(identity.user_id, product.id, amount)
        if not eligibility["eligible"]:
            return {"type": "message", "message": f"暂不能生成申购计划：{eligibility['reason']}。"}
        kind = "subscribe_product"
        payload = {
            "product_id": product.id, "product_code": product.code, "product_name": product.name,
            "amount": f"{amount:.2f}", "title": "确认申购",
            "detail": f"申购 {product.name}（{product.code}）¥{amount:,.2f}。风险 {product.risk_level}，锁定期 {product.lock_days} 天；确认后从账户余额扣款。",
        }

    redeem_match = re.fullmatch(r"(?:请)?赎回(?:投资)?订单\s*#?\s*(\d+)(?:的)?(?:全部(?:份额)?)?", text)
    if redeem_match:
        order_id = int(redeem_match.group(1))
        order = await session.scalar(select(InvestmentOrder).where(InvestmentOrder.id == order_id, InvestmentOrder.user_id == identity.user_id))
        if not order:
            return {"type": "message", "message": "未找到属于你的这笔投资订单。请先发送“查看投资订单”。"}
        product = await session.get(Product, order.product_id)
        remaining = order.remaining_shares or Decimal("0")
        kind = "redeem_product"
        payload = {
            "order_id": order.id, "product_code": product.code, "product_name": product.name,
            "shares": f"{remaining:.4f}", "title": "确认赎回",
            "detail": f"赎回订单 #{order.id} 的全部剩余份额 {remaining:,.4f}，产品 {product.name}；锁定期和订单状态将在执行时再次校验。",
        }

    sub_match = re.fullmatch(r"(?:请)?(取消|撤销)(.{1,40}?)(订阅|代扣)", text)
    if sub_match:
        verb, merchant, target = sub_match.groups()
        rows = [r for r in await SubscriptionService(session).list_user_subscriptions(identity.user_id) if r['merchant_name'] == merchant.strip()]
        if len(rows) != 1:
            return {"type": "message", "message": "未找到唯一匹配的订阅，请先查看订阅并使用完整名称。"}
        row = rows[0]
        kind = "cancel_subscription" if target == "订阅" else "revoke_mandate"
        if kind == "revoke_mandate" and row['mandate_id'] is None:
            raise BusinessRuleException("此订阅没有代扣授权")
        payload = {"subscription_id": row['subscription_id'], "mandate_id": row['mandate_id'], "merchant": row['merchant_name'], "title": f"确认{verb}{merchant}{target}", "detail": "终止此商户合同；代扣授权需另外撤销。" if target == "订阅" else "撤销此代扣授权；商户合同不会自动终止。"}

    if payload is None:
        return {"type": "message", "message": HELP}
    payload['message'] = message
    action = DemoAction(id=str(uuid4()), session_id=identity.id, request_id=request_id, kind=kind, payload=payload, status="PENDING", created_at=datetime.now())
    session.add(action)
    await session.flush()
    from .security.authorization import seal
    await seal(session,identity.user_id,action)
    result = view(action)
    # Persist the grounded slots so a follow-up turn can refer to them with a
    # pronoun ("再给他转50"). Only slots that were actually resolved here.
    if kind == "transfer":
        result["context"] = {"intent": "transfer", "recipient": payload["recipient"], "amount": payload["amount"]}
    elif kind in {"lock_card", "unlock_card", "report_lost"}:
        result["context"] = {"intent": kind, "last4": payload["last4"]}
    return result
