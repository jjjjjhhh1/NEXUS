"""Turn 5-star answers into a document you can polish against.

A score alone is not a benchmark. What is worth reusing later is the pairing:
the question that was asked, the answer type it produced, the layout the model
chose, and the reason it gave for that layout. Four or five star answers are
where the layout layer is working, so they are what gets written down.

Run:
    PYTHONPATH=<repo> python -m nexus.scripts.export_benchmarks
    PYTHONPATH=<repo> python -m nexus.scripts.export_benchmarks --min-stars 4 -o docs/x.md
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

from sqlalchemy import select

from ..backend.core import database
from ..backend.core.models import AgentTurn, AnswerRating

# Which answers are worth looking at again, per type. Not a filter on the score —
# a note on what a high score of this type is actually asserting.
WHAT_IT_MEANS = {
    "account_snapshot": "问一个数，先把那个数放到最大，其余退后",
    "bill_analysis": "问一个数，先把那个数放到最大，结构退后",
    "risk_report": "先给结论与体检表，再给禁忌",
    "financial_analysis": "先给可行性，再给配置",
    "product_catalog": "先讲能不能买，再讲产品细节",
    "recurring_detection": "先讲识别结论与总额，再列每一笔",
}


async def collect(min_stars: int) -> list[dict]:
    async with database.session_scope() as session:
        ratings = (await session.scalars(
            select(AnswerRating)
            .where(AnswerRating.stars >= min_stars)
            .order_by(AnswerRating.stars.desc(), AnswerRating.id.desc())
        )).all()
        rows: list[dict] = []
        for rating in ratings:
            turn = await session.scalar(
                select(AgentTurn).where(
                    AgentTurn.session_id == rating.session_id,
                    AgentTurn.request_id == rating.request_id,
                )
            )
            if turn is None:
                # The turn was pruned but the score survived; a score with no
                # question attached is not reproducible, so it is not a benchmark.
                continue
            response = turn.response or {}
            layout = rating.layout or response.get("presentation") or {}
            hero = response.get("hero") or {}
            rows.append({
                "stars": rating.stars,
                "answer_type": rating.answer_type,
                "question": response.get("question") or response.get("message") or "",
                "headline": hero.get("value") or response.get("title") or "",
                "headline_label": hero.get("label") or "",
                "order": layout.get("order") or [],
                "fold": layout.get("fold") or [],
                "emphasis": layout.get("emphasis") or "",
                "rationale": layout.get("rationale") or "",
                "source": layout.get("source") or "",
                "created_at": rating.created_at.strftime("%Y-%m-%d %H:%M") if rating.created_at else "",
            })
        return rows


def render(rows: list[dict], min_stars: int) -> str:
    lines = [
        "# 高分回答基准",
        "",
        f"来自 {min_stars} 星及以上的回答。每一行是一组可复现的搭配：问了什么、排成了什么样、为什么这么排。",
        "整体打磨时对着这张表改，不要凭印象调。",
        "",
    ]
    if not rows:
        lines += ["_还没有达到这个分数的回答。先在页面上给几条回答打分，再重新导出。_", ""]
        return "\n".join(lines)

    by_type: dict[str, list[dict]] = {}
    for row in rows:
        by_type.setdefault(row["answer_type"], []).append(row)

    lines += [
        "## 概览",
        "",
        "| 回答类型 | 条数 | 最高分 | 这类高分说明什么 |",
        "| --- | ---: | ---: | --- |",
    ]
    for answer_type, group in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        best = max(row["stars"] for row in group)
        note = WHAT_IT_MEANS.get(answer_type, "—")
        lines.append(f"| `{answer_type}` | {len(group)} | {best} ★ | {note} |")
    lines.append("")

    for answer_type, group in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        lines += [f"## `{answer_type}`", ""]
        for row in group:
            if row["headline"]:
                headline = f"{row['headline_label']} **{row['headline']}**".strip()
            else:
                headline = "—"
            lines += [
                f"### {row['stars']} ★ · {row['question'] or '（问题未记录）'}",
                "",
                f"- 打在最前面的：{headline}",
                f"- 区块顺序：`{' → '.join(row['order']) or '—'}`",
                f"- 折叠：`{'、'.join(row['fold']) or '无'}`",
                f"- AI 的侧重：{row['emphasis'] or '—'}",
                f"- AI 给的理由：{row['rationale'] or '—'}",
                f"- 排版来源：`{row['source'] or '—'}` · {row['created_at']}",
                "",
            ]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-stars", type=int, default=5)
    parser.add_argument("-o", "--out", default="docs/answer-benchmarks.md")
    args = parser.parse_args()

    await database.init_db()
    try:
        rows = await collect(args.min_stars)
    finally:
        await database.close_db()

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, args.min_stars), encoding="utf-8")
    print(f"{len(rows)} 条 ≥{args.min_stars} 星回答 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
