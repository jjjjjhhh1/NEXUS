"""Immutable confirmation contract, checked before each execution boundary."""
from __future__ import annotations

import hmac
from datetime import timedelta

from ...core.models import User, Recipient, Product, InvestmentOrder
from ...core.exceptions import BusinessRuleException
from ...services.evidence_service import digest, sign, append_evidence


async def facts(session, user_id: int, action) -> dict:
    payload = action.payload or {}
    result = {}
    if payload.get("recipient_id"):
        recipient = await session.get(Recipient, payload["recipient_id"])
        result["recipient_identity_hash"] = digest({key:getattr(recipient,key) for key in ("id","user_id","name","linked_account_id","account_no","bank_name","phone")}) if recipient else None
    product_id = payload.get("product_id")
    if payload.get("order_id"):
        order = await session.get(InvestmentOrder, payload["order_id"])
        product_id = order.product_id if order and order.user_id == user_id else None
    if product_id:
        product = await session.get(Product, product_id)
        user = await session.get(User, user_id)
        result["suitability"] = {"customer_risk":user.risk_score,"product_id":product_id,
            "product_risk":product.risk_level if product else None,
            "minimum":str(product.min_purchase) if product else None,
            "lock_days":product.lock_days if product else None}
    return result


def contract(action, evidence: dict, user_id: int) -> dict:
    return {"version":1,"action_id":action.id,"session_id":action.session_id,
            "user_id":user_id,"kind":action.kind,"payload_hash":digest(action.payload),
            "expires_at":(action.created_at+timedelta(minutes=5)).isoformat(),"facts":evidence}


async def seal(session, user_id: int, action) -> None:
    if action.authorization_contract:
        return
    action.authorization_contract = contract(action, await facts(session,user_id,action),user_id)
    action.contract_signature = sign(session, action.authorization_contract)
    await append_evidence(session,user_id,"PLAN_SEALED",{
        "action_id":action.id,"kind":action.kind,"contract_hash":digest(action.authorization_contract),
        "facts":action.authorization_contract["facts"],"policy_version":"banking-v1"})


async def validate(session, user_id: int, action) -> None:
    stored = action.authorization_contract
    if not stored or not action.contract_signature or not hmac.compare_digest(action.contract_signature, sign(session,stored)):
        raise BusinessRuleException("确认计划缺少有效的授权签名，请重新发起")
    current = contract(action, await facts(session,user_id,action),user_id)
    if digest(current) != digest(stored):
        raise BusinessRuleException("确认内容或关键业务资料已变化，请重新发起并确认")


async def record_result(session, user_id: int, action, event: str) -> None:
    await append_evidence(session,user_id,event,{
        "action_id":action.id,"kind":action.kind,"contract_hash":digest(action.authorization_contract),
        "status":action.status,"result_hash":digest(action.result) if action.result else None})
