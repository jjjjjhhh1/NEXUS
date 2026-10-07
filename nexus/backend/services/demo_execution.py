"""Persisted, explicitly authorized demo jobs. Never contacts real merchants."""
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select
from ..core import database
from ..core.models import Plan, PlanOrder, AuditLog
from ..core.logging import logger
from .scheduled_transfer_service import ScheduledTransferService


async def run_due(now: datetime, user_id: int | None = None) -> dict:
    async with database.session_scope() as session:
        transfers = await ScheduledTransferService(session).run_due(now, user_id)
        query = select(Plan).where(Plan.status == "ACTIVE", Plan.type == "BIRTHDAY")
        if user_id is not None:
            query = query.where(Plan.user_id == user_id)
        orders = []
        for plan in (await session.scalars(query)).all():
            if now.date() < plan.event_date - timedelta(days=plan.order_lead_days):
                continue
            consent = await session.scalar(select(AuditLog.id).where(
                AuditLog.action == "AUTHORIZE_BIRTHDAY_SIMULATION", AuditLog.target_id == plan.id,
                AuditLog.target_type == "plan", AuditLog.user_id == plan.user_id))
            if consent is None:
                continue
            items = list((await session.scalars(select(PlanOrder).where(PlanOrder.plan_id == plan.id))).all())
            total = sum(item.product_price * item.quantity for item in items)
            if not items or total > plan.reserved_amount:
                plan.status = "NEEDS_ATTENTION"
                session.add(AuditLog(user_id=plan.user_id, action="BIRTHDAY_ORDER_FAILED",
                    target_type="plan", target_id=plan.id, after_state="预算与订单不一致，资金继续预留，未下单"))
                continue
            for item in items:
                item.status = "SIMULATED_PLACED"
                item.order_id_at_merchant = f"DEMO-{plan.id}-{item.id}"
            plan.status = "SIMULATED_ORDERED"
            session.add(AuditLog(user_id=plan.user_id, action="BIRTHDAY_ORDER_SIMULATED",
                target_type="plan", target_id=plan.id, after_state="模拟预订完成；未联系真实商户，资金继续预留"))
            orders.append({"plan_id": plan.id, "status": plan.status, "order_total": str(total),
                "references": [item.order_id_at_merchant for item in items], "simulation": True})
        return {"as_of": now.isoformat(), "transfers": transfers, "birthday_orders": orders}


async def worker() -> None:
    while True:
        try:
            await run_due(datetime.now())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("demo_scheduler_failed", extra={"error_type": type(error).__name__})
        await asyncio.sleep(15)
