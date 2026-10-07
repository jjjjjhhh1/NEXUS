"""
订阅 / 代扣服务（三层状态分离）
负责：
- 周期扣费检测（从交易流识别）
- 三层状态分离：识别候选 / 商户合同 / 支付授权
- 订阅取消 + 代扣撤销
"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from collections import defaultdict
from ..core.money import positive_amount

from .base import BaseService
from ..core.exceptions import BusinessRuleException
from ..core.models import Subscription, Merchant, MerchantContract, PaymentMandate, StatementTransaction, Transaction, Ticket


class SubscriptionService(BaseService):
    """订阅服务"""

    async def list_merchants(self) -> list[Merchant]:
        """列出所有商户"""
        result = await self.session.execute(select(Merchant))
        return list(result.scalars().all())

    async def detect_recurring_charges(
        self, user_id: int, min_months: int = 3
    ) -> list[dict]:
        """检测周期扣费（从历史交易识别）

        算法：
        - 同一商户 / 同一金额的交易
        - 至少出现 min_months 次
        - 时间间隔规律（月 / 季 / 年）
        """
        # 简化：从 StatementTransaction 中检测
        result = await self.session.execute(
            select(StatementTransaction).where(
                StatementTransaction.user_id == user_id,
                StatementTransaction.txn_date >= datetime.now().date() - timedelta(days=min_months * 30)
            ).order_by(StatementTransaction.txn_date)
        )
        txns = list(result.scalars().all())

        # 按商户 + 金额分组
        groups = defaultdict(list)
        for t in txns:
            key = (t.merchant_name, t.amount)
            groups[key].append(t)

        recurring = []
        for (merchant_name, amount), group in groups.items():
            if len(group) < min_months:
                continue

            # 检查时间间隔
            dates = sorted([t.txn_date for t in group])
            intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
            avg_interval = sum(intervals) / len(intervals) if intervals else 0
            month_indexes = sorted({d.year * 12 + d.month for d in dates})
            month_gaps = [month_indexes[i+1] - month_indexes[i] for i in range(len(month_indexes)-1)]

            # 推断周期
            period = "UNKNOWN"
            # Calendar-month continuity is stable at month boundaries. A demo
            # transaction dated on the first day of a new month must not make
            # an otherwise monthly subscription look irregular.
            if len(month_indexes) >= min_months and month_gaps and all(gap == 1 for gap in month_gaps):
                period = "MONTHLY"
            elif 85 <= avg_interval <= 100:
                period = "QUARTERLY"
            elif 355 <= avg_interval <= 375:
                period = "YEARLY"

            if period == "UNKNOWN":
                continue

            # 计算下次扣费
            last_date = dates[-1]
            if period == "MONTHLY":
                next_date = last_date + timedelta(days=30)
            elif period == "QUARTERLY":
                next_date = last_date + timedelta(days=90)
            elif period == "YEARLY":
                next_date = last_date + timedelta(days=365)

            # 计算置信度
            confidence = min(len(group) / 6.0, 1.0)

            recurring.append({
                "merchant_name": merchant_name,
                "amount": float(amount) if amount else 0,
                "period": period,
                "occurrence_count": len(group),
                "last_charged": last_date.isoformat(),
                "next_charge_estimate": next_date.isoformat(),
                "confidence": round(confidence, 2),
                "transactions": [
                    {"date": d.isoformat(), "amount": float(amount) if amount else 0}
                    for d in dates
                ],
            })

        return recurring

    async def confirm_subscription(
        self,
        user_id: int,
        merchant_name: str,
        amount: Decimal,
        period: str,
    ) -> Subscription:
        """用户确认订阅（候选 → 已签约）"""
        amount = positive_amount(amount)
        if period not in {"MONTHLY", "QUARTERLY", "YEARLY"}:
            raise BusinessRuleException("不支持的订阅周期")
        # 查找商户
        merchant_result = await self.session.execute(
            select(Merchant).where(Merchant.name == merchant_name)
        )
        merchant = merchant_result.scalar_one_or_none()
        if not merchant:
            # 自动创建商户
            merchant = Merchant(name=merchant_name, category="auto_detected")
            self.session.add(merchant)
            await self.session.flush()

        # 创建订阅
        sub = Subscription(
            user_id=user_id,
            merchant_id=merchant.id,
            detected_at=datetime.now(),
            confirmed_at=datetime.now(),
            period=period,
            amount=amount,
            last_charged_at=datetime.now(),
            next_charge_at=self._calc_next_charge(period),
            confidence=1.0,  # 用户确认后为 1.0
            status="ACTIVE",
        )
        self.session.add(sub)
        await self.session.flush()

        # 同时创建商户合同 + 代扣授权（三层状态分离）
        contract = MerchantContract(
            user_id=user_id,
            subscription_id=sub.id,
            merchant_name=merchant_name,
            status="ACTIVE",
            started_at=datetime.now(),
        )
        self.session.add(contract)
        await self.session.flush()

        mandate = PaymentMandate(
            subscription_id=sub.id,
            status="ACTIVE",
            granted_at=datetime.now(),
        )
        self.session.add(mandate)

        await self._audit(
            user_id=user_id,
            action="CONFIRM_SUBSCRIPTION",
            target_type="SUBSCRIPTION",
            extra={"merchant": merchant_name, "amount": float(amount), "period": period},
        )

        await self.session.flush()
        return sub

    async def list_user_subscriptions(self, user_id: int) -> list[dict]:
        """列出用户所有订阅（含三层状态）"""
        result = await self.session.execute(
            select(Subscription, Merchant).join(
                Merchant, Subscription.merchant_id == Merchant.id
            ).where(Subscription.user_id == user_id)
        )

        subs = []
        for sub, merchant in result.all():
            # 查合同状态
            contract_result = await self.session.execute(
                select(MerchantContract).where(
                    MerchantContract.subscription_id == sub.id,
                    MerchantContract.user_id == user_id,
                )
            )
            contract = contract_result.scalar_one_or_none()

            # 查代扣授权状态
            mandate_result = await self.session.execute(
                select(PaymentMandate).where(
                    PaymentMandate.subscription_id == sub.id
                )
            )
            mandate = mandate_result.scalar_one_or_none()

            subs.append({
                "subscription_id": sub.id,
                "status": sub.status,
                "mandate_id": mandate.id if mandate else None,
                "merchant_name": merchant.name,
                "merchant_category": merchant.category,
                "amount": float(sub.amount) if sub.amount else 0,
                "period": sub.period,
                "next_charge_at": sub.next_charge_at.isoformat() if sub.next_charge_at else None,
                "last_charge_at": sub.last_charged_at.isoformat() if sub.last_charged_at else None,
                "confidence": sub.confidence,
                "detected": sub.detected_at.isoformat() if sub.detected_at else None,
                "confirmed": sub.confirmed_at is not None,
                "contract_status": contract.status if contract else None,
                "mandate_status": mandate.status if mandate else None,
            })

        return subs

    async def cancel_subscription(
        self, subscription_id: int, user_id: int
    ) -> Subscription:
        """取消订阅（撤商户合同 + 改订阅状态）"""
        result = await self.session.execute(
            select(Subscription).where(
                Subscription.id == subscription_id,
                Subscription.user_id == user_id,
            )
        )
        sub = result.scalar_one_or_none()
        if not sub:
            raise BusinessRuleException(f"订阅 {subscription_id} 不存在")

        before = {"status": sub.status}
        sub.status = "CANCELLED"

        # 同时撤合同
        contract_result = await self.session.execute(
            select(MerchantContract).where(
                MerchantContract.user_id == user_id,
                MerchantContract.subscription_id == subscription_id,
            )
        )
        contract = contract_result.scalar_one_or_none()
        if contract:
            contract.status = "TERMINATED"
            contract.terminated_at = datetime.now()

        await self._audit(
            user_id=user_id,
            action="CANCEL_SUBSCRIPTION",
            target_type="SUBSCRIPTION",
            target_id=subscription_id,
            before_state=before,
            after_state={"status": sub.status},
        )

        await self.session.flush()
        return sub

    async def revoke_payment_mandate(
        self, mandate_id: int, user_id: int
    ) -> PaymentMandate:
        """撤销代扣授权（单独撤销，不动合同和订阅状态）"""
        # 查 mandate 找到 subscription
        mandate_result = await self.session.execute(
            select(PaymentMandate).where(PaymentMandate.id == mandate_id)
        )
        mandate = mandate_result.scalar_one_or_none()
        if not mandate:
            raise BusinessRuleException(f"代扣授权 {mandate_id} 不存在")

        # 校验：mandate 对应的 subscription 必须属于 user_id
        sub_result = await self.session.execute(
            select(Subscription).where(Subscription.id == mandate.subscription_id)
        )
        sub = sub_result.scalar_one()
        if sub.user_id != user_id:
            raise BusinessRuleException("无权访问")

        before = {"status": mandate.status}
        mandate.status = "REVOKED"
        mandate.revoked_at = datetime.now()

        await self._audit(
            user_id=user_id,
            action="REVOKE_MANDATE",
            target_type="MANDATE",
            target_id=mandate_id,
            before_state=before,
            after_state={"status": mandate.status},
        )

        await self.session.flush()
        return mandate

    async def detect_price_increase(
        self, user_id: int
    ) -> list[dict]:
        """检测订阅涨价（与历史金额对比）"""
        result = await self.session.execute(
            select(Subscription, Merchant).join(
                Merchant, Subscription.merchant_id == Merchant.id
            ).where(
                Subscription.user_id == user_id,
                Subscription.confirmed_at.isnot(None),
            )
        )

        increases = []
        for sub, merchant in result.all():
            # 查历史交易
            stmt_result = await self.session.execute(
                select(StatementTransaction).where(
                    StatementTransaction.user_id == user_id,
                    StatementTransaction.merchant_name == merchant.name,
                ).order_by(StatementTransaction.txn_date)
            )
            txns = list(stmt_result.scalars().all())

            if len(txns) < 2:
                continue

            first_amount = float(txns[0].amount) if txns[0].amount else 0
            current_amount = float(txns[-1].amount) if txns[-1].amount else 0

            if current_amount > first_amount * 1.2:  # 涨价 20%
                increase_pct = ((current_amount - first_amount) / first_amount * 100) if first_amount else 0
                increases.append({
                    "merchant_name": merchant.name,
                    "subscription_id": sub.id,
                    "old_amount": first_amount,
                    "new_amount": current_amount,
                    "increase_pct": round(increase_pct, 1),
                    "first_charge": txns[0].txn_date.isoformat(),
                    "latest_charge": txns[-1].txn_date.isoformat(),
                })

        return increases

    def _calc_next_charge(self, period: str) -> datetime:
        """计算下次扣费时间"""
        now = datetime.now()
        if period == "MONTHLY":
            return now + timedelta(days=30)
        elif period == "QUARTERLY":
            return now + timedelta(days=90)
        elif period == "YEARLY":
            return now + timedelta(days=365)
        else:
            return now + timedelta(days=30)
