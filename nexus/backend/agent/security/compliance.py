"""Compliance review of a finished answer, before a customer ever sees it.

Everything upstream protects the system from the *user*: the guard stops
injection before it reaches a model, the grounder stops invented slots, the
confirmation card stops unapproved writes. None of them looks at the sentence
about to be displayed. An answer can be assembled entirely from verified data
and still be unacceptable to send, because the prose wrapped around it made a
promise the data does not support.

So the answer gets one more pass, after every tool has run and before the
render. It is split in two on purpose:

  L0  deterministic, local, no model. It checks facts that can be decided:
      does every amount on screen exist in the data we read, does the total
      equal the sum of its parts, is a product within the customer's measured
      risk, is anything unredacted showing. These are the failures that matter
      most in finance, and they cost nothing to detect.

  L1  semantic, model-based. It reads prose and judges promises. It is only a
      backstop for L0, never a replacement, and it is deliberately allowed to
      fail open: an unreachable reviewer must not take the assistant offline,
      because the answer underneath was already verified at the point of
      computation.

Layer contract:
  owns      — whether this answer may be shown, and the record of that decision
  does NOT own — the data (read views), the layout (presentation), the wording

Note on what this layer does not do: it does not rewrite. Rewriting is a
separate concern, and folding it in here would mean a model touching the
answer a fourth time with the power to alter a number it was asked to check.
A reviewer that can edit is no longer a reviewer.
"""
from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from ...core.config import settings
from ..integrations import model as model_module
from . import untrusted

# Amounts rendered into an answer look like ¥1,234.56 or 1,234.56. A number
# that matches none of the values behind the answer is a number with no source.
_MONEY = re.compile(r"[¥￥]?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")

# Fields the renderer must never place on screen in full.
_SECRET_KEYS = {
    "card_number", "card_no", "cardno", "cvv", "cvc", "password", "passcode",
    "id_card", "idcard", "identity_number", "secret", "token", "api_key",
    "account_number_full", "phone_full",
}

# Risk ladder used by the profile. A product may never be recommended above the
# level the customer actually measured, whatever the surrounding prose says.
RISK_ORDER = ["R1", "R2", "R3", "R4", "R5"]

# Fields a model wrote, as distinct from fields the backend computed. Amounts
# found here are checked against the computed data rather than trusted from it.
_NARRATIVE_KEYS = {
    "message", "headline", "verdict", "summary_text", "binding_reason",
    "rationale", "emphasis", "note", "hint", "advice_text", "answer",
}
# Lists whose items are narrative even though the key is a structural noun.
_NARRATIVE_CONTAINERS = {"insights", "findings", "observations", "steps", "actions"}


class Finding(BaseModel):
    """One problem, stated so it can be acted on or explained.

    ``severity`` is a classification, not a permission. The model may only
    choose between the two values; whether either one stops the answer is
    decided by this module, not by the reviewer. A reviewer that could refuse
    on its own authority would be able to shut the assistant down over a
    wording preference, and a layer that can do that is not a backstop.
    """

    code: str = Field(max_length=40)
    detail: str = Field(max_length=200)
    severity: Literal["block", "flag"] = "flag"


class Verdict(BaseModel):
    findings: list[Finding] = Field(default_factory=list, max_length=10)
    summary: str = Field(default="", max_length=200)

    @property
    def ok(self) -> bool:
        """Only a hard finding stops the answer. Everything else is advice."""
        return not any(f.severity == "block" for f in self.findings)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]


def _as_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _money_tokens(value: object) -> set[str]:
    """Monetary values written in a way a person would read as an amount.

    Deliberately narrow, because precision matters more than recall here. A
    bare integer is not an amount — it is a count, an index, a year or part of
    an id — and treating those as money made this check fire on essentially
    every answer. Ratio-scale values (a 0.6 weight, a 2.5% variance) are
    likewise not amounts. What is left is a figure large enough that inventing
    one would change a decision, which is exactly the failure worth catching.
    """
    found: set[str] = set()
    text = re.sub(r"(?:尾号|卡尾号)\s*\d{4}", "", str(value))
    for match in _MONEY.finditer(text):
        raw = match.group(1)
        if not raw or not ("," in raw or len(raw) >= 4 or "." in raw):
            continue
        try:
            amount = Decimal(raw.replace(",", ""))
        except ArithmeticError:
            continue
        if amount < 1:
            continue
        found.add(format(amount.normalize(), "f"))
    return found


def _collect_known(answer: dict, question: str) -> set[str]:
    """Every value the answer is entitled to show, however it is spelled.

    Three legitimate sources: numbers the backend computed, amounts already
    rendered into fields such as the hero value, and figures the customer
    themselves supplied — an echoed amount is not a hallucination.

    Narrative fields are excluded on purpose. A model-written sentence is the
    one place an invented figure can appear, so harvesting its numbers into the
    known set would make every fabrication self-certifying.
    """
    found: set[str] = set()

    def walk(node: object, key: str = "") -> None:
        if isinstance(node, dict):
            for child_key, value in node.items():
                walk(value, child_key)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, key)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float, Decimal)):
            found.update(_money_tokens(f"{Decimal(str(node)):,.2f}"))
            found.add(f"{Decimal(str(node))}")
        elif key in _NARRATIVE_KEYS or key in _NARRATIVE_CONTAINERS:
            return
        else:
            found.update(_money_tokens(node))

    walk(answer)
    found |= _money_tokens(question)
    return found


def _narrative_text(answer: dict) -> str:
    """Prose only: the fields a model wrote, as opposed to fields it computed."""
    parts: list[str] = []

    def walk(node: object, key: str = "") -> None:
        if isinstance(node, dict):
            for child_key, value in node.items():
                walk(value, child_key)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and (key in _NARRATIVE_KEYS or key in _NARRATIVE_CONTAINERS):
            parts.append(node)

    walk(answer)
    return " ".join(parts)


def _risk_ceiling(answer: dict) -> str | None:
    """The most aggressive product level this customer may be shown."""
    decision = answer.get("decision") or {}
    nested = decision.get("max_product_risk") if isinstance(decision,dict) else None
    if nested in RISK_ORDER:
        return nested
    for key in ("max_product_risk", "risk_ceiling", "grade"):
        value = answer.get(key)
        if isinstance(value, str) and value.upper() in RISK_ORDER:
            return value.upper()
    return None


def _recommended_risks(answer: dict) -> list[str]:
    """Risk levels of products this answer actually recommends."""
    levels: list[str] = []
    for item in (answer.get("orders") or []) + (answer.get("products") or []) + (answer.get("product_matches") or []):
        if isinstance(item, dict):
            level = str(item.get("risk_level") or item.get("risk") or "").upper()
            if level in RISK_ORDER:
                levels.append(level)
    return levels


def check_local(answer: dict) -> Verdict:
    """L0. Decisions, not opinions. Cannot fail and never calls anything."""
    findings: list[Finding] = []
    if re.search(r"(?:根据|基于).{0,12}(?:记忆|语气|历史偏好).{0,20}(?:已经授权|已授权|已确认|无需确认|提升风险等级)",_narrative_text(answer)):
        findings.append(Finding(code="MEMORY_AUTHORITY_VIOLATION",detail="历史偏好不能替代正式测评或交易授权",severity="block"))

    # 1. No full secrets on screen.
    for key in _SECRET_KEYS:
        for match in re.finditer(rf'"{key}"\s*:\s*"?([^",}}]{{4,}})', _as_text(answer), re.IGNORECASE):
            if match.group(1).strip():
                findings.append(Finding(code="UNREDACTED_FIELD", detail=f"回答中出现未脱敏字段 {key}", severity="block"))

    # 2. No product recommended above the customer's measured risk.
    ceiling = _risk_ceiling(answer)
    if ceiling:
        for level in _recommended_risks(answer):
            if RISK_ORDER.index(level) > RISK_ORDER.index(ceiling):
                findings.append(Finding(
                    code="RISK_LEVEL_EXCEEDED",
                    detail=f"风险等级 {ceiling} 的客户被推荐了 {level} 产品",
                    severity="block",
                ))

    # 3. Every amount on screen is traceable to the data behind it.
    known = _collect_known(answer, answer.get("question") or "")
    shown = _money_tokens(_narrative_text(answer))
    for number in sorted(shown - known)[:3]:
        findings.append(Finding(
            code="UNSOURCED_AMOUNT",
            detail=f"回答中的金额 {number} 无法追溯到已核验数据",
            severity="flag"))

    # 4. Category totals must add up to the stated total.
    categories = answer.get("categories") or []
    total = answer.get("summary", {}).get("total") if isinstance(answer.get("summary"), dict) else None
    if categories and total:
        try:
            parts = sum(Decimal(str(c["amount"])) for c in categories if isinstance(c, dict) and c.get("amount") is not None)
            stated = Decimal(str(total).replace(",", "").replace("¥", "").replace("￥", ""))
            # Categories are a ranked list, so a sum *below* the total is the
            # normal case. Only a sum above it is impossible, and that is a
            # genuine arithmetic defect worth surfacing.
            if parts > stated + Decimal("1"):
                findings.append(Finding(
                    code="TOTAL_MISMATCH",
                    detail=f"分类合计 {parts:,.2f} 超过本期支出 {stated:,.2f}",
                    severity="flag",
                ))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            pass

    # 5. Third-party prose that is not safe to show.
    for key in ("company", "title", "name", "summary"):
        value = answer.get(key)
        # Locally authored summaries are sentences, not external names. Keep
        # injection/markup checks but apply the appropriate text length bound.
        bound = 4000 if key == "summary" and answer.get("type") in {"financial_analysis", "universal_plan"} else untrusted.MAX_EXTERNAL_TEXT
        if isinstance(value, str) and not untrusted.is_safe(value, max_length=bound):
            findings.append(Finding(code="UNTRUSTED_SOURCE_TEXT", detail=f"外部数据字段 {key} 不可直接展示", severity="block"))

    return Verdict(findings=findings, summary="本地校验通过")


REVIEW_SYSTEM = """你是金融回答的合规审查员。回答已经由后端根据已核验数据生成，你要判断它能不能发给客户。

只审查合规与合理性，不要评价排版或措辞优雅度。检查：
1. 是否出现收益承诺或绝对化表述（保证、稳赚、必涨、无风险、一定）。
2. 结论是否超出数据支撑（数据只到 A，却下了 B 的结论）。
3. 是否把客观数据说成了投资建议。
4. 是否遗漏了必要的风险或时效限定。
5. 是否有与"数据由本地核验"相矛盾的说法。
6. 是否把历史记忆、用户语气或投资偏好当成正式风险承受能力测评、当前财务事实或交易授权；这种明显越权用 block。

severity 的判定标准（必须严格遵守）：
- "block"：用于绝对化收益承诺（保证/稳赚/必涨/无风险/一定），结论与数据明显矛盾，或用记忆替代正式测评/授权而误导客户决策。
- "flag"：其他一切问题，包括措辞、缺少时效限定、分类小计与总计不一致、建议改进项。

拿不准时一律用 "flag"。不要因为建议改进就判 "block"。

你看不到原始数据，也不要尝试推测数字。你只能判断这段话本身是否越界。
只输出一个 JSON 对象，不要输出其他文字、不要用 markdown 代码块。
必须严格符合以下 JSON Schema：
{schema}"""


async def review_model(answer: dict, question: str) -> Verdict | None:
    """L1. Returns None when the reviewer could not be reached.

    Failing open is the deliberate choice. This layer exists to catch a rare
    overreach, not to be load-bearing: blocking everything because a provider
    timed out would be a worse failure than the thing it prevents.
    """
    if not settings.llm_enabled or not model_module.is_configured():
        return None
    schema = json.dumps(Verdict.model_json_schema(), ensure_ascii=False)
    visible = {k: v for k, v in answer.items() if k not in {"trace", "context"}}
    payload = {
        "model": settings.llm_model, "max_tokens": 700,
        "messages": [
            {"role": "system", "content": REVIEW_SYSTEM.format(schema=schema)},
            {"role": "user", "content": json.dumps(
                {"customer_question": question, "draft_answer": visible}, ensure_ascii=False, default=str)},
        ],
    }
    try:
        async with asyncio.timeout(min(settings.llm_timeout, 8)):
            async with httpx.AsyncClient(timeout=settings.llm_timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.post(model_module.endpoint(), headers=model_module._headers(), json=payload)
        if response.status_code != 200:
            return None
        return Verdict.model_validate(model_module._json_object(response.json()))
    except (model_module.ModelUnavailable, TimeoutError, httpx.HTTPError,
            ValueError, TypeError, AttributeError, ValidationError):
        return None


async def review(answer: dict, question: str) -> dict:
    """Run both layers and attach the outcome.

    The answer is annotated, never altered.
    """
    # L0 is pure local computation over an already-built dict — microseconds,
    # no I/O — so running it inline costs nothing and keeps the two layers
    # independent: a broken L1 cannot prevent the deterministic checks.
    local = check_local(answer)
    try:
        semantic = await review_model(answer, question)
    except Exception:
        # Any failure of the semantic layer is a pass, by design.
        semantic = None

    # The blocking policy is ours, not the reviewer's: a finding stops the
    # answer only when it is classed "block" by a layer that is allowed to
    # class it that way. Everything else travels to the customer as advice.
    blocking = local.blocking + (semantic.blocking if semantic is not None else [])
    advice = [f for f in local.findings + (semantic.findings if semantic is not None else [])
              if f.severity != "block"][:3]
    answer["compliance"] = {
        "ok": not blocking,
        "layers": ["local"] + (["model"] if semantic is not None else []),
        "findings": [f.model_dump() for f in (blocking or advice)],
        "summary": local.summary if semantic is None else (semantic.summary or local.summary),
    }
    return answer


__all__ = ["Finding", "Verdict", "check_local", "review", "review_model"]
