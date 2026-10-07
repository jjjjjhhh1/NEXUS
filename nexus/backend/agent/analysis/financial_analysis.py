"""Goal-driven financial planning on a verified local-data blackboard."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
from statistics import pstdev

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select

from ...core.models import Account, DeclaredSubscription, FinancialProfile, FinancialSnapshot, InvestmentOrder, Product, Transaction, User
from ...services.subscription_service import SubscriptionService

ZERO, CENT = Decimal("0.00"), Decimal("0.01")
RISK_STYLES = {"C1": "保守型", "C2": "稳健型", "C3": "平衡型", "C4": "成长型", "C5": "进取型"}


class DeclaredSubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merchant_name: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(gt=0, le=1_000_000, max_digits=15, decimal_places=2)
    period: str = Field(pattern=r"^(MONTHLY|QUARTERLY|YEARLY)$")
    essential: bool = False
    next_charge_day: int = Field(ge=1, le=28)

    @field_validator("merchant_name")
    @classmethod
    def clean_merchant(cls, value):
        value = value.strip()
        if not value or re.search(r"[<>]", value):
            raise ValueError("订阅名称不受支持")
        return value


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_income: Decimal = Field(ge=0, le=100_000_000, max_digits=15, decimal_places=2)
    essential_expenses: Decimal = Field(ge=0, le=100_000_000, max_digits=15, decimal_places=2)
    debt_balance: Decimal = Field(ge=0, le=1_000_000_000, max_digits=15, decimal_places=2)
    monthly_debt_payment: Decimal = Field(ge=0, le=100_000_000, max_digits=15, decimal_places=2)
    goal_name: str = Field(min_length=1, max_length=80)
    goal_amount: Decimal = Field(gt=0, le=1_000_000_000, max_digits=15, decimal_places=2)
    goal_saved: Decimal = Field(ge=0, le=1_000_000_000, max_digits=15, decimal_places=2)
    horizon_months: int = Field(ge=1, le=600)
    max_drawdown_pct: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    income_stability: str = Field(pattern=r"^(STABLE|VARIABLE)$")
    liquid_savings: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000_000, max_digits=15, decimal_places=2)
    investment_assets: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000_000, max_digits=15, decimal_places=2)
    declared_assets: Decimal = Field(default=Decimal("0"), ge=0, le=10_000_000_000, max_digits=15, decimal_places=2)
    debt_interest_rate: Decimal = Field(default=Decimal("0"), ge=0, le=50, max_digits=5, decimal_places=2)
    annual_income: Decimal = Field(default=Decimal("0"), ge=0, le=1_200_000_000, max_digits=15, decimal_places=2)
    annual_expenses: Decimal = Field(default=Decimal("0"), ge=0, le=1_200_000_000, max_digits=15, decimal_places=2)
    seasonal_monthly_income: list[Decimal] = Field(default_factory=list)
    seasonal_monthly_expenses: list[Decimal] = Field(default_factory=list)
    declared_subscriptions: list[DeclaredSubscriptionInput] = Field(default_factory=list, max_length=20)

    @field_validator("goal_name")
    @classmethod
    def clean_goal(cls, value):
        value = value.strip()
        if not value or re.search(r"[<>]", value):
            raise ValueError("目标名称不受支持")
        return value

    @field_validator("seasonal_monthly_income", "seasonal_monthly_expenses")
    @classmethod
    def validate_seasonal_months(cls, values):
        if len(values) not in {0, 12}:
            raise ValueError("季节性收支必须完整填写 12 个月，或全部留空")
        if any(value < 0 or value > Decimal("100000000") for value in values):
            raise ValueError("季节性月度金额超出支持范围")
        return values


class PersonalizedNarrative(BaseModel):
    """Model prose cannot introduce amounts or executable actions."""
    model_config = ConfigDict(extra="forbid", strict=True)
    headline: str = Field(min_length=4, max_length=80)
    assessment: str = Field(min_length=20, max_length=320)
    priorities: list[str] = Field(min_length=2, max_length=4)
    tradeoffs: list[str] = Field(min_length=1, max_length=3)
    review_triggers: list[str] = Field(min_length=2, max_length=4)

    @field_validator("headline", "assessment")
    @classmethod
    def prose_without_numbers(cls, value):
        if re.search(r"[0-9０-９%％¥￥]", value):
            raise ValueError("模型叙事不得引入数字")
        return value.strip()

    @field_validator("priorities", "tradeoffs", "review_triggers")
    @classmethod
    def list_without_numbers(cls, values):
        if any(not item.strip() or len(item) > 160 or re.search(r"[0-9０-９%％¥￥]", item) for item in values):
            raise ValueError("模型叙事不得引入数字")
        return [item.strip() for item in values]


def money(value: Decimal) -> str:
    return f"¥{value.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}"


def percent(value: Decimal) -> str:
    """百分比分量固定两位，避免同一张卡上出现 3.9% 和 3.90% 并排。"""
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP)}%"


def monthly_amount(amount: Decimal, period: str) -> Decimal:
    factors = {"MONTHLY": Decimal("1"), "QUARTERLY": Decimal("0.333333"), "YEARLY": Decimal("0.083333")}
    return amount * factors.get(period, ZERO)


async def get_profile(session, user_id: int):
    return await session.scalar(select(FinancialProfile).where(FinancialProfile.user_id == user_id))


async def get_snapshot(session, user_id: int):
    return await session.scalar(select(FinancialSnapshot).where(FinancialSnapshot.user_id == user_id))


async def get_declared_subscriptions(session, user_id: int):
    return list((await session.scalars(select(DeclaredSubscription).where(DeclaredSubscription.user_id == user_id, DeclaredSubscription.status == "ACTIVE").order_by(DeclaredSubscription.id))).all())


def intake(profile=None, snapshot=None, declared_subscriptions=None) -> dict:
    values = {}
    if profile:
        for field in FinancialProfile.__table__.columns.keys():
            if field not in ProfileInput.model_fields:
                continue
            value = getattr(profile, field)
            values[field] = str(value) if isinstance(value, Decimal) else value
    if snapshot:
        for field in ("liquid_savings", "investment_assets", "declared_assets", "debt_interest_rate", "annual_income", "annual_expenses"):
            value = getattr(snapshot, field)
            values[field] = str(value) if isinstance(value, Decimal) else value
    seasonal_income = list(snapshot.seasonal_income or []) if snapshot else []
    seasonal_expenses = list(snapshot.seasonal_expenses or []) if snapshot else []
    if profile and not seasonal_income:
        seasonal_income = [str(profile.monthly_income)] * 12
    if profile and not seasonal_expenses:
        seasonal_expenses = [str(profile.essential_expenses)] * 12
    subscriptions = [{"merchant_name": row.merchant_name, "amount": str(row.amount), "period": row.period, "essential": row.essential, "next_charge_day": row.next_charge_day} for row in (declared_subscriptions or [])]
    templates = [
        {"id": "young_professional", "name": "职场起步型", "description": "收入稳定、负债较轻，重点建立应急金与中期目标", "values": {"annual_income": "180000", "annual_expenses": "84000", "monthly_income": "15000", "essential_expenses": "7000", "debt_balance": "0", "monthly_debt_payment": "0", "debt_interest_rate": "0", "liquid_savings": "30000", "investment_assets": "15000", "goal_name": "三年首付储备", "goal_amount": "180000", "goal_saved": "30000", "horizon_months": "36", "max_drawdown_pct": "10", "income_stability": "STABLE", "seasonal_monthly_income": ["15000"] * 12, "seasonal_monthly_expenses": ["7000"] * 12, "declared_subscriptions": [{"merchant_name": "音乐会员", "amount": "18", "period": "MONTHLY", "essential": False, "next_charge_day": 8}, {"merchant_name": "云盘", "amount": "25", "period": "MONTHLY", "essential": True, "next_charge_day": 15}]}},
        {"id": "family_balanced", "name": "家庭稳健型", "description": "家庭收入与房贷并存，重点平衡现金流、保障与教育目标", "values": {"annual_income": "360000", "annual_expenses": "192000", "monthly_income": "30000", "essential_expenses": "16000", "debt_balance": "680000", "monthly_debt_payment": "6200", "debt_interest_rate": "3.6", "liquid_savings": "160000", "investment_assets": "120000", "goal_name": "子女教育金", "goal_amount": "500000", "goal_saved": "120000", "horizon_months": "84", "max_drawdown_pct": "12", "income_stability": "STABLE", "seasonal_monthly_income": ["30000"] * 12, "seasonal_monthly_expenses": ["16000"] * 12, "declared_subscriptions": [{"merchant_name": "家庭视频会员", "amount": "35", "period": "MONTHLY", "essential": False, "next_charge_day": 12}, {"merchant_name": "在线教育", "amount": "2400", "period": "YEARLY", "essential": True, "next_charge_day": 20}]}},
        {"id": "seasonal_freelancer", "name": "季节经营型", "description": "收入淡旺季明显，重点验证最差月份和波动惩罚", "values": {"annual_income": "216000", "annual_expenses": "90500", "monthly_income": "18000", "essential_expenses": "7500", "debt_balance": "60000", "monthly_debt_payment": "2000", "debt_interest_rate": "6.8", "liquid_savings": "40000", "investment_assets": "50000", "goal_name": "改善型住房首付", "goal_amount": "300000", "goal_saved": "50000", "horizon_months": "36", "max_drawdown_pct": "12", "income_stability": "VARIABLE", "seasonal_monthly_income": ["8000", "8000", "10000", "12000", "16000", "18000", "20000", "24000", "22000", "20000", "18000", "40000"], "seasonal_monthly_expenses": ["9000", "8000", "7000", "7000", "7000", "7500", "8000", "8000", "7500", "7000", "7000", "7500"], "declared_subscriptions": [{"merchant_name": "设计软件", "amount": "168", "period": "MONTHLY", "essential": True, "next_charge_day": 3}, {"merchant_name": "云存储", "amount": "288", "period": "YEARLY", "essential": True, "next_charge_day": 18}]}},
    ]
    return {
        "type": "financial_intake", "engine": "analysis", "title": "先补齐你的理财约束",
        "message": "选择一个典型画像作为起点，或填写自己的真实情况。画像只负责预填数据，最终结论仍由年度收支、最差月份、负债、订阅、目标和风险约束重新计算。资料只保存在你自己的账户下。",
        "values": values,
        "templates": templates,
        "fields": [
            {"name": "annual_income", "label": "全年总收入", "type": "money", "hint": "包含年终奖和季节性收入；填 0 时由月度数据合计"},
            {"name": "annual_expenses", "label": "全年生活支出", "type": "money", "hint": "不含还贷和订阅；填 0 时由月度数据合计"},
            {"name": "monthly_income", "label": "税后月收入", "type": "money", "hint": "用于计算每月可持续投入"},
            {"name": "essential_expenses", "label": "每月必要支出", "type": "money", "hint": "房租、吃住、保险等刚性开支"},
            {"name": "debt_balance", "label": "当前负债余额", "type": "money", "hint": "没有负债请填 0"},
            {"name": "monthly_debt_payment", "label": "每月还款", "type": "money", "hint": "没有请填 0"},
            {"name": "debt_interest_rate", "label": "负债综合年利率", "type": "percent", "hint": "用于判断先还债还是投资；没有请填 0"},
            {"name": "liquid_savings", "label": "其他现金与储蓄", "type": "money", "hint": "不含本页显示的银行账户余额"},
            {"name": "investment_assets", "label": "其他投资资产", "type": "money", "hint": "基金、股票等当前市值"},
            {"name": "declared_assets", "label": "自报总资产", "type": "money", "hint": "含自住房与车辆等不产生现金流的部分；不确定可填 0"},
            {"name": "goal_name", "label": "首要理财目标", "type": "text", "hint": "例如购车、留学、长期增值"},
            {"name": "goal_amount", "label": "目标金额", "type": "money", "hint": "目标所需总额"},
            {"name": "goal_saved", "label": "已为目标准备", "type": "money", "hint": "已经单独留出的金额"},
            {"name": "horizon_months", "label": "距离目标还有几个月", "type": "integer", "hint": "例如 36"},
            {"name": "max_drawdown_pct", "label": "可接受最大账面回撤", "type": "percent", "hint": "例如 10"},
            {"name": "income_stability", "label": "收入稳定性", "type": "choice", "options": [{"value": "STABLE", "label": "较稳定"}, {"value": "VARIABLE", "label": "波动较大"}]},
            {"name": "seasonal_cashflow", "label": "12 个月季节性收支", "type": "monthly_grid", "hint": "填写每月税后收入与生活支出；不含还贷和订阅", "income_values": seasonal_income, "expense_values": seasonal_expenses},
            {"name": "declared_subscriptions", "label": "我的订阅与固定扣费", "type": "subscription_list", "hint": "补充未接入银行账单的会员、软件、保险或年费", "values": subscriptions},
        ],
    }


async def save_profile(session, user_id: int, data: ProfileInput) -> FinancialProfile:
    profile = await get_profile(session, user_id)
    profile_fields = {name: value for name, value in data.model_dump().items() if name in FinancialProfile.__table__.columns.keys()}
    if profile is None:
        profile = FinancialProfile(user_id=user_id, **profile_fields)
        session.add(profile)
    else:
        for field, value in profile_fields.items():
            setattr(profile, field, value)
        profile.updated_at = datetime.now()
    snapshot = await get_snapshot(session, user_id)
    snapshot_values = {name: getattr(data, name) for name in ("liquid_savings", "investment_assets", "declared_assets", "debt_interest_rate", "annual_income", "annual_expenses")}
    snapshot_values["seasonal_income"] = [str(value) for value in data.seasonal_monthly_income] or None
    snapshot_values["seasonal_expenses"] = [str(value) for value in data.seasonal_monthly_expenses] or None
    if snapshot is None:
        snapshot = FinancialSnapshot(user_id=user_id, **snapshot_values)
        session.add(snapshot)
    else:
        for field, value in snapshot_values.items():
            setattr(snapshot, field, value)
        snapshot.updated_at = datetime.now()
    await session.execute(delete(DeclaredSubscription).where(DeclaredSubscription.user_id == user_id))
    session.add_all([DeclaredSubscription(user_id=user_id, **item.model_dump(), status="ACTIVE") for item in data.declared_subscriptions])
    await session.flush()
    return profile


RISK_LEVELS = {"C1", "C2", "C3", "C4", "C5"}


def _risk_cap_detail(user_risk: str, drawdown: Decimal, horizon_months: int) -> dict:
    """风险上限由三条约束共同决定，并指出究竟是哪一条在卡住。

    只回一个 R2 而不说清是谁定的，用户既不知道自己被什么限制，也无法判断
    该去改回撤容忍度、做风险测评，还是把目标期限拉长。取最小值的那条才是
    真正的瓶颈，也是唯一值得展示给用户的理由。
    """
    capacity = 1 if drawdown < 5 else 2 if drawdown < 10 else 3 if drawdown < 20 else 4 if drawdown < 35 else 5
    assessed = int(user_risk[1:]) if user_risk in RISK_LEVELS else 1
    horizon_cap = 1 if horizon_months <= 12 else 2 if horizon_months <= 36 else 3 if horizon_months <= 60 else 5
    detail = {
        "可接受回撤": f"可接受最大回撤 {drawdown.normalize()}%",
        "风险测评": f"风险测评 {user_risk}" if user_risk in RISK_LEVELS else "尚未做风险测评，暂按 C1 处理",
        "目标期限": f"目标期限 {horizon_months} 个月",
    }
    binding_value, binding_name = min((capacity, "可接受回撤"), (assessed, "风险测评"), (horizon_cap, "目标期限"), key=lambda pair: pair[0])
    return {
        "cap": binding_value,
        "binding": binding_name,
        "binding_reason": detail[binding_name],
        "constraints": [{"name": name, "value": text, "cap": value} for (name, value), text in zip(
            (("可接受回撤", capacity), ("风险测评", assessed), ("目标期限", horizon_cap)), detail.values())],
    }


def _risk_cap(user_risk: str, drawdown: Decimal, horizon_months: int) -> int:
    return _risk_cap_detail(user_risk, drawdown, horizon_months)["cap"]


def _buy_reasons(product, detail: dict, profile, investable: Decimal) -> list[str]:
    """每支被选中的产品都要能自己解释自己为什么被选中。

    给出产品却不给出理由，等于把判断责任推回给用户；这三条理由全部来自
    已经核对过的约束，因此不会和卡片上的其他数字打架。
    """
    return [
        f"风险等级 {product.risk_level} 不超过你的上限 R{detail['cap']}",
        f"锁定 {product.lock_days} 天，可在你 {profile.horizon_months} 个月的目标期限内调整" if product.lock_days else "无锁定期，随时可取，不影响应急取用",
        f"起购 {money(Decimal(product.min_purchase))} 在你可投的 {money(investable)} 之内",
    ]


def _product_reasons(product, detail: dict, profile, investable: Decimal) -> list[str]:
    """逐条列出这只产品被排除的具体原因，而不是笼统地说“不适合”。"""
    reasons = []
    level = int(product.risk_level[1:]) if product.risk_level and product.risk_level.startswith("R") else 99
    if level > detail["cap"]:
        reasons.append(f"风险等级 {product.risk_level} 超过你的上限 R{detail['cap']}（受限于{detail['binding_reason']}）")
    if product.lock_days > profile.horizon_months * 30:
        reasons.append(f"锁定 {product.lock_days} 天长于你的目标期限 {profile.horizon_months} 个月")
    if Decimal(product.min_purchase) > investable:
        reasons.append(f"起购 {money(Decimal(product.min_purchase))} 高于你现在能投的 {money(investable)}")
    return reasons


def build_recommendation(*, investable: Decimal, matched: list, rejected: list, risk_detail: dict,
                         emergency_gap: Decimal, emergency_months: int, debt_balance: Decimal, debt_rate: Decimal,
                         monthly_surplus: Decimal, required_monthly: Decimal, goal_gap: Decimal,
                         goal_name: str, feasibility: str, horizon_months: int) -> dict:
    """把已经算出来的约束翻译成一句“买什么、不买什么、为什么”。

    卡片里其他 section 回答的是“你的数据是多少”，而用户真正要的是“所以呢”。
    这里只复用上游已经算好的数，不再做新的假设：所有金额、比例和产品都来自
    build_financial_analysis 的同一批计算，避免建议和数据对不上。
    """
    cap_level = risk_detail["cap"]
    # 负债利率和参考收益率都是外部填报值，缺一就不做这个比较——宁可不给建议，
    # 也不给一个用户会照着执行的错误比较。
    comparable = [item for item in matched if debt_rate > ZERO]
    best_yield = max((Decimal(item["reference_yield"].rstrip("%")) for item in comparable), default=None)
    debt_wins = best_yield is not None and debt_rate > best_yield

    if emergency_gap > ZERO:
        verdict = f"先别买。你的应急资金还差{money(emergency_gap)}，这部分钱补齐之前不适合放进任何有锁定期的产品。"
    elif investable <= ZERO:
        verdict = f"现在没有可投资金。应急资金和{goal_name}目标已经占满你全部可用现金，先积累月度结余再考虑产品。"
    elif debt_wins:
        verdict = (f"能买，但只能买无锁定的低风险产品。你的负债利率 {percent(debt_rate)} 高于可选产品的最高参考值 {percent(best_yield)}，"
                   f"还债比买理财更划算；{money(investable)} 放货币类产品留着随时能用。")
    else:
        verdict = f"可以买 {len(matched)} 支，最多投 {money(investable)}，只买 R{cap_level} 及以下。"

    if emergency_gap > ZERO:
        investable_basis = f"当前可投为零：应急资金还差{money(emergency_gap)}"
    else:
        investable_basis = f"总可用现金扣除应急金和{goal_name}目标占用后的余额"

    actions = []
    if emergency_gap > ZERO:
        actions.append(f"先把应急资金补到{money(emergency_gap)}，补齐前不加任何锁定期。")
    else:
        actions.append(f"应急资金已覆盖{emergency_months}个月必要支出，不需要额外加码。")
    if feasibility == "ON_TRACK":
        actions.append(f"每月为{goal_name}存{money(required_monthly)}，按当前期限可以按时达成。")
    elif monthly_surplus > ZERO:
        actions.append(f"每月为{goal_name}存 {money(monthly_surplus)}，这是你真实存得起的；{money(required_monthly)} 达不到，"
                       f"要么延长期限，要么下调目标金额。")
    else:
        actions.append(f"当前月结余为 {money(monthly_surplus)}，没有可持续投入的钱；先处理支出和负债，再谈理财。")
    if debt_wins:
        actions.append(f"优先偿还高息负债：{percent(debt_rate)} 高于所有可选产品的参考收益率。")
    elif debt_balance > ZERO and debt_rate <= ZERO:
        actions.append("补充你的负债利率后才能判断“还债还是投资”，现在本报告不做这个比较。")
    if rejected:
        actions.append(f"不要买 R{cap_level + 1} 及以上的产品：当前上限 R{cap_level} 由{risk_detail['binding_reason']}决定。")

    avoid = [{"subject": f"{item['name']}（{item['code']}）", "reason": "；".join(item["blocked_by"])} for item in rejected]
    if emergency_gap > ZERO:
        avoid.append({"subject": "任何有锁定期的产品", "reason": f"应急资金还差{money(emergency_gap)}，锁定会让应急取不出来"})
    if debt_wins:
        avoid.append({"subject": "靠借钱做投资", "reason": f"负债利率已达 {percent(debt_rate)}，再举债只会放大成本"})
    if feasibility == "GAP":
        avoid.append({"subject": "用消费贷补目标缺口", "reason": "月结余为负，缺口靠加杠杆补会在下一期集中爆发"})

    return {
        "verdict": verdict,
        "investable": {"amount": money(investable), "basis": investable_basis},
        "buy": matched, "avoid": avoid, "actions": actions,
        "risk_cap": {"level": f"R{cap_level}", "binding": risk_detail["binding"], "reason": risk_detail["binding_reason"], "constraints": risk_detail["constraints"]},
        "debt_comparison": {
            "debt_rate": percent(debt_rate), "best_reference_yield": percent(best_yield) if best_yield is not None else None,
            "conclusion": "还债更划算" if debt_wins else "本报告不作比较" if debt_rate <= ZERO else "买理财更划算",
            "note": "产品参考值是演示数据且非保证收益，只用于量级比较，不构成投资建议。",
        } if debt_balance > ZERO else None,
    }


async def build_financial_analysis(session, user_id: int) -> dict:
    user = await session.get(User, user_id)
    profile = await get_profile(session, user_id)
    if profile is None:
        return intake()
    snapshot = await get_snapshot(session, user_id)
    external_liquid = Decimal(snapshot.liquid_savings) if snapshot else ZERO
    external_investments = Decimal(snapshot.investment_assets) if snapshot else ZERO
    debt_rate = Decimal(snapshot.debt_interest_rate) if snapshot else ZERO

    accounts = list((await session.scalars(select(Account).where(Account.user_id == user_id))).all())
    account_ids = [row.id for row in accounts]
    available = sum((Decimal(row.available_balance) for row in accounts), ZERO)
    reserved = sum((Decimal(row.reserved_balance) for row in accounts), ZERO)
    subscriptions = await SubscriptionService(session).list_user_subscriptions(user_id)
    declared_subscriptions = await get_declared_subscriptions(session, user_id)
    contracts = sum((monthly_amount(Decimal(row.amount), row.period) for row in declared_subscriptions), ZERO) if declared_subscriptions else sum((monthly_amount(Decimal(str(row["amount"])), row["period"]) for row in subscriptions if row["contract_status"] == "ACTIVE"), ZERO)
    orphan_mandates = [row["merchant_name"] for row in subscriptions if row["contract_status"] != "ACTIVE" and row["mandate_status"] == "ACTIVE"]

    outgoing = ZERO
    if account_ids:
        txs = list((await session.scalars(select(Transaction).where(Transaction.from_account_id.in_(account_ids), Transaction.created_at >= datetime.now() - timedelta(days=30), Transaction.status.in_(["COMPLETED", "SETTLED"])))).all())
        outgoing = sum((Decimal(row.amount) for row in txs), ZERO)
    holdings = ZERO
    orders = list((await session.scalars(select(InvestmentOrder).where(InvestmentOrder.user_id == user_id, InvestmentOrder.order_type == "SUBSCRIBE", InvestmentOrder.status.in_(["CONFIRMED", "SETTLED"])))).all())
    for order in orders:
        holdings += Decimal(order.remaining_shares or ZERO) * Decimal(order.nav or 1)
    total_liquid = available + external_liquid
    total_investments = holdings + external_investments
    total_assets = total_liquid + total_investments
    debt_balance = Decimal(profile.debt_balance)
    net_worth = total_assets - debt_balance

    profile_monthly_income = Decimal(profile.monthly_income)
    profile_monthly_expenses = Decimal(profile.essential_expenses)
    seasonal_income = [Decimal(str(value)) for value in (snapshot.seasonal_income or [])] if snapshot else []
    seasonal_expenses = [Decimal(str(value)) for value in (snapshot.seasonal_expenses or [])] if snapshot else []
    if len(seasonal_income) != 12:
        seasonal_income = [profile_monthly_income] * 12
    if len(seasonal_expenses) != 12:
        seasonal_expenses = [profile_monthly_expenses] * 12
    annual_income = Decimal(snapshot.annual_income) if snapshot and Decimal(snapshot.annual_income) > ZERO else sum(seasonal_income, ZERO)
    annual_living_expenses = Decimal(snapshot.annual_expenses) if snapshot and Decimal(snapshot.annual_expenses) > ZERO else sum(seasonal_expenses, ZERO)
    annual_fixed_outflow = (Decimal(profile.monthly_debt_payment) + contracts) * Decimal("12")
    annual_total_outflow = annual_living_expenses + annual_fixed_outflow
    annual_surplus = annual_income - annual_total_outflow
    average_income = annual_income / Decimal("12") if annual_income > ZERO else ZERO
    average_outflow = annual_total_outflow / Decimal("12")
    monthly_surplus = annual_surplus / Decimal("12")
    monthly_outflows = [value + Decimal(profile.monthly_debt_payment) + contracts for value in seasonal_expenses]
    monthly_net = [income - outflow for income, outflow in zip(seasonal_income, monthly_outflows)]
    ratios = [(income / outflow) if outflow > ZERO else Decimal("99") for income, outflow in zip(seasonal_income, monthly_outflows)]
    worst_index = min(range(12), key=lambda index: monthly_net[index])
    worst_month_ratio = ratios[worst_index]
    worst_month_net = monthly_net[worst_index]
    worst_month_outflow = monthly_outflows[worst_index]
    annual_savings_rate = (annual_surplus / annual_income * 100) if annual_income > ZERO else ZERO
    mean_income = sum(seasonal_income, ZERO) / Decimal("12")
    volatility = Decimal(str(pstdev([float(value) for value in monthly_net]))) / max(abs(mean_income), Decimal("1"))
    volatility = min(max(volatility, ZERO), Decimal("1"))
    core_outflow = average_outflow
    emergency_months = Decimal("6" if profile.income_stability == "STABLE" else "9")
    emergency_target = (max(core_outflow, worst_month_outflow) * emergency_months).quantize(CENT)
    emergency_allocated = min(max(total_liquid - reserved, ZERO), emergency_target)
    emergency_gap = max(emergency_target - emergency_allocated, ZERO)
    remaining_cash = max(total_liquid - reserved - emergency_allocated, ZERO)
    goal_gap = max(Decimal(profile.goal_amount) - Decimal(profile.goal_saved), ZERO)
    required_monthly = (goal_gap / Decimal(profile.horizon_months)).quantize(CENT, rounding=ROUND_HALF_UP)
    goal_now = min(goal_gap, remaining_cash) if profile.horizon_months <= 36 else min(goal_gap, remaining_cash * Decimal("0.50"))
    long_term = max(remaining_cash - goal_now, ZERO)
    feasibility = "ON_TRACK" if monthly_surplus >= required_monthly else "TIGHT" if monthly_surplus > ZERO else "GAP"
    risk_detail = _risk_cap_detail(user.risk_score or "C1", Decimal(profile.max_drawdown_pct), profile.horizon_months)
    risk_cap = risk_detail["cap"]

    def bounded(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
        return min(max(value, low), high)

    savings_rate = annual_savings_rate
    debt_service_ratio = (Decimal(profile.monthly_debt_payment) / average_income * 100) if average_income > ZERO else ZERO
    liquidity_months = (total_liquid / core_outflow) if core_outflow > ZERO else Decimal("99")
    cashflow_score = bounded(savings_rate / Decimal("20") * Decimal("25"), ZERO, Decimal("25"))
    emergency_coverage_score = bounded(liquidity_months / emergency_months, ZERO, Decimal("1"))
    worst_month_score = bounded(worst_month_ratio, ZERO, Decimal("1"))
    emergency_score = (min(emergency_coverage_score, worst_month_score) * Decimal("25")).quantize(Decimal("0.1"))
    if debt_balance == ZERO:
        debt_score = Decimal("20")
    else:
        debt_score = Decimal("20") if debt_service_ratio < 15 else Decimal("15") if debt_service_ratio < 25 else Decimal("8") if debt_service_ratio < 40 else Decimal("2")
        debt_score = max(ZERO, debt_score - (Decimal("4") if debt_rate > 12 else Decimal("2") if debt_rate > 8 else ZERO))
    goal_score = Decimal("20") if required_monthly == ZERO else bounded(monthly_surplus / required_monthly * Decimal("20"), ZERO, Decimal("20"))
    risk_score = Decimal("5") if emergency_gap > ZERO and total_investments > ZERO else Decimal("10")
    base_health_score = cashflow_score + emergency_score + debt_score + goal_score + risk_score
    volatility_factor = Decimal("1") - Decimal("0.3") * volatility
    adjusted_health_score = base_health_score * volatility_factor
    health_score = int(adjusted_health_score.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    health_label = "稳健" if health_score >= 85 else "良好" if health_score >= 70 else "需改善" if health_score >= 50 else "需优先调整"

    stress_cases = []
    stress_inputs = [
        ("全年平均", average_income, average_outflow),
        ("最差月份", seasonal_income[worst_index], monthly_outflows[worst_index]),
        ("收入再降 10%", seasonal_income[worst_index] * Decimal("0.90"), monthly_outflows[worst_index]),
        ("支出再增 10%", seasonal_income[worst_index], monthly_outflows[worst_index] * Decimal("1.10")),
    ]
    for name, case_income, case_outflow in stress_inputs:
        surplus = case_income - case_outflow
        projected = Decimal(profile.goal_saved) + max(surplus, ZERO) * Decimal(profile.horizon_months)
        attainment = bounded(projected / Decimal(profile.goal_amount) * 100, ZERO, Decimal("999"))
        stress_cases.append({"name": name, "monthly_surplus": money(surplus), "monthly_surplus_value": float(surplus), "projected_goal": money(projected), "attainment_pct": float(attainment.quantize(Decimal("0.1"))), "status": "可达" if projected >= Decimal(profile.goal_amount) else "有缺口"})

    products = list((await session.scalars(select(Product).order_by(Product.risk_level, Product.lock_days))).all())
    from ..context.memory import context as memory_context
    remembered = await memory_context(session,user_id)
    preferences = remembered.get("preferences", {})
    investable = max(goal_now + long_term, ZERO)
    matched, rejected = [], []
    for product in products:
        if not product.is_fictional:
            continue
        level = int(product.risk_level[1:]) if product.risk_level and product.risk_level.startswith("R") else 99
        card = {"code": product.code, "name": product.name, "type": product.type, "risk_level": product.risk_level, "lock_days": product.lock_days, "min_purchase": money(Decimal(product.min_purchase)), "reference_yield": f"{Decimal(product.yield_rate or ZERO):.2f}%", "data_date": product.data_date.isoformat() if product.data_date else None, "fictional": True}
        blockers = _product_reasons(product, risk_detail, profile, investable)
        if preferences.get("risk_preference") == "稳健" and level > 2:
            blockers.append("你自述偏好稳健，优先考虑较低风险产品；正式风险评级不变")
        if preferences.get("liquidity_preference") == "随时可用" and product.lock_days:
            blockers.append("你希望资金随时可用，此产品有锁定期")
        if blockers:
            rejected.append({**card, "blocked_by": blockers})
            continue
        card["why"] = _buy_reasons(product, risk_detail, profile, investable)
        matched.append(card)

    observations = []
    monthly_income_sum = sum(seasonal_income, ZERO)
    monthly_expense_sum = sum(seasonal_expenses, ZERO)
    if snapshot and Decimal(snapshot.annual_income) > ZERO and monthly_income_sum > ZERO and abs(annual_income - monthly_income_sum) / annual_income > Decimal("0.05"):
        observations.append("全年总收入与 12 个月收入合计相差超过 5%，评分采用全年总收入，季节性曲线采用月度数据。")
    if snapshot and Decimal(snapshot.annual_expenses) > ZERO and monthly_expense_sum > ZERO and abs(annual_living_expenses - monthly_expense_sum) / annual_living_expenses > Decimal("0.05"):
        observations.append("全年生活支出与 12 个月支出合计相差超过 5%，评分采用全年总支出，压力月份采用月度数据。")
    if volatility >= Decimal("0.20"):
        observations.append(f"收支波动率 σ={volatility.quantize(Decimal('0.01'))}，已按 1−0.3σ 对基础健康分做季节性风险扣减。")
    if orphan_mandates:
        observations.append(f"{', '.join(orphan_mandates)}的合同已不活跃，但代扣仍有效，建议先核对授权。")
    if emergency_gap:
        observations.append(f"应急资金距离目标还差{money(emergency_gap)}，在补齐前不宜把这部分资金锁定。")
    if debt_balance > ZERO:
        if debt_rate > ZERO:
            annual_interest = debt_balance * debt_rate / Decimal("100")
            observations.append(f"当前负债为{money(debt_balance)}，按填报利率粗算年利息约{money(annual_interest)}；应与投资的非保证收益和流动性风险一起比较。")
        else:
            observations.append(f"当前负债余额为{money(debt_balance)}；因尚未记录利率，本报告不判断提前还款与投资谁优先。")
    if feasibility != "ON_TRACK":
        observations.append(f"目标每月需要{money(required_monthly)}，可持续结余约{money(monthly_surplus)}，需要调整期限、目标金额或支出。")

    blackboard = {
        "user_preferences":preferences, "memory_policy":remembered.get("policy", ""),
        "risk_profile": f"{user.risk_score or '未测评'} {user.investment_style or RISK_STYLES.get(user.risk_score, '待评估')}",
        "income_stability": "较稳定" if profile.income_stability == "STABLE" else "波动较大", "goal": profile.goal_name,
        "horizon_band": "短期" if profile.horizon_months <= 24 else "中期" if profile.horizon_months <= 60 else "长期",
        "cashflow_status": "有结余" if monthly_surplus > ZERO else "无可持续结余", "goal_feasibility": feasibility,
        "emergency_status": "已覆盖" if emergency_gap == ZERO else "有缺口", "risk_cap": f"R{risk_cap}",
        "orphan_mandate": bool(orphan_mandates), "has_debt": debt_balance > ZERO,
    }
    recommendation = build_recommendation(
        investable=investable, matched=matched, rejected=rejected, risk_detail=risk_detail,
        emergency_gap=emergency_gap, emergency_months=int(emergency_months),
        debt_balance=debt_balance, debt_rate=debt_rate,
        monthly_surplus=monthly_surplus, required_monthly=required_monthly, goal_gap=goal_gap,
        goal_name=profile.goal_name, feasibility=feasibility, horizon_months=profile.horizon_months,
    )
    return {
        "memory_context": {"summary":remembered.get("summary", ""),"version":remembered.get("version",0),"policy":remembered.get("policy", "")},
        "type": "financial_analysis", "engine": "analysis", "title": f"围绕“{profile.goal_name}”的资金方案", "as_of": datetime.now().date().isoformat(),
        "profile": {"risk_score": user.risk_score or "未测评", "style": user.investment_style or RISK_STYLES.get(user.risk_score, "待评估"), "data_quality": "资产、负债、目标与现金流已补充", "goal": profile.goal_name, "horizon_months": profile.horizon_months, "max_drawdown_pct": str(profile.max_drawdown_pct), "debt_interest_rate": str(debt_rate)},
        "summary": f"以{profile.goal_name}为目标，当前月度可持续结余约{money(monthly_surplus)}，目标缺口为{money(goal_gap)}，按现有期限每月需准备{money(required_monthly)}。",
        "recommendation": recommendation,
        "metrics": [
            {"label": "现金与可用余额", "value": money(total_liquid), "basis": "本地账户与用户填报的其他现金"},
            {"label": "理财持仓", "value": money(total_investments), "basis": "本地持仓与用户填报的其他投资"},
            {"label": "月度必要流出", "value": money(core_outflow), "basis": "必要支出、每月还款与有效订阅"},
            {"label": "月度可持续结余", "value": money(monthly_surplus), "basis": "税后收入减月度必要流出"},
            {"label": "目标资金缺口", "value": money(goal_gap), "basis": "目标金额减已准备金额"},
            {"label": "近三十天转出", "value": money(outgoing), "basis": "已完成转账，仅作旁证"},
        ],
        "allocation": {"method": "目标驱动：先覆盖应急资金，再为有期限的目标留资，剩余才进入长期配置。收入波动会提高应急月数；产品风险同时受风险测评和最大回撤约束。", "buckets": [
            {"name": "应急资金", "amount": money(emergency_allocated), "target": money(emergency_target), "status": "READY" if emergency_gap == ZERO else "GAP", "rationale": f"按当前必要流出覆盖{'六' if profile.income_stability == 'STABLE' else '九'}个月。"},
            {"name": profile.goal_name, "amount": money(goal_now), "target": money(goal_gap), "status": feasibility, "rationale": f"距离目标还有 {profile.horizon_months} 个月，每月需准备 {money(required_monthly)}。"},
            {"name": "长期配置", "amount": money(long_term), "target": f"最高风险 R{risk_cap}", "status": "AVAILABLE" if long_term > ZERO else "WAIT", "rationale": "仅使用应急资金和阶段目标之外的余额。"},
        ]},
        "health": {"score": health_score, "base_score": float(base_health_score.quantize(Decimal('0.1'))), "label": health_label, "volatility": float(volatility.quantize(Decimal('0.001'))), "volatility_factor": float(volatility_factor.quantize(Decimal('0.001'))), "method": "基础分 = 现金流 25 + 应急能力 25 + 负债压力 20 + 目标可行性 20 + 风险匹配 10；最终分 = 基础分 × (1 − 0.3 × 收支波动率 σ)。", "components": [
            {"name": "现金流", "score": float(cashflow_score.quantize(Decimal('0.1'))), "max": 25, "basis": f"年度储蓄率 {savings_rate.quantize(Decimal('0.1'))}%"},
            {"name": "应急能力", "score": float(emergency_score.quantize(Decimal('0.1'))), "max": 25, "basis": f"最差月收支比 M={worst_month_ratio.quantize(Decimal('0.01'))}；现金覆盖 {liquidity_months.quantize(Decimal('0.1'))} 个月"},
            {"name": "负债压力", "score": float(debt_score), "max": 20, "basis": f"月还款占收入 {debt_service_ratio.quantize(Decimal('0.1'))}%"},
            {"name": "目标可行性", "score": float(goal_score.quantize(Decimal('0.1'))), "max": 20, "basis": f"月结余 / 月目标 {((monthly_surplus / required_monthly * 100) if required_monthly else Decimal('100')).quantize(Decimal('0.1'))}%"},
            {"name": "风险匹配", "score": float(risk_score), "max": 10, "basis": "先满足安全垫，再承担投资波动"},
        ]},
        "balance_sheet": {"assets": money(total_assets), "assets_value": float(total_assets), "liabilities": money(debt_balance), "liabilities_value": float(debt_balance), "net_worth": money(net_worth), "net_worth_value": float(net_worth), "cash": money(total_liquid), "investments": money(total_investments)},
        "cashflow": {"income": money(average_income), "income_value": float(average_income), "essential": money(annual_living_expenses / Decimal('12')), "essential_value": float(annual_living_expenses / Decimal('12')), "debt_payment": money(Decimal(profile.monthly_debt_payment)), "debt_payment_value": float(profile.monthly_debt_payment), "subscriptions": money(contracts), "subscriptions_value": float(contracts), "surplus": money(monthly_surplus), "surplus_value": float(monthly_surplus), "savings_rate_pct": float(savings_rate.quantize(Decimal('0.1'))), "annual_income": money(annual_income), "annual_outflow": money(annual_total_outflow), "annual_surplus": money(annual_surplus)},
        "seasonality": {"annual_savings_rate_pct": float(annual_savings_rate.quantize(Decimal('0.1'))), "volatility": float(volatility.quantize(Decimal('0.001'))), "penalty_pct": float(((Decimal('1') - volatility_factor) * 100).quantize(Decimal('0.1'))), "worst_month": worst_index + 1, "worst_month_net": money(worst_month_net), "worst_month_ratio": float(worst_month_ratio.quantize(Decimal('0.01'))), "months": [{"month": index + 1, "income": float(seasonal_income[index]), "expenses": float(monthly_outflows[index]), "net": float(monthly_net[index])} for index in range(12)]},
        "stress_tests": stress_cases,
        "product_matches": matched, "product_rejected": rejected, "observations": observations or ["当前约束之间未发现明显冲突，可继续细化产品与执行节奏。"],
        "decision": {"goal_feasibility": feasibility, "monthly_required": money(required_monthly), "monthly_surplus": money(monthly_surplus), "max_product_risk": f"R{risk_cap}"},
        "blackboard": blackboard, "narrative": None,
    }
