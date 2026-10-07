"""Step-up verification for the writes that can actually cost money.

A confirmation card proves the user *read* the summary. It does not prove the
person holding the keyboard is the account holder. Real retail banking solves
that with a second factor, and a financial agent that moves money without one is
missing the control that matters most.

The two halves of the check are deliberately different in kind:

* **Echo-back** — the user retypes a value they were just shown (the last four
  digits, the amount). This proves attention, not identity, so it is cheap and
  applies to every write.
* **Passcode** — a local secret the user set for this browser session. This is
  the identity half, and it only exists in memory for the session: it is never
  written to the database, never logged, never returned to the client, and it
  dies with the session.

Boundary this module must not cross: it is a *demonstration* of the control, not
a credential system. The value is compared in constant time, failures are
counted and lock the action, and nothing here can be mistaken at the call site
for a real authentication service — ``verify`` is the only entry point and it
returns a verdict, never the secret.

Layer contract:
  owns      — the second factor, the attempt counter, and the escalation policy
  does NOT own — what an action does (plan_builder / tools.banking), the audit
                 trail, or the UI
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field

# 4 位数字，便于在演示中口述；真实系统不会用这么短的码。
PASSCODE_LENGTH = 4
MAX_ATTEMPTS = 3
# 常量时间比较所需的假值长度，避免通过响应时间区分"长度不对"和"值不对"。
_DUMMY_DIGEST = hashlib.sha256(b"nexus-step-up-dummy").digest()

# 资金真正会离开账户的操作才要口令。只读、订阅取消这类不涉及资金流量的写操作
# 走回显核对即可 —— 否则每一步都要输码，正常的银行业务根本不会这么做。
PASSCODE_REQUIRED_KINDS = frozenset({
    "transfer", "create_scheduled_transfer", "subscribe_product", "redeem_product",
    "set_card_limit", "apply_card",
})


class StepUpLocked(Exception):
    """Too many wrong codes: the action can no longer be verified this session."""


@dataclass
class StepUpState:
    """Per-session verification state. Never persisted, never serialised."""

    # 只有哈希留在内存里，明文用完即弃。
    passcode_digest: bytes | None = None
    attempts: int = 0
    locked: bool = False
    # 已通过回显核对的字段：键是动作 id，值是通过核对的字段名集合。
    echoed: dict[str, set[str]] = field(default_factory=dict)
    # 仅沙箱演示用：会话建立时生成并告知用户的那串码。它不参与核验逻辑，
    # 也不会随 challenge 下发；真实部署里这一栏永远为空。
    demo_passcode: str | None = None

    def has_passcode(self) -> bool:
        return self.passcode_digest is not None


def _digest(value: str) -> bytes:
    return hashlib.sha256(f"nexus-step-up::{value}".encode()).hexdigest().encode()


def set_passcode(state: StepUpState, passcode: str) -> None:
    """Record the session passcode. Only its hash is kept."""
    if not passcode.isdigit() or len(passcode) != PASSCODE_LENGTH:
        raise ValueError(f"验证密码需为 {PASSCODE_LENGTH} 位数字")
    state.passcode_digest = _digest(passcode)
    state.attempts = 0
    state.locked = False


def requires_passcode(kind: str) -> bool:
    return kind in PASSCODE_REQUIRED_KINDS


def check_echo(action_id: str, value: str, expected: str, *, numeric: bool = False) -> bool:
    """Constant-time check of a value the user was shown a moment ago.

    ``numeric`` compares *numbers*, not strings. The card shows ¥100.00, and a
    customer who types 100 has echoed the amount exactly right — failing them
    teaches them the control is an obstacle rather than a check, and in a demo
    it reads as "the app is broken". Separators, currency marks and trailing
    zeros are all normalised away first.
    """
    if numeric:
        left = _as_number(value)
        right = _as_number(expected)
        if left is not None and right is not None:
            return left == right
    value = value.replace(",", "").replace("，", "").replace("¥", "").strip()
    expected = expected.replace(",", "").replace("，", "").replace("¥", "").strip()
    return hmac.compare_digest(value.encode(), expected.encode())


def _as_number(value: str) -> Decimal | None:
    cleaned = value.replace(",", "").replace("，", "").replace("¥", "").replace("元", "").strip()
    try:
        return Decimal(cleaned)
    except (InvalidOperation, TypeError):
        return None


def check_passcode(state: StepUpState, passcode: str) -> None:
    """Verify the identity factor.

    Raises :class:`StepUpLocked` once the attempt budget is spent, so a caller
    cannot accidentally keep trying, and never reveals which of the two failures
    it was.
    """
    if state.locked:
        raise StepUpLocked("验证次数已用尽，请重新发起操作")
    digest = state.passcode_digest or _DUMMY_DIGEST
    if not hmac.compare_digest(_digest(passcode), digest):
        state.attempts += 1
        remaining = MAX_ATTEMPTS - state.attempts
        if remaining <= 0:
            state.locked = True
            raise StepUpLocked("验证密码错误次数过多，本次操作已锁定")
        raise ValueError(f"验证密码不正确，还可以尝试 {remaining} 次")
    state.attempts = 0


def challenge_payload(action_id: str, kind: str, *, last4: str | None = None,
                      amount: str | None = None) -> dict:
    """What the UI must show, and what the user is expected to type back.

    Only the values being challenged are returned. The expected answer is
    deliberately *not* included: the client needs to render the prompt, not the
    solution, and shipping it would make the check a formality.
    """
    fields = []
    if last4:
        fields.append({
            "name": "last4", "label": f"请输入卡片尾号后四位（{last4}）",
            "format": "digits", "length": 4,
        })
    if amount:
        fields.append({
            "name": "amount", "label": f"请输入本次金额（{amount}）",
            "format": "decimal", "length": None,
        })
    return {
        "type": "step_up",
        "action_id": action_id,
        "kind": kind,
        "fields": fields,
        "passcode_required": requires_passcode(kind),
        "passcode_length": PASSCODE_LENGTH,
        "passcode_hint": f"本次演示的 {PASSCODE_LENGTH} 位验证口令（进入页面时已给你）。",
        "max_attempts": MAX_ATTEMPTS,
        "message": "为确认这笔操作由你本人发起，请完成二次核验。",
    }


def expected_values(action_id: str, payload: dict) -> dict:
    """Server-side answers for the echoed fields. Never leaves the process."""
    return {
        "last4": str(payload.get("card_last4") or payload.get("last4") or ""),
        "amount": str(payload.get("amount") or ""),
    }


__all__ = [
    "PASSCODE_LENGTH", "MAX_ATTEMPTS", "PASSCODE_REQUIRED_KINDS",
    "StepUpState", "StepUpLocked",
    "set_passcode", "requires_passcode", "check_echo", "check_passcode",
    "challenge_payload", "expected_values",
]
