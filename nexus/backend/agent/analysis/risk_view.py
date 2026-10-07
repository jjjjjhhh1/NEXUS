"""Risk assessment: run the model, keep the record, and say what it means.

This is the layer between the scoring rules in :mod:`agent.risk_assessment` and
the screen. It supplies the numbers from records that already exist, runs both
halves of the model, stores the result as its own dated row, and writes the
resulting grade back onto the user so product suitability checks elsewhere read
one consistent answer.

A grade is never inferred from a conversation. It comes from the recorded
financial profile plus the customer's own four answers, and it expires.

Layer contract:
  owns      — assembling the inputs, persisting the assessment, applying it
  does NOT own — the scoring rules, the questionnaire wording, or the UI
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from ...core.models import Account, FinancialProfile, FinancialSnapshot, RiskAssessment, User
from . import risk_assessment as rules

ZERO = Decimal("0")


async def _load_inputs(session, user_id: int) -> dict | None:
    """Every objective input, taken from records rather than from the message."""
    profile = await session.scalar(select(FinancialProfile).where(FinancialProfile.user_id == user_id))
    if not profile:
        return None
    snapshot = await session.scalar(select(FinancialSnapshot).where(FinancialSnapshot.user_id == user_id))
    accounts = list((await session.scalars(
        select(Account).where(Account.user_id == user_id)
    )).all())

    # seasonal_income / seasonal_expenses 存在 JSON 列里，读回来是字符串（测试
    # 同一会话内拿到的是内存里的对象，所以只有进程重启后才会暴露）。档案字段则
    # 是 Decimal。统一成 Decimal 再算，否则 "5000" + Decimal 会在算安全垫时炸掉。
    def as_decimals(values, fallback: Decimal) -> list[Decimal]:
        padded = (list(values or []) or [fallback] * 12)[:12]
        padded = (padded + [fallback] * 12)[:12]
        return [Decimal(str(value)) for value in padded]

    seasonal_income = as_decimals(
        snapshot.seasonal_income if snapshot else None, Decimal(str(profile.monthly_income)))
    seasonal_expenses = as_decimals(
        snapshot.seasonal_expenses if snapshot else None, Decimal(str(profile.essential_expenses)))

    account_liquid = sum((row.available_balance for row in accounts), ZERO)
    annual_income = Decimal(str(snapshot.annual_income)) if snapshot and snapshot.annual_income else sum(seasonal_income, ZERO)
    annual_expenses = Decimal(str(snapshot.annual_expenses)) if snapshot and snapshot.annual_expenses else sum(seasonal_expenses, ZERO)
    liquid_assets = account_liquid + (Decimal(str(snapshot.liquid_savings)) if snapshot else ZERO)
    investments = Decimal(str(snapshot.investment_assets)) if snapshot else ZERO
    # 自报总资产优先：房贷客户的负债对应的是自住房，不算进来就会把净资产算成 0。
    declared = Decimal(str(snapshot.declared_assets)) if snapshot else ZERO
    total_assets = declared if declared > 0 else liquid_assets + investments
    debt = Decimal(str(profile.debt_balance))
    # 刚性支出是月度口径；年度口径按 12 个月折算，避免把一次性支出算进安全垫。
    essential = Decimal(str(profile.essential_expenses))
    debt_payment = Decimal(str(profile.monthly_debt_payment))
    monthly_net = [
        seasonal_income[index] - (seasonal_expenses[index] + debt_payment)
        for index in range(12)
    ]
    return {
        "annual_income": annual_income,
        "annual_expenses": annual_expenses,
        "monthly_income": Decimal(str(profile.monthly_income)),
        "essential_expenses": essential,
        "monthly_debt_payment": debt_payment,
        "liquid_assets": liquid_assets,
        "total_assets": total_assets,
        "total_debt": debt,
        "monthly_net": monthly_net,
    }


async def current(session, user_id: int) -> RiskAssessment | None:
    """The newest assessment that is still inside its validity window."""
    return await session.scalar(
        select(RiskAssessment)
        .where(
            RiskAssessment.user_id == user_id,
            RiskAssessment.status == "ACTIVE",
            (RiskAssessment.valid_until.is_(None)) | (RiskAssessment.valid_until >= date.today()),
        )
        .order_by(RiskAssessment.id.desc())
        .limit(1)
    )


async def questionnaire(session, user_id: int) -> dict | None:
    """The four stated questions, prefilled from what is already known.

    ``None`` means there is no financial profile yet — the objective half has
    nothing to read, so there is no grade to move towards. Callers render
    :func:`needs_profile_message` in that case.
    """
    inputs = await _load_inputs(session, user_id)
    if inputs is None:
        return None
    return {
        "type": "risk_intake",
        "engine": "analysis",
        "title": "风险测评",
        "message": "监管要求风险等级必须同时包含客观财务承受能力和客户自述意愿，因此需要你回答四个问题。客观部分我们已经从你的财务档案里取到了。",
        "objective_preview": _objective_preview(inputs),
        "questions": [
            {
                "name": question.name, "prompt": question.prompt, "hint": question.hint,
                "options": [{"label": name, "hint": hint} for name, hint, _ in question.options],
            }
            for question in rules.SUBJECTIVE_QUESTIONS
        ],
    }


def needs_profile_message() -> dict:
    """What to say when there is no financial profile to read the objective half from.

    Shared by the chat node and the REST endpoint so the customer is told the
    same thing whichever door they came through.
    """
    return {
        "type": "message", "engine": "analysis",
        "message": "风险测评需要先补齐财务档案（收入、支出、负债与流动资产），客观部分才有依据。",
        "category": "slot_request", "needs_input": True,
    }


def _objective_preview(inputs: dict) -> dict:
    """Show the customer the half we already hold, before they answer."""
    objective = rules.score_objective(**inputs)
    return {
        "score": objective["score"],
        "grade": rules.grade_of(objective["score"])[0],
        "grade_label": rules.grade_of(objective["score"])[1],
        "metrics": objective["metrics"],
    }


async def submit(session, user_id: int, answers: dict) -> dict:
    """Score both halves, store the record, and apply the grade.

    Raises ``ValueError`` when a question is unanswered — a half-finished
    assessment must not be able to produce a grade, because the missing answer
    is exactly the subjective half the prudence rule depends on.
    """
    inputs = await _load_inputs(session, user_id)
    if inputs is None:
        raise ValueError("请先补齐财务档案，再完成风险测评")

    subjective = rules.score_subjective(answers)
    if subjective["missing"]:
        raise ValueError("还有问题没有回答：" + "、".join(subjective["missing"]))

    objective = rules.score_objective(**inputs)
    merged = rules.combine(objective["score"], subjective["score"])
    grade = merged["grade"]

    record = RiskAssessment(
        user_id=user_id, status="ACTIVE",
        objective_score=Decimal(str(objective["score"])),
        subjective_score=Decimal(str(subjective["score"])),
        final_score=Decimal(str(merged["final"])),
        objective_grade=merged["objective_grade"],
        subjective_grade=merged["subjective_grade"],
        grade=grade, binding=merged["binding"],
        objective_breakdown={"components": objective["components"], "metrics": objective["metrics"]},
        subjective_answers={
            "answers": {question.name: answers.get(question.name) for question in rules.SUBJECTIVE_QUESTIONS},
            "components": subjective["components"],
        },
        diagnosis={"warnings": rules.warnings(objective, grade)},
        valid_until=rules.expiry(),
    )
    session.add(record)
    # 等级是对外承诺，必须落到用户档案上；产品适配校验读的是同一个值。
    user = await session.get(User, user_id)
    if user:
        user.risk_score = grade
        user.investment_style = rules.grade_of(merged["final"])[1]
    await session.flush()
    return report(record, objective=objective, subjective=subjective, merged=merged)


def report(record: RiskAssessment, *, objective: dict | None = None,
           subjective: dict | None = None, merged: dict | None = None) -> dict:
    """The 体检报告: grade, the two halves, the diagnosis, the plan, the taboos."""
    breakdown = record.objective_breakdown or {}
    objective = objective or {
        "score": float(record.objective_score),
        "components": breakdown.get("components", []),
        "metrics": breakdown.get("metrics", {}),
    }
    stored = record.subjective_answers or {}
    subjective = subjective or {
        "score": float(record.subjective_score),
        "components": stored.get("components", []),
    }
    merged = merged or {
        "blended": float((float(record.objective_score) + float(record.subjective_score)) / 2),
        "final": float(record.final_score),
        "grade": record.grade,
        "label": rules.grade_of(record.final_score)[1],
        "objective_grade": record.objective_grade,
        "subjective_grade": record.subjective_grade,
        "binding": record.binding,
        "downgraded": float(record.final_score) < (float(record.objective_score) + float(record.subjective_score)) / 2,
        "binding_reason": rules._binding_reason(record.binding, record.objective_grade, record.subjective_grade),
    }
    grade = record.grade
    advice = rules.GRADE_ADVICE[grade]
    diagnosis = record.diagnosis or {}
    return {
        "type": "risk_report",
        "engine": "analysis",
        "title": "风险测评报告",
        "as_of": (record.created_at or datetime.now()).date().isoformat(),
        "valid_until": record.valid_until.isoformat() if record.valid_until else None,
        "score": merged["final"],
        "grade": grade,
        "grade_label": merged["label"],
        "max_product_risk": rules.GRADE_MAX_RISK[grade],
        "verdict": rules.verdict(grade, record.binding),
        "prudence": {
            "binding": record.binding,
            "binding_reason": merged["binding_reason"],
            "objective_grade": record.objective_grade,
            "subjective_grade": record.subjective_grade,
            "blended": merged["blended"],
            "downgraded": merged["downgraded"],
        },
        "objective": {"score": objective["score"], "grade": record.objective_grade, "components": objective["components"]},
        "subjective": {"score": subjective["score"], "grade": record.subjective_grade, "components": subjective["components"]},
        "checklist": _checklist(objective),
        "warnings": diagnosis.get("warnings", []),
        "allocation": rules.allocation(grade, objective),
        "advice": advice,
        "trace": [
            {"label": "读取财务档案", "detail": "收入、支出、负债、流动资产与 12 个月收支明细", "status": "done"},
            {"label": "客观承受能力", "detail": f"财务 BMI {objective['score']:.1f} 分 → {record.objective_grade}", "status": "done"},
            {"label": "主观风险偏好", "detail": f"问卷 {subjective['score']:.1f} 分 → {record.subjective_grade}", "status": "done"},
            {"label": "审慎原则", "detail": merged["binding_reason"], "status": "done"},
        ],
    }


def _checklist(objective: dict) -> list[dict]:
    """分项体检报告：每一项都带数值、评价和它是怎么算出来的。"""
    out = []
    for component in objective.get("components", []):
        out.append({
            "name": component["name"],
            "value": component["value"],
            "rating": component["rating"],
            "score": component["score"],
            "max": component["max"],
            "basis": component["basis"],
        })
    return out


__all__ = ["current", "questionnaire", "needs_profile_message", "submit", "report"]
