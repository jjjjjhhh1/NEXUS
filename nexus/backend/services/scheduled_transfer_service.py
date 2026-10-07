"""Confirmed demo schedules with persisted due dates and idempotent payments."""
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select

from ..core.exceptions import BusinessRuleException
from ..core.models import Account, AuditLog, Recipient, ScheduledTransferPlan


class ScheduledTransferService:
    def __init__(self, session):
        self.session = session

    async def create(
        self,
        user_id: int,
        recipient_id: int,
        amount: Decimal,
        purpose: str,
        schedule,
    ) -> ScheduledTransferPlan:
        """Create a plan from an already-resolved :mod:`agent.schedule` result.

        The caller has already decided *whether* this repeats. Persisting that
        decision verbatim is the point: the executor can never answer "how many
        times?" differently than the card the customer read did.
        """
        from ..core import scheduling as schedule_module

        frequency = schedule.frequency or schedule_module.MONTHLY
        if frequency != schedule_module.ONCE and not 1 <= (schedule.day_of_month or 0) <= 28:
            raise BusinessRuleException("重复扣款的日期需在每月 1 至 28 日之间（单次转账不受此限制）")
        if amount <= 0 or amount > Decimal("1000000"):
            raise BusinessRuleException("转账金额超出可设置范围")
        recipient = await self.session.scalar(select(Recipient).where(Recipient.id == recipient_id, Recipient.user_id == user_id))
        source = await self.session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
        if recipient is None or source is None:
            raise BusinessRuleException("无法创建计划：账户或收款人不存在")
        clean_purpose = purpose.strip()
        if not clean_purpose or len(clean_purpose) > 100:
            raise BusinessRuleException("请填写 1 至 100 个字的转账用途")
        first = schedule.first_run_on
        plan = ScheduledTransferPlan(
            user_id=user_id,
            source_account_id=source.id,
            recipient_id=recipient.id,
            amount=amount,
            frequency=frequency,
            day_of_month=schedule.day_of_month or first.day,
            first_run_on=first,
            next_run_at=datetime.combine(first, time(hour=schedule_module.EXECUTE_AT_HOUR)),
            purpose=clean_purpose,
            status="ACTIVE",
            total_occurrences=schedule.occurrences,
            completed_count=0,
        )
        self.session.add(plan)
        await self.session.flush()
        self.session.add(AuditLog(
            user_id=user_id,
            action="CREATE_SCHEDULED_TRANSFER",
            target_type="scheduled_transfer",
            target_id=plan.id,
            after_state=f"{schedule.cadence_label()}/{recipient.name}/{amount:.2f}/{clean_purpose}/{schedule.scope_label()}",
        ))
        self.session.add(AuditLog(user_id=user_id, action="AUTHORIZE_SCHEDULED_EXECUTION",
            target_type="scheduled_transfer", target_id=plan.id, after_state="v1:confirmed-demo-schedule"))
        await self.session.flush()
        return plan

    async def create_monthly(
        self, user_id: int, recipient_id: int, amount: Decimal,
        day_of_month: int, purpose: str, first_run_on: date,
    ) -> ScheduledTransferPlan:
        """Monthly shorthand for callers that already decided the cadence.

        Kept as a named convenience rather than a separate code path: a second
        creation route is exactly how "only ever monthly" crept back in as the
        only thing the service could do.
        """
        from ..core import scheduling as schedule_module

        return await self.create(
            user_id, recipient_id, amount, purpose,
            schedule_module.Schedule(
                frequency=schedule_module.MONTHLY,
                first_run_on=first_run_on,
                day_of_month=day_of_month,
                occurrences=None,
            ),
        )

    async def list_user_plans(self, user_id: int) -> list[dict]:
        rows = (await self.session.execute(
            select(ScheduledTransferPlan, Recipient)
            .join(Recipient, Recipient.id == ScheduledTransferPlan.recipient_id)
            .where(ScheduledTransferPlan.user_id == user_id)
            .order_by(ScheduledTransferPlan.status.asc(), ScheduledTransferPlan.next_run_at.asc())
        )).all()
        return [{
            "id": plan.id,
            "recipient": recipient.name,
            "amount": str(plan.amount),
            "frequency": plan.frequency,
            "day_of_month": plan.day_of_month,
            "purpose": plan.purpose,
            "status": plan.status,
            "first_run_on": plan.first_run_on.isoformat(),
            "next_run_at": plan.next_run_at.isoformat(),
            "total_occurrences": plan.total_occurrences,
            "completed_count": plan.completed_count or 0,
        } for plan, recipient in rows]

    async def run_due(self, now: datetime, user_id: int | None = None) -> list[dict]:
        """Execute one due installment per plan atomically, only with v1 consent.

        next_run_at and payment idempotency survive process restarts. Missed
        months are not charged in a burst; the next date is after this run.

        A plan that has just made its last payment is marked ``COMPLETED`` and
        never scheduled again. Without that, "转三万，就这一次" quietly becomes
        an open-ended charge and the customer finds out from their statement,
        months later, with no record of ever having agreed to it.
        """
        from ..core import scheduling as schedule_module
        from .payment_service import PaymentService
        from ..core.exceptions import NexusException
        query = select(ScheduledTransferPlan).where(
            ScheduledTransferPlan.status == "ACTIVE", ScheduledTransferPlan.next_run_at <= now)
        if user_id is not None:
            query = query.where(ScheduledTransferPlan.user_id == user_id)
        plans = list((await self.session.scalars(query)).all())
        receipts = []
        for plan in plans:
            consent = await self.session.scalar(select(AuditLog.id).where(
                AuditLog.action == "AUTHORIZE_SCHEDULED_EXECUTION",
                AuditLog.target_type == "scheduled_transfer", AuditLog.target_id == plan.id,
                AuditLog.user_id == plan.user_id))
            if consent is None:
                continue
            due = plan.next_run_at
            key = f"scheduled:{plan.id}:{due.isoformat()}"
            try:
                async with self.session.begin_nested():
                    payment = PaymentService(self.session)
                    tx = await payment.create_transfer_draft(plan.user_id, plan.recipient_id,
                        Decimal(plan.amount), plan.purpose, idempotency_key=key)
                    await payment.confirm_transfer(tx.id, plan.user_id)
                    await payment.submit_transfer(tx.id, plan.user_id)
                    reference = f"TX-{tx.id:06d}"
            except NexusException as error:
                # A failed installment requires attention, never endless retries.
                plan.status = "PAUSED"
                self.session.add(AuditLog(user_id=plan.user_id, action="SCHEDULED_TRANSFER_FAILED",
                    target_type="scheduled_transfer", target_id=plan.id, after_state=error.message))
                receipts.append({"plan_id": plan.id, "status": "PAUSED", "message": error.message})
                continue
            plan.completed_count = (plan.completed_count or 0) + 1
            plan.last_run_on = due.date()
            remaining = (
                plan.total_occurrences - plan.completed_count
                if plan.total_occurrences else None
            )
            if remaining is not None and remaining <= 0:
                # Last payment made. Retire the plan instead of scheduling
                # another one -- this is the line between "连着转三个月" and
                # "扣到客户自己发现为止".
                plan.status = "COMPLETED"
                self.session.add(AuditLog(user_id=plan.user_id, action="SCHEDULED_TRANSFER_COMPLETED",
                    target_type="scheduled_transfer", target_id=plan.id,
                    after_state=f"{reference}/已到约定期数，计划自动结束"))
                receipts.append({"plan_id": plan.id, "status": "COMPLETED", "reference": reference,
                                 "amount": str(plan.amount), "due_at": due.isoformat(),
                                 "message": "已完成约定期数，计划自动结束，不会再扣款"})
                continue
            cadence = schedule_module.Schedule(
                frequency=plan.frequency or schedule_module.MONTHLY,
                first_run_on=plan.first_run_on,
                occurrences=plan.total_occurrences,
                day_of_month=plan.day_of_month,
            )
            next_date = cadence.next_after(due.date())
            # A successor calculation that does not move is a bug, and this loop
            # runs on a 15-second worker: unguarded, one bad plan hangs the whole
            # demo clock. Two years is far past any real cadence.
            for _ in range(24):
                if next_date > due.date():
                    break
                next_date = cadence.next_after(next_date)
            else:
                plan.status = "PAUSED"
                self.session.add(AuditLog(
                    user_id=plan.user_id, action="SCHEDULED_TRANSFER_FAILED",
                    target_type="scheduled_transfer", target_id=plan.id,
                    after_state="排期无法推进，已暂停等待人工处理"))
                receipts.append({"plan_id": plan.id, "status": "PAUSED",
                                 "message": "排期无法推进，计划已暂停，没有扣款"})
                continue
            candidate = datetime.combine(next_date, time(hour=schedule_module.EXECUTE_AT_HOUR))
            plan.next_run_at = candidate
            self.session.add(AuditLog(user_id=plan.user_id, action="SCHEDULED_TRANSFER_COMPLETED",
                target_type="scheduled_transfer", target_id=plan.id, after_state=reference,
                evidence_ids=key))
            receipts.append({"plan_id": plan.id, "status": "COMPLETED", "reference": reference,
                             "amount": str(plan.amount), "due_at": due.isoformat(), "next_run_at": candidate.isoformat()})
        await self.session.flush()
        return receipts

    async def pause(self, user_id: int, plan_id: int) -> None:
        plan = await self.session.get(ScheduledTransferPlan, plan_id)
        if plan is None or plan.user_id != user_id:
            raise BusinessRuleException("无法访问此定时计划")
        plan.status = "PAUSED"
        self.session.add(AuditLog(user_id=user_id, action="PAUSE_SCHEDULED_TRANSFER",
            target_type="scheduled_transfer", target_id=plan_id, after_state="PAUSED"))
