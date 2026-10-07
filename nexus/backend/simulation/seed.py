"""Repeatable local-only fixtures. Restarting never resets balances."""
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from sqlalchemy import select
from ..core.models import User, Account, Recipient, Card, Product, ProductPerformance, ImportBatch, StatementTransaction, ContextEvent, FinancialProfile, FinancialSnapshot
from ..services.subscription_service import SubscriptionService


async def ensure_demo_profile(session, user_id: int) -> None:
    """A declared financial profile, so the risk model has real inputs to read.

    Seeded rather than demanded: a customer who has entered nothing cannot be
    told their risk grade, and leaving it empty would make the assessment
    reachable only by hand-filling the profile form first. The numbers are a
    plausible mid-career baseline and every field stays editable afterwards.
    """
    if await session.scalar(select(FinancialProfile).where(FinancialProfile.user_id == user_id)):
        # 档案已存在，只补齐后加的字段：老数据库里自报总资产是 0，会把有房贷的
        # 客户算成净资产为 0。只写默认值，不动客户已经填过的内容。
        snapshot = await session.scalar(select(FinancialSnapshot).where(FinancialSnapshot.user_id == user_id))
        if snapshot is not None and not snapshot.declared_assets:
            snapshot.declared_assets = Decimal("1150000")
            await session.flush()
        return
    session.add(FinancialProfile(
        user_id=user_id,
        monthly_income=Decimal("18000"), essential_expenses=Decimal("8200"),
        debt_balance=Decimal("320000"), monthly_debt_payment=Decimal("4600"),
        goal_name="三年购房首付", goal_amount=Decimal("450000"), goal_saved=Decimal("96000"),
        horizon_months=36, max_drawdown_pct=Decimal("12"), income_stability="STABLE",
    ))
    session.add(FinancialSnapshot(
        user_id=user_id,
        liquid_savings=Decimal("86000"), investment_assets=Decimal("145000"),
        # 含自住房与车辆：房贷余额对应的是这套房，不计进来净资产会被算成 0。
        declared_assets=Decimal("1150000"),
        debt_interest_rate=Decimal("3.9"),
        annual_income=Decimal("240000"), annual_expenses=Decimal("158000"),
        # 有淡旺季：年终奖集中在第四季度。这让"收入稳定性"这一项真的被考到，
        # 而不是在一份全年恒定的假数据上永远拿满分。
        seasonal_income=[str(v) for v in (
            18000, 18000, 18000, 20000, 18000, 18000,
            18000, 18000, 19000, 36000, 19000, 20000,
        )],
        seasonal_expenses=[str(v) for v in (
            13000, 12500, 12800, 13200, 13500, 14000,
            13200, 13000, 12800, 16000, 13500, 14500,
        )],
    ))
    await session.flush()


# Daily sigma per risk grade, chosen so a ~90 day window looks like a plausible
# fund curve (annualised: R1 ≈0.4%, R3 ≈1.7%, R5 ≈4.2%) rather than noise.
RISK_VOLATILITY = {"R1": 0.0002, "R2": 0.0005, "R3": 0.0009, "R4": 0.0014, "R5": 0.0022}
PERFORMANCE_DAYS = 90
# Re-cut the tail once a week so the chart never shows a stale "recent" move.
PERFORMANCE_MAX_AGE_DAYS = 7


def _nav_path(code: str, risk_level: str, annual_pct: float, days: int) -> list[tuple[date, Decimal]]:
    """A deterministic but non-monotonic NAV path for a fictional product.

    Two properties matter and pull in opposite directions: the path must wander
    so "is it up or down lately" is a real question, and it must end near the
    advertised yield rate so the curve cannot contradict the number printed
    beside it. A drift-corrected random walk gives both.
    """
    rng = random.Random(f"nexus-nav::{code}::{days}")
    vol = RISK_VOLATILITY.get(risk_level, 0.0006)
    daily_drift = annual_pct / 100 / 365
    shocks = [rng.gauss(0.0, vol) for _ in range(days)]
    correction = (daily_drift * days - sum(shocks)) / days
    today = date.today()
    nav = 1.0
    rows: list[tuple[date, Decimal]] = []
    for index, shock in enumerate(shocks):
        nav *= 1 + daily_drift + shock + correction
        rows.append((today - timedelta(days=days - 1 - index), Decimal(str(round(nav, 6)))))
    return rows


async def ensure_product_performance(session) -> None:
    """Give every seeded product a price history so a trend chart is possible."""
    products = list((await session.scalars(select(Product).where(Product.is_fictional.is_(True)))).all())
    for product in products:
        latest = await session.scalar(
            select(ProductPerformance.trade_date)
            .where(ProductPerformance.product_id == product.id)
            .order_by(ProductPerformance.trade_date.desc())
            .limit(1)
        )
        if latest and (date.today() - latest).days <= PERFORMANCE_MAX_AGE_DAYS:
            continue
        if latest:
            # Keep the history already on screen and only re-cut the stale tail.
            keep_days = PERFORMANCE_DAYS - max((date.today() - latest).days, 0)
            if keep_days >= 2:
                window = _nav_path(product.code, product.risk_level, float(product.yield_rate or 0), keep_days)
                await session.execute(
                    ProductPerformance.__table__.delete().where(
                        ProductPerformance.product_id == product.id,
                        ProductPerformance.trade_date > window[0][0],
                    )
                )
            else:
                window = _nav_path(product.code, product.risk_level, float(product.yield_rate or 0), PERFORMANCE_DAYS)
                await session.execute(
                    ProductPerformance.__table__.delete().where(ProductPerformance.product_id == product.id)
                )
        else:
            window = _nav_path(product.code, product.risk_level, float(product.yield_rate or 0), PERFORMANCE_DAYS)
        session.add_all([
            ProductPerformance(product_id=product.id, trade_date=day, nav=nav)
            for day, nav in window
        ])
    await session.flush()


async def ensure_demo_products(session) -> None:
    """Seed a clearly fictional catalog without replacing existing rows."""
    catalog = [
        {"code": "NX-CASH", "name": "Nexus 灵活现金管理", "type": "CURRENCY_FUND", "risk_level": "R1", "yield_rate": Decimal("1.85"), "lock_days": 0, "min_purchase": Decimal("100")},
        {"code": "NX-BOND", "name": "Nexus 稳健债券组合", "type": "BOND", "risk_level": "R2", "yield_rate": Decimal("2.80"), "lock_days": 30, "min_purchase": Decimal("1000")},
        {"code": "NX-BAL", "name": "Nexus 平衡配置组合", "type": "STABLE", "risk_level": "R3", "yield_rate": Decimal("4.20"), "lock_days": 90, "min_purchase": Decimal("1000")},
    ]
    existing = set((await session.scalars(select(Product.code).where(Product.code.in_([row["code"] for row in catalog])))).all())
    session.add_all([Product(**row, is_fictional=True, data_date=date.today()) for row in catalog if row["code"] not in existing])
    await session.flush()
    await ensure_product_performance(session)


def _shift_month(value: date, delta: int) -> date:
    index = value.year * 12 + value.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


# 附言是付款方自己写的用途说明。同一家商户在不同月份写不同附言，
# 分类就必须跟着附言走 —— 这正是真实账单里最有信息量的一列。
# (日, 商户, 附言, 分类, 起始金额, 每月增幅, 是否周期性)
STATEMENT_SAMPLES = [
    (5, "安居公寓", "10月房租", "居住", 2600, 0, False),
    (8, "城市地铁", "通勤充值", "交通", 138, 5, False),
    (12, "盒马鲜生", "买菜", "餐饮", 420, 18, False),
    (16, "街角小馆", "晚饭", "餐饮", 286, 11, False),
    (20, "视频会员", "会员自动续费", "订阅", 25, 0, True),
    (21, "云音乐", "会员自动续费", "订阅", 18, 0, True),
    (24, "京东商城", "日用品", "购物", 360, 36, False),
]


def _memo_for(merchant: str, offset: int, today: date) -> str:
    """The memo this merchant wrote in the month ``offset`` months from now.

    Keyed by (merchant, month offset) rather than by day: a row in the current
    month is clamped to today's day, so the day no longer identifies which month
    the memo came from — but the month always does.
    """
    for _, sample_merchant, base_note, *_ in STATEMENT_SAMPLES:
        if sample_merchant != merchant:
            continue
        if merchant == "街角小馆" and offset == -2:
            return "同事聚餐AA"
        if merchant == "京东商城" and offset == -3:
            return "换季衣物"
        if merchant == "京东商城" and offset == 0:
            return "同事生日礼物"
        if merchant == "安居公寓":
            # 付款方写的是自己那个月的房租，不是永远写死一个月份。
            return f"{_shift_month(today.replace(day=1), offset).month}月房租"
        return base_note
    return ""


def _month_offset(value: date, today: date) -> int:
    return (value.year - today.year) * 12 + (value.month - today.month)


async def _backfill_statement_notes(session, user_id: int) -> None:
    """Re-apply the fixture memos to the fixture's own rows.

    The bill fixture is seeded once behind an import-batch marker, so a database
    created before memos existed — or before a memo changed — would keep its rows
    forever. Scope is the demo batch only: a customer's own imported rows are
    matched by batch, never by "has no memo", and are never rewritten.
    """
    batch = await session.scalar(select(ImportBatch).where(
        ImportBatch.user_id == user_id, ImportBatch.file_name == "nexus-demo-statements-v1"))
    if batch is None:
        return
    today = date.today()
    rows = (await session.scalars(
        select(StatementTransaction).where(StatementTransaction.batch_id == batch.id)
    )).all()
    for row in rows:
        note = "换手机" if row.is_anomaly else _memo_for(row.merchant_name, _month_offset(row.txn_date, today), today)
        if note:
            row.note = note
    if rows:
        await session.flush()


async def ensure_demo_statements(session, user_id: int) -> None:
    """Add a stable six-month bill fixture once, including one explainable anomaly."""
    marker = await session.scalar(select(ImportBatch).where(ImportBatch.user_id == user_id, ImportBatch.file_name == "nexus-demo-statements-v1"))
    if marker:
        await _backfill_statement_notes(session, user_id)
        return
    today = date.today()
    batch = ImportBatch(
        user_id=user_id, file_name="nexus-demo-statements-v1", file_type="DEMO",
        total_rows=0, imported_rows=0, status="COMPLETED", started_at=datetime.now(), completed_at=datetime.now(),
    )
    session.add(batch)
    await session.flush()
    rows = []
    for offset in range(-5, 1):
        month = _shift_month(today.replace(day=1), offset)
        for day, merchant, _base_note, category, base, step, recurring in STATEMENT_SAMPLES:
            txn_day = min(day, today.day) if month.year == today.year and month.month == today.month else day
            rows.append(StatementTransaction(
                batch_id=batch.id, user_id=user_id,
                txn_date=month.replace(day=max(txn_day, 1)),
                amount=Decimal(base + (offset + 5) * step),
                merchant_name=merchant, note=_memo_for(merchant, offset, today), category=category,
                is_recurring=recurring, source_row=len(rows) + 1,
            ))
    anomaly_day=min(max(today.day,1),26)
    rows.append(StatementTransaction(batch_id=batch.id,user_id=user_id,txn_date=today.replace(day=anomaly_day),amount=Decimal("3299"),merchant_name="星环数码商店",note="换手机",category="购物",is_anomaly=True,anomaly_reason="金额显著高于近六个月购物类交易基线",source_row=len(rows)+1))
    session.add_all(rows);batch.total_rows=batch.imported_rows=len(rows);await session.flush()


async def ensure_advanced_context(session, user_id: int) -> None:
    """Seed consented mock-tool facts used by the award-level scenario engine."""
    now = datetime.now()
    fixtures = [
        ("GROUP_DINNER", "昨晚四人聚餐", now-timedelta(days=1), {"merchant":"江畔餐厅","total":1200,"participants":["林知夏","李四","王敏","陈宇"],"payer":"李四","user_share":300}),
        ("SALARY", "每月工资到账", now, {"day":28,"amount":18000,"employer":"远航科技"}),
        ("CONTACT_BIRTHDAY", "老王生日", now, {"contact":"老王","date":date.today().isoformat(),"recent_average":176,"suggested":188}),
        ("SPOUSE_BIRTHDAY", "爱人生日", now, {"contact":"爱人","date":(date.today()+timedelta(days=30)).isoformat(),"preferred_brand":"暮光花艺","last_year_gift":"白玫瑰"}),
        ("LATE_NIGHT_DELIVERY", "本周深夜外卖", now, {"orders":4,"amount":380,"baseline_orders":1,"dining_budget_used_pct":85}),
        ("FRAUD_TRANSACTION", "凌晨异地大额交易", now, {"time":"02:03","city":"A市","amount":5000,"merchant":"陌生电子商户","card_last4":"8826","new_device":True}),
        ("SPENDING_FORECAST", "月底信用卡预测", now, {"current_spend":7496,"projected_spend":12000,"credit_limit":10000,"projected_over":2000,"confidence_pct":82}),
        ("MARKET_EVENT", "美联储降息情景", now, {"event":"政策利率下调25bp","holding":"美元货币基金","expected_yield_change_pct":-0.35,"alternative":"Nexus 稳健债券组合"}),
        ("FLIGHT_PURCHASE", "日本机票消费", now, {"destination":"日本","departure_date":(date.today()+timedelta(days=18)).isoformat(),"amount":3280,"current_fx_fee_pct":1.5,"recommended_card":"全币种旅行 JCB 卡"}),
        ("CARD_BENEFIT", "机场权益", now, {"card_last4":"1024","lounge_visits":2,"fast_track":True,"valid_until":(date.today()+timedelta(days=60)).isoformat()}),
        ("SUBSCRIPTION_OFFER", "视频会员平替", now, {"current":"视频会员","current_price":25,"alternative":"电商联合会员","alternative_price":15,"saving_year":120}),
        ("SUBSCRIPTION_USAGE", "云盘长期未使用", now, {"merchant":"云盘会员","days_unused":96,"next_fee":198,"next_charge":(date.today()+timedelta(days=20)).isoformat()}),
        ("HOUSEHOLD_SUBSCRIPTIONS", "家庭订阅汇总", now, {"members":3,"items":6,"monthly_total":120,"family_plan_monthly":86.67,"saving_year":400}),
        ("INVESTMENT_MATURITY", "即将到期理财", now, {"amount":10000,"matures_on":(date.today()+timedelta(days=12)).isoformat(),"redeemable":True}),
        ("TRAVEL_PLAN", "上海三日出差", now, {"destination":"上海","days":3,"weather":"12–18℃，有阵雨","preferred_transport":"高铁","hotel_benefit":"合作酒店双早","temporary_limit_needed":3000}),
        ("FAMILY_RISK", "父亲陌生大额转账", now, {"relation":"父亲","amount":50000,"recipient":"陌生账户","deviation_pct":940,"status":"INTERCEPTED"}),
    ]
    existing = {(row.event_type,row.title) for row in (await session.scalars(select(ContextEvent).where(ContextEvent.user_id==user_id))).all()}
    session.add_all([ContextEvent(user_id=user_id,event_type=t,title=title,occurred_at=at,payload=payload) for t,title,at,payload in fixtures if (t,title) not in existing])
    await session.flush()


async def seed_demo(session) -> int:
    await ensure_demo_products(session)
    existing = await session.scalar(select(User).where(User.phone == "demo-local-owner"))
    if existing:
        if not existing.is_demo:
            raise RuntimeError("演示身份冲突，请使用独立的演示数据库")
        await ensure_demo_statements(session, existing.id)
        recipients = list((await session.scalars(select(Recipient).where(Recipient.user_id == existing.id).order_by(Recipient.id))).all())
        metadata = [("13800001333", "房东"), ("13800001444", "同事")]
        for recipient, (phone, alias) in zip(recipients, metadata):
            recipient.phone = phone
            recipient.alias = alias
        for name, phone, alias in [("妈妈","13800001555","母亲"),("老王","13800001666","好友")]:
            if not await session.scalar(select(Recipient).where(Recipient.user_id==existing.id, Recipient.name==name)):
                person = await session.scalar(select(User).where(User.phone==phone))
                if not person:
                    person=User(name=name,phone=phone,is_demo=True);session.add(person);await session.flush()
                account=await session.scalar(select(Account).where(Account.user_id==person.id,Account.type=="checking"))
                if not account:
                    account=Account(user_id=person.id,type="checking",balance=Decimal("1000"),available_balance=Decimal("1000"));session.add(account);await session.flush()
                session.add(Recipient(user_id=existing.id,name=name,linked_account_id=account.id,bank_name="Nexus 沙箱银行",account_no=f"DEMO-{account.id:04d}",phone=phone,alias=alias))
        await ensure_advanced_context(session, existing.id)
        await ensure_demo_profile(session, existing.id)
        return existing.id
    owner = User(name="林知夏", phone="demo-local-owner", is_demo=True, risk_score="C3")
    people = [User(name=name, phone=f"demo-local-{i}", is_demo=True) for i, name in enumerate(["张三", "李四"])]
    session.add_all([owner, *people])
    await session.flush()
    primary = Account(user_id=owner.id, type="checking", balance=Decimal("28650.00"), available_balance=Decimal("28650.00"))
    destinations = [Account(user_id=p.id, type="checking", balance=Decimal("1000"), available_balance=Decimal("1000")) for p in people]
    session.add_all([primary, *destinations])
    await session.flush()
    recipient_meta = [("13800001333", "房东"), ("13800001444", "同事")]
    for person, account, (phone, alias) in zip(people, destinations, recipient_meta):
        session.add(Recipient(user_id=owner.id, name=person.name, linked_account_id=account.id, bank_name="Nexus 沙箱银行", account_no=f"DEMO-{account.id:04d}", phone=phone, alias=alias))
    for name, phone, alias in [("妈妈","13800001555","母亲"),("老王","13800001666","好友")]:
        person=User(name=name,phone=phone,is_demo=True);session.add(person);await session.flush()
        account=Account(user_id=person.id,type="checking",balance=Decimal("1000"),available_balance=Decimal("1000"));session.add(account);await session.flush()
        session.add(Recipient(user_id=owner.id,name=name,linked_account_id=account.id,bank_name="Nexus 沙箱银行",account_no=f"DEMO-{account.id:04d}",phone=phone,alias=alias))
    session.add_all([
        Card(account_id=primary.id, bank_name="Nexus 日常卡", last4="8826", card_type="DEBIT", single_limit=Decimal("5000"), daily_limit=Decimal("20000")),
        Card(account_id=primary.id, bank_name="Nexus 备用卡", last4="1024", card_type="DEBIT", single_limit=Decimal("2000"), daily_limit=Decimal("10000")),
    ])
    subscriptions = SubscriptionService(session)
    await subscriptions.confirm_subscription(owner.id, "云音乐", Decimal("18"), "MONTHLY")
    await subscriptions.confirm_subscription(owner.id, "视频会员", Decimal("25"), "MONTHLY")
    await ensure_demo_statements(session, owner.id)
    await ensure_advanced_context(session, owner.id)
    await ensure_demo_profile(session, owner.id)
    return owner.id
