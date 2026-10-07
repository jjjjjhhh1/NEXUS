"""
账户服务
负责账户管理、余额查询、资金预留 / 释放
所有写操作必须经过审计日志
"""
from .base import BaseService
from ..core.models import Account, RiskReservation, AuditLog
from ..core.exceptions import AccountNotFoundException, InsufficientBalanceException, AccountFrozenException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal
from datetime import datetime
from typing import Optional
import json
from ..core.money import positive_amount


class AccountService(BaseService):
    """账户服务"""

    async def get_account(self, account_id: int) -> Account:
        """获取账户"""
        result = await self.session.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            raise AccountNotFoundException(f"账户 {account_id} 不存在")
        return account

    async def get_user_accounts(self, user_id: int) -> list[Account]:
        """获取用户的所有账户"""
        result = await self.session.execute(
            select(Account).where(Account.user_id == user_id)
        )
        return list(result.scalars().all())

    async def get_balance(self, user_id: int, account_type: str = "checking") -> Decimal:
        """获取用户某类型账户的可用余额"""
        result = await self.session.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.type == account_type
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            raise AccountNotFoundException(f"用户 {user_id} 没有 {account_type} 账户")
        return account.available_balance

    async def check_available_balance(
        self, account_id: int, amount: Decimal
    ) -> bool:
        """检查可用余额是否足够"""
        amount = positive_amount(amount)
        account = await self.get_account(account_id)
        return account.available_balance >= amount

    async def reserve_balance(
        self, account_id: int, amount: Decimal, plan_id: Optional[int] = None
    ) -> RiskReservation:
        """资金预留（用于生日计划等）

        - 扣减 available_balance
        - 增加 reserved_balance
        - 写 risk_reservations 表
        - 写审计日志
        """
        amount = positive_amount(amount)
        account = await self.get_account(account_id)

        if account.available_balance < amount:
            raise InsufficientBalanceException(
                f"余额不足：需要 {amount}，可用 {account.available_balance}"
            )

        # 扣减可用余额
        account.available_balance -= amount
        account.reserved_balance += amount

        # 写预留记录
        reservation = RiskReservation(
            account_id=account_id,
            plan_id=plan_id,
            amount=amount,
            status="ACTIVE",
        )
        self.session.add(reservation)

        # 写审计日志
        await self._audit(
            user_id=account.user_id,
            action="RESERVE_BALANCE",
            target_type="ACCOUNT",
            target_id=account_id,
            before_state={"available": float(account.available_balance + amount)},
            after_state={"available": float(account.available_balance)},
            extra={"amount": float(amount), "plan_id": plan_id},
        )

        await self.session.flush()
        return reservation

    async def release_reservation(self, reservation_id: int) -> None:
        """释放预留（计划完成 / 取消时）"""
        result = await self.session.execute(
            select(RiskReservation).where(RiskReservation.id == reservation_id)
        )
        reservation = result.scalar_one_or_none()
        if not reservation:
            return
        if reservation.status != "ACTIVE":
            return

        account = await self.get_account(reservation.account_id)

        # 释放预留：reserved → available
        account.reserved_balance -= reservation.amount
        account.available_balance += reservation.amount
        reservation.status = "RELEASED"
        reservation.released_at = datetime.now()

        await self._audit(
            user_id=account.user_id,
            action="RELEASE_RESERVATION",
            target_type="RESERVATION",
            target_id=reservation_id,
            before_state={"status": "ACTIVE"},
            after_state={"status": "RELEASED"},
            extra={"amount": float(reservation.amount)},
        )

    async def debit_balance(
        self, account_id: int, amount: Decimal, transaction_id: int
    ) -> None:
        """扣减余额（转账用）"""
        amount = positive_amount(amount)
        account = await self.get_account(account_id)

        if account.available_balance < amount:
            raise InsufficientBalanceException()

        account.available_balance -= amount
        account.balance -= amount

        await self._audit(
            user_id=account.user_id,
            action="DEBIT_BALANCE",
            target_type="ACCOUNT",
            target_id=account_id,
            before_state={"available": float(account.available_balance + amount)},
            after_state={"available": float(account.available_balance)},
            extra={"amount": float(amount), "tx_id": transaction_id},
        )

    async def credit_balance(
        self, account_id: int, amount: Decimal, transaction_id: int
    ) -> None:
        """入账（转账收款）"""
        amount = positive_amount(amount)
        account = await self.get_account(account_id)
        account.available_balance += amount
        account.balance += amount

        await self._audit(
            user_id=account.user_id,
            action="CREDIT_BALANCE",
            target_type="ACCOUNT",
            target_id=account_id,
            before_state={"available": float(account.available_balance - amount)},
            after_state={"available": float(account.available_balance)},
            extra={"amount": float(amount), "tx_id": transaction_id},
        )
