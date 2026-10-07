import asyncio
from decimal import Decimal as D
import json
import pytest
from sqlalchemy import select, func
from nexus.backend.core.models import Account, Transaction, AuditLog, MerchantContract, InvestmentOrder
from nexus.backend.core.exceptions import BusinessRuleException, PermissionDeniedException, InsufficientBalanceException
from nexus.backend.services.payment_service import PaymentService
from nexus.backend.services.product_service import ProductService
from nexus.backend.services.card_service import CardService
from nexus.backend.services.subscription_service import SubscriptionService
from nexus.backend.services.account_service import AccountService
from nexus.backend.services.cache_service import BusinessCache


async def test_transfer_retry_and_reversal_conserve_money(db, seeded):
    x = seeded
    async with db() as s:
        svc = PaymentService(s)
        tx = await svc.create_transfer_draft(x['user'], x['recipient'], D('100'), idempotency_key='once')
        await svc.confirm_transfer(tx.id, x['user'])
        await svc.submit_transfer(tx.id, x['user'])
        await svc.submit_transfer(tx.id, x['user'])
        assert (await s.get(Account, x['account'])).balance == D('900')
        assert (await s.get(Account, x['destination'])).balance == D('200')
        logs = (await s.scalars(select(AuditLog).where(AuditLog.action == 'COMPLETE_TRANSFER'))).all()
        assert len(logs) == 1 and json.loads(logs[0].evidence_ids)['amount'] == '100'
        await svc.reverse_transfer(tx.id, x['user'])
        await svc.reverse_transfer(tx.id, x['user'])
        assert (await s.get(Account, x['account'])).balance == D('1000')
        assert (await s.get(Account, x['destination'])).balance == D('100')


async def test_concurrent_transfer_retry(db, seeded):
    x = seeded
    async def send():
        async with db() as s:
            svc = PaymentService(s)
            tx = await svc.create_transfer_draft(x['user'], x['recipient'], D('100'), idempotency_key='same')
            await svc.confirm_transfer(tx.id, x['user'])
            await svc.submit_transfer(tx.id, x['user'])
    await asyncio.gather(send(), send())
    async with db() as s:
        assert (await s.get(Account, x['account'])).balance == D('900')
        assert await s.scalar(select(func.count()).select_from(Transaction)) == 1


async def test_idempotency_mismatch_and_ownership(db, seeded):
    x = seeded
    async with db() as s:
        svc = PaymentService(s)
        await svc.create_transfer_draft(x['user'], x['recipient'], D('10'), idempotency_key='key')
        with pytest.raises(BusinessRuleException):
            await svc.create_transfer_draft(x['user'], x['recipient'], D('20'), idempotency_key='key')
        with pytest.raises(PermissionDeniedException):
            await svc.create_transfer_draft(x['other'], x['recipient'], D('10'))


@pytest.mark.parametrize('amount', ['-10', '0', 'NaN', 'Infinity', '1.001'])
async def test_invalid_amount_cannot_change_balance(db, seeded, amount):
    async with db() as s:
        with pytest.raises(BusinessRuleException):
            await AccountService(s).debit_balance(seeded['account'], D(amount), 0)
        assert (await s.get(Account, seeded['account'])).balance == D('1000')


async def test_failure_rolls_back_money_and_audit(db, seeded, monkeypatch):
    x = seeded
    async with db() as s:
        svc = PaymentService(s)
        tx = await svc.create_transfer_draft(x['user'], x['recipient'], D('100'))
        await svc.confirm_transfer(tx.id, x['user'])
        txid = tx.id
    async def fail(*args, **kwargs):
        raise RuntimeError('injected clearing failure')
    monkeypatch.setattr(AccountService, 'credit_balance', fail)
    with pytest.raises(RuntimeError):
        async with db() as s:
            await PaymentService(s).submit_transfer(txid, x['user'])
    async with db() as s:
        assert (await s.get(Account, x['account'])).balance == D('1000')
        assert (await s.get(Transaction, txid)).status == 'CONFIRMED'
        assert await s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'DEBIT_BALANCE')) == 0


async def test_insufficient_balance(db, seeded):
    x = seeded
    with pytest.raises(InsufficientBalanceException):
        async with db() as s:
            svc = PaymentService(s)
            tx = await svc.create_transfer_draft(x['user'], x['recipient'], D('1001'))
            await svc.confirm_transfer(tx.id, x['user'])
            await svc.submit_transfer(tx.id, x['user'])
    async with db() as s:
        assert (await s.get(Account, x['account'])).balance == D('1000')


async def test_partial_redemption_and_repeat_rejected(db, seeded):
    x = seeded
    async with db() as s:
        svc = ProductService(s)
        order = await svc.subscribe(x['user'], x['product'], D('100'))
        await svc.redeem(x['user'], order.id, D('40'))
        assert order.remaining_shares == D('60')
        with pytest.raises(BusinessRuleException):
            await svc.redeem(x['user'], order.id, D('61'))
        await svc.redeem(x['user'], order.id)
        with pytest.raises(BusinessRuleException):
            await svc.redeem(x['user'], order.id)
        assert (await s.get(Account, x['account'])).balance == D('1000')


async def test_concurrent_redemption(db, seeded):
    x = seeded
    async with db() as s:
        order = await ProductService(s).subscribe(x['user'], x['product'], D('100'))
        oid = order.id
    async def redeem():
        async with db() as s:
            await ProductService(s).redeem(x['user'], oid)
    results = await asyncio.gather(redeem(), redeem(), return_exceptions=True)
    assert sum(isinstance(r, BusinessRuleException) for r in results) == 1
    async with db() as s:
        assert (await s.get(Account, x['account'])).balance == D('1000')


async def test_subscription_contract_and_mandate_are_separate(db, seeded):
    async with db() as s:
        svc = SubscriptionService(s)
        sub = await svc.confirm_subscription(seeded['user'], '音乐会员', D('15'), 'MONTHLY')
        await svc.cancel_subscription(sub.id, seeded['user'])
        contract = await s.scalar(select(MerchantContract).where(MerchantContract.subscription_id == sub.id))
        assert contract.status == 'TERMINATED'
        row = (await svc.list_user_subscriptions(seeded['user']))[0]
        assert row['mandate_status'] == 'ACTIVE'
        await svc.revoke_payment_mandate(row['mandate_id'], seeded['user'])
        assert (await svc.list_user_subscriptions(seeded['user']))[0]['mandate_status'] == 'REVOKED'


async def test_card_owner_and_loss_state(db, seeded):
    x = seeded
    async with db() as s:
        svc = CardService(s)
        with pytest.raises(PermissionDeniedException):
            await svc.lock_card(x['card'], x['other'])
        await svc.lock_card(x['card'], x['user'])
        assert not await svc.check_transaction_allowed(x['card'], x['user'], D('1'))
        await svc.unlock_card(x['card'], x['user'])
        await svc.report_lost(x['card'], x['user'])
        with pytest.raises(Exception):
            await svc.unlock_card(x['card'], x['user'])


def test_exchange_rate_cache():
    cache = BusinessCache()
    cache.set_exchange_rate('CNY', 'USD', 0.14)
    assert cache.get_exchange_rate('CNY', 'USD') == 0.14
