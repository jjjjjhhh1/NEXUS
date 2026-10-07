"""Investor risk assessment: what the customer can bear, and what they will accept.

Regulators do not accept one number here. A suitability grade has to stand on
two independent legs, because either one alone can be wrong in a way that
sells the wrong product:

* **Objective capacity** — the hard numbers. Savings rate, how many months of
  essentials the liquid assets cover, debt service against income, how much the
  income swings across the year, and how thick the balance sheet is. This is a
  *financial BMI*. It is computed, not asked.
* **Stated appetite** — what the customer says they will tolerate. Nobody can
  derive willingness to lose money from a bank statement, and a customer who
  cannot sleep after a 5% drawdown must not be sold a product that routinely
  delivers one, however sound their finances.

The two are scored separately, then combined 50/50 — and then the **prudence
rule** applies: if the grades disagree, the *more conservative* one wins. A
financially strong customer who will only accept a 5% loss is a conservative
customer, and that is what the file has to say.

Every score here is derived from records the user already filled in. The
questionnaire only supplies the subjective half, because that is the half that
genuinely cannot be measured from a balance sheet.

Layer contract:
  owns      — the scoring rules, the grade, and the written diagnosis
  does NOT own — where the numbers come from (read_views / financial_analysis),
                   the suitability mapping (product_service), or the UI
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
# A risk grade is a dated opinion. Twelve months is the usual ceiling before
# the customer has to be asked again.
VALID_MONTHS = 12

# 证监会 C1–C5：分数越高，承受能力越强。
GRADE_BANDS = ((20, "C1", "保守型"), (40, "C2", "稳健型"), (60, "C3", "平衡型"),
               (80, "C4", "成长型"), (101, "C5", "进取型"))
GRADE_RISK = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}
GRADE_MAX_RISK = {"C1": "R1", "C2": "R2", "C3": "R3", "C4": "R4", "C5": "R5"}
GRADE_SUMMARY = {
    "C1": "财务缓冲薄，只能承受本金损失",
    "C2": "可承受极小波动，不宜亏损本金",
    "C3": "可接受小幅回撤，适合中长期配置",
    "C4": "能承受中等亏损，具备一定抗波动能力",
    "C5": "财务底子厚，可承受大幅波动",
}
GRADE_ADVICE = {
    "C1": {"label": "适配 R1", "detail": "活期、存款、国债、货币基金等本金安全型工具", "forbid": "任何净值会波动的产品"},
    "C2": {"label": "适配 R2", "detail": "存款类、短债与低风险固收类理财", "forbid": "股票、权益基金、任何非保本浮动收益产品"},
    "C3": {"label": "适配 R3", "detail": "中低波动固收+、混合基金、分级理财的稳健档", "forbid": "高杠杆、复杂衍生品、单一行业集中持仓"},
    "C4": {"label": "适配 R4", "detail": "宽基指数、偏股基金与成长型资产配置", "forbid": "非标资产、场外配资与看不懂的结构"},
    "C5": {"label": "适配 R5", "detail": "在 R4 基础上可配置权益与合规衍生品", "forbid": "任何以保本为名的高杠杆结构"},
}


def _q(value) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _bounded(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, Decimal("0")), high)


def grade_of(score: float | Decimal) -> tuple[str, str]:
    """C1–C5 band for a 0–100 score. Returns ``(code, label)``."""
    value = float(score)
    for ceiling, code, label in GRADE_BANDS:
        if value < ceiling:
            return code, label
    return "C5", "进取型"


def band_max(grade: str) -> Decimal:
    for ceiling, code, _ in GRADE_BANDS:
        if code == grade:
            return Decimal(str(ceiling - 1))
    return Decimal("100")


# ============ 主观问卷 ============
@dataclass
class Question:
    name: str
    prompt: str
    hint: str
    max_score: float
    options: list[tuple[str, str, float]] = field(default_factory=list)


# 亏损容忍度权重最高：它不是偏好，是客户在真跌到那个幅度时还会不会继续持有
# 计划的前提。其余三题决定同样一笔钱能不能等得起。
SUBJECTIVE_QUESTIONS = [
    Question("experience", "你的投资经验", "从第一次买入到现在的实际时长", 20, [
        ("无经验", "没有独立投资经历", 0.0),
        ("1年以内", "不满一年", 7.0),
        ("1-3年", "一到三年", 14.0),
        ("3年以上", "三年以上", 20.0),
    ]),
    Question("max_loss", "你能接受的最大亏损", "以投入本金为基准，指可承受的最大回撤", 35, [
        ("不能亏", "本金一分都不能少", 0.0),
        ("5%以内", "小幅回撤可以接受", 12.0),
        ("15%以内", "中等波动可接受", 25.0),
        ("30%以上", "大幅回撤也能承受", 35.0),
    ]),
    Question("horizon", "这笔钱多久不用", "决定能否承担净值波动而不被迫赎回", 30, [
        ("半年内", "随时可能要用", 0.0),
        ("1-3年", "中期", 13.0),
        ("3-5年", "较长", 23.0),
        ("5年以上", "长期不动", 30.0),
    ]),
    Question("purpose", "这笔钱的主要目的", "用途越明确，越不该承担波动", 15, [
        ("保本为主", "首要是不亏", 0.0),
        ("稳健增值", "跑赢活期即可", 9.0),
        ("长期增值", "接受阶段性波动", 15.0),
    ]),
]
SUBJECTIVE_BY_NAME = {item.name: item for item in SUBJECTIVE_QUESTIONS}
SUBJECTIVE_MAX = sum(item.max_score for item in SUBJECTIVE_QUESTIONS)


def score_subjective(answers: dict) -> dict:
    """0–100 from the four stated answers, plus what each one contributed."""
    components, total, missing = [], Decimal("0"), []
    for question in SUBJECTIVE_QUESTIONS:
        chosen = answers.get(question.name)
        label, score = next(((name, value) for name, _, value in question.options if name == chosen), (None, None))
        if score is None:
            missing.append(question.prompt)
            continue
        total += Decimal(str(score))
        components.append({
            "name": question.prompt, "answer": label, "hint": question.hint,
            "score": float(score), "max": question.max_score,
        })
    score = float((total / Decimal("100") * 100).quantize(CENT))
    return {"score": score, "components": components, "missing": missing, "raw": float(total)}


# ============ 客观财务 BMI ============
def score_objective(
    *,
    annual_income: Decimal,
    annual_expenses: Decimal,
    monthly_income: Decimal,
    essential_expenses: Decimal,
    monthly_debt_payment: Decimal,
    liquid_assets: Decimal,
    total_assets: Decimal,
    total_debt: Decimal,
    monthly_net: list[Decimal],
) -> dict:
    """0–100 from the recorded numbers alone.

    Weights follow how hard each factor is to fix: savings rate and the
    emergency cushion move slowly, income volatility and balance-sheet depth
    move faster, and debt service can move tomorrow.
    """
    components: list[dict] = []
    income = max(annual_income, Decimal("0"))
    expenses = max(annual_expenses, Decimal("0"))
    surplus = income - expenses
    savings_rate = (surplus / income * 100) if income > 0 else Decimal("0")
    # 25分：结余率 30% 拿满，10% 归零
    savings_score = _bounded(savings_rate / Decimal("30") * Decimal("25"), Decimal("0"), Decimal("25"))

    core = max(essential_expenses + monthly_debt_payment, Decimal("1"))
    safety_months = liquid_assets / core
    # 25分：安全垫 12 个月拿满，1 个月归零
    safety_score = _bounded(safety_months / Decimal("12") * Decimal("25"), Decimal("0"), Decimal("25"))

    monthly_income_pos = max(monthly_income, Decimal("0"))
    debt_ratio = (monthly_debt_payment / monthly_income_pos * 100) if monthly_income_pos > 0 else Decimal("0")
    # 20分：负债率 ≤10% 拿满，≥50% 归零
    debt_score = _bounded((Decimal("50") - min(debt_ratio, Decimal("50"))) / Decimal("40") * Decimal("20"), Decimal("0"), Decimal("20"))

    # 15分：12 个月净结余的变异系数。σ=0 拿满，σ≥0.6 归零
    volatility = _coefficient_of_variation(monthly_net)
    volatility_score = _bounded((Decimal("0.6") - min(volatility, Decimal("0.6"))) / Decimal("0.6") * Decimal("15"), Decimal("0"), Decimal("15"))

    net_worth = max(total_assets - total_debt, Decimal("0"))
    essential_year = max(core * Decimal("12"), Decimal("1"))
    # 15分：净资产覆盖 3 年必要支出拿满
    thickness = net_worth / essential_year
    thickness_score = _bounded(thickness / Decimal("3") * Decimal("15"), Decimal("0"), Decimal("15"))

    total = savings_score + safety_score + debt_score + volatility_score + thickness_score
    components = [
        {"name": "年度结余率", "score": float(savings_score.quantize(CENT)), "max": 25,
         "value": f"{savings_rate.quantize(CENT)}%", "rating": _rate_savings(savings_rate),
         "basis": "（全年收入 − 全年支出）÷ 全年收入"},
        {"name": "现金流安全垫", "score": float(safety_score.quantize(CENT)), "max": 25,
         "value": f"{safety_months.quantize(CENT)} 个月", "rating": _rate_safety(safety_months),
         "basis": "流动资产 ÷ 每月刚性支出"},
        {"name": "负债收入比", "score": float(debt_score.quantize(CENT)), "max": 20,
         "value": f"{debt_ratio.quantize(CENT)}%", "rating": _rate_debt(debt_ratio),
         "basis": "每月还款 ÷ 每月收入"},
        {"name": "收入稳定性", "score": float(volatility_score.quantize(CENT)), "max": 15,
         "value": f"σ={volatility.quantize(Decimal('0.001'))}", "rating": _rate_volatility(volatility),
         "basis": "12 个月净结余的变异系数"},
        {"name": "资产厚度", "score": float(thickness_score.quantize(CENT)), "max": 15,
         "value": f"{thickness.quantize(CENT)} 年", "rating": _rate_thickness(thickness),
         "basis": "净资产 ÷ 每年刚性支出"},
    ]
    return {
        "score": float(total.quantize(CENT)),
        "components": components,
        "metrics": {
            "savings_rate_pct": float(savings_rate.quantize(CENT)),
            "safety_months": float(safety_months.quantize(CENT)),
            "debt_ratio_pct": float(debt_ratio.quantize(CENT)),
            "volatility": float(volatility.quantize(Decimal("0.001"))),
            "net_worth": float(net_worth.quantize(CENT)),
        },
    }


def _coefficient_of_variation(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    from statistics import pstdev
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    if mean == 0:
        return Decimal("1")
    sigma = Decimal(str(pstdev([float(value) for value in values]))) / abs(mean)
    return min(sigma, Decimal("1"))


def _rate_savings(rate: Decimal) -> str:
    return "优秀" if rate >= 30 else "良好" if rate >= 15 else "一般" if rate >= 5 else "不足"


def _rate_safety(months: Decimal) -> str:
    return "应急资金充足" if months >= 6 else "偏薄" if months >= 3 else "应急资金短缺"


def _rate_debt(ratio: Decimal) -> str:
    return "负债压力很小" if ratio <= 15 else "压力可控" if ratio <= 30 else "偏高" if ratio <= 45 else "偏高，需优先处理"


def _rate_volatility(sigma: Decimal) -> str:
    return "收入稳定" if sigma <= 0.15 else "有季节性波动" if sigma <= 0.35 else "波动较大，淡季需留意"


def _rate_thickness(years: Decimal) -> str:
    return "资产厚实" if years >= 2 else "有一定积累" if years >= 1 else "积累不足"


# ============ 合并与审慎原则 ============
def combine(objective_score: float, subjective_score: float) -> dict:
    """50/50 blend, then the prudence rule decides the grade.

    The reported score is pulled down to the top of the binding grade's band
    when the two disagree, so the number and the letter can never contradict
    each other on the same page.
    """
    blended = (objective_score + subjective_score) / 2
    objective_grade = grade_of(objective_score)[0]
    subjective_grade = grade_of(subjective_score)[0]
    objective_risk, subjective_risk = GRADE_RISK[objective_grade], GRADE_RISK[subjective_grade]
    binding = "MATCH"
    if subjective_risk < objective_risk:
        grade, binding = subjective_grade, "SUBJECTIVE"
    elif objective_risk < subjective_risk:
        grade, binding = objective_grade, "OBJECTIVE"
    else:
        grade = objective_grade
    capped = min(blended, float(band_max(grade)))
    final = round(capped, 2)
    return {
        "blended": round(blended, 2),
        "final": final,
        "grade": grade,
        "label": grade_of(final)[1],
        "objective_grade": objective_grade,
        "subjective_grade": subjective_grade,
        "binding": binding,
        "downgraded": final < blended,
        "binding_reason": _binding_reason(binding, objective_grade, subjective_grade),
    }


def _binding_reason(binding: str, objective_grade: str, subjective_grade: str) -> str:
    if binding == "OBJECTIVE":
        return f"你的财务承受能力为 {objective_grade}，低于自己填写的 {subjective_grade}；按审慎原则取 {objective_grade}。"
    if binding == "SUBJECTIVE":
        return f"你能接受的最大亏损只支持 {subjective_grade}，低于财务承受的 {objective_grade}；按审慎原则取 {subjective_grade}。"
    return f"客观与主观结果一致，均为 {objective_grade}。"


# ============ 输出：体检报告 / 配置 / 禁忌 ============
def allocation(grade: str, objective: dict) -> list[dict]:
    """Bucket percentages, capped by what the emergency cushion can spare."""
    safety_months = objective["metrics"]["safety_months"]
    # 安全垫不足时，配置比例整体后移：先补现金，再谈配置。
    liquidity_weight = 55 if safety_months < 3 else 45 if safety_months < 6 else 35
    fixed = 100 - liquidity_weight
    if grade in {"C1", "C2"}:
        buckets = [("现金与存款", liquidity_weight), ("低风险固收", fixed)]
    elif grade == "C3":
        buckets = [("现金与货币基金", liquidity_weight), ("固收类", fixed - 10), ("权益类", 10)]
    elif grade == "C4":
        buckets = [("现金与货币基金", liquidity_weight), ("固收类", fixed - 25), ("权益类", 25)]
    else:
        buckets = [("现金与货币基金", liquidity_weight), ("固收类", fixed - 40), ("权益类", 40)]
    return [
        {"name": name, "weight": weight, "max_risk": GRADE_MAX_RISK[grade] if name != "现金与存款" and name != "现金与货币基金" else "R1"}
        for name, weight in buckets
    ]


def warnings(objective: dict, grade: str) -> list[dict]:
    """The things a health report has to say out loud, not bury."""
    metrics = objective["metrics"]
    items: list[dict] = []
    if metrics["savings_rate_pct"] < 10:
        items.append({"level": "warn", "title": "结余率偏低",
                      "detail": f"全年结余率 {metrics['savings_rate_pct']}%，可用于长期配置的资金有限，建议先提高储蓄比例。"})
    if metrics["safety_months"] < 3:
        items.append({"level": "danger", "title": "应急资金短缺",
                      "detail": f"流动资产仅覆盖 {metrics['safety_months']} 个月刚性支出，一次意外支出就可能被迫赎回投资。"})
    if metrics["volatility"] > 0.35:
        items.append({"level": "warn", "title": "收入季节性波动较高",
                      "detail": f"12 个月净结余波动率 σ={metrics['volatility']}，淡季现金流有压力，不宜把淡季收入提前投出去。"})
    if metrics["debt_ratio_pct"] > 40:
        items.append({"level": "danger", "title": "负债压力偏高",
                      "detail": f"每月还款占收入 {metrics['debt_ratio_pct']}%，建议优先偿还高息负债再考虑投资。"})
    if grade in {"C1", "C2"} and metrics["savings_rate_pct"] > 20:
        items.append({"level": "info", "title": "结余能力被保守偏好限制",
                      "detail": "你的财务承受能力高于自己填写的风险偏好，可以适当放宽期限要求，但不必提高风险等级。"})
    return items or [{"level": "info", "title": "未发现明显短板", "detail": "各项指标均在合理区间，可按下方配置方案执行。"}]


def verdict(grade: str, binding: str) -> str:
    return (f"综合得分落在 {grade}（{GRADE_SUMMARY[grade]}），最高可适配 {GRADE_MAX_RISK[grade]} 风险等级的产品"
            f"{'；最终等级按审慎原则取较低一方。' if binding != 'MATCH' else '。'}")


def expiry(from_date: date | None = None) -> date:
    start = from_date or date.today()
    month = start.month - 1 + VALID_MONTHS
    return date(start.year + month // 12, month % 12 + 1, start.day)


__all__ = [
    "GRADE_BANDS", "GRADE_RISK", "GRADE_MAX_RISK", "GRADE_SUMMARY", "GRADE_ADVICE",
    "SUBJECTIVE_QUESTIONS", "SUBJECTIVE_MAX", "VALID_MONTHS",
    "grade_of", "band_max", "score_subjective", "score_objective",
    "combine", "allocation", "warnings", "verdict", "expiry",
]
