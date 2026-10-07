"""Turn an understood request into a confirmation plan.

The model names a scene and an operation; this module turns that into a
DemoAction with a human-readable title and detail. It owns two guarantees:

  1. Every write goes through here, so no write can bypass the confirmation card.
  2. The card text is generated locally from verified records, never from model
     prose — a model cannot invent a payee name or an amount on a confirmation
     screen.

There is deliberately no keyword matching in this file. "锁卡 / 挂失 / 撤销代扣"
are decisions the model made in understanding.py; re-deriving them here from the
user's sentence would reintroduce exactly the guessing this architecture removes.
Grounding is enforced by the caller (grounder.ground_understanding) beforehand.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select

from ...core.exceptions import BusinessRuleException
from ...core.models import InvestmentOrder, Product, Recipient
from ...core.money import positive_amount
from ...services.subscription_service import SubscriptionService
from ..contracts.understanding import Understanding, resolve_operation


class Unresolvable(Exception):
    """The user is missing a slot or named something that does not exist.
    Carries the exact question to ask — never a guess.

    Str() must stay a plain sentence: this message is shown to the user verbatim,
    so it must never leak a traceback or an internal identifier.
    """

    def __str__(self) -> str:
        return self.args[0] if self.args else "请补充必要信息。"


class AlreadyDone(Exception):
    """The requested change is already in the target state.

    Offering a confirmation card for work that is finished would train the user
    to click "confirm" without reading, which is exactly the habit the card
    exists to prevent. The answer is a plain statement of the current state.
    """

    def __str__(self) -> str:
        return self.args[0] if self.args else "这个操作已经是当前状态了。"


def _one(items, what: str):
    if len(items) != 1:
        raise Unresolvable(f"没有找到唯一的{what}，请核对名称或编号。")
    return items[0]


# Contract and mandate states as the user reads them. A confirmation screen
# must never show a raw enum to someone deciding whether to authorise money.
CONTRACT_LABELS = {
    "ACTIVE": "正常",
    "TERMINATED": "已终止",
    "CANCELLED": "已取消",
    "REVOKED": "已撤销",
    "EXPIRED": "已过期",
}


def _required_amount(understanding: Understanding, subject: str) -> Decimal:
    if understanding.amount is None:
        raise Unresolvable(f"{subject}多少？需要一个明确的金额。")
    return positive_amount(understanding.amount)


async def _resolve_recipient(session, user_id: int, understanding: Understanding) -> Recipient:
    """Match a payee by name, phone, alias, or a handle the user used colloquially.

    An unresolvable handle must become a question, not an assumption: guessing a
    payee is the single most damaging failure this agent could have.

    Three passes, each stricter than the last being *broader* only in how it
    reads the registry, never in how sure it is allowed to be:

    1. exact name, phone or alias — "张三" / "138…"
    2. substring either way — "张三丰" → 张三, "三张" → 张三
    3. a distinctive character in common — "我妈" → 妈妈, "王阿姨" → 老王

    Pass 3 exists because a customer introducing their own mother as 我妈 is not
    being vague, and making them re-read the registry to find the row labelled
    妈妈 is a failure of the registry, not of the customer. It only ever resolves
    when exactly one registered payee could match; two candidates and it asks,
    because at that point the cost of a wrong payee is the customer's money.
    """
    key = (understanding.recipient or understanding.account_handle or "").strip()
    known = (await session.scalars(
        select(Recipient.name).where(Recipient.user_id == user_id)
    )).all()
    hint = "、".join(known[:4]) if known else "（当前没有已登记收款人）"
    if not key:
        raise Unresolvable(f"要转给谁？当前已登记：{hint}。")

    rows = (await session.scalars(
        select(Recipient).where(Recipient.user_id == user_id)
    )).all()

    def surfaces(row: Recipient) -> list[str]:
        return [text for text in (row.name, row.phone, row.alias) if text]

    exact = [row for row in rows if key in surfaces(row)]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        # A single character is not a payee. "给张转账" resolving to 张三 because
        # one 张 happens to be on file is the same class of mistake as guessing a
        # name outright — it just happens to be more likely to be right. The
        # fuzzy passes below are for handles like 我妈 or 王阿姨, where the
        # customer named a relationship rather than a registry label.
        if len(key) < 2:
            raise Unresolvable(f"没有找到叫‘{key}’的收款人。当前已登记：{hint}。要转给谁？")
        partial = [
            row for row in rows
            if any(key in text or text in key for text in surfaces(row))
        ]
        if len(partial) == 1:
            return partial[0]
        if not partial and len(key) >= 2:
            # Shared-character resolution, unique candidate only.
            shared = [
                row for row in rows
                if set(key) & {c for text in surfaces(row) for c in text}
            ]
            if len(shared) == 1:
                return shared[0]
    raise Unresolvable(f"没有找到叫‘{key}’的收款人。当前已登记：{hint}。要转给谁？")


async def _user_cards(session, user_id: int):
    from ...services.card_service import CardService
    return await CardService(session).list_user_cards(user_id)


async def _resolve_card(session, user_id: int, understanding: Understanding):
    last4 = (understanding.last4 or "").strip()
    if not last4:
        raise Unresolvable("请告诉我要操作哪张卡，提供尾号四位即可。")
    cards = [c for c in await _user_cards(session, user_id) if c.last4 == last4]
    return _one(cards, f"尾号 {last4} 的银行卡")


async def build_plan(
    session, user_id: int, understanding: Understanding, message: str,
    *, demo_session_id: int | None = None,
) -> dict:
    """Return the DemoAction payload for a write scene.

    Raises ``Unresolvable`` when a slot cannot be verified against the user's own
    records, and ``BusinessRuleException`` when a value is present but invalid
    (the caller turns that into a 400, because retrying the same words cannot help).

    ``demo_session_id`` scopes conversation-scoped context (the handoff summary);
    operations that do not need it ignore it.
    """
    operation = resolve_operation(understanding)
    if operation is None:
        raise Unresolvable("请说明你要执行的具体操作，例如锁定卡片、调整限额或申请新卡。")

    if operation == "transfer":
        return await _transfer_plan(session, user_id, understanding, message)
    if operation == "create_scheduled_transfer":
        # No date at all means "now", whatever the model called it. Rendering
        # that as a dated plan put "确认 10 月 5 日向张三转账" on a card that
        # debited that same afternoon — a headline that reads as a future
        # commitment and behaves as a present one. A date that *is* given
        # stays a dated plan, including today's: the user said when.
        if not _has_timing_intent(understanding):
            return await _transfer_plan(session, user_id, understanding, message)
        return await _scheduled_plan(session, user_id, understanding, message)
    if operation == "create_aa_collection":
        return _aa_plan(understanding, message)
    if operation == "apply_card":
        return _apply_card_plan(understanding)
    if operation in {"lock_card", "unlock_card", "report_lost", "set_card_limit"}:
        return await _card_plan(session, user_id, understanding, operation)
    if operation in {"cancel_subscription", "revoke_mandate"}:
        return await _subscription_plan(session, user_id, understanding, operation)
    if operation in {"subscribe_product", "redeem_product"}:
        return await _product_plan(session, user_id, understanding, operation)
    if operation == "create_birthday_plan":
        return await _birthday_plan(session, understanding)
    if operation in {"create_support_ticket", "create_human_handoff"}:
        if demo_session_id is None:
            raise Unresolvable("我这边还没有可以转接的上下文。请先说说你想解决什么。")
        return await _handoff_plan(session, demo_session_id, understanding)
    raise Unresolvable("这个操作我还没支持，换一种说法或直接描述你要办的事。")


# ---------------------------------------------------------------------------
# transfer / scheduled transfer / AA
# ---------------------------------------------------------------------------
async def _transfer_plan(session, user_id: int, understanding: Understanding, message: str) -> dict:
    payee = await _resolve_recipient(session, user_id, understanding)
    if understanding.amount is None:
        raise Unresolvable(f"要给{payee.name}转多少？")
    amount = positive_amount(understanding.amount)
    remark = _remark(message, understanding) or "转账"

    # The user named a day, so this is a booked payment — not an instruction to
    # move the money now. Routing it to an immediate transfer would pull ¥30,000
    # out five days before the birthday it was meant for, and no confirmation
    # card would have told them, because the card would have said "立即". A
    # missing 定时 in the sentence is not permission to be early.
    schedule = _requested_schedule(understanding)
    # A date the customer named is a booked payment even when it is today —
    # "每月 5 号" is a standing order whether or not the 5th has arrived.
    # What must never happen is executing a *future* date right now.
    if _has_timing_intent(understanding) and schedule.first_run_on > date.today():
        return await _scheduled_plan(
            session, user_id, understanding, message, payee=payee,
            amount=amount, schedule=schedule,
        )

    return {
        "kind": "transfer",
        "payload": {
            "recipient_id": payee.id,
            "recipient": payee.name,
            "recipient_tail4": payee.phone[-4:] if payee.phone else None,
            "amount": f"{amount:.2f}",
            "remark": remark,
            "title": f"确认转账给{payee.name}",
            "detail": (
                f"付款账户：日常账户\n"
                f"收款人：{payee.name}（尾号 {payee.phone[-4:] if payee.phone else '—'}）\n"
                f"金额：¥{amount:,.2f}\n"
                f"用途：{remark}\n"
                f"手续费：¥0.00"
            ),
            **_editable_metadata(amount, None),
        },
    }


def _requested_schedule(understanding: Understanding):
    """The timing the user asked for, before payee and amount are resolved.

    Timing is resolved separately from the rest of the plan so that the
    one-off / standing-order question gets asked before anything is written,
    and so a request with no usable timing falls back to a single payment today
    rather than failing the whole turn.
    """
    from ...core import scheduling as schedule_module

    try:
        return schedule_module.resolve(
            frequency=(schedule_module.FROM_UNDERSTANDING.get(understanding.recurrence or "once")),
            run_date=understanding.run_date,
            day_of_month=understanding.day_of_month,
            occurrences=understanding.occurrences,
            weekday=understanding.weekday,
        )
    except BusinessRuleException:
        raise
    except Exception:
        return schedule_module.resolve(frequency="once")


def _has_timing_intent(understanding: Understanding) -> bool:
    """Did the customer actually say *when*?

    This — not "does the resolved date happen to be today" — is what separates
    "现在转 1000" from "每月 5 号转 1000" on the 5th. The second one resolves to
    a first run of today, and a rule keyed on the resolved date quietly turned a
    standing order into an immediate payment, losing the standing order entirely
    on the one day of the month it would have fired.
    """
    return bool(
        (understanding.run_date or "").strip()
        or understanding.day_of_month
        or understanding.weekday is not None
    )


def _editable_metadata(amount: Decimal, sched) -> dict:
    """Tell the client which fields a customer may still change.

    The payee is deliberately absent. It was resolved from the user's own
    words and grounded against their records; letting a pending card re-point
    at a different account is the one edit that turns "确认" into "确认了另一笔".
    A customer who wants a different payee cancels and re-issues, and the card
    says so in words.
    """
    fields = ["amount", "purpose"]
    if sched is not None:
        fields += ["run_date", "recurrence", "occurrences"]
    return {"editable": fields, "payee_locked": True, "schedule": sched.as_payload() if sched else None}


async def _scheduled_plan(session, user_id: int, understanding: Understanding, message: str,
                          *, payee=None, amount=None, schedule=None) -> dict:
    payee = payee or await _resolve_recipient(session, user_id, understanding)
    if amount is None:
        if understanding.amount is None:
            raise Unresolvable(f"要转给{payee.name}多少钱？")
        amount = positive_amount(understanding.amount)
    if schedule is None:
        schedule = _requested_schedule(understanding)
    schedule = schedule.with_amount(amount)
    purpose = _remark(message, understanding) or "定时转账"

    # A single payment gets "金额" and "共 1 笔"; a standing order gets
    # "每期金额" and the total it will take. Writing "每期金额" on a one-off is
    # how a one-off read as a standing order before anyone pressed anything.
    title = (
        f"确认{'今天' if schedule.first_run_on == date.today() else f'{schedule.first_run_on.month} 月 {schedule.first_run_on.day} 日'}向{payee.name}转账"
        if schedule.is_one_off else
        f"确认{schedule.cadence_label()}向{payee.name}转账"
    )
    return {
        "kind": "create_scheduled_transfer",
        "payload": {
            "recipient_id": payee.id,
            "recipient": payee.name,
            "recipient_tail4": payee.phone[-4:] if payee.phone else None,
            "amount": f"{amount:.2f}",
            "day_of_month": schedule.day_of_month or schedule.first_run_on.day,
            "first_run_on": schedule.first_run_on.isoformat(),
            "frequency": schedule.frequency,
            "occurrences": schedule.occurrences,
            "weekday": schedule.weekday,
            "purpose": purpose,
            "title": title,
            "detail": (
                f"收款人：{payee.name}（尾号 {payee.phone[-4:] if payee.phone else '—'}）\n"
                f"{schedule.amount_label()}：¥{amount:,.2f}\n"
                f"执行时间：{schedule.window_label()}\n"
                f"用途：{purpose}\n"
                f"扣款范围：{schedule.scope_label()}\n"
                f"确认前不会扣款；执行时若余额不足会暂停并告诉你差额，不会自动改金额。"
            ),
            **_editable_metadata(amount, schedule),
        },
    }


def _aa_plan(understanding: Understanding, message: str) -> dict:
    count = int(understanding.participant_count or 0)
    if not 2 <= count <= 50:
        raise BusinessRuleException("AA 人数需在 2 至 50 人之间")
    total = _required_amount(understanding, "这次 AA 收款总额")
    per = (total / Decimal(count)).quantize(Decimal("0.01"))
    purpose = _remark(message) or "AA 收款"
    return {
        "kind": "create_aa_collection",
        "payload": {
            "participant_count": count,
            "total": f"{total:.2f}",
            "purpose": purpose,
            "title": f"确认发起 {count} 人 AA 收款",
            "detail": (
                f"参与人数：{count} 人\n"
                f"总额：¥{total:,.2f}\n"
                f"每人应收：约 ¥{per:,.2f}\n"
                f"用途：{purpose}\n"
                f"创建后可逐笔标记收款状态，不会自动向任何人发起扣款。"
            ),
        },
    }


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------
async def _card_plan(session, user_id: int, understanding: Understanding, operation: str) -> dict:
    card = await _resolve_card(session, user_id, understanding)
    if operation == "report_lost":
        return {
            "kind": "report_lost",
            "payload": {
                "card_id": card.id, "last4": card.last4,
                "title": f"确认挂失尾号 {card.last4}",
                "detail": (
                    f"卡片：{card.bank_name} · 尾号 {card.last4}\n"
                    f"当前状态：{card.status}\n"
                    f"操作：标记为挂失并建立补卡工单\n"
                    f"挂失后无法通过普通解锁恢复，需要重新申领。"
                ),
            },
        }
    if operation == "unlock_card":
        return {
            "kind": "unlock_card",
            "payload": {
                "card_id": card.id, "last4": card.last4,
                "title": f"确认解锁尾号 {card.last4}",
                "detail": (
                    f"卡片：{card.bank_name} · 尾号 {card.last4}\n"
                    f"当前状态：{card.status}\n"
                    f"操作：解除临时锁定，恢复正常使用。"
                ),
            },
        }
    if operation == "set_card_limit":
        field = understanding.limit_type or "single"
        limit = _required_amount(understanding, "要把限额调整到")
        new = positive_amount(limit)
        if new > Decimal("1000000"):
            raise BusinessRuleException("限额最高支持 100 万元")
        old = card.daily_limit if field == "daily" else card.single_limit
        label = "每日" if field == "daily" else "单笔"
        return {
            "kind": "set_card_limit",
            "payload": {
                "card_id": card.id, "last4": card.last4, "limit_type": field,
                # `amount` is the executor's field name; old/new are shown to the user.
                "amount": f"{new:.2f}",
                "old_value": f"{old:.2f}", "new_value": f"{new:.2f}",
                "title": f"确认调整尾号 {card.last4} 的{label}限额",
                "detail": (
                    f"卡片：{card.bank_name} · 尾号 {card.last4}\n"
                    f"当前{label}限额：¥{old:,.2f}\n"
                    f"调整为：¥{new:,.2f}"
                ),
            },
        }
    return {
        "kind": "lock_card",
        "payload": {
            "card_id": card.id, "last4": card.last4,
            "title": f"确认锁定尾号 {card.last4}",
            "detail": (
                f"卡片：{card.bank_name} · 尾号 {card.last4}\n"
                f"当前状态：{card.status}\n"
                f"操作：临时锁定，可随时解锁。"
            ),
        },
    }


def _apply_card_plan(understanding: Understanding) -> dict:
    # A new card has no last4 yet, so this is the one card operation that needs
    # no existing card record — only the kind of card the user asked for.
    label = "信用卡" if understanding.card_type in (None, "CREDIT") else "借记卡"
    return {
        "kind": "apply_card",
        "payload": {
            "card_type": understanding.card_type or "CREDIT",
            "product_name": label,
            "title": f"确认申请一张{label}",
            "detail": (
                f"申请类型：{label}\n"
                f"提交后会建立申请记录，可随时查询进度；不会查询征信或开立真实银行卡。"
            ),
        },
    }


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------
async def _subscription_plan(session, user_id: int, understanding: Understanding, operation: str) -> dict:
    merchant = (understanding.merchant or "").strip()
    connected = await SubscriptionService(session).list_user_subscriptions(user_id)
    active = [row["merchant_name"] for row in connected if row.get("contract_status") == "ACTIVE"]
    if not merchant:
        # Name the user's real subscriptions: the side panel already shows them,
        # so a generic "请提供商户名称" makes the user repeat what they can see.
        listed = "、".join(active or [row["merchant_name"] for row in connected]) or "（当前没有已连接的订阅）"
        raise Unresolvable(f"要取消哪一项订阅？当前已连接：{listed}。")
    rows = [r for r in connected if r["merchant_name"] == merchant]
    row = _one(rows, f"订阅「{merchant}」")
    contract_state = row.get("contract_status")
    mandate_state = row.get("mandate_status")
    if operation == "revoke_mandate":
        if row.get("mandate_id") is None:
            raise Unresolvable(f"「{merchant}」没有单独的代扣授权，只需取消订阅合同。")
        if mandate_state == "REVOKED":
            raise AlreadyDone(f"「{merchant}」的代扣授权已经撤销过了，不会再从你的账户扣这笔钱。")
        return {
            "kind": "revoke_mandate",
            "payload": {
                "subscription_id": row["subscription_id"],
                "mandate_id": row["mandate_id"],
                "merchant": row["merchant_name"],
                "title": f"确认撤销{merchant}的代扣授权",
                "detail": (
                    f"商户：{merchant}\n"
                    f"当前代扣状态：{CONTRACT_LABELS.get(mandate_state, mandate_state)}\n"
                    f"操作：停止该商户的自动扣款能力\n"
                    f"订阅合同：不受影响，仍会继续计费\n"
                    f"如需同时终止合同，请另外确认「取消{merchant}订阅」。"
                ),
            },
        }
    if contract_state in {"TERMINATED", "CANCELLED"}:
        raise AlreadyDone(
            f"「{merchant}」的订阅合同已经是「{CONTRACT_LABELS.get(contract_state, contract_state)}」了，不用重复取消。"
            + ("代扣授权还在，需要的话我可以单独帮你停掉。" if mandate_state == "ACTIVE" else "")
        )
    return {
        "kind": "cancel_subscription",
        "payload": {
            "subscription_id": row["subscription_id"],
            "mandate_id": row["mandate_id"],
            "merchant": row["merchant_name"],
            "title": f"确认取消{merchant}订阅",
            "detail": (
                f"商户：{merchant}\n"
                f"月费：¥{Decimal(str(row['amount'])):,.2f}\n"
                f"当前合同状态：{CONTRACT_LABELS.get(contract_state, contract_state)}\n"
                f"操作：终止订阅合同\n"
                f"代扣授权：{'仍然有效。如需同时停止扣款，需另外确认撤销代扣。' if mandate_state == 'ACTIVE' else '已撤销，不会再扣款。'}"
            ),
        },
    }


# ---------------------------------------------------------------------------
# investment products
# ---------------------------------------------------------------------------
async def _product_plan(session, user_id: int, understanding: Understanding, operation: str) -> dict:
    if operation == "redeem_product":
        order_id = understanding.order_id
        order = await session.scalar(
            select(InvestmentOrder).where(
                InvestmentOrder.id == order_id, InvestmentOrder.user_id == user_id
            )
        )
        if not order:
            raise Unresolvable(f"没有找到属于你的订单 #{order_id}。")
        product = await session.get(Product, order.product_id) if order.product_id else None
        product_code = product.code if product else (understanding.product_code or "")
        remaining = order.remaining_shares or Decimal("0")
        name = product.name if product else (understanding.product_code or f"订单#{order_id}")
        lock_days = product.lock_days if product else 0
        return {
            "kind": "redeem_product",
            "payload": {
                "order_id": order.id,
                "product_code": product_code,
                "product_name": name,
                "shares": f"{remaining:.4f}",
                "title": f"确认赎回 {name}",
                "detail": (
                    f"产品：{name}" + (f"（{product_code}）" if product_code else "") + "\n"
                    f"赎回份额：{remaining:,.4f}\n"
                    f"锁定期：{lock_days} 天\n"
                    f"执行时将再次校验锁定期与订单状态。"
                ),
            },
        }

    code = (understanding.product_code or "").strip()
    if not code:
        raise Unresolvable("要申购哪个产品？可以说产品名称或代码。")
    matches = [p for p in (await session.scalars(select(Product))).all()
               if code in p.name or code in p.code]
    product = _one(matches, f"产品「{code}」")
    amount = _required_amount(understanding, f"申购{product.name}要投")
    return {
        "kind": "subscribe_product",
        "payload": {
            "product_id": product.id,
            "product_code": product.code,
            "product_name": product.name,
            "amount": f"{amount:.2f}",
            "title": f"确认申购 {product.name}",
            "detail": (
                f"产品：{product.name}（{product.code}）\n"
                f"风险等级：{product.risk_level}\n"
                f"申购金额：¥{amount:,.2f}\n"
                f"锁定期：{product.lock_days} 天\n"
                f"执行前会再次校验你的风险等级与账户余额。"
            ),
        },
    }


# ---------------------------------------------------------------------------
# cross-scene: birthday reservation, human handoff ticket
# ---------------------------------------------------------------------------
async def _birthday_plan(session, understanding: Understanding) -> dict:
    raw_date = (understanding.event_date or "").strip()
    try:
        event_date = date.fromisoformat(raw_date)
    except ValueError:
        raise Unresolvable("生日计划需要一个具体日期，格式为 YYYY-MM-DD。") from None
    budget = _required_amount(understanding, "生日计划的预留预算")
    option = (understanding.option or "").strip().upper()
    if option not in {"A", "B", "C"}:
        raise Unresolvable("请选择方案 A、B 或 C 之一。")
    from ...services.cross_scene_service import quote_birthday
    quote_birthday(option, budget)
    if event_date < date.today():
        raise BusinessRuleException("生日日期已经过去，请提供未来日期")
    return {
        "kind": "create_birthday_plan",
        "payload": {
            "event_date": raw_date,
            "budget": f"{budget:.2f}",
            "option": option,
            "title": "确认创建生日惊喜计划",
            "detail": (
                f"日期：{raw_date}\n"
                f"预留预算：¥{budget:,.2f}\n"
                f"采用方案：{option}\n"
                f"确认后可用余额会转为计划预留，生日前两天生成礼品订单草稿。"
            ),
        },
    }


async def _handoff_plan(session, demo_session_id: int, understanding: Understanding) -> dict:
    """Build the ticket from the *previous* turn's de-identified summary.

    The summary is produced by boundary.handoff_response and stored with that
    turn; regenerating it here would risk putting words in the user's mouth.
    """
    from ...core.models import AgentTurn
    latest = await session.scalar(
        select(AgentTurn).where(AgentTurn.session_id == demo_session_id)
        .order_by(AgentTurn.id.desc()).limit(1)
    )
    handoff = latest.context.get("handoff") if latest and isinstance(latest.context, dict) else None
    if not handoff:
        raise Unresolvable("我这边还没有可以转接的上下文。请先说说你想解决什么，我再帮你找人工客服。")
    human = understanding.operation == "create_human_handoff"
    return {
        "kind": "create_support_ticket",
        "payload": {
            "mode": "HUMAN_HANDOFF" if human else "SUPPORT_TICKET",
            "summary": handoff["summary"],
            "reason": handoff.get("reason", "user_request"),
            # Plain language only: the user asked for help, not for a ticket number.
            "title": "确认转接人工客服" if human else "确认让客服帮我跟进",
            "detail": (
                f"你刚才的诉求摘要：{handoff['summary'][:60]}\n"
                + ("转接后由真人坐席接手，过程中不需要你重复描述。"
                   if human else
                   "人工坐席不在线，登记后会按顺序联系你，过程中不需要你重复描述。")
                + "\n确认后生成一个可查询的受理编号。"
            ),
        },
    }


def _remark(message: str, understanding: Understanding | None = None) -> str | None:
    """The purpose of this payment, in the customer's own words.

    The model reads it (``purpose``) and the grounder has already checked the
    phrase exists in this message; the regex is only the explicit "备注X" form,
    which is unambiguous enough to take literally. Neither path invents one — a
    birthday transfer used to be labelled 定时转账 on its own confirmation card,
    which is not a purpose at all.
    """
    import re
    match = re.search(r"(?:备注|用于|作为|用途是?)\s*[:：]?\s*(.{1,60})", message)
    if match:
        value = re.split(r"[。！!,，;；]", match.group(1))[0].strip()
        if value:
            return value
    if understanding is not None and understanding.purpose:
        return understanding.purpose.strip()
    return None


__all__ = ["build_plan", "Unresolvable"]
