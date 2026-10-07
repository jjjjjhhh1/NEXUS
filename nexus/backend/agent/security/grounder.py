"""Slot grounding. Given a Proposal emitted by the model, validate that every
non-empty slot value can be traced back to either the current user message
or a confirmed prior turn. Invented slots (e.g. a card suffix the user never
mentioned) raise BusinessRuleException, which the caller surfaces as a
clarification question.

This is one of the two layers (along with guard.py) that prevents the model
from acting on information it invented.

This layer does NOT own:
  - Detecting prompt injection / role spoofing        -> guard.py
  - Detecting out-of-scope / unsupported business     -> boundary.py
  - Knowing which names/cards the user has on file      -> demo_agent / DB
  - Asking the clarifying question                   -> graph / demo_agent
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from ...core.exceptions import BusinessRuleException
from ...core.money import positive_amount
from .guard import normalized
from ..planning.parser import evidence_amount
from ..contracts.proposal import Proposal


_SLOT_FIELDS_FROM_PRIOR = ("recipient", "last4", "merchant", "amount")
_SLOT_FIELDS_USER_FACING = ("recipient", "last4", "merchant")
_SLOT_NAME_PATTERN = re.compile(r"[\w\u4e00-\u9fff ·-]+")


def ground(proposal: Proposal, message: str, context: dict | None) -> Proposal:
    """Return a new Proposal with each slot verified against the message
    or a confirmed prior turn. Raises BusinessRuleException on invented slots
    so the caller can convert it into a clarification question."""
    data = proposal.model_dump()
    prior = context or {}
    # Only inherit from a prior turn if the intent is the same — never mix slots
    # across different intents (e.g. a transfer's recipient on a lock_card turn).
    if prior.get("intent") != proposal.intent:
        prior = {}
    for field in _SLOT_FIELDS_FROM_PRIOR:
        if data.get(field) is None and prior.get(field) is not None:
            data[field] = prior[field]
    for field in _SLOT_FIELDS_USER_FACING:
        value = data[field]
        if not value:
            continue
        if value not in normalized(message) and value != prior.get(field):
            raise BusinessRuleException("识别结果中有未被你明确提供的信息，请重新说明业务对象")
        if not _SLOT_NAME_PATTERN.fullmatch(value):
            raise BusinessRuleException("业务对象名称不受支持")
    if proposal.amount is not None:
        amount = positive_amount(proposal.amount)
        quoted = proposal.amount_evidence
        if quoted and normalized(quoted) in normalized(message):
            if evidence_amount(quoted) != amount:
                raise BusinessRuleException("金额识别不一致，请用数字重新输入")
        elif str(amount) != prior.get("amount"):
            raise BusinessRuleException("请明确要转账的金额")
        data["amount"] = str(amount)
    return Proposal.model_validate(data)


# Slots that name a business object the user must have actually mentioned. A
# handle ("房东", "老张") is also user-supplied, so it is checked the same way.
_GROUNDED_OBJECT_FIELDS = ("recipient", "last4", "merchant", "account_handle", "product_code")
_GROUNDED_NAME_PATTERN = re.compile(r"[\w一-鿿 ·-]+")


_WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")


def _weekday_stated(weekday: int, text: str) -> bool:
    """Accept 周三 / 星期三 / 礼拜三 for the weekday the model claims."""
    name = _WEEKDAY_CN[weekday]
    return bool(re.search(rf"(?:周|星期|礼拜){name}(?![数末])", text))


def _run_date_stated(run_date: str, text: str) -> bool:
    """A date is grounded by the day number, the ISO string, or the weekday.

    "每周三" states a day without stating a number, and clearing its run_date
    for that reason would leave a weekly transfer with no start at all. The
    weekday the date falls on is part of what the customer said, so it counts.
    """
    day = run_date[-2:].lstrip("0") or "0"
    if re.search(rf"(?:{re.escape(day)}\s*[号日])|(?:{re.escape(run_date)})", text):
        return True
    try:
        return _weekday_stated(date.fromisoformat(run_date).weekday(), text)
    except ValueError:
        return False


def _day_stated(day: int, text: str) -> bool:
    """Accept "10 号" and "十号" alike.

    The old check only matched Arabic digits, so a customer who wrote 十月十号
    had the day silently cleared — and the payment was then booked for *today*
    instead. A cleared date is worse than a wrong one: it produces a confident
    card with the wrong execution day and no trace of what was dropped.
    """
    from ...core.scheduling import day_is_stated

    return day_is_stated(day, text)


def ground_understanding(understanding, message: str, context: dict | None = None):
    """Enforce that every object slot came from the user's own words.

    This is what makes model intent safe to execute: the model decides *what*
    the user wants, but it may never introduce a payee, a card suffix, a
    merchant, or an amount the user did not say. Any slot that cannot be traced
    back to this message is cleared, so the caller asks instead of guessing.
    """
    from ..contracts.understanding import Understanding

    data = understanding.model_dump()
    prior = context or {}
    if prior.get("scene") not in (None, understanding.scene):
        # Never carry a payee across scenes: that is how a card request would
        # silently inherit a transfer's recipient.
        prior = {}
    value = normalized(message)

    for field in _GROUNDED_OBJECT_FIELDS:
        current = data.get(field)
        if current is None:
            inherited = prior.get(field)
            if inherited and normalized(str(inherited)) in value:
                data[field] = inherited
            continue
        text = str(current)
        if text not in value and text != prior.get(field):
            data[field] = None
            continue
        if not _GROUNDED_NAME_PATTERN.fullmatch(text):
            data[field] = None

    if data.get("amount") is not None:
        # Traceability only. Whether the amount is *valid* (positive, at most two
        # decimals) belongs to plan_builder, which owns business rules — so an
        # invalid-but-quoted amount like 0 stays here and is rejected later with
        # a business-rule error instead of turning into another question.
        amount_text = str(data["amount"])
        quoted = data.get("amount_evidence")
        checked: bool | None = None
        if quoted and normalized(str(quoted)) in value:
            try:
                checked = evidence_amount(str(quoted)) == Decimal(amount_text)
            except (BusinessRuleException, ArithmeticError):
                checked = None
        # A matching quote is itself the traceable form: the user said "两百块",
        # not "200", so requiring the digits to also appear would clear a
        # perfectly grounded amount.
        if checked is False or (checked is None and not re.search(rf"{re.escape(amount_text)}", value)):
            data["amount"] = None
            data["amount_evidence"] = None

    if data.get("purpose"):
        # A purpose is printed on the receipt, so it has to be the customer's
        # own words. "生日" out of "给张三生日转账" qualifies; an invented
        # "生日红包" does not appear in the message and is cleared.
        if str(data["purpose"]) not in value:
            data["purpose"] = None
    if data.get("last4") is not None and not re.search(rf"{re.escape(str(data['last4']))}", value):
        data["last4"] = None
    if data.get("day_of_month") is not None and not _day_stated(int(data["day_of_month"]), value):
        data["day_of_month"] = None

    # ---- the standing-order guard --------------------------------------
    # A period is the one slot where guessing costs the customer real money and
    # is invisible until the next month: "十月十号给张三转三万" turned into a
    # monthly standing order once meant ¥360,000 the user never agreed to.
    #
    # The model does the reading; this layer only checks that the words it read
    # the period from actually exist in the message. A period it cannot quote
    # is downgraded to a single payment — and because the confirmation card is
    # editable, downgrading is the safe direction in both cases. An unearned
    # standing order is removed; a genuine one that was missed shows up as
    # "仅此一次" on a card the customer can switch to 每月 with one tap.
    if data.get("recurrence") not in (None, "once"):
        quoted = data.get("recurrence_evidence")
        if not (quoted and normalized(str(quoted)) in value):
            data["recurrence"] = "once"
            data["recurrence_evidence"] = None
            data["occurrences"] = None
    if data.get("occurrences") is not None and not re.search(
        rf"{int(data['occurrences'])}", value
    ):
        data["occurrences"] = None
    if data.get("weekday") is not None and not _weekday_stated(int(data["weekday"]), value):
        data["weekday"] = None
    if data.get("run_date"):
        if not _run_date_stated(str(data["run_date"]), value):
            data["run_date"] = None
    if data.get("participant_count") is not None and not re.search(
        rf"{int(data['participant_count'])}\s*(?:个)?人", value
    ):
        data["participant_count"] = None
    if data.get("order_id") is not None and not re.search(
        rf"#?\s*{int(data['order_id'])}", value
    ):
        data["order_id"] = None
    if data.get("option") is not None and not re.search(
        rf"(?:方案\s*)?{re.escape(str(data['option']))}", value
    ):
        data["option"] = None

    return Understanding.model_validate(data)


__all__ = ["ground", "ground_understanding"]