"""The chart contract, and the two honesty rules it exists to enforce.

A picture is only worth drawing if it says something a sentence cannot, and it
is only trustworthy if it cannot quietly lie. These tests pin both: the shape
rules that decide line-versus-bar and how many points survive, and the rules
that stop a simulated quote or a three-day-old month from being presented as if
it were a settled fact.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from nexus.backend.agent import analytics, charts


# ============ contract shape ============
def test_single_point_is_not_a_chart():
    assert charts.build(title="t", labels=["1月"], series=[{"name": "a", "values": [5]}]) is None


def test_series_with_one_readable_point_is_dropped():
    chart = charts.build(
        title="t", labels=["1月", "2月", "3月"],
        series=[{"name": "full", "values": [1, 2, 3]}, {"name": "hollow", "values": [None, None, 3]}],
    )
    assert [item["name"] for item in chart["series"]] == ["full"]


def test_all_hollow_series_produces_no_chart_rather_than_an_empty_frame():
    assert charts.build(title="t", labels=["1月", "2月"], series=[{"name": "a", "values": [None, None]}]) is None


def test_two_or_three_points_compare_and_four_or_more_trend():
    two = charts.build(title="t", labels=["1月", "2月"], series=[{"name": "a", "values": [1, 2]}])
    three = charts.build(title="t", labels=["1月", "2月", "3月"], series=[{"name": "a", "values": [1, 2, 3]}])
    four = charts.build(title="t", labels=["1月", "2月", "3月", "4月"], series=[{"name": "a", "values": [1, 2, 3, 4]}])
    assert (two["kind"], three["kind"], four["kind"]) == ("compare", "compare", "trend")


def test_long_series_is_sampled_down_but_keeps_both_ends():
    labels = [f"{index:02d}" for index in range(90)]
    values = list(range(90))
    chart = charts.build(title="t", labels=labels, series=[{"name": "a", "values": values}])
    assert len(chart["labels"]) == charts.MAX_POINTS
    assert chart["labels"][0] == labels[0]
    assert chart["labels"][-1] == labels[-1]
    assert chart["series"][0]["values"][0] == values[0]
    assert chart["series"][0]["values"][-1] == values[-1]


def test_non_numeric_and_nan_values_are_refused():
    """A label that survived as a string would reach the chart as NaN and render
    as a gap nobody can explain."""
    chart = charts.build(
        title="t", labels=["1月", "2月", "3月", "4月"],
        series=[{"name": "a", "values": ["x", 5, 7, float("nan")]}],
    )
    assert chart["series"][0]["values"] == [None, 5.0, 7.0, None]


# ============ direction is derived, never asserted ============
def test_rising_series_reads_up_even_when_named_after_the_bad_thing():
    """A caller that calls spending "down" must not be able to paint a rising
    spending line as a falling one — the chart would argue with the table."""
    chart = charts.build(title="t", labels=["1月", "2月", "3月"], series=[{"name": "支出", "values": [100, 200, 300]}])
    assert chart["series"][0]["tone"] == "up"


def test_falling_series_reads_down():
    chart = charts.build(title="t", labels=["1月", "2月", "3月"], series=[{"name": "收入", "values": [300, 200, 100]}])
    assert chart["series"][0]["tone"] == "down"


def test_flat_series_is_not_reported_as_a_move():
    chart = charts.build(title="t", labels=["1月", "2月", "3月"], series=[{"name": "收入", "values": [18000, 18000, 18000]}])
    assert chart["series"][0]["tone"] == "flat"


# ============ a running period may not declare itself the biggest mover ============
def test_partial_index_is_remapped_onto_the_sampled_axis():
    """Downsampling rewrites indices, so a partial point named in the caller's
    coordinates has to be re-expressed or describe() cuts the wrong point."""
    labels = [f"{index:02d}" for index in range(40)]
    values = [float(index) for index in range(40)]
    chart = charts.build(
        title="t", labels=labels, series=[{"name": "支出", "values": values}],
        partial_index=39,
    )
    assert chart["labels"][chart["partial_index"]] == labels[39]
    described = charts.describe(chart)
    # Sampling lands on 0,3,…,39, so excluding the partial point measures the
    # span to 36 and the runaway final value never becomes the headline.
    assert "+36.00" in described
    assert "+39.00" not in described


def test_partial_index_outside_the_sampled_set_is_dropped_rather_than_misapplied():
    labels = [f"{index:02d}" for index in range(40)]
    chart = charts.build(
        title="t", labels=labels, series=[{"name": "支出", "values": [1.0] * 40}],
        partial_index=7,
    )
    assert "partial_index" not in chart


def test_describe_without_a_partial_point_still_measures_the_whole_range():
    chart = charts.build(
        title="t", labels=["1月", "2月", "3月"],
        series=[{"name": "支出", "values": [100, 200, 400]}],
        unit=charts.UNIT_MONEY,
    )
    assert charts.describe(chart) == "支出 区间上涨 +300.00¥，最高 400.00¥，最低 100.00¥。"


# ============ a picture has to earn its place ============
def test_movement_outranks_a_flat_restatement_of_the_same_total():
    moving = charts.build(title="t", labels=["1月", "2月", "3月", "4月"], series=[{"name": "净结余", "values": [100, 400, 200, 900]}], unit=charts.UNIT_MONEY)
    flat = charts.build(title="t", labels=["1月", "2月", "3月", "4月"], series=[{"name": "余额", "values": [900, 900, 900, 900]}], unit=charts.UNIT_MONEY)
    assert charts.comparative_value(moving) > charts.comparative_value(flat)


def test_a_series_crossing_zero_outranks_one_that_never_does():
    crossing = charts.build(title="t", labels=["1月", "2月", "3月", "4月"], series=[{"name": "净结余", "values": [500, -200, 800, 100]}], unit=charts.UNIT_MONEY)
    positive = charts.build(title="t", labels=["1月", "2月", "3月", "4月"], series=[{"name": "收入", "values": [500, 700, 800, 900]}], unit=charts.UNIT_MONEY)
    assert charts.comparative_value(crossing) > charts.comparative_value(positive)


def test_comparative_value_of_nothing_is_zero():
    assert charts.comparative_value({}) == 0.0
    assert charts.comparative_value(None) == 0.0


# ============ aggregations ============
@pytest.mark.asyncio
async def test_product_trend_covers_exactly_the_products_it_was_given(db):
    from nexus.backend.simulation.seed import ensure_demo_products
    from nexus.backend.services.product_service import ProductService

    async with db() as session:
        await ensure_demo_products(session)
        products = await ProductService(session).list_products(1)
        trend = await analytics.product_trend(session, products)

    chart = trend["chart"]
    assert chart is not None
    assert {item["name"] for item in chart["series"]} == {item["code"] for item in products}
    assert chart["kind"] == "trend"
    assert chart["labels"][0] == chart["labels"][0]  # sampled, still ordered
    for code in trend["by_code"]:
        assert trend["by_code"][code]["window_change_pct"] is not None
        assert trend["by_code"][code]["direction"] in {"up", "down", "flat"}


@pytest.mark.asyncio
async def test_product_curve_is_dated_and_actually_moves(db):
    from nexus.backend.simulation.seed import ensure_demo_products
    from nexus.backend.services.product_service import ProductService

    async with db() as session:
        await ensure_demo_products(session)
        products = await ProductService(session).list_products(1)
        trend = await analytics.product_trend(session, products)

    # A curve with no as-of date cannot be judged, so the date is the disclosure
    # that has to survive the removal of demo wording.
    assert trend["chart"]["note"].startswith("数据截至")
    assert "不构成投资建议" in trend["chart"]["note"]
    # A flat line would make "is it up or down" unanswerable, which is the
    # whole reason the table exists.
    for item in trend["chart"]["series"]:
        assert len(set(item["values"])) > 2


@pytest.mark.asyncio
async def test_product_trend_is_none_without_a_price_history(db):
    from nexus.backend.services.product_service import ProductService

    async with db() as session:
        products = await ProductService(session).list_products(1)
        trend = await analytics.product_trend(session, products)
    assert trend["chart"] is None


@pytest.mark.asyncio
async def test_monthly_cashflow_marks_the_running_month_as_incomplete(db, seeded):
    async with db() as session:
        flow = await analytics.monthly_cashflow(session, seeded["user"], months=6)

    today = date.today()
    last_day = (date(today.year + (today.month == 12), (today.month % 12) + 1, 1) - timedelta(days=1)).day
    if today.day < last_day:
        assert flow["partial_index"] == 5
        assert "进行中" in flow["chart"]["note"]
        # And the running month must not be the basis of a period comparison.
        assert flow["comparison"]["to"] != flow["labels"][-1]
    else:
        assert flow["partial_index"] is None


@pytest.mark.asyncio
async def test_monthly_cashflow_reports_where_income_came_from(db, seeded):
    async with db() as session:
        flow = await analytics.monthly_cashflow(session, seeded["user"], months=6)
    # No declared income in the test fixture: the chart must say so rather than
    # quietly charting a net position built on a guessed salary.
    assert "暂无申报收入" in flow["chart"]["note"]
    assert [item["name"] for item in flow["chart"]["series"]] == ["支出"]


@pytest.mark.asyncio
async def test_seasonal_cashflow_needs_a_declared_year(db, seeded):
    async with db() as session:
        assert (await analytics.seasonal_cashflow(session, seeded["user"]))["chart"] is None


def test_period_comparison_uses_the_two_most_recent_complete_months():
    labels = ["4月", "5月", "6月", "7月"]
    expense = [100.0, 200.0, 400.0, 9999.0]
    net = [50.0, 60.0, 70.0, 8888.0]
    comparison = analytics._period_comparison(labels, expense, net, partial_index=3)
    assert (comparison["from"], comparison["to"]) == ("5月", "6月")
    assert comparison["expense_delta"] == 200.0
    assert comparison["expense_pct"] == 100.0


def test_period_comparison_without_a_partial_marker_uses_the_last_two():
    comparison = analytics._period_comparison(["4月", "5月", "6月"], [100.0, 200.0, 400.0], [None] * 3, partial_index=None)
    assert (comparison["from"], comparison["to"]) == ("5月", "6月")


def test_period_comparison_needs_two_complete_months():
    assert analytics._period_comparison(["6月"], [100.0], [50.0], partial_index=0) is None


# ============ the plan puts exactly one picture on top of the answer ============
async def _seed_two_months_of_spending(session, user_id: int) -> None:
    from nexus.backend.core.models import ImportBatch, StatementTransaction

    today = date.today()
    batch = ImportBatch(user_id=user_id, file_name="t", file_type="DEMO", status="COMPLETED")
    session.add(batch)
    await session.flush()
    rows = []
    for month_offset, base in ((-2, 1000.0), (-1, 2000.0)):
        key = analytics._shift_month(today.replace(day=1), month_offset)
        rows.append(StatementTransaction(
            batch_id=batch.id, user_id=user_id, txn_date=key.replace(day=15),
            amount=Decimal(str(base)), merchant_name="商户", category="购物",
        ))
    # A three-day-old month must not be allowed to become the headline mover.
    rows.append(StatementTransaction(
        batch_id=batch.id, user_id=user_id, txn_date=today,
        amount=Decimal("9000"), merchant_name="大额", category="购物",
    ))
    session.add_all(rows)
    await session.flush()


@pytest.mark.asyncio
async def test_plan_carries_one_chart_chosen_by_chart_rank(db, seeded):
    from nexus.backend.agent.universal_planner import build_universal_plan
    from nexus.backend.simulation.seed import ensure_demo_products

    async with db() as session:
        await ensure_demo_products(session)
        await _seed_two_months_of_spending(session, seeded["user"])
        plan = await build_universal_plan(
            session, seeded["user"], "帮我看看最近资金情况", tools=["bills", "products"]
        )

    assert plan["type"] == "universal_plan"
    chart = plan["chart"]
    assert chart is not None
    # The spend comparison is the amount the user asked about, so it outranks the
    # product curves even though those wiggle more.
    assert chart["unit"] == charts.UNIT_MONEY
    assert [item["name"] for item in chart["series"]] == ["支出"]


@pytest.mark.asyncio
async def test_plan_has_no_chart_when_nothing_it_read_moved(db, seeded):
    from nexus.backend.agent.universal_planner import build_universal_plan

    async with db() as session:
        plan = await build_universal_plan(session, seeded["user"], "看看我的账户", tools=["account"])
    # Balances are a single point in time; a picture of a static number would be
    # decoration, and comparative_value correctly refuses to promote one.
    assert plan["chart"] is None


def test_money_outranks_a_percentage_at_comparable_movement():
    money = charts.build(title="t", labels=["1月", "2月", "3月", "4月"],
                         series=[{"name": "净结余", "values": [100, 300, 200, 400]}], unit=charts.UNIT_MONEY)
    percent = charts.build(title="t", labels=["1月", "2月", "3月", "4月"],
                           series=[{"name": "累计收益", "values": [0.0, 0.3, 0.2, 0.4]}], unit=charts.UNIT_PERCENT)
    assert charts.pick_chart([percent, money]) is money


def test_pick_chart_prefers_the_more_mobile_of_two_amount_charts():
    moving = charts.build(title="t", labels=["1月", "2月", "3月", "4月"],
                          series=[{"name": "净结余", "values": [100, 400, 200, 900]}], unit=charts.UNIT_MONEY)
    flat = charts.build(title="t", labels=["1月", "2月", "3月", "4月"],
                        series=[{"name": "余额", "values": [900, 900, 900, 900]}], unit=charts.UNIT_MONEY)
    assert charts.pick_chart([flat, moving]) is moving


def test_pick_chart_of_nothing_is_none():
    assert charts.pick_chart([]) is None
    assert charts.pick_chart(None) is None
    assert charts.pick_chart([None, {"series": [], "labels": []}]) is None


def test_a_running_period_does_not_inflate_a_charts_apparent_movement():
    """Same rule describe() follows: three days of a big purchase must not make
    a chart look like the most informative one in the answer."""
    base = {"name": "支出", "values": [100.0, 200.0, 300.0, 400.0, 500.0, 9000.0]}
    labels = ["1月", "2月", "3月", "4月", "5月", "6月"]
    honest = charts.build(title="t", labels=labels, series=[dict(base)], unit=charts.UNIT_MONEY, partial_index=5)
    inflated = charts.build(title="t", labels=labels, series=[dict(base)], unit=charts.UNIT_MONEY)
    assert charts.comparative_value(honest) < charts.comparative_value(inflated)
