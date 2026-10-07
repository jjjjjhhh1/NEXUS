"""
卡片服务（含状态机）
负责卡片状态切换、锁卡 / 解锁 / 挂失
"""
from enum import Enum
from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .base import BaseService
from ..core.money import positive_amount
from ..core.exceptions import PermissionDeniedException
from ..core.models import Card, Account, Ticket
from ..core.exceptions import CardLockedException, CardLostException, CardException, AccountNotFoundException


class CardStatus(str, Enum):
    """卡片状态"""
    ACTIVE = "ACTIVE"
    TEMP_LOCKED = "TEMP_LOCKED"
    LOST = "LOST"
    REPLACEMENT_REQUESTED = "REPLACEMENT_REQUESTED"
    CLOSED = "CLOSED"


# 状态机转换表
CARD_TRANSITIONS = {
    CardStatus.ACTIVE: [CardStatus.TEMP_LOCKED, CardStatus.LOST],
    CardStatus.TEMP_LOCKED: [CardStatus.ACTIVE, CardStatus.LOST],
    CardStatus.LOST: [CardStatus.REPLACEMENT_REQUESTED],  # LOST 不能普通解锁
    CardStatus.REPLACEMENT_REQUESTED: [CardStatus.CLOSED],
    CardStatus.CLOSED: [],  # 终态
}


class CardService(BaseService):
    """卡片服务"""

    async def get_card(self, card_id: int, user_id: int) -> Card:
        """获取卡片"""
        result = await self.session.execute(
            select(Card).where(Card.id == card_id)
        )
        card = result.scalar_one_or_none()
        if not card:
            raise CardException(f"卡片 {card_id} 不存在")
        account = await self.session.get(Account, card.account_id)
        if not account or account.user_id != user_id:
            raise PermissionDeniedException()
        return card

    async def list_user_cards(self, user_id: int) -> list[Card]:
        """获取用户所有卡片"""
        # 先查账户
        account_result = await self.session.execute(
            select(Account).where(Account.user_id == user_id)
        )
        accounts = list(account_result.scalars().all())
        account_ids = [a.id for a in accounts]

        if not account_ids:
            return []

        # 再查卡片
        card_result = await self.session.execute(
            select(Card).where(Card.account_id.in_(account_ids))
        )
        return list(card_result.scalars().all())

    def _can_transition(self, from_status: str, to_status: str) -> bool:
        """检查状态转换是否合法"""
        try:
            from_e = CardStatus(from_status)
            to_e = CardStatus(to_status)
            return to_e in CARD_TRANSITIONS.get(from_e, [])
        except ValueError:
            return False

    async def lock_card(self, card_id: int, user_id: int, reason: str = "user_request") -> Card:
        """临时锁卡"""
        card = await self.get_card(card_id, user_id)

        if not self._can_transition(card.status, CardStatus.TEMP_LOCKED.value):
            if card.status == CardStatus.LOST.value:
                raise CardLostException(f"卡片 {card.last4} 已挂失，普通锁定无效")
            if card.status == CardStatus.CLOSED.value:
                raise CardException(f"卡片 {card.last4} 已关闭")
            raise CardException(f"卡片 {card.last4} 当前状态 {card.status} 无法锁定")

        before = {"status": card.status}
        card.status = CardStatus.TEMP_LOCKED.value
        card.locked_at = datetime.now()

        # 找用户 ID（从账户查）
        account = await self.session.execute(
            select(Account).where(Account.id == card.account_id)
        )
        account = account.scalar_one()
        user_id = account.user_id

        await self._audit(
            user_id=user_id,
            action="LOCK_CARD",
            target_type="CARD",
            target_id=card_id,
            before_state=before,
            after_state={"status": card.status},
            extra={"reason": reason},
        )

        return card

    async def unlock_card(self, card_id: int, user_id: int) -> Card:
        """解锁（仅 TEP_LOCKED → ACTIVE）"""
        card = await self.get_card(card_id, user_id)

        if card.status == CardStatus.LOST.value:
            raise CardLostException(f"卡片 {card.last4} 已挂失，无法普通解锁")
        if card.status == CardStatus.CLOSED.value:
            raise CardException(f"卡片 {card.last4} 已关闭")
        if card.status == CardStatus.ACTIVE.value:
            return card  # 已经是 ACTIVE，幂等

        if not self._can_transition(card.status, CardStatus.ACTIVE.value):
            raise CardException("当前状态不可解锁")
        before = {"status": card.status}
        card.status = CardStatus.ACTIVE.value
        card.locked_at = None

        account = await self.session.execute(
            select(Account).where(Account.id == card.account_id)
        )
        account = account.scalar_one()
        user_id = account.user_id

        await self._audit(
            user_id=user_id,
            action="UNLOCK_CARD",
            target_type="CARD",
            target_id=card_id,
            before_state=before,
            after_state={"status": card.status},
        )

        return card

    async def report_lost(self, card_id: int, user_id: int) -> Ticket:
        """挂失（升级到 LOST + 提交补卡工单）"""
        card = await self.get_card(card_id, user_id)

        if not self._can_transition(card.status, CardStatus.LOST.value):
            if card.status == CardStatus.LOST.value:
                raise CardException(f"卡片 {card.last4} 已挂失")
            raise CardException(f"卡片 {card.last4} 当前状态无法挂失")

        before = {"status": card.status}
        card.status = CardStatus.LOST.value
        card.locked_at = datetime.now()

        account = await self.session.execute(
            select(Account).where(Account.id == card.account_id)
        )
        account = account.scalar_one()
        user_id = account.user_id

        # 创建补卡工单
        ticket_no = f"LOSS{datetime.now().strftime('%Y%m%d%H%M%S')}{card_id:04d}"
        ticket = Ticket(
            ticket_no=ticket_no,
            user_id=user_id,
            type="LOSS_REPORT",
            status="PROCESSING",
            payload='{"card_id": ' + str(card_id) + ', "last4": "' + card.last4 + '"}',
        )
        self.session.add(ticket)

        await self._audit(
            user_id=user_id,
            action="REPORT_LOST",
            target_type="CARD",
            target_id=card_id,
            before_state=before,
            after_state={"status": card.status, "ticket_no": ticket_no},
        )

        await self.session.flush()
        return ticket

    async def set_limit(
        self,
        card_id: int,
        user_id: int,
        single_limit: Optional[Decimal] = None,
        daily_limit: Optional[Decimal] = None,
    ) -> Card:
        """设置交易限额"""
        card = await self.get_card(card_id, user_id)

        if card.status in [CardStatus.LOST.value, CardStatus.CLOSED.value]:
            raise CardException(f"卡片 {card.last4} 当前状态无法调整限额")

        before = {
            "single_limit": float(card.single_limit) if card.single_limit else None,
            "daily_limit": float(card.daily_limit) if card.daily_limit else None,
        }

        if single_limit is not None:
            card.single_limit = positive_amount(single_limit)
        if daily_limit is not None:
            card.daily_limit = positive_amount(daily_limit)

        account = await self.session.execute(
            select(Account).where(Account.id == card.account_id)
        )
        account = account.scalar_one()
        user_id = account.user_id

        await self._audit(
            user_id=user_id,
            action="SET_CARD_LIMIT",
            target_type="CARD",
            target_id=card_id,
            before_state=before,
            after_state={
                "single_limit": float(card.single_limit) if card.single_limit else None,
                "daily_limit": float(card.daily_limit) if card.daily_limit else None,
            },
        )

        return card

    async def check_transaction_allowed(
        self, card_id: int, user_id: int, amount: Decimal
    ) -> bool:
        """检查交易是否被允许（用于模拟刷卡）"""
        card = await self.get_card(card_id, user_id)

        amount = positive_amount(amount)
        # 非 ACTIVE 状态一律拒绝
        if card.status != CardStatus.ACTIVE.value:
            return False

        # 单笔限额
        if card.single_limit and amount > card.single_limit:
            return False

        # 每日限额
        if card.daily_limit and (card.daily_used + amount) > card.daily_limit:
            return False

        return True

    async def simulate_transaction(
        self, card_id: int, user_id: int, amount: Decimal, merchant: str = "POS刷卡"
    ) -> dict:
        """模拟一笔刷卡（用于演示）"""
        card = await self.get_card(card_id, user_id)
        allowed = await self.check_transaction_allowed(card_id, user_id, amount)

        if not allowed:
            # 写审计日志
            account = await self.session.execute(
                select(Account).where(Account.id == card.account_id)
            )
            account = account.scalar_one()
            await self._audit(
                user_id=account.user_id,
                action="TRANSACTION_REJECTED",
                target_type="CARD",
                target_id=card_id,
                extra={"amount": float(amount), "merchant": merchant, "reason": f"卡片状态 {card.status}"},
            )
            return {
                "success": False,
                "card_id": card_id,
                "last4": card.last4,
                "amount": float(amount),
                "reason": f"交易被拒绝：卡片状态 {card.status}",
            }

        # 模拟成功
        card.daily_used = (card.daily_used or Decimal(0)) + amount

        account = await self.session.execute(
            select(Account).where(Account.id == card.account_id)
        )
        account = account.scalar_one()
        await self._audit(
            user_id=account.user_id,
            action="TRANSACTION_SUCCESS",
            target_type="CARD",
            target_id=card_id,
            extra={"amount": float(amount), "merchant": merchant},
        )

        return {
            "success": True,
            "card_id": card_id,
            "last4": card.last4,
            "amount": float(amount),
            "merchant": merchant,
            "tx_id": f"TX{datetime.now().strftime('%Y%m%d%H%M%S')}{card_id:04d}",
        }