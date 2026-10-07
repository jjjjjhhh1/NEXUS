import pytest_asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import event
from nexus.backend.core import database
from nexus.backend.core.models import User, Account, Recipient, Product, Card
from decimal import Decimal as D
from decimal import Decimal


@pytest.fixture(autouse=True)
def fresh_step_up_state():
    """Second-factor state is process memory, keyed by session id.

    Session ids keep climbing across a run, so this normally does not collide —
    but a suite that inherits a live attempt budget fails for reasons that have
    nothing to do with the code, and "还可以尝试 2 次" in a failure message is a
    genuinely confusing thing to read.
    """
    from nexus.backend.agent.demo_agent import step_up_states
    step_up_states.clear()
    yield
    step_up_states.clear()


@pytest.fixture(autouse=True)
def fresh_rate_buckets():
    """Rate buckets are time-windowed process state and must not leak.

    The ASGI test client always presents the same peer address, so without
    this the first test file to spend the 40-per-minute budget on
    /api/messages starved every file after it — which is how a green
    per-file suite turns into a red full-suite run for reasons that have
    nothing to do with the code under test.
    """
    from nexus.backend.api import app as api_app
    api_app._rate_buckets.clear()
    yield
    api_app._rate_buckets.clear()


@pytest.fixture(autouse=True)
def no_live_model(monkeypatch):
    """Tests must never call a real model.

    Intent understanding is stubbed at the boundary rather than disabled, so the
    suite still exercises the real contract: Understanding schema -> routing ->
    plan_builder -> confirmation card. Tests that need a specific scene override
    ``fake_understand`` with their own handler.
    """
    from nexus.backend.core.config import settings
    from nexus.backend.agent import model
    monkeypatch.setattr(settings, "llm_enabled", False)
    monkeypatch.setattr(settings, "agent_loop", False)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(model, "is_configured", lambda: True)
    monkeypatch.setattr(model, "understand", _rule_based_understanding)


async def _rule_based_understanding(message: str, context=None):
    """Stand-in for the model used by the offline test suite.

    It returns exactly what the real model is asked for: scene, operation,
    quoted slots, the read tools the answer needs, and the output shape. Anything
    it does not recognise comes back as a low-confidence clarify, so a command
    form this stub does not know still fails its test — exactly as a form the
    real model misreads would.

    Tests that need one specific reading override ``model.understand``
    themselves, so the rest of the pipeline still runs for real.
    """
    import re
    from nexus.backend.agent.parser import evidence_amount
    from nexus.backend.agent.understanding import Understanding

    text = message.strip().rstrip("。！!").strip()
    prior = context or {}

    def build(**kwargs):
        base = dict(
            scene="account_query", operation=None, recipient=None, amount=None,
            amount_evidence=None, last4=None, merchant=None, account_handle=None,
            day_of_month=None, participant_count=None, product_code=None, order_id=None,
            period=None, base_currency=None, quote_currency=None, event_date=None,
            keyword=None, limit_type=None, card_type=None, option=None, goal=None,
            read_tools=[], output="text", reasoning=None, missing_information=[],
            confidence=0.95, write_intent=False,
        )
        base.update(kwargs)
        return Understanding.model_validate(base)

    def numeric(raw: str) -> str | None:
        """What the model would return: a precise decimal string, no trailing zeros."""
        try:
            text = f"{evidence_amount(raw).quantize(Decimal('0.01')):f}"
        except Exception:
            return raw
        return text.rstrip("0").rstrip(".") if "." in text else text

    # -- greeting / smalltalk ---------------------------------------------
    # The real model is asked to separate "no request at all" (greeting) from
    # "a request we do not serve" (smalltalk). The stub has to agree, or this
    # suite would only exercise the degraded escalate path for greetings.
    if re.fullmatch(
        r"[\s，。！？!?,.、~～]*"
        r"(你好|您好|哈喽|嗨|hi|hello|在吗|在不在|早上好|中午好|下午好|晚上好|"
        r"谢谢|多谢|辛苦了|没事了|拜拜|再见)"
        r"[\s，。！？!?,.、~～]*",
        text, re.I,
    ):
        return build(
            scene="greeting", read_tools=[], output="text", confidence=0.95,
            reasoning="用户只是打招呼，没有提出任何银行业务需求，直接寒暄回应即可",
        )
    if re.search(r"写.{0,2}诗|写.{0,2}首诗|讲.{0,2}笑话|作诗|陪我聊天|写个?代码|编程|爬虫|"
                 r"前端|写个?java|今天天气|明天天气|会不会下雨|电影推荐|游戏攻略|失恋|心情不好", text, re.I):
        return build(
            scene="smalltalk", read_tools=[], output="text", confidence=0.95,
            reasoning="用户提出了一个与金融无关的要求",
        )
    if re.search(r"企业贷款|对公贷款|公司贷款|经营贷|社保卡|医保卡|公积金|"
                 r"炒股|买股票|卖股票|荐股|代我交易股票|保证收益|内幕消息|内幕交易", text):
        return build(
            scene="unsupported", read_tools=[], output="text", confidence=0.95,
            reasoning="与金融相关，但当前能力不覆盖这项业务",
        )

    # -- external data ----------------------------------------------------
    if re.search(r"\d+\s*(?:人民币|元)?\s*(?:能)?换(?:多少)?\s*美元|汇率", text):
        return build(scene="external_data", read_tools=["fx"], output="text", reasoning="汇率换算")
    if "GDP" in text or "宏观" in text:
        return build(scene="external_data", read_tools=["macro"], output="text", reasoning="宏观指标")
    if "SEC" in text or "财报" in text or "披露" in text:
        return build(scene="external_data", read_tools=["sec"], output="text",
                     keyword=text, reasoning="公司披露")

    # -- scope ------------------------------------------------------------
    if re.search(r"天气|笑话|写[^，。]{0,4}诗|唱歌|电影推荐|写代码|编程|Python|爬虫|失恋", text):
        return build(scene="smalltalk", output="text", reasoning="与金融无关")
    if re.search(r"股票|炒股|贷款|社保|公积金", text):
        return build(scene="unsupported", output="text", reasoning="相关但当前不支持")

    if re.search(r"你能做什么|有哪些功能|怎么用|能帮我做什么", text):
        return build(scene="capabilities", output="text", reasoning="用户询问助手能力")

    # -- risk assessment ---------------------------------------------------
    if re.search(r"风险测评|风险评估|风险等级|测评报告|我(?:是)?(?:什么|哪种)风险", text):
        return build(scene="risk_assessment", read_tools=["financial_profile", "account"],
                     output="analysis", reasoning="需要客观承受能力加主观问卷共同定级")

    # -- handoff ----------------------------------------------------------
    if text in {"创建客服工单", "创建人工接管工单"}:
        return build(scene="handoff",
                     operation="create_human_handoff" if "人工" in text else "create_support_ticket",
                     write_intent=True, output="confirmation", read_tools=[],
                     reasoning="把已整理的诉求提交为工单或人工接管")

    # -- birthday write ----------------------------------------------------
    m = re.fullmatch(r"创建生日计划\s*日期(20\d{2}-\d{2}-\d{2})\s*预算([0-9]+(?:\.[0-9]{1,2})?)元\s*方案([ABC])", text)
    if m:
        raw_date, budget, option = m.groups()
        return build(scene="birthday", operation="create_birthday_plan", write_intent=True,
                     output="confirmation", event_date=raw_date,
                     amount=budget, amount_evidence=budget, option=option,
                     read_tools=["events", "calendar", "account"],
                     reasoning="按已选方案预留生日预算")

    # -- scheduled transfer ------------------------------------------------
    m = re.fullmatch(r"(?:请)?(?:创建|设置|安排)?(?:一个)?每月\s*(\d{1,2})\s*[号日]\s*给(.{1,20}?)转账\s*([0-9]+(?:\.[0-9]+)?)\s*元?(?:[，,\s]*(?:备注|用于|用途是?)\s*(.{1,100}))?", text)
    if m:
        day, handle, amount, purpose = m.groups()
        known = {"张三", "李四", "妈妈", "老王"}
        return build(
            scene="scheduled_transfer", operation="create_scheduled_transfer",
            write_intent=True, output="confirmation",
            recipient=handle if handle in known else None,
            account_handle=None if handle in known else handle,
            amount=amount, amount_evidence=amount, day_of_month=int(day),
            read_tools=["recipients", "account"], reasoning="每月定时给固定收款人转账",
        )

    # -- AA collection -----------------------------------------------------
    m = re.fullmatch(
        r"(?:请)?(?:帮我|帮忙|替我|给我)?\s*(?:发起|创建|设置)?\s*(\d{1,2})\s*人\s*"
        r"AA(?:收款|分摊)\s*([0-9]+(?:\.[0-9]+)?)\s*元?"
        r"(?:[，,\s]*(?:备注|用于)\s*(.{1,100}))?", text, re.I)
    if m:
        count, total, purpose = m.groups()
        return build(
            scene="aa_collection", operation="create_aa_collection",
            write_intent=True, output="confirmation",
            participant_count=int(count), amount=total, amount_evidence=total,
            read_tools=["account"], reasoning="发起多人分摊收款",
        )
    if re.search(r"AA\s*(收款|任务|分摊)", text, re.I) and re.search(r"查看|我的|看看", text):
        return build(scene="aa_collection", write_intent=False, read_tools=["account"],
                     output="table", reasoning="查看 AA 收款任务")

    # -- transfer ---------------------------------------------------------
    polite = r"(?:请|麻烦你?|麻烦|帮我|帮忙|替我|给我|我要|我想|再|然后|顺便|另外)?"
    verb = r"(?:转账|转钱|打款|打给|打|发红包|发个红包|发|转)"
    number = r"(-?[0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万]+)"
    tail = r"(?:\s*(?:元|块|块钱))?(?:[，,\s]*(?:备注|用于|作为|用途是?)\s*(.{1,100}))?"
    known = {"张三", "李四", "妈妈", "老王", "房东", "13800001333"}

    # A bare amount is a follow-up: the payee came from the previous turn and
    # is already grounded, so it does not need to appear again.
    if re.fullmatch(rf"{number}\s*(?:元|块|块钱)[。！!]?", text) and \
            prior.get("scene") == "transfer" and prior.get("recipient"):
        bare = re.fullmatch(rf"({number})\s*(?:元|块|块钱)[。！!]?", text)
        return build(
            scene="transfer", operation="transfer", write_intent=True, output="confirmation",
            recipient=prior["recipient"], amount=numeric(bare.group(1)),
            amount_evidence=bare.group(0),
            read_tools=["recipients", "account"],
            reasoning="用户补充了上一轮缺失的金额，收款人沿用已核验槽位",
        )

    payee_first = re.fullmatch(
        rf"{polite}\s*(?:AA\s*)?给\s*([一-龥A-Za-z0-9]{{1,20}}?)\s*{verb}\s*(?:AA\s*)?{number}{tail}",
        text, re.I)
    verb_first = re.fullmatch(
        rf"{polite}\s*(?:AA\s*)?{verb}\s*(?:AA\s*)?(?:给|向)?\s*"
        rf"([一-龥A-Za-z0-9]{{1,20}}?)\s*{number}{tail}", text, re.I)
    match = payee_first or verb_first
    if match:
        handle, amount, remark = match.groups()
        handle = re.sub(r"(?i)aa", "", handle).strip() or prior.get("recipient") or ""
        recipient = handle if handle in known else None
        return build(
            scene="transfer", operation="transfer", write_intent=True, output="confirmation",
            recipient=recipient, account_handle=None if recipient else (handle or None),
            amount=numeric(amount), amount_evidence=amount,
            read_tools=["recipients", "account"], reasoning="转账给用户点名的收款人",
        )
    # A transfer named with a payee but no amount yet: ask, do not assume.
    # "帮我把钱转给老王" — the 把钱 filler is ordinary Chinese and the model
    # reads straight through it.
    vague = re.fullmatch(
        rf"{polite}\s*(?:AA\s*)?(?:把\s*)?(?:钱|钱款|钱儿)?\s*(?:给\s*([一-龥A-Za-z0-9]{{1,20}}?)\s*(?:转点|转账|转).{{0,3}}|"
        rf"{verb}\s*(?:AA\s*)?(?:给|向)?\s*([一-龥A-Za-z0-9]{{1,20}}?)\s*)$", text, re.I)
    if vague:
        handle = (vague.group(1) or vague.group(2) or "").strip()
        return build(
            scene="transfer", operation="transfer", write_intent=True, output="clarify",
            recipient=handle if handle in known else None,
            account_handle=None if handle in known else (handle or None),
            read_tools=["recipients"], reasoning="用户说了收款人但还没说金额",
            missing_information=["金额"],
        )

    # -- card -------------------------------------------------------------
    if re.search(r"申请.{0,6}卡", text):
        return build(scene="card", operation="apply_card", write_intent=True,
                     output="confirmation", card_type="CREDIT", read_tools=["cards"],
                     reasoning="申请一张新卡")
    last4 = re.search(r"(\d{4})", text)
    limit = re.search(r"(?:调到|调整到|设置为|到)\s*([0-9]+(?:\.[0-9]+)?)", text)
    card_verb = re.search(r"(挂失|解锁|锁定|锁卡)", text)
    if card_verb or re.search(r"限额|额度", text):
        if limit:
            return build(
                scene="card", operation="set_card_limit", write_intent=True,
                output="confirmation", last4=last4.group(1) if last4 else None,
                amount=limit.group(1),
                limit_type="daily" if "每日" in text else "single",
                read_tools=["cards"], reasoning="调整卡片限额",
            )
        operation = {"挂失": "report_lost", "解锁": "unlock_card"}.get(card_verb.group(1), "lock_card")
        return build(
            scene="card", operation=operation, write_intent=True, output="confirmation",
            last4=last4.group(1) if last4 else None, read_tools=["cards"],
            reasoning="卡片状态变更",
        )
    if re.search(r"我的卡|查看卡片|卡片列表", text):
        return build(scene="card", write_intent=False, read_tools=["cards"],
                     output="table", reasoning="查看名下卡片")

    # -- subscription / mandate -------------------------------------------
    merchant = re.search(r"(云音乐|视频会员|NX-[A-Z]+|Test)", text)
    if re.search(r"识别|检测", text) and re.search(r"扣费|订阅", text):
        return build(scene="subscription", write_intent=False,
                     read_tools=["subscriptions", "bills"], output="table",
                     reasoning="识别周期扣费与订阅状态")
    if merchant and re.search(r"代扣|扣款|停止扣", text):
        return build(scene="subscription", operation="revoke_mandate", write_intent=True,
                     output="confirmation", merchant=merchant.group(1),
                     read_tools=["subscriptions"], reasoning="撤销商户代扣授权")
    if merchant and re.search(r"取消|退订|解约|退掉|退了|别续", text):
        return build(scene="subscription", operation="cancel_subscription", write_intent=True,
                     output="confirmation", merchant=merchant.group(1),
                     read_tools=["subscriptions"], reasoning="终止订阅合同")
    if re.search(r"查看订阅|我的订阅", text):
        return build(scene="subscription", write_intent=False,
                     read_tools=["subscriptions"], output="table", reasoning="查看订阅状态")

    # -- investment products ----------------------------------------------
    order = re.search(r"订单\s*#?\s*(\d+)", text)
    if re.search(r"赎回", text):
        return build(scene="financial_profile", operation="redeem_product", write_intent=True,
                     output="confirmation",
                     order_id=int(order.group(1)) if order else None,
                     read_tools=["products"], reasoning="赎回持仓")
    code = re.search(r"(NX-[A-Z]+|TEST)", text)
    if re.search(r"申购", text) and code:
        amount = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
        return build(scene="financial_profile", operation="subscribe_product",
                     write_intent=True, output="confirmation",
                     product_code=code.group(1),
                     amount=amount.group(1) if amount else None,
                     read_tools=["products"], reasoning="申购理财产品")
    if re.search(r"理财产品|产品对比|有哪些产品|持仓|投资订单", text):
        return build(scene="financial_profile", write_intent=False, read_tools=["products"],
                     output="table", reasoning="对比理财产品与持仓")

    # -- scheduled transfer listing ---------------------------------------
    if re.search(r"定时转账|转账计划", text):
        return build(scene="scheduled_transfer", write_intent=False,
                     read_tools=["account"], output="table", reasoning="查看定时转账计划")

    # -- bill analysis ----------------------------------------------------
    if re.search(r"账单|消费|支出|花了多少|分类|异常|年度", text):
        return build(scene="bill_analysis", write_intent=False,
                     period="year" if "年" in text else "month",
                     read_tools=["bills", "account"], output="chart", reasoning="账单分析")

    # -- financial planning ------------------------------------------------
    if re.search(r"理财分析|资产配置|资金规划|投资规划", text):
        return build(scene="financial_planning", write_intent=False,
                     read_tools=["account", "financial_profile", "products"],
                     output="analysis", reasoning="个性化理财规划")

    # -- cross-scene goals --------------------------------------------------
    cross_tools = {
        "上海": ["travel_context", "calendar", "account", "cards", "card_benefits"],
        "出差": ["travel_context", "calendar", "account", "cards"],
        "父亲": ["family_risk", "cards", "account"],
        "母亲": ["family_risk", "cards", "account"],
        "降息": ["market_events", "products", "financial_profile"],
        "未使用": ["subscriptions", "subscription_usage", "bills"],
        "聚餐": ["social_context", "bills", "recipients", "account"],
        "生日": ["events", "calendar", "social_context", "account"],
    }
    for keyword, tools in cross_tools.items():
        if keyword in text:
            reasoning = "生日跨场景任务" if keyword == "生日" else f"{keyword}相关的跨场景任务"
            # The birthday intake is selected by the structured goal, not by the
            # word 生日 appearing in the reasoning prose.
            return build(scene="cross_scene", write_intent=False, read_tools=tools,
                         output="plan", reasoning=reasoning,
                         goal="birthday_plan" if keyword == "生日" else None)
    if re.search(r"诉求|需求|帮我理解|请理解", text):
        return build(scene="cross_scene", write_intent=False,
                     read_tools=["account", "bills", "cards", "subscriptions"],
                     output="plan", reasoning="开放式诉求，需要跨域取数后给方案")

    # -- a bare amount continues the previous write --------------------------
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*(?:元|块|块钱)?", text) and prior.get("scene"):
        return build(scene=prior["scene"], operation=prior.get("operation"),
                     write_intent=True, output="confirmation",
                     recipient=prior.get("recipient"),
                     account_handle=prior.get("account_handle"),
                     amount=numeric(text), amount_evidence=text,
                     read_tools=["recipients", "account"],
                     reasoning=f"承接上一轮的 {prior['scene']}，补上金额")

    # -- plain account reads ------------------------------------------------
    if re.search(r"余额|账户|流水|概览|查看|我的|收入|进账|工资|到账|花了多少", text):
        tools = []
        if "余额" in text or "账户" in text or "概览" in text:
            tools.append("account")
        if "流水" in text or "收入" in text or "进账" in text or "到账" in text or "工资" in text:
            tools.append("bills")
        if not tools:
            tools.append("account")
        return build(scene="account_query", read_tools=tools,
                     output="table", reasoning="查询本人账户数据")

    return build(scene="account_query", output="clarify", confidence=0.5,
                 missing_information=["明确要办理的业务"],
                 reasoning="无法确定意图")


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_async_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    await database.init_db()
    yield database.session_scope
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db):
    from nexus.backend.api.app import app
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Nexus-Demo": "1"},
        ) as test_client:
            response = await test_client.post("/api/session")
            assert response.status_code == 200
            yield test_client


@pytest_asyncio.fixture
async def seeded(db):
    async with db() as s:
        user = User(name="Test", phone="test", risk_score="C3")
        other = User(name="Other", phone="other")
        s.add_all([user, other]); await s.flush()
        a = Account(user_id=user.id, type="checking", balance=D("1000"), available_balance=D("1000"))
        b = Account(user_id=other.id, type="checking", balance=D("100"), available_balance=D("100"))
        s.add_all([a, b]); await s.flush()
        r = Recipient(user_id=user.id, name="张三", linked_account_id=b.id)
        p = Product(code="TEST", name="Demo", type="CURRENCY_FUND", risk_level="R1", min_purchase=D("1"), lock_days=0)
        c = Card(account_id=a.id, bank_name="Demo", last4="1234", card_type="DEBIT")
        s.add_all([r, p, c]); await s.flush()
        return dict(user=user.id, other=other.id, account=a.id, destination=b.id, recipient=r.id, product=p.id, card=c.id)
