"""Closed tool registry: executors receive server-resolved IDs, never free-form code."""
import json
from decimal import Decimal
from ...services.payment_service import PaymentService
from ...services.card_service import CardService
from ...services.subscription_service import SubscriptionService
from ...services.product_service import ProductService
from ...services.scheduled_transfer_service import ScheduledTransferService
from ...services.cross_scene_service import CrossSceneService
from ...services.aa_collection_service import AACollectionService
from ...core.exceptions import BusinessRuleException
from ...core.models import AuditLog, Ticket, OrchestratedPlan


async def execute(session, user_id: int, action) -> dict:
    p = action.payload
    kind = action.kind
    if kind == "transfer":
        svc = PaymentService(session)
        tx = await svc.create_transfer_draft(user_id, p['recipient_id'], Decimal(p['amount']), remark=p.get('remark','转账'), idempotency_key=action.id)
        await svc.confirm_transfer(tx.id, user_id)
        await svc.submit_transfer(tx.id, user_id)
        return {"title": "转账已完成", "detail": f"已向{p['recipient']}转账 ¥{p['amount']}，双方账户已记账。", "reference": f"TX-{tx.id:06d}", "status": tx.status}
    if kind == "create_scheduled_transfer":
        from ...core import scheduling as schedule_module
        from ...core.money import positive_amount
        # Rebuild the same Schedule the confirmation card was built from. The
        # card said "共 1 笔" or "共 3 期"; the plan has to say exactly that, or
        # the executor is the one quietly widening the authorisation.
        sched = schedule_module.resolve(
            frequency=p.get("frequency") or "once",
            run_date=p.get("first_run_on"),
            day_of_month=p.get("day_of_month"),
            occurrences=p.get("occurrences"),
        )
        amount = positive_amount(p['amount'])
        # The copy has to come from the same schedule *with* the amount on it,
        # otherwise 扣款范围 quotes "合计 ¥0.00" on a receipt for ¥20,000.
        sched = sched.with_amount(amount)
        plan = await ScheduledTransferService(session).create(
            user_id=user_id,
            recipient_id=p['recipient_id'],
            amount=amount,
            purpose=p['purpose'],
            schedule=sched,
        )
        detail = (
            f"已登记 {sched.window_label()}，向{p['recipient']}转账 ¥{plan.amount:,.2f}，用途“{plan.purpose}”。"
            f"{sched.scope_label()}。执行时若余额不足会暂停并告诉你差额，不会自动改金额。"
        )
        return {
            "title": "转账计划已登记" if sched.is_one_off else "定时转账计划已创建",
            "detail": detail,
            "reference": f"SCH-{plan.id:06d}",
            "status": plan.status,
            "schedule": {**sched.as_payload(), "cadence": sched.cadence_label(), "scope": sched.scope_label()},
        }
    if kind == "create_birthday_plan":
        from datetime import date
        plan = await CrossSceneService(session).create_birthday_plan(user_id, date.fromisoformat(p['event_date']), Decimal(p['budget']), p['option'])
        return {"title": "生日惊喜计划已创建", "detail": f"已预留 ¥{plan.reserved_amount:,.2f}，方案 {p['option']} 的礼品订单已生成草稿，将在生日前两天完成模拟预订；不联系真实商户，资金继续预留。", "reference": f"PLAN-{plan.id:06d}", "status": plan.status}
    if kind == "apply_card":
        ticket = Ticket(ticket_no=f"CARD-{action.id[:8].upper()}", user_id=user_id, type="CARD_APPLICATION", status="CREATED", payload=f"产品={p['product_name']}")
        session.add(ticket)
        await session.flush()
        session.add(AuditLog(user_id=user_id, action="APPLY_CARD", target_type="ticket", target_id=ticket.id, after_state=p['product_name']))
        return {"title": "办卡申请已提交", "detail": f"“{p['product_name']}”申请已建立，当前状态为待审核。", "reference": ticket.ticket_no, "status": ticket.status}
    if kind == "create_support_ticket":
        prefix = "HUMAN" if p.get("mode") == "HUMAN_HANDOFF" else "CASE"
        status = "READY" if prefix == "HUMAN" else "QUEUED"
        ticket = Ticket(
            ticket_no=f"{prefix}-{action.id[:8].upper()}", user_id=user_id,
            type=p.get("mode", "SUPPORT_TICKET"), status=status,
            payload=json.dumps({"summary":p["summary"], "reason":p.get("reason")}, ensure_ascii=False),
        )
        session.add(ticket)
        await session.flush()
        session.add(AuditLog(
            user_id=user_id, action="CREATE_SUPPORT_HANDOFF", target_type="ticket", target_id=ticket.id,
            after_state=json.dumps({"ticket_no":ticket.ticket_no,"status":status}, ensure_ascii=False),
        ))
        detail = "人工坐席已接手，诉求摘要已同步。" if status == "READY" else "诉求已进入处理队列，预计 1 个工作日内处理。"
        return {"title":"人工接管已准备" if status == "READY" else "客服工单已创建", "detail":detail, "reference":ticket.ticket_no, "status":status}
    if kind == "create_aa_collection":
        row = await AACollectionService(session).create(user_id, int(p['participant_count']), Decimal(p['total']), p['purpose'])
        return {"title": "AA 收款任务已创建", "detail": f"已创建 {row.participant_count} 人 AA 收款任务，每人 ¥{row.per_person_amount:,.2f}，备注“{row.purpose}”。", "reference": f"AA-{row.id:06d}", "status": row.status, "chart": {"type":"split_progress", "labels":["已收款", "待收款"], "values":[0, row.participant_count], "total": str(row.total_amount), "per_person": str(row.per_person_amount)}}
    if kind == "activate_orchestrated_plan":
        plan = await session.get(OrchestratedPlan, int(p["plan_id"]))
        if not plan or plan.user_id != user_id or plan.status != "DRAFT":
            raise BusinessRuleException("方案不存在、已处理或不属于当前用户")
        plan.status="ACTIVE"
        session.add(AuditLog(user_id=user_id,action="ACTIVATE_AGENT_PLAN",target_type="orchestrated_plan",target_id=plan.id,after_state=plan.objective))
        await session.flush()
        return {"title":"多工具方案已启用","detail":"计划已保存为进行中。所有涉及资金或账户状态的具体动作仍会逐项生成确认卡。","reference":f"FLOW-{plan.id:06d}","status":plan.status}
    cards = CardService(session)
    if kind == "lock_card":
        card = await cards.lock_card(p['card_id'], user_id)
        return {"title": "卡片已锁定", "detail": f"尾号 {card.last4} 已临时锁定，可在需要时解锁。", "status": card.status}
    if kind == "unlock_card":
        card = await cards.unlock_card(p['card_id'], user_id)
        return {"title": "卡片已解锁", "detail": f"尾号 {card.last4} 已恢复使用。", "status": card.status}
    if kind == "report_lost":
        ticket = await cards.report_lost(p['card_id'], user_id)
        return {"title": "挂失已登记", "detail": f"尾号 {p['last4']} 已挂失，已建立补卡工单；此状态不能普通解锁。", "reference": ticket.ticket_no, "status": "LOST"}
    if kind == "set_card_limit":
        value = Decimal(p['amount'])
        card = await cards.set_limit(
            p['card_id'], user_id,
            single_limit=value if p['limit_type'] == 'single' else None,
            daily_limit=value if p['limit_type'] == 'daily' else None,
        )
        label = "单笔" if p['limit_type'] == 'single' else "每日"
        return {"title": "卡片限额已调整", "detail": f"尾号 {card.last4} 的{label}限额已设为 ¥{value:,.2f}。", "reference": f"CARD-{card.id:04d}", "status": card.status}
    products = ProductService(session)
    if kind == "subscribe_product":
        order = await products.subscribe(user_id, p['product_id'], Decimal(p['amount']))
        return {"title": "申购已完成", "detail": f"{p['product_name']} 已申购 ¥{Decimal(p['amount']):,.2f}，获得 {order.shares:,.4f} 份。", "reference": f"INV-{order.id:06d}", "status": order.status}
    if kind == "redeem_product":
        order = await products.redeem(user_id, p['order_id'])
        return {"title": "赎回已完成", "detail": f"{p['product_name']} 已赎回 {order.shares:,.4f} 份，¥{order.amount:,.2f} 已返回你的账户。", "reference": f"INV-{order.id:06d}", "status": order.status}
    subscriptions = SubscriptionService(session)
    if kind == "cancel_subscription":
        sub = await subscriptions.cancel_subscription(p['subscription_id'], user_id)
        return {"title": "订阅合同已终止", "detail": f"{p['merchant']}订阅已取消。代扣授权独立管理，如需撤销，请发送“撤销{p['merchant']}代扣”。", "status": sub.status}
    if kind == "revoke_mandate":
        mandate = await subscriptions.revoke_payment_mandate(p['mandate_id'], user_id)
        return {"title": "代扣授权已撤销", "detail": f"{p['merchant']}代扣授权已撤销。商户合同状态保持独立。", "status": mandate.status}
    raise BusinessRuleException("不支持的操作")
