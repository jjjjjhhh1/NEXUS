"""
支付 / 转账服务（核心 - 状态机 + 幂等性）
负责：
- 转账状态机：DRAFT → CONFIRMED → SUBMITTED → CLEARING → COMPLETED
- 幂等性：idempotency_key 唯一约束
- 反诈拦截后回退
- 模拟清算流程
"""
from enum import Enum
from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4
from ..core.money import positive_amount
from ..core.exceptions import BusinessRuleException, PermissionDeniedException

from .base import BaseService
from ..core.exceptions import BusinessRuleException
from .account_service import AccountService
from ..core.models import Transaction, Recipient, Account, RiskEvent, UserProfile
from ..core.exceptions import DuplicateTransferException, InsufficientBalanceException, TransferTimeoutException, RecipientNotFoundException, AccountNotFoundException, HighRiskTransferException


class TransferStatus(str, Enum):
    """转账状态机"""
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    SUBMITTED = "SUBMITTED"
    CLEARING = "CLEARING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"  # 响应丢失，需查单
    REVERSED = "REVERSED"
    CANCELLED = "CANCELLED"


# 状态机转换规则
TRANSFER_TRANSITIONS = {
    TransferStatus.DRAFT: [TransferStatus.CONFIRMED, TransferStatus.CANCELLED],
    TransferStatus.CONFIRMED: [TransferStatus.DRAFT, TransferStatus.SUBMITTED, TransferStatus.CANCELLED],
    TransferStatus.SUBMITTED: [
        TransferStatus.CLEARING, TransferStatus.REVERSED,
        TransferStatus.FAILED, TransferStatus.UNKNOWN,
    ],
    TransferStatus.CLEARING: [TransferStatus.COMPLETED, TransferStatus.FAILED],
    TransferStatus.COMPLETED: [TransferStatus.REVERSED],  # 模拟沙箱允许 5 秒撤销
    TransferStatus.FAILED: [],
    TransferStatus.UNKNOWN: [TransferStatus.CLEARING, TransferStatus.COMPLETED, TransferStatus.FAILED],
    TransferStatus.REVERSED: [],
    TransferStatus.CANCELLED: [],
}


def generate_idempotency_key(
    user_id: int, recipient_id: int, amount: Decimal, remark: str = ""
) -> str:
    """生成幂等键"""
    return uuid4().hex


class PaymentService(BaseService):
    """支付服务"""

    def __init__(self, session: AsyncSession):
        super().__init__(session)
        self.account_service = AccountService(session)

    async def find_recipients(
        self, user_id: int, query: str
    ) -> list[Recipient]:
        """查找收款人（按人名 / 手机号 / 备注模糊查询）"""
        result = await self.session.execute(
            select(Recipient).where(
                Recipient.user_id == user_id,
                (Recipient.name.contains(query)) |
                (Recipient.phone.contains(query)) |
                (Recipient.alias.contains(query))
            )
        )
        return list(result.scalars().all())

    async def find_recipient_by_id(self, recipient_id: int) -> Recipient:
        """通过 ID 查找收款人"""
        result = await self.session.execute(
            select(Recipient).where(Recipient.id == recipient_id)
        )
        recipient = result.scalar_one_or_none()
        if not recipient:
            raise RecipientNotFoundException(f"收款人 {recipient_id} 不存在")
        return recipient

    def _can_transition(self, from_status: str, to_status: str) -> bool:
        """检查状态转换是否合法"""
        try:
            from_e = TransferStatus(from_status)
            to_e = TransferStatus(to_status)
            return to_e in TRANSFER_TRANSITIONS.get(from_e, [])
        except ValueError:
            return False

    async def create_transfer_draft(
        self,
        user_id: int,
        recipient_id: int,
        amount: Decimal,
        remark: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Transaction:
        """创建转账草稿"""
        amount = positive_amount(amount)
        # 检查幂等键（如果提供）
        if idempotency_key:
            result = await self.session.execute(
                select(Transaction).where(
                    Transaction.idempotency_key == idempotency_key
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                await self._get_tx(existing.id, user_id)
                if (existing.to_recipient_id, existing.amount, existing.remark) != (recipient_id, amount, remark):
                    raise BusinessRuleException("幂等键已用于不同的转账请求")
                return existing  # 幂等返回已存在的

        recipient = await self.find_recipient_by_id(recipient_id)
        if recipient.user_id != user_id:
            raise PermissionDeniedException("无权使用此收款人")
        if not recipient.linked_account_id:
            raise BusinessRuleException("当前仅支持已绑定账户的收款人")

        # 找用户的主账户
        account_result = await self.session.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.type == "checking"
            )
        )
        account = account_result.scalar_one_or_none()
        if not account:
            raise AccountNotFoundException("未找到用户主账户")
        if recipient.linked_account_id == account.id:
            raise BusinessRuleException("不能向同一账户转账")

        if idempotency_key is None:
            idempotency_key = generate_idempotency_key(
                user_id, recipient_id, amount, remark
            )

        tx = Transaction(
            idempotency_key=idempotency_key,
            from_account_id=account.id,
            to_account_id=recipient.linked_account_id,
            to_recipient_id=recipient.id,
            amount=amount,
            type="TRANSFER",
            status=TransferStatus.DRAFT.value,
            remark=remark,
        )
        self.session.add(tx)
        await self.session.flush()

        await self._audit(
            user_id=user_id,
            action="CREATE_TRANSFER_DRAFT",
            target_type="TRANSFER",
            target_id=tx.id,
            extra={
                "recipient_id": recipient_id,
                "amount": float(amount),
                "remark": remark,
            },
        )

        await self.session.flush()
        return tx

    async def confirm_transfer(
        self, tx_id: int, user_id: int
    ) -> Transaction:
        """用户确认 → CONFIRMED"""
        tx = await self._get_tx(tx_id, user_id)

        if tx.status in (TransferStatus.CONFIRMED.value, TransferStatus.COMPLETED.value):
            return tx

        if not self._can_transition(tx.status, TransferStatus.CONFIRMED.value):
            raise BusinessRuleException(f"不能从 {tx.status} 转到 CONFIRMED")

        tx.status = TransferStatus.CONFIRMED.value
        await self._audit(
            user_id=user_id,
            action="CONFIRM_TRANSFER",
            target_type="TRANSFER",
            target_id=tx_id,
            before_state={"status": TransferStatus.DRAFT.value},
            after_state={"status": tx.status},
        )
        return tx

    async def submit_transfer(
        self, tx_id: int, user_id: int
    ) -> Transaction:
        """提交转账 → SUBMITTED（实际扣款）"""
        tx = await self._get_tx(tx_id, user_id)
        if tx.status == TransferStatus.COMPLETED.value:
            return tx

        if not self._can_transition(tx.status, TransferStatus.SUBMITTED.value):
            raise BusinessRuleException(f"不能从 {tx.status} 转到 SUBMITTED")

        # 检查余额
        account = await self.account_service.get_account(tx.from_account_id)
        available = account.available_balance
        if available < tx.amount:
            # "余额不足" on its own leaves the customer nothing to do about it.
            # The gap is the whole decision: they either lower the amount to
            # what is actually there, wait for money to arrive, or cancel. A
            # number turns a dead end into a choice, and the card offers all
            # three rather than only "try again".
            short = tx.amount - available
            tx.status = TransferStatus.FAILED.value
            await self._audit(
                user_id=user_id,
                action="TRANSFER_FAILED",
                target_type="TRANSFER",
                target_id=tx_id,
                extra={"reason": "余额不足", "shortfall": f"{short:.2f}"},
            )
            await self.session.flush()
            raise InsufficientBalanceException(
                f"可用余额 ¥{available:,.2f}，这笔需要 ¥{tx.amount:,.2f}，还差 ¥{short:,.2f}。",
                shortfall=f"{short:.2f}",
                available=f"{available:.2f}",
                requested=f"{tx.amount:.2f}",
            )

        # 扣减余额
        await self.account_service.debit_balance(
            tx.from_account_id, tx.amount, tx_id
        )

        before = {"status": tx.status}
        tx.status = TransferStatus.SUBMITTED.value

        await self._audit(
            user_id=user_id,
            action="SUBMIT_TRANSFER",
            target_type="TRANSFER",
            target_id=tx_id,
            before_state=before,
            after_state={"status": tx.status},
            extra={"amount": float(tx.amount)},
        )

        # 立即进入 CLEARING（模拟清算）
        await self.session.flush()
        return await self.begin_clearing(tx_id, user_id)

    async def begin_clearing(
        self, tx_id: int, user_id: int
    ) -> Transaction:
        """进入清算流程 → CLEARING → COMPLETED"""
        tx = await self._get_tx(tx_id, user_id)

        if not self._can_transition(tx.status, TransferStatus.CLEARING.value):
            raise BusinessRuleException(f"不能从 {tx.status} 转到 CLEARING")

        tx.status = TransferStatus.CLEARING.value
        await self.session.flush()

        # 模拟清算（实际生产会调支付通道）
        # 这里直接成功（异步调用返回后状态改为 COMPLETED）
        tx.status = TransferStatus.COMPLETED.value
        tx.completed_at = datetime.now()

        if not tx.to_account_id:
            raise BusinessRuleException("缺少沙箱收款账户")
        await self.account_service.credit_balance(tx.to_account_id, tx.amount, tx.id)

        await self._audit(
            user_id=user_id,
            action="COMPLETE_TRANSFER",
            target_type="TRANSFER",
            target_id=tx_id,
            before_state={"status": TransferStatus.SUBMITTED.value},
            after_state={"status": tx.status},
            extra={"amount": tx.amount},
        )

        await self.session.flush()
        return tx

    async def cancel_transfer(
        self, tx_id: int, user_id: int
    ) -> Transaction:
        """取消转账（DRAFT / CONFIRMED 状态）"""
        tx = await self._get_tx(tx_id, user_id)

        if tx.status not in [TransferStatus.DRAFT.value, TransferStatus.CONFIRMED.value]:
            raise BusinessRuleException(f"状态 {tx.status} 不可取消")

        before = {"status": tx.status}
        tx.status = TransferStatus.CANCELLED.value

        await self._audit(
            user_id=user_id,
            action="CANCEL_TRANSFER",
            target_type="TRANSFER",
            target_id=tx_id,
            before_state=before,
            after_state={"status": tx.status},
        )

        await self.session.flush()
        return tx

    async def reverse_transfer(
        self, tx_id: int, user_id: int
    ) -> Transaction:
        """撤销已清算的转账（5 秒内可撤销）"""
        tx = await self._get_tx(tx_id, user_id)

        if tx.status == TransferStatus.REVERSED.value:
            return tx
        if tx.status != TransferStatus.COMPLETED.value:
            raise BusinessRuleException("只有已完成的转账可以撤销")

        # 检查时间
        if not tx.completed_at:
            raise BusinessRuleException("转账无完成时间")

        elapsed = (datetime.now() - tx.completed_at).total_seconds()
        if elapsed > 5:  # 演示模式 5 秒内
            raise BusinessRuleException(f"转账已超过 5 秒，不可撤销（已过 {elapsed:.1f} 秒）")

        if not tx.to_account_id:
            raise BusinessRuleException("缺少沙箱收款账户")
        await self.account_service.debit_balance(tx.to_account_id, tx.amount, tx.id)
        await self.account_service.credit_balance(tx.from_account_id, tx.amount, tx.id)
        before = {"status": tx.status}
        tx.status = TransferStatus.REVERSED.value

        await self._audit(
            user_id=user_id,
            action="REVERSE_TRANSFER",
            target_type="TRANSFER",
            target_id=tx_id,
            before_state=before,
            after_state={"status": tx.status},
            extra={"elapsed_seconds": elapsed},
        )

        await self.session.flush()
        return tx

    async def query_transfer(
        self, tx_id: int, user_id: int
    ) -> Transaction:
        """查单（用于响应丢失 / UNKNOWN 状态）"""
        tx = await self._get_tx(tx_id, user_id)

        # UNKNOWN remains unknown until there is a real clearing result.

        await self._audit(
            user_id=user_id,
            action="QUERY_TRANSFER",
            target_type="TRANSFER",
            target_id=tx_id,
            extra={"final_status": tx.status},
        )

        return tx

    async def _get_tx(self, tx_id: int, user_id: int) -> Transaction:
        """获取转账（带权限校验）"""
        result = await self.session.execute(
            select(Transaction).where(Transaction.id == tx_id)
        )
        tx = result.scalar_one_or_none()
        if not tx:
            raise BusinessRuleException(f"转账 {tx_id} 不存在")

        # 权限校验：转账的 from_account 必须属于 user_id
        account_result = await self.session.execute(
            select(Account).where(Account.id == tx.from_account_id)
        )
        account = account_result.scalar_one()
        if account.user_id != user_id:
            raise PermissionDeniedException("无权访问此转账")

        return tx

    async def log_risk_event(
        self,
        user_id: int,
        level: str,
        risk_type: str,
        keywords: list,
        action: str,
    ) -> RiskEvent:
        """记录反诈事件"""
        import json
        event = RiskEvent(
            user_id=user_id,
            level=level,
            risk_type=risk_type,
            trigger_keywords=json.dumps(keywords, ensure_ascii=False),
            action=action,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def get_user_baseline(self, user_id: int) -> Optional[UserProfile]:
        """获取用户行为基线"""
        result = await self.session.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_baseline_after_transfer(
        self,
        user_id: int,
        amount: Decimal,
    ) -> None:
        """转账后更新用户行为基线（用于反诈）"""
        profile = await self.get_user_baseline(user_id)
        if not profile:
            return

        # 简单的指数移动平均
        if profile.avg_transfer_amount:
            old_avg = float(profile.avg_transfer_amount)
            new_avg = old_avg * 0.9 + float(amount) * 0.1
            profile.avg_transfer_amount = Decimal(str(new_avg))

        if not profile.max_transfer_amount or amount > profile.max_transfer_amount:
            profile.max_transfer_amount = amount
