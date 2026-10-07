"""When money moves, and what the customer is told about it.

This module exists because "把 October 10 日给张三转三万" is not a monthly
standing order, and the difference is worth ¥240,000 of the customer's money
over two years. Timing used to be smeared across three places — a ``每月``
hard-coded in the confirmation copy, a ``first_run_on`` derived from
``date.today()`` instead of the date the user actually said, and a plan table
with no notion of "this is the last time". A user who said *this time* got a
perpetual authorisation, and a user who said *十月十号* on the 20th got the
11th of next month.

So timing now has exactly one owner. It answers three questions once, and
everything else — the confirmation card, the executor, the scheduler, the
post-edit re-render — reads the answer instead of re-deriving it.

  1. WHEN       → :attr:`Schedule.first_run_on`, the concrete date.
  2. HOW OFTEN  → :attr:`Schedule.frequency`, from a closed vocabulary.
  3. HOW LONG   → :attr:`Schedule.occurrences`, ``None`` meaning "until you stop it".

The two invariants this module exists to hold:

  * **A date is not a cadence.** "十月十号" is one execution on one day. Only
    an explicit period turns a transfer into a standing order, and the evidence
    for that period has to survive :mod:`grounder` before it is honoured.
  * **A one-off has no day-of-month cap.** The 1–28 restriction only ever made
    sense for a *monthly* plan that has to fire again in February. Pinning a
    single payment to the 28th because the user said the 31st is a business
    rule applied to the wrong object.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from .exceptions import BusinessRuleException

# Closed vocabulary. Adding a value here is a business decision, not a refactor.
ONCE = "ONCE"
WEEKLY = "WEEKLY"
MONTHLY = "MONTHLY"
QUARTERLY = "QUARTERLY"
YEARLY = "YEARLY"

FREQUENCIES = (ONCE, WEEKLY, MONTHLY, QUARTERLY, YEARLY)

#: Model-level (lowercase) values mapped onto the persisted vocabulary.
FROM_UNDERSTANDING = {
    "once": ONCE, "weekly": WEEKLY, "monthly": MONTHLY,
    "quarterly": QUARTERLY, "yearly": YEARLY,
}

#: Execution hour, in the demo's UTC+8 schedule convention.
EXECUTE_AT_HOUR = 9

MAX_OCCURRENCES = 60
#: The most a single payment may be. Applies to one-offs too — the cap is about
#: the amount, not about how often it repeats.
MAX_AMOUNT = 1_000_000

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_CADENCE_CN = {WEEKLY: "每周", MONTHLY: "每月", QUARTERLY: "每季", YEARLY: "每年"}


@dataclass(frozen=True)
class Schedule:
    """A resolved execution window. Immutable: a change means a new plan."""

    frequency: str
    first_run_on: date
    occurrences: int | None = None   # None = 长期有效，直到用户暂停
    day_of_month: int | None = None
    weekday: int | None = None       # 0=周一 … 6=周日，WEEKLY 才有
    # The amount is not part of *when* the plan fires, but the confirmation
    # card has to state the total exposure before anyone commits, so it rides
    # along as an optional field rather than being formatted by each caller.
    amount: Decimal | None = None

    @property
    def is_one_off(self) -> bool:
        return self.frequency == ONCE or self.occurrences == 1

    @property
    def is_recurring(self) -> bool:
        return not self.is_one_off

    # -- copy the customer actually reads ---------------------------------
    # The wording is a business fact, not decoration. Calling a single payment
    # "每期金额" is what made an unwanted standing order look like a receipt.

    def cadence_label(self) -> str:
        if self.frequency == ONCE:
            return "仅此一次"
        if self.frequency == WEEKLY:
            return f"每{_WEEKDAY_CN[self.weekday or 0]}"
        return f"{_CADENCE_CN[self.frequency]} {self.day_of_month} 日"

    def amount_label(self) -> str:
        """'每期金额' only when there is more than one payment."""
        return "金额" if self.is_one_off else "每期金额"

    def scope_label(self) -> str:
        """The total exposure, stated before the customer commits to anything."""
        if self.is_one_off:
            return f"共 1 笔，合计 ¥{_fmt(self.total_exposure())}，本笔执行后计划自动结束"
        if self.occurrences:
            return (
                f"共 {self.occurrences} 期，合计 ¥{_fmt(self.total_exposure())}，"
                f"第 {self.occurrences} 期后自动结束"
            )
        return "长期有效，每期扣款后自动安排下一期，直到你暂停或取消"

    def window_label(self) -> str:
        if self.is_one_off:
            return f"{self.first_run_on.isoformat()} 执行 {self.cadence_label()}"
        tail = f"共 {self.occurrences} 期" if self.occurrences else "长期有效"
        return (
            f"首次 {self.first_run_on.isoformat()}，之后 {self.cadence_label()}，{tail}"
        )

    def total_exposure(self) -> Decimal:
        unit = self.amount or Decimal("0")
        return unit * Decimal(self.occurrences or 0) if self.occurrences else unit

    def with_amount(self, amount) -> "Schedule":
        """Return a copy that knows the money involved, for the exposure copy."""
        return replace(self, amount=amount)

    def as_payload(self) -> dict:
        return {
            "frequency": self.frequency,
            "first_run_on": self.first_run_on.isoformat(),
            "day_of_month": self.day_of_month,
            "weekday": self.weekday,
            "occurrences": self.occurrences,
        }

    def next_after(self, after: date) -> date:
        """The execution date that follows ``after``.

        This is the *successor* calculation, and it is deliberately not
        :func:`resolve`. ``resolve`` interprets what a customer asked for and,
        given a start date, hands that same start date straight back — which is
        correct for reading a request and wrong for advancing a schedule, where
        it never moves and the caller's loop never ends.
        """
        if self.frequency == ONCE:
            return self.first_run_on
        if self.frequency == WEEKLY:
            return after + timedelta(days=7)
        if self.frequency == MONTHLY:
            return _add_months(after, 1)
        if self.frequency == QUARTERLY:
            return _add_months(after, 3)
        return _add_months(after, 12)


def _fmt(value) -> str:
    return f"{float(value):,.2f}"


def _add_months(anchor: date, months: int) -> date:
    """Advance by whole months, clamping to the last valid day of the target.

    A plan set on the 31st must still fire in a 30-day month, or February, and
    then resume the original day-of-month — not drift to the 28th for good.
    """
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    last = _days_in_month(year, month)
    return date(year, month, min(anchor.day, last))


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def next_day_of_month(day: int, today: date) -> date:
    """The next date in the future that falls on ``day``.

    Today counts. A transfer meant for *today* must not be pushed to next month
    because the user opened the app at 10am.

    Months that do not have the day are skipped, not clamped. Clamping turns
    "31 号" into "28 号" and then keeps firing on the 28th, which is a
    different instruction from the one the customer gave. There is no correct
    "near enough" here, so a single payment waits for the next month that
    actually has a 31st.
    """
    for offset in range(0, 26):
        total = today.month - 1 + offset
        year = today.year + total // 12
        month = total % 12 + 1
        if day > _days_in_month(year, month):
            continue
        candidate = date(year, month, day)
        if candidate >= today:
            return candidate
    # Only reachable if ``day`` is outside 1-31, which resolve() validates
    # before getting here.
    return today


def _parse_run_date(raw: str) -> date:
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise BusinessRuleException("执行日期格式不正确，请重新选择日期") from None
    return parsed


def resolve(
    *,
    frequency: str | None,
    run_date: str | None = None,
    day_of_month: int | None = None,
    occurrences: int | None = None,
    weekday: int | None = None,
    today: date | None = None,
) -> Schedule:
    """Turn the model's timing slots into a concrete, checkable schedule.

    ``frequency`` defaults to :data:`ONCE`. That default is the whole point: a
    model that forgets to name a period, or names one the grounder could not
    trace to the user's own words, produces a single payment rather than an
    open-ended authorisation. The customer can always ask for a standing order
    afterwards; nobody can be surprised by one they never asked for.
    """
    today = today or date.today()
    kind = (frequency or ONCE).upper()
    if kind not in FREQUENCIES:
        kind = ONCE

    if kind == ONCE:
        # A one-off keeps the user's own calendar date, month and year. The old
        # code only ever had a day number and rebuilt the month from today(), so
        # asking for the 10th on the 20th silently moved the payment a month
        # forward — a birthday gift arriving late and looking deliberate.
        if run_date:
            first = _parse_run_date(run_date)
            if first < today:
                # The customer named a day that has already gone. Executing it
                # today instead would be a different payment on a different day
                # than the one they authorised, and booking it into next month
                # would be a third. Say so and let them choose.
                raise BusinessRuleException(
                    f"执行日期 {first.isoformat()} 已经过去，请确认一个未来的日期"
                )
        elif day_of_month and 1 <= day_of_month <= 31:
            first = next_day_of_month(day_of_month, today)
        elif day_of_month:
            raise BusinessRuleException("日期需要在 1 至 31 日之间")
        else:
            first = today
        return Schedule(frequency=ONCE, first_run_on=first, occurrences=1)

    if occurrences is not None and not 1 <= occurrences <= MAX_OCCURRENCES:
        raise BusinessRuleException(f"期数需要在 1 至 {MAX_OCCURRENCES} 期之间")

    if kind == WEEKLY:
        if run_date:
            first = _parse_run_date(run_date)
            # A start date in the past keeps its weekday and rolls forward in
            # whole weeks. Silently jumping to "the next same weekday" is what
            # the user asked for; jumping to some other day is not.
            while first < today:
                first += timedelta(days=7)
        elif weekday is not None:
            first = today + timedelta(days=(weekday - today.weekday()) % 7 or 7)
        else:
            raise BusinessRuleException("每周执行需要指定一个星期几，或一个起始日期")
        return Schedule(
            frequency=WEEKLY, first_run_on=first,
            occurrences=occurrences, weekday=first.weekday(),
        )

    # Monthly / quarterly / yearly all need a day of month, and only these need
    # it to be 1–28: a plan that repeats monthly must have a date that exists in
    # every month it will ever run in. Anything past the 28th would skip months
    # forever, so it is a real limit — but it belongs here, on the repeating
    # plans, not on single payments.
    if not day_of_month or not 1 <= day_of_month <= 28:
        raise BusinessRuleException(f"重复扣款的日期需在每月 1 至 28 日之间（单次转账不受此限制）")

    first = _parse_run_date(run_date) if run_date else next_day_of_month(day_of_month, today)
    if run_date and first < today:
        # The user named a month that has already passed. Say so rather than
        # quietly rolling it forward to a date they never mentioned.
        raise BusinessRuleException(
            f"首次执行日期 {first.isoformat()} 已经过去，请确认一个未来的日期"
        )
    if not run_date and first < today:
        first = next_day_of_month(day_of_month, today)

    if occurrences == 1:
        return Schedule(frequency=ONCE, first_run_on=first, occurrences=1)

    return Schedule(
        frequency=kind, first_run_on=first,
        occurrences=occurrences, day_of_month=day_of_month,
    )


_CN_DIGITS = "零一二三四五六七八九"


def chinese_day(day: int) -> str:
    """Write a day-of-month the way a customer actually says it.

    1-31 only, and fully determined: 10 is 十, 21 is 二十一, 31 is 三十一.
    A bounded notation table, not phrase matching — "十月十号" contains the
    same fact as "10 号", and the grounder has to see both or it clears a date
    the customer did state and then books the payment for today instead.
    """
    if not 1 <= day <= 31:
        raise ValueError(f"day out of range: {day}")
    if day < 10:
        return _CN_DIGITS[day]
    if day == 10:
        return "十"
    if day < 20:
        return "十" + _CN_DIGITS[day - 10]
    tens, ones = divmod(day, 10)
    return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")


def day_is_stated(day: int, text: str) -> bool:
    """True when the customer wrote this day of the month, in either notation.

    Used to ground ``day_of_month`` against the message. The pattern is built
    from the number itself, so it needs no maintenance when new phrasings
    appear — and it cannot be satisfied by a day the customer never said.
    """
    import re

    return bool(re.search(rf"(?:{day}|{chinese_day(day)})\s*[号日]", text))


def validate_amount(amount) -> None:
    """Shared ceiling for a single payment, one-off or repeating."""
    from .money import positive_amount

    value = positive_amount(amount)
    if value > MAX_AMOUNT:
        raise BusinessRuleException(f"单笔金额不能超过 {MAX_AMOUNT:,} 元")


def validate_purpose(purpose: str | None) -> str:
    clean = (purpose or "").strip()
    if not clean or len(clean) > 100:
        raise BusinessRuleException("请填写 1 至 100 个字的用途")
    return clean


__all__ = [
    "ONCE", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY", "FREQUENCIES",
    "FROM_UNDERSTANDING", "MAX_OCCURRENCES", "MAX_AMOUNT", "EXECUTE_AT_HOUR",
    "Schedule", "resolve", "next_day_of_month", "chinese_day", "day_is_stated",
    "validate_amount", "validate_purpose",
]
