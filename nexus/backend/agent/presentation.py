"""How the answer should be laid out, decided after the data exists.

Retrieval answers "what happened". It does not answer "how should this be shown",
and that second question is a real one: asking about income and receiving a
spending breakdown first is a correct card answering the wrong question.

So the layout gets its own pass, between the verified data and the render. The
model is given the *shape* of the answer — which sections exist and how big they
are — plus the customer's own question, and returns an ordering, what to fold
away, and one sentence saying what should hit the eye first.

What it never gets: the numbers. Amounts, balances, payees and dates stay on
this side of the boundary. The model rearranges blocks that are already bound to
verified data; it cannot read the data, and it cannot introduce any.

Layer contract:
  owns      — block ordering, folding, emphasis, and the stated reason
  does NOT own — the data, the renderer's markup, or the fallback layout
"""
from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import BaseModel, Field, ValidationError

from ..core.config import settings
from .integrations import model as model_module

MAX_BLOCKS = 8


class LayoutPlan(BaseModel):
    """A closed schema: an ordering, a fold list, and the reason for both."""

    order: list[str] = Field(default_factory=list, max_length=MAX_BLOCKS)
    fold: list[str] = Field(default_factory=list, max_length=MAX_BLOCKS)
    emphasis: str = Field(default="", max_length=120)
    rationale: str = Field(default="", max_length=200)
    source: str = "fallback"


# 答案类型 → 可用区块。每种回答能排的只有它自己有的东西，模型不能凭空加块。
BLOCKS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "account_snapshot": ("hero", "comparison", "transactions", "cards", "subscriptions", "accounts"),
    "bill_analysis": ("hero", "summary", "chart", "categories", "ranking",
                      "anomalies", "comparison", "insights"),
    "risk_report": ("hero", "prudence", "checklist", "warnings", "allocation", "advice"),
    "risk_intake": ("preview", "questions", "submit"),
    "recurring_detection": ("head", "items"),
    "product_catalog": ("head", "chart", "grid", "orders"),
    "financial_analysis": ("head", "metrics", "balance_sheet", "scoring", "decision", "products"),
    "scheduled_transfer_list": ("head", "rows"),
    "subscription_list": ("head", "rows"),
    "card_list": ("head", "rows"),
    "aa_collection_list": ("head", "rows"),
    "external_data": ("head", "quote", "provenance"),
    "analysis": ("head", "plan", "evidence", "decision"),
    "table": ("head", "rows"),
    "chart": ("head", "chart", "insight"),
    "plan": ("head", "plan", "steps"),
}

# 每类回答的默认排版。模型不可用、返回垃圾或超时，就用这套。
DEFAULT_ORDER: dict[str, tuple[str, ...]] = {
    "account_snapshot": ("hero", "comparison", "transactions", "cards", "subscriptions"),
    "bill_analysis": ("hero", "summary", "chart", "categories", "anomalies", "insights"),
    "risk_report": ("hero", "prudence", "checklist", "warnings", "allocation", "advice"),
    "risk_intake": ("preview", "questions", "submit"),
    "recurring_detection": ("head", "items"),
    "product_catalog": ("head", "chart", "grid", "orders"),
}


def allowed_blocks(answer: dict) -> tuple[str, ...]:
    if answer.get("empty"):
        return ("head",)
    return BLOCKS_BY_TYPE.get(answer.get("type", ""), ("head",))


def _shape_of(answer: dict, blocks: tuple[str, ...]) -> dict:
    """Structural digest handed to the model — sizes and field names, never values.

    Telling the model there are two cards with a status field is enough for it
    to decide a layout. Telling it the card status would be handing over the
    customer's account data to get a cosmetic opinion.
    """
    size_of = {
        "transactions": len(answer.get("transactions") or []),
        "cards": len(answer.get("cards") or []),
        "subscriptions": len(answer.get("subscriptions") or []),
        "accounts": len(answer.get("accounts") or []),
        "categories": len(answer.get("categories") or []),
        "anomalies": len(answer.get("anomalies") or []),
        "checklist": len(answer.get("checklist") or []),
        "warnings": len(answer.get("warnings") or []),
        "allocation": len(answer.get("allocation") or []),
        "products": len(answer.get("products") or []),
        "orders": len(answer.get("orders") or []),
        "questions": len(answer.get("questions") or []),
        "items": len(answer.get("items") or []),
        "rows": len(answer.get("rows") or answer.get("items") or answer.get("plans") or []),
    }
    digest = {name: size_of[name] for name in blocks if name in size_of}
    digest["hero"] = {"label": (answer.get("hero") or {}).get("label", "")}
    digest["chart"] = bool(answer.get("chart"))
    return digest


def fallback(answer: dict) -> dict:
    """The deterministic layout: the asked-for number first, everything else after."""
    blocks = allowed_blocks(answer)
    default = DEFAULT_ORDER.get(answer.get("type", ""), blocks)
    order = [name for name in default if name in blocks]
    order += [name for name in blocks if name not in order]
    return {
        "order": order,
        "fold": [],
        "emphasis": (answer.get("hero") or {}).get("label", "") or answer.get("title", ""),
        "rationale": "按“用户问的那个数在最上面”的默认排版",
        "source": "fallback",
    }


async def plan(answer: dict, question: str) -> dict:
    """Decide how this answer should read. Never fails: falls back instead."""
    blocks = allowed_blocks(answer)
    base = fallback(answer)
    if not model_module.is_configured() or len(blocks) < 2:
        return base
    digest = _shape_of(answer, blocks)
    schema = json.dumps(LayoutPlan.model_json_schema(), ensure_ascii=False)
    system = f"""你是金融回答的排版决策器。用户已经问完问题，数据也已经取到了；你只决定这些数据怎么呈现。

可用区块：{', '.join(blocks)}
每个区块的规模：{json.dumps(digest, ensure_ascii=False)}

规则：
1. order 是区块显示顺序，把最能回答用户这次提问的那个区块放在第一位。
2. fold 是次要区块，前端会折叠起来；最多 3 个，不要折叠与问题直接相关的区块。
3. emphasis 是一句话，不超过 25 字，说明这条回答最该让人先看到什么。
4. rationale 是一句话，不超过 35 字，说明为什么这么排。
5. 你看不到任何金额、姓名或明细，禁止编造或推测具体数字，只谈结构。
6. emphasis 和 rationale 是给客户看的，不要出现 hero、summary、chart 这类区块代号，用中文说明内容本身。

只输出一个 JSON 对象，不要输出任何其他文字、不要用 markdown 代码块。
必须严格符合以下 JSON Schema：
{schema}"""
    payload = {
        "model": settings.llm_model, "max_tokens": 600,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}],
    }
    try:
        async with asyncio.timeout(min(settings.llm_timeout, 6)):
            async with httpx.AsyncClient(timeout=settings.llm_timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.post(model_module.endpoint(), headers=model_module._headers(), json=payload)
        if response.status_code != 200:
            return base
        choice = LayoutPlan.model_validate(model_module._json_object(response.json()))
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, ValidationError, AttributeError):
        return base

    order = [name for name in dict.fromkeys(choice.order) if name in blocks]
    if not order:
        return base
    # Anything the model forgot still has to render; append it rather than drop it.
    order += [name for name in blocks if name not in order]
    fold = [name for name in dict.fromkeys(choice.fold) if name in blocks and name != order[0]]
    return {
        "order": order[:MAX_BLOCKS],
        "fold": fold[:3],
        "emphasis": choice.emphasis or base["emphasis"],
        "rationale": choice.rationale or base["rationale"],
        "source": "model",
    }


async def apply(answer: dict, question: str) -> dict:
    """Decide the layout and attach it. Safe to call on any answer."""
    if answer.get("presentation"):
        # A replayed turn already carries the layout it was answered with;
        # re-deciding it would make the same turn look like two decisions.
        return answer
    try:
        answer["presentation"] = await plan(answer, question)
    except Exception:
        answer["presentation"] = fallback(answer)
    return answer


__all__ = ["LayoutPlan", "BLOCKS_BY_TYPE", "allowed_blocks", "fallback", "plan", "apply"]
