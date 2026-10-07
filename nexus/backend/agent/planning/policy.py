"""Local routing + rendering for parsed Proposals.

Two responsibilities live here and only here:
  1. Recognizing whether a user message matches a *deterministic* command
     that bypasses the model (exact_command).
  2. Rendering a fully-grounded Proposal into user-visible action text
     and any required clarification question (command_or_question).

This module sits between the parsed message and the rendered UI. It does
NOT decide safety, scope, slot validity, or write authorization — those are
owned by guard.py, boundary.py, grounder.py, and graph.py respectively.

This layer does NOT own:
  - Security attack detection                            -> guard.py
  - Out-of-scope / unsupported-finanial classification   -> boundary.py
  - Pydantic schema / allowed intents                    -> proposal.py
  - Slot grounding against the message                   -> grounder.py
  - Canned replies (FAQ / FALLBACK / BLOCKED)            -> responses.py
  - Amount parsing                                      -> parser.py
"""
from __future__ import annotations

import re

from ..security.guard import normalized
from ..contracts.proposal import Proposal


# ---------------------------------------------------------------------------
# Routing — local fast paths that bypass the model entirely.
# ---------------------------------------------------------------------------

def exact_command(text: str) -> bool:
    """True if the message is a precise instruction that can be parsed
    locally without going through the LLM (e.g. "给张三转账100元").

    These regexes are deliberately strict — when they match we save an
    LLM call and an extra hallucination surface; when they don't we fall
    through to the model."""
    text = text.strip().rstrip("。！!")
    return bool(
        text in {
            "确认", "取消", "余额", "查看余额", "我的余额",
            "查看账户", "查看我的账户", "账户概览",
            "查看卡片", "我的卡片",
            "查看订阅", "我的订阅",
            "查看流水",
            "对比理财产品", "查看理财产品", "有哪些理财产品",
            "看看产品", "产品对比",
            "识别周期扣费", "检测周期扣费", "分析周期扣费",
            "识别我的订阅", "检测我的订阅",
            "查看投资订单", "查看我的投资", "查看持仓", "我的持仓",
            "查看定时转账", "我的定时转账", "查看转账计划", "定时转账计划",
            "查看AA收款", "我的AA收款",
            "创建客服工单", "创建人工接管工单",
        }
        # 月度自动转账
        or re.fullmatch(
            r"(?:请)?(?:创建|设置|安排)?(?:一个)?每月\s*(\d{1,2})\s*[号日]\s*"
            r"给(.{1,30}?)转账\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?"
            r"(?:[，,\s]*(?:备注|用于|用途是?)\s*(.{1,100}))?",
            text,
        )
        # 生日计划
        or re.fullmatch(
            r"创建生日计划\s*日期(20\d{2}-\d{2}-\d{2})\s*预算([0-9]+(?:\.[0-9]{1,2})?)元\s*方案([ABC])",
            text,
        )
        # 启用方案
        or re.fullmatch(r"启用方案#(\d+)", text)
        # 申请卡片
        or re.fullmatch(r"(?:请)?申请一张(.{1,20}?)(?:信用卡|银行卡|卡)", text)
        # AA 收款
        or re.fullmatch(
            r"(?:请|帮我|帮忙)?(?:发起|创建|设置)\s*(\d{1,2})\s*人\s*AA(?:收款|分摊)\s*"
            r"([0-9]+(?:\.[0-9]{1,2})?)\s*元?"
            r"(?:[，,\s]*(?:备注|用于)\s*(.{1,100}))?",
            text,
            re.I,
        )
        # 标准转账
        or re.fullmatch(
            r"(?:请)?给(.{1,30}?)转账\s*([0-9]+(?:\.[0-9]+)?)\s*元?"
            r"(?:[，,\s]*备注\s*(.{1,100}))?",
            text,
        )
        # 口语红包 — "我帮张三发个红包 88.88"
        or re.fullmatch(
            r"(?:请)?(?:帮|替|为|给)?(.{1,30}?)\s*发个红包\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?"
            r"(?:[，,\s]*(?:备注|用于|作为)\s*(.{1,100}))?",
            text,
        )
        # "给X发红包88元"
        or re.fullmatch(
            r"(?:请)?给(.{1,30}?)\s*发红包\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?"
            r"(?:[，,\s]*(?:备注|用于|作为)\s*(.{1,100}))?",
            text,
        )
        # "向X发/转N元红包"
        or re.fullmatch(
            r"(?:请)?(?:向|给)?(.{1,30}?)\s*(?:发|转)\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?\s*红包"
            r"(?:[，,\s]*(?:备注|用于|作为)\s*(.{1,100}))?",
            text,
        )
        # Verb-first transfer — "转账给张三100元" / "帮我转账给张三 30 元".
        # The politeness prefix and the verb-first word order are both common in
        # real chat input, so the recipient must not be captured greedily.
        # "AA" may sit on either side of the verb ("转账aa给张三80元" /
        # "aa转账给张三80元"); it is a settlement hint, never part of the name.
        or re.fullmatch(
            r"(?:请)?(?:帮我|帮忙|替我|给我|我要|我想)?\s*(?:AA\s*)?"
            r"(?:转账|转钱|打款|发红包)\s*(?:AA\s*)?(?:给|向)?\s*"
            r"([一-龥A-Za-z]{1,20}?)\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?"
            r"(?:[，,\s]*(?:备注|用于|作为|用途是?)\s*(.{1,100}))?",
            text,
            re.I,
        )
        # Follow-up transfer with a pronoun or a repeat amount — "再给他转50元".
        # The payee comes from the previous grounded turn, so this stays a
        # deterministic instruction instead of falling through to the model.
        or re.fullmatch(
            r"(?:请)?(?:再|然后|接着|顺便|另外)?\s*(?:AA\s*)?"
            r"(?:帮|给|替)?(?:我)?(?:[一-龥]{0,4}?)?(?:转账|转|发红包)\s*(?:AA\s*)?"
            r"(?:给|向)?\s*([一-龥A-Za-z]{1,20}?)\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?"
            r"(?:[，,\s]*(?:备注|用于|作为|用途是?)\s*(.{1,100}))?",
            text,
        )
        # Follow-up transfer with a pronoun or a repeat amount — "再给他转50元".
        # The payee comes from the previous grounded turn, so this stays a
        # deterministic instruction instead of falling through to the model.
        or re.fullmatch(
            r"(?:请)?(?:再|然后|接着|顺便|另外)?\s*"
            r"(?:帮|给|替)?(?:我)?(?:[一-龥]{0,4}?)?(?:转账|转|发红包)\s*"
            r"(?:给|向)?\s*([一-龥A-Za-z]{1,20}?)\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|块钱)?"
            r"(?:[，,\s]*(?:备注|用于|作为|用途是?)\s*(.{1,100}))?",
            text,
        )
        # 卡片操作 — 锁定 / 解锁 / 挂失
        or re.fullmatch(
            r"(?:请)?(锁定|锁卡|解锁|挂失)(?:尾号)?\s*(\d{4})(?:的)?(?:银行卡|卡片|卡)?",
            text,
        )
        # 取消订阅 / 撤销代扣
        or re.fullmatch(r"(?:请)?(取消|撤销)(.{1,40}?)(订阅|代扣)", text)
        # 调整限额
        or re.fullmatch(
            r"(?:请)?(?:把|将)?(?:尾号)?\s*(\d{4})(?:的)?(?:银行卡|卡片|卡)?(?:的)?"
            r"(单笔|每日|日)(?:交易)?限额(?:调整|调|设|设置)?到?\s*"
            r"([0-9]+(?:\.[0-9]{1,2})?)\s*元?",
            text,
        )
        # 申购理财
        or re.fullmatch(
            r"(?:请)?申购\s*([A-Za-z0-9-]{2,20})\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?",
            text,
            re.I,
        )
        # 赎回投资订单
        or re.fullmatch(r"(?:请)?赎回(?:投资)?订单\s*#?\s*(\d+)(?:的)?(?:全部(?:份额)?)?", text)
    )


# ---------------------------------------------------------------------------
# Rendering — convert a fully-grounded Proposal into action text.
# ---------------------------------------------------------------------------

def command_or_question(proposal: Proposal) -> tuple[str | None, str | None]:
    """Given a Proposal whose slots are already grounded, return either
    (action_text, None) for a renderable write, or (None, clarification)
    if a required slot is still missing.

    The action_text is the *displayed* operation name, never model prose —
    it lives in code so it cannot be hallucinated."""
    p = proposal
    if p.intent == "transfer":
        if not p.recipient:
            return None, "你想转给谁？请提供完整收款人姓名。"
        if not p.amount:
            return None, f"你想给{p.recipient}转多少人民币？请提供明确金额。"
        return f"给{p.recipient}转账{p.amount}元", None
    if p.intent in {"lock_card", "unlock_card", "report_lost"}:
        if not p.last4:
            return None, "请提供要操作的银行卡尾号四位。"
        label = {"lock_card": "锁定", "unlock_card": "解锁", "report_lost": "挂失"}[p.intent]
        return f"{label}尾号{p.last4}", None
    if p.intent in {"cancel_subscription", "revoke_mandate"}:
        if not p.merchant:
            return None, "请提供完整的订阅商户名称。"
        if p.intent == "cancel_subscription":
            return f"取消{p.merchant}订阅", None
        return f"撤销{p.merchant}代扣", None
    return None, None


__all__ = ["exact_command", "command_or_question"]