"""Safety guard layer. Detects prompt injection, credential leak, role spoofing,
and money-laundering cues that must never reach the model or any write tool.

Defense in depth: this layer is a tripwire, not a guarantee. The real boundary is
the closed intent schema (Proposal), server-side slot grounding, server-side identity
resolution, and the separate confirmation endpoint.

Failure modes this layer owns:
  - Credential leak in user message (sk-..., API_KEY=, etc.)
  - Prompt injection (ignore rules / bypass confirmation / <|system|>)
  - Role spoofing (pretend admin, override confirmed flag)
  - System-command injection (curl, wget, SQL, shell, scripts)
  - Anti-money-laundering cues (split transfers to dodge monitoring)
  - Negated write requests (don't transfer / 暂不转账)

This layer does NOT own:
  - Out-of-scope topics (weather, jokes)         -> boundary.py
  - Unsupported financial business (stock, loan)  -> boundary.py
  - Vague transfers (missing slots)               -> boundary.py
  - Misunderstanding handoff                       -> boundary.py
"""
from __future__ import annotations

import re
import unicodedata


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")


# Single, fixed reply for every blocked message. Never echo the user's prompt.
BLOCKED = "我不能绕过确认、切换他人身份、读取密钥或执行系统指令。你可以查询自己的账户，或发起需要确认的转账、卡片和订阅操作。"


# Hard-rule patterns. Each pattern means: if present → reject.
# These are tried in order; first match wins. Add new attacks here, not inline.
_GUARD_PATTERNS = (
    # 1. Credential leak. Never forward to provider or store in turns.
    ("credential_leak",
     r"sk-[A-Za-z0-9_-]{12,}|(?:[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET)|API_KEY)\s*[:=]",
     "请不要在业务对话中输入密钥。我不会转发这条消息，也不会创建操作。"),

    # 2. Prompt injection: try to discard system rules.
    ("prompt_injection",
     r"(?:忽略|无视|覆盖|忘掉).{0,15}(?:规则|指令|提示词|限制)",
     BLOCKED),

    # 3. Prompt injection: try to skip confirmation/authorization.
    ("bypass_confirmation",
     r"(?:跳过|绕过|无需|不需要|不用).{0,10}(?:确认|授权|鉴权|验证)",
     BLOCKED),

    # 4. Prompt injection: raw system markers.
    ("raw_system_marker",
     r"(?:system|developer)\s*:|<\|(?:system|im_start)\|>|</?(?:system|developer)>",
     BLOCKED),

    # 5. Prompt injection: extract system prompt / credentials.
    ("extract_secrets",
     r"(?:读取|输出|显示|泄露|打印).{0,15}(?:密钥|令牌|环境变量|系统提示词|system prompt|token|secret)",
     BLOCKED),

    # 6. Prompt injection: English variants.
    ("prompt_injection_en",
     r"(?:ignore|override|disregard).{0,30}(?:instruction|rule|prompt)|(?:bypass|skip).{0,20}(?:confirm|auth)",
     BLOCKED),

    # 7. Role spoofing: pretend to be admin/another user.
    ("role_spoofing",
     r"(?:切换|冒充).{0,12}(?:用户|管理员)|(?:user_id|session_id|confirmed)\s*[:=]",
     BLOCKED),

    # 8. System command injection.
    ("system_command",
     r"(?:执行|运行).{0,15}(?:SQL|shell|命令|脚本)|(?:curl|wget)\s+https?://",
     BLOCKED),

    # 9. Anti-money-laundering cues.
    ("money_laundering",
     r"(?:帮我|替我|教我|协助我)?.{0,8}(?:洗钱|资金漂白|掩饰资金来源|规避反洗钱|逃避反洗钱|拆分转账逃避监控)",
     BLOCKED),
)

# Conservative handling of negated write requests. Not a hard attack, but the user
# is telling us not to act; surface that before any model call.
#
# Two things a naive "negation word + action word" match gets wrong, and both
# happen in normal speech:
#   * scope   — "云音乐我不想用了，取消掉" negates *usage*, not the cancellation
#     that follows it. A clause boundary between the negation and the verb means
#     they are not the same instruction.
#   * override — "我不要转账，也帮我取消云音乐订阅" states a refusal and then asks
#     for something else; the later, non-negated instruction is the real request.
# So a negation only blocks when it governs the verb, and the last non-negated
# write instruction wins.
_NEGATION = r"(?:不要|别|暂不|不想|don't|do not)"
_WRITE_VERB = r"(?:转账|转钱|锁卡|解锁|挂失|取消|撤销|退订|申购|赎回|调限额|transfer|send)"
_CLAUSE_BREAK_CLASS = "，,。；;！!？?\\n"  # a clause boundary ends the negation's reach
# The negation must sit in the same clause as the verb; at most a short,
# comma-free stretch may sit between them ("不要给张三转账").
_NEGATION_TAIL = rf"{_NEGATION}[^{_CLAUSE_BREAK_CLASS}]{{0,12}}$"


def _negated_write(value: str) -> bool:
    verbs = [m.start() for m in re.finditer(_WRITE_VERB, value, re.I)]
    if not verbs:
        return False
    blocked_from = None
    for index, start in enumerate(verbs):
        # Governed means the negation and the verb sit in the same clause with no
        # break between them ("不要给张三转账" = one instruction). A clause break
        # ("我不想用了，取消掉") means two separate instructions.
        governed = bool(re.search(_NEGATION_TAIL, value[max(0, start - 30):start], re.I))
        if governed:
            blocked_from = index
        elif blocked_from is not None:
            # A later, non-negated instruction overrides the earlier refusal.
            return False
    return blocked_from is not None


def guard(text: str) -> str | None:
    """Return a fixed Chinese reply if the message matches a hard-rule attack,
    otherwise None. Always returns None on a normal message — the caller decides
    what to do with non-attack messages."""
    value = normalized(text)
    for _label, pattern, reply in _GUARD_PATTERNS:
        if re.search(pattern, value, re.I | re.S):
            return reply
    if _negated_write(value):
        return "收到，不会为这条消息创建操作。已有的待确认卡如需取消，请点击对应的取消按钮。"
    return None


def is_attack(text: str) -> bool:
    """Boolean variant. Use when the caller needs to log or count attacks
    without consuming the fixed reply."""
    return guard(text) is not None


# Public re-export so old code paths keep working.
__all__ = ["guard", "is_attack", "normalized", "BLOCKED"]