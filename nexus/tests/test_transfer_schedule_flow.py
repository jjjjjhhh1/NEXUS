"""转账的"什么时候扣、扣几次"是业务规则，不是措辞问题。

三条真实事故驱动了这组测试：

1. 用户说"十月十号给张三生日转账，**这次**转三万"，系统给出的是"每月 10 日、
   首次 2026-10-10"的按月重复计划，卡片上写着"**每期**金额"。一年后还会再转，
   两年二十四万——用户从未同意过。
2. 确认卡不可修改。日期读错、金额多打一个零，客户只能在执行后申诉。
3. 余额不足后点"取消"没反应，必须回到更早那张确认卡才能取消。

以及三个在改这些的过程中被挖出来的：

4. `_next_run` 用 `date.today()` 重建月份：10 月 20 日说"十月十号"，钱晚一个月才到，
   而且卡片显示的是一个用户没提过的日期。
5. 收款人只认精确姓名，"我妈"匹配不到"妈妈"，用户被迫去翻自己的登记列表。
6. 调度器算下一期时复用了面向用户的解析器，对每周计划不推进，15 秒轮询的 worker
   死循环。

这些都不该靠"以后注意"来防住。每条一个测试。
"""
from datetime import date, timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from nexus.backend.agent import schedule
from nexus.backend.agent.grounder import ground_understanding
from nexus.backend.agent.plan_builder import build_plan, Unresolvable
from nexus.backend.agent.understanding import Understanding


async def _passcode(client) -> str:
    """The sandbox hands the demo passcode to the page on load; tests ask for it."""
    return (await client.post("/api/session")).json()["demo_passcode"]


def read(**overrides) -> Understanding:
    base = dict(
        scene="scheduled_transfer", operation="create_scheduled_transfer",
        recipient="张三", account_handle="张三", amount="30000",
        amount_evidence="三万", day_of_month=10, write_intent=True,
        output="confirmation", read_tools=["recipients", "account"], confidence=0.95,
    )
    return Understanding.model_validate({**base, **overrides})


# ---------------------------------------------------------------------------
# 1. 一次性 vs 按月重复
# ---------------------------------------------------------------------------

def test_unquoted_period_is_downgraded_to_a_single_payment():
    """模型说每月，但用户原话里没有"每月"两个字——不能据此生成长期授权。

    这是本组测试的核心不变量：周期必须有原话依据。缺了就降级成一次。
    降级是安全方向，因为确认卡可改：真被误降的，用户点一下就能改回每月；
    反过来，把一次性的说成每月，钱会在没人同意的时候一直扣。
    """
    u = read(recurrence="monthly", recurrence_evidence=None)
    g = ground_understanding(u, "十月十号给张三转三万块钱")
    assert g.recurrence == "once", "没有原话依据的周期性必须降级为一次性"


def test_quoted_period_survives_grounding():
    u = read(recurrence="monthly", recurrence_evidence="每月")
    g = ground_understanding(u, "每月10号给张三转500块房租")
    assert g.recurrence == "monthly"


def test_once_card_never_says_periodic_amount():
    """一次性转账的卡上不能出现"每期金额"。

    "每期"两个字就是把单笔说成周期的那个动作，用户没仔细看就会以为签了长期授权。
    """
    plan = schedule.resolve(frequency="once", run_date="2026-10-10").with_amount(D("30000"))
    assert plan.is_one_off
    assert plan.amount_label() == "金额"
    assert "每期" not in plan.window_label()
    assert "共 1 笔" in plan.scope_label()


def test_recurring_card_states_total_exposure():
    plan = schedule.resolve(frequency="monthly", day_of_month=10, occurrences=3).with_amount(D("500"))
    assert plan.amount_label() == "每期金额"
    assert "共 3 期" in plan.scope_label()
    assert "1,500.00" in plan.scope_label(), "客户必须在确认前看到总共会被扣走多少钱"


def test_unlimited_plan_says_so():
    plan = schedule.resolve(frequency="monthly", day_of_month=10)
    assert "长期有效" in plan.scope_label()
    assert "直到你暂停" in plan.scope_label()


# ---------------------------------------------------------------------------
# 2. 日期不是周期，且用用户说的那个日期
# ---------------------------------------------------------------------------

def test_one_off_keeps_the_month_the_user_named():
    """用户说了"今年十月十号"，就必须是 10 月 10 日，不是用今天重建出来的下个月。

    原来的实现只保留"几号"，月份拿 date.today() 拼出来，于是 10 月 20 日说
    "十月十号"，钱在 11 月 10 日才到——生日礼物晚到一个月，而卡上写着一个用户
    根本没提过的日期。
    """
    plan = schedule.resolve(frequency="once", run_date="2026-10-10", today=date(2026, 10, 5))
    assert plan.first_run_on == date(2026, 10, 10)


def test_a_day_that_has_already_gone_is_asked_about_not_guessed():
    """只说了"10 号"而这个 10 号已过 → 落到下一个 10 号，并让卡片写清是哪一天。

    月份没被说出来的时候，后端无从知道用户指哪个月。悄悄顺延一个月是替用户
    决定了一笔他没同意的付款；卡片上写明具体日期、客户可改，才是对的。
    """
    plan = schedule.resolve(frequency="once", day_of_month=10, today=date(2026, 10, 20))
    assert plan.first_run_on == date(2026, 11, 10)
    assert "2026-11-10" in plan.window_label(), "卡上必须写明具体日期，让客户能核对"


def test_a_past_named_date_is_refused_rather_than_moved():
    """点名了一个已经过去的日期，不能悄悄改到今天执行。

    那是一笔不同日期、不同性质的钱；改到下个月则是第三种。都要客户自己定。
    """
    with pytest.raises(Exception):
        schedule.resolve(frequency="once", run_date="2020-01-05", today=date(2026, 10, 5))


def test_day_already_passed_rolls_to_the_next_one():
    plan = schedule.resolve(frequency="once", day_of_month=5, today=date(2026, 10, 20))
    assert plan.first_run_on == date(2026, 11, 5)


def test_monthly_still_caps_at_the_28th():
    """1-28 的限制只属于"要重复扣"的计划，二月没有 29 号。"""
    with pytest.raises(Exception):
        schedule.resolve(frequency="monthly", day_of_month=31)
    plan = schedule.resolve(frequency="once", day_of_month=31, today=date(2026, 10, 5))
    assert plan.first_run_on == date(2026, 10, 31), "单次转账不该被重复扣款的限制连累"


def test_thirty_first_skips_months_that_lack_it():
    """11 月没有 31 号。单次预约要等到真正有 31 号的那个月，而不是退化成 28 号。

    退化成 28 号是另一条指令了，而且之后会一直停在 28 号。
    """
    plan = schedule.resolve(frequency="once", day_of_month=31, today=date(2026, 11, 5))
    assert plan.first_run_on == date(2026, 12, 31)


def test_chinese_numerals_ground_the_same_as_digits():
    """用户写"十月十号"和"10 号"是同一个事实，接地层必须都认。"""
    assert schedule.day_is_stated(10, "十月十号给张三转三万")
    assert schedule.day_is_stated(10, "10号给张三转三万")
    assert not schedule.day_is_stated(12, "十月十号给张三转三万")


def test_weekly_from_a_weekday_needs_no_extra_question():
    """用户说了"每周三"就已经把时间说清楚了，不该再追问执行日期。"""
    from nexus.backend.agent.routing import missing_write_slots
    u = read(recipient="我妈", account_handle="我妈", recurrence="weekly",
             recurrence_evidence="每周三", weekday=2, day_of_month=None,
             amount="1000", amount_evidence="1000")
    u = ground_understanding(u, "每周三给我妈转1000")
    assert u.weekday == 2
    assert missing_write_slots(u) == [], "用户说了每周三，不该再追问执行日期"


# ---------------------------------------------------------------------------
# 3. 确认卡可改，且改完必须重新被确认
# ---------------------------------------------------------------------------

async def test_customer_can_change_amount_date_and_cadence_before_confirming(client, seeded):
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(recurrence=None, amount="30000", amount_evidence="三万", day_of_month=10)

    model_module.understand = understands
    card = (await client.post("/api/messages", json={
        "message": "十月十号给张三转三万块钱", "request_id": str(uuid4())})).json()
    assert card["type"] == "confirmation"
    assert "editable" in card and "amount" in card["editable"]
    assert "run_date" in card["editable"], "执行时间必须可改"

    changed = (await client.patch(f"/api/actions/{card['action_id']}", json={
        "amount": "1500", "run_date": "2026-10-12", "purpose": "生日红包",
    })).json()
    assert "¥1,500.00" in changed["detail"]
    assert "2026-10-12" in changed["detail"]
    assert "生日红包" in changed["detail"]


async def test_switching_to_monthly_does_not_inherit_the_one_off_count(client):
    """从"仅此一次"改成"每月"，不能变成"每月，共 1 期"。

    一次性计划的期数是 1，直接继承给"每月"就会得到一个用户明确不要的东西——
    而他刚刚点的就是"每月"。这条曾经真的发生过。
    """
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(recurrence=None, amount="30000", amount_evidence="三万", day_of_month=10)

    model_module.understand = understands
    card = (await client.post("/api/messages", json={
        "message": "十月十号给张三转三万块钱", "request_id": str(uuid4())})).json()
    changed = (await client.patch(f"/api/actions/{card['action_id']}", json={
        "recurrence": "monthly"})).json()
    assert changed["terms"]["frequency"] == "MONTHLY"
    assert changed["terms"]["occurrences"] is None, "改周期必须重新问期数，不能沿用一次的 1 期"
    assert "每月" in changed["title"]
    assert "长期有效" in changed["detail"]


async def test_editing_an_immediate_transfer_keeps_it_immediate(client):
    """改一笔立即转账的金额，卡片不能变成带日期的预约计划。

    曾经无条件用排期文案重写标题，把"确认转账给张三"改成"确认 10 月 5 日转账"——
    一个承诺未来日期、实际当场扣钱的卡。
    """
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(scene="transfer", operation="transfer", recurrence=None,
                    day_of_month=None, amount="1000", amount_evidence="1000")

    model_module.understand = understands
    card = (await client.post("/api/messages", json={
        "message": "现在给张三转1000", "request_id": str(uuid4())})).json()
    assert "月" not in card["title"], f"立即转账不该带日期：{card['title']}"
    changed = (await client.patch(f"/api/actions/{card['action_id']}",
                                  json={"amount": "1200"})).json()
    assert "¥1,200.00" in changed["detail"]
    assert "月" not in changed["title"], "改金额不应该把立即转账变成预约"


async def test_the_payee_cannot_be_re_pointed(client):
    """确认卡可以改条款，但不能改收款人。

    收款人是从用户原话核出来的。把一张待确认的卡改指到另一个账户，等于把
    "我确认"变成"我确认了另一笔"——这是确认卡存在的意义所在。
    """
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(recurrence=None, amount="30000", amount_evidence="三万", day_of_month=10)

    model_module.understand = understands
    card = (await client.post("/api/messages", json={
        "message": "十月十号给张三转三万块钱", "request_id": str(uuid4())})).json()
    assert card["payee_locked"] is True
    assert "收款人" in card["edit_notice"], "限制要写在卡上，不能只是不显示"
    response = await client.patch(f"/api/actions/{card['action_id']}",
                                  json={"amount": "2000"})
    assert response.status_code == 200
    assert "张三" in response.json()["detail"]


async def test_an_unknown_field_cannot_smuggle_a_payee_into_the_editor(client):
    """编辑接口不认收款人字段——多传要报错，不能静默忽略后让人以为改成功了。"""
    card = (await client.post("/api/messages", json={
        "message": "每月10号给张三转账1000元备注房租", "request_id": str(uuid4())})).json()
    response = await client.patch(f"/api/actions/{card['action_id']}",
                                  json={"amount": "500", "recipient": "李四"})
    assert response.status_code == 422, "多出来的字段必须被拒绝"


# ---------------------------------------------------------------------------
# 4. 二次核验失败后，钱没动，而且能就地改
# ---------------------------------------------------------------------------

async def test_insufficient_balance_says_by_how_much_and_moves_nothing(client, seeded):
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(scene="transfer", operation="transfer", recurrence=None, day_of_month=None,
                    amount="999999", amount_evidence="999999")

    model_module.understand = understands
    passcode = await _passcode(client)
    card = (await client.post("/api/messages", json={
        "message": "现在给张三转999999", "request_id": str(uuid4())})).json()
    action_id = card["action_id"]
    await client.post(f"/api/actions/{action_id}/confirm", json={})
    response = await client.post(f"/api/actions/{action_id}/step-up",
                                 json={"echoes": {"amount": "999999"}, "passcode": passcode})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INSUFFICIENT_BALANCE"
    assert body["extra"]["shortfall"], "客户需要一个能照着改的数字，而不是四个字"
    assert float(body["extra"]["shortfall"]) == pytest.approx(
        999999 - float(body["extra"]["available"]), abs=0.01)


async def test_cancelling_after_a_failed_step_up_works(client, seeded):
    """核验失败后点"取消"必须真的取消。

    这条曾经彻底失效：取消按钮调的是 api(url, undefined)，而 api 把"没有 body"
    当成 GET，打在一个只收 POST 的路由上——405，一行滚出屏幕的红字。客户唯一
    能取消的地方是更早那张确认卡，于是他以为应用坏了。
    """
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(scene="transfer", operation="transfer", recurrence=None, day_of_month=None,
                    amount="999999", amount_evidence="999999")

    model_module.understand = understands
    card = (await client.post("/api/messages", json={
        "message": "现在给张三转999999", "request_id": str(uuid4())})).json()
    action_id = card["action_id"]
    await client.post(f"/api/actions/{action_id}/confirm", json={})
    await client.post(f"/api/actions/{action_id}/step-up",
                      json={"echoes": {"amount": "999999"}, "passcode": await _passcode(client)})

    cancelled = await client.post(f"/api/actions/{action_id}/cancel", json={})
    assert cancelled.status_code == 200
    assert "未执行" in cancelled.json()["message"]

    # 取消之后再点确认不能把交易复活。
    again = await client.post(f"/api/actions/{action_id}/confirm", json={})
    assert again.json()["type"] == "message"
    assert "未执行" in again.json()["message"]


async def test_cancelling_while_the_second_factor_is_open(client, seeded):
    """用户点"确认执行"看到核验卡，此时点"取消"必须真的取消——钱没动过。

    这条是用户原话复现：点确认执行 → 提示金额不够 → 回核验卡点取消 → 没反应 →
    只好回到最开始那张确认卡才取消得掉。

    后端曾经只处理 PENDING，对正在核验的单子原样把核验卡再画一遍，看起来就像
    按钮没生效。前端当时还把取消发成了 GET，打在只收 POST 的路由上。两层都得修，
    只修一层这个按钮依然是死的。
    """
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(scene="transfer", operation="transfer", recurrence=None, day_of_month=None,
                    amount="500", amount_evidence="500")

    model_module.understand = understands
    card = (await client.post("/api/messages", json={
        "message": "现在给张三转500", "request_id": str(uuid4())})).json()
    action_id = card["action_id"]

    awaiting = await client.post(f"/api/actions/{action_id}/confirm", json={})
    assert awaiting.json()["type"] == "step_up", "资金类写操作必须先过二次核验"

    cancelled = await client.post(f"/api/actions/{action_id}/cancel", json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["type"] == "message"
    assert "未执行" in cancelled.json()["message"], "取消后必须说清没有执行"

    # 取消之后核验卡不能再把它执行掉。
    replay = await client.post(f"/api/actions/{action_id}/step-up",
                               json={"echoes": {"amount": "500"},
                                     "passcode": await _passcode(client)})
    assert replay.json().get("type") == "message", "已取消的单子不能因为补一次核验就复活"


async def test_editing_after_a_failed_step_up_puts_it_back_in_front_of_the_customer(client, seeded):
    """改了金额就要重新确认——身份核验是为了核那些新的数字。"""
    import nexus.backend.agent.model as model_module

    async def understands(message, context=None):
        return read(scene="transfer", operation="transfer", recurrence=None, day_of_month=None,
                    amount="999999", amount_evidence="999999")

    model_module.understand = understands
    card = (await client.post("/api/messages", json={
        "message": "现在给张三转999999", "request_id": str(uuid4())})).json()
    action_id = card["action_id"]
    await client.post(f"/api/actions/{action_id}/confirm", json={})
    await client.post(f"/api/actions/{action_id}/step-up",
                      json={"echoes": {"amount": "999999"}, "passcode": await _passcode(client)})
    again = await client.patch(f"/api/actions/{action_id}", json={"amount": "100"})
    assert again.status_code == 200
    assert again.json()["type"] == "confirmation", "改完必须回到待确认，不能直接执行"


# ---------------------------------------------------------------------------
# 5. 收款人：客户不必背我们的登记写法
# ---------------------------------------------------------------------------

async def test_a_kinship_handle_resolves_to_the_registered_payee(db, seeded):
    """客户介绍自己妈妈说"我妈"，不是说得不清楚，是我们登记的写法不一样。"""
    from nexus.backend.core.models import Recipient
    from nexus.backend.agent.plan_builder import _resolve_recipient
    async with db() as session:
        row = await session.get(Recipient, seeded["recipient"])
        row.name = "妈妈"
        await session.flush()
        u = read(recipient="我妈", account_handle=None, recurrence=None,
                 amount="1000", amount_evidence="1000", day_of_month=10)
        payee = await _resolve_recipient(session, seeded["user"], u)
        assert payee.name == "妈妈"


async def test_an_unmatched_handle_still_asks(db, seeded):
    """没有唯一候选时必须问，不能挑一个。猜错收款人是这里最贵的错。"""
    from nexus.backend.agent.plan_builder import _resolve_recipient
    async with db() as session:
        u = read(recipient="王阿姨", account_handle=None, recurrence=None,
                 amount="1000", amount_evidence="1000", day_of_month=10)
        with pytest.raises(Unresolvable):
            await _resolve_recipient(session, seeded["user"], u)


# ---------------------------------------------------------------------------
# 6. 排期推进：worker 15 秒跑一次，不能被一个计划拖死
# ---------------------------------------------------------------------------

def test_next_after_always_moves():
    """后继日期必须严格递增。

    调度器原来用面向用户的解析器算下一期，而那个函数收到一个起始日期会原样返回——
    对"每周"来说就是永不推进，while 循环把整个 demo 时钟挂住。
    """
    anchor = date(2026, 10, 7)
    for frequency in (schedule.WEEKLY, schedule.MONTHLY, schedule.QUARTERLY, schedule.YEARLY):
        cadence = schedule.Schedule(frequency=frequency, first_run_on=anchor,
                                    day_of_month=anchor.day)
        current = anchor
        for _ in range(6):
            nxt = cadence.next_after(current)
            assert nxt > current, f"{frequency} 的下一期没有推进：{nxt} <= {current}"
            current = nxt


def test_a_finished_plan_is_not_scheduled_again():
    """约定了 3 期，第 3 期扣完就必须收工。

    这是"连着转三个月"和"一直扣到客户自己发现"之间的那条线。
    """
    plan = schedule.Schedule(frequency=schedule.MONTHLY, first_run_on=date(2026, 10, 10),
                             occurrences=3, day_of_month=10)
    assert plan.occurrences == 3
    assert plan.is_recurring
    assert "第 3 期后自动结束" in plan.scope_label()
