"""
理财服务
负责：
- 理财产品库管理
- 申赎订单
- 风险测评
- 适配性筛选
"""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from ..core.money import positive_amount

from .base import BaseService
from ..core.exceptions import BusinessRuleException
from ..core.models import Product, InvestmentOrder, Account, User
from ..core.exceptions import InsufficientSubscriptionPeriodException, PlanConflictException, AccountNotFoundException


class ProductService(BaseService):
    """理财服务"""

    async def list_products(
        self,
        user_id: Optional[int] = None,
        period_filter: Optional[str] = None,
        risk_filter: Optional[str] = None,
    ) -> list[dict]:
        """列出产品（按筛选条件）"""
        query_obj = select(Product)

        conditions = []
        if period_filter:
            # period_filter: "短期" / "中期" / "长期"
            if period_filter == "短期":
                conditions.append(Product.lock_days <= 30)
            elif period_filter == "中期":
                conditions.append(and_(Product.lock_days > 30, Product.lock_days <= 90))
            elif period_filter == "长期":
                conditions.append(Product.lock_days > 90)

        if risk_filter:
            conditions.append(Product.risk_level == risk_filter)

        if conditions:
            query_obj = query_obj.where(and_(*conditions))

        result = await self.session.execute(query_obj)
        products = list(result.scalars().all())

        return [self._product_to_dict(p) for p in products]

    async def get_product(self, product_id: int) -> dict:
        """获取产品详情"""
        result = await self.session.execute(
            select(Product).where(Product.id == product_id)
        )
        product = result.scalar_one_or_none()
        if not product:
            raise BusinessRuleException(f"产品 {product_id} 不存在")
        return self._product_to_dict(product)

    async def get_product_by_code(self, code: str) -> Optional[Product]:
        """通过 code 查找"""
        result = await self.session.execute(
            select(Product).where(Product.code == code)
        )
        return result.scalar_one_or_none()

    async def check_eligibility(
        self,
        user_id: int,
        product_id: int,
        amount: Decimal,
    ) -> dict:
        """检查购买适配性"""
        product = await self.get_product(product_id)

        # 检查起购金额
        if amount < product["min_purchase"]:
            return {
                "eligible": False,
                "reason": f"起购金额 {product['min_purchase']}，您输入 {amount}",
            }

        # 风险测评检查
        user_result = await self.session.execute(
            select(User).where(User.id == user_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            return {"eligible": False, "reason": "用户不存在"}

        if not user.risk_score:
            return {
                "eligible": False,
                "reason": "请先完成风险测评",
            }

        # 简单适配性规则：R3+ 产品要求 C3+
        risk_mapping = {"R1": ["C1", "C2", "C3", "C4", "C5"],
                        "R2": ["C1", "C2", "C3", "C4", "C5"],
                        "R3": ["C3", "C4", "C5"],
                        "R4": ["C4", "C5"],
                        "R5": ["C5"]}

        product_risk = product["risk_level"]
        allowed_scores = risk_mapping.get(product_risk, [])

        if user.risk_score not in allowed_scores:
            return {
                "eligible": False,
                "reason": f"您的风险等级 {user.risk_score} 不适合 {product_risk} 产品",
            }

        return {
            "eligible": True,
            "user_risk_score": user.risk_score,
            "product_risk": product_risk,
            "min_purchase": float(product["min_purchase"]),
        }

    async def subscribe(
        self,
        user_id: int,
        product_id: int,
        amount: Decimal,
    ) -> InvestmentOrder:
        """申购"""
        amount = positive_amount(amount)
        product = await self.get_product_by_id(product_id)

        # 适配性检查
        eligibility = await self.check_eligibility(user_id, product_id, amount)
        if not eligibility["eligible"]:
            raise BusinessRuleException(eligibility["reason"])

        # 资金检查（含预留资金冲突）
        account_result = await self.session.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.type == "checking"
            )
        )
        account = account_result.scalar_one_or_none()
        if not account:
            raise AccountNotFoundException()

        # 可用金额 = balance - reserved
        available = account.balance - account.reserved_balance
        if amount > available:
            raise PlanConflictException(
                f"可用余额不足（已预留 {account.reserved_balance}），"
                f"实际可用 {available}"
            )

        # 找产品价格（用 yield_rate 当 NAV 简化）
        nav = Decimal("1.0")  # 演示
        shares = amount / nav

        order = InvestmentOrder(
            user_id=user_id,
            product_id=product_id,
            order_type="SUBSCRIBE",
            amount=amount,
            shares=shares,
            remaining_shares=shares,
            nav=nav,
            status="SUBMITTED",
            submitted_at=datetime.now(),
        )
        self.session.add(order)

        # 扣减余额
        account.balance -= amount
        account.available_balance -= amount

        await self._audit(
            user_id=user_id,
            action="SUBSCRIBE_PRODUCT",
            target_type="INVESTMENT_ORDER",
            extra={
                "product_code": product.code,
                "amount": float(amount),
                "shares": float(shares),
            },
        )

        # 简化：直接进入 CONFIRMED + SETTLED
        order.status = "CONFIRMED"
        order.confirmed_at = datetime.now()
        order.status = "SETTLED"
        order.settled_at = datetime.now()

        await self.session.flush()
        return order

    async def redeem(
        self,
        user_id: int,
        order_id: int,
        shares: Optional[Decimal] = None,
    ) -> InvestmentOrder:
        """赎回"""
        result = await self.session.execute(
            select(InvestmentOrder).where(
                InvestmentOrder.id == order_id,
                InvestmentOrder.user_id == user_id,
            )
        )
        order = result.scalar_one_or_none()
        if not order:
            raise BusinessRuleException(f"订单 {order_id} 不存在")

        if order.order_type != "SUBSCRIBE":
            raise BusinessRuleException("只能赎回申购订单")
        if order.status != "SETTLED" or order.remaining_shares is None or order.remaining_shares <= 0:
            raise BusinessRuleException("此订单没有可赎回份额")

        # 检查产品锁定期
        product = await self.get_product_by_id(order.product_id)
        if product.lock_days > 0:
            days_held = (datetime.now() - order.submitted_at).days
            if days_held < product.lock_days:
                raise InsufficientSubscriptionPeriodException(
                    f"产品锁定期 {product.lock_days} 天，已持有 {days_held} 天"
                )

        # 赎回数量（默认全部）
        redeem_shares = positive_amount(shares if shares is not None else order.remaining_shares, places=4)
        if redeem_shares > order.remaining_shares:
            raise BusinessRuleException("赎回份额超过剩余持仓")
        redeem_amount = (redeem_shares * (order.nav or Decimal("1"))).quantize(Decimal("0.01"))
        positive_amount(redeem_amount)
        order.remaining_shares -= redeem_shares
        if order.remaining_shares == 0:
            order.status = "REDEEMED"

        # 创建赎回订单
        redeem_order = InvestmentOrder(
            user_id=user_id,
            product_id=order.product_id,
            order_type="REDEEM",
            source_order_id=order.id,
            amount=redeem_amount,
            shares=redeem_shares,
            nav=order.nav,
            status="SUBMITTED",
            submitted_at=datetime.now(),
        )
        self.session.add(redeem_order)

        await self._audit(
            user_id=user_id,
            action="REDEEM_PRODUCT",
            target_type="INVESTMENT_ORDER",
            extra={
                "product_code": product.code,
                "redeem_shares": float(redeem_shares),
                "redeem_amount": float(redeem_amount),
            },
        )

        # 简化：直接 SETTLED
        redeem_order.status = "CONFIRMED"
        redeem_order.confirmed_at = datetime.now()
        redeem_order.status = "SETTLED"
        redeem_order.settled_at = datetime.now()

        # 给账户入账
        account_result = await self.session.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.type == "checking"
            )
        )
        account = account_result.scalar_one()
        account.balance += redeem_amount
        account.available_balance += redeem_amount

        await self.session.flush()
        return redeem_order

    async def list_user_orders(
        self, user_id: int, order_type: Optional[str] = None
    ) -> list[dict]:
        """列出用户订单"""
        query = select(InvestmentOrder, Product).join(
            Product, InvestmentOrder.product_id == Product.id
        ).where(InvestmentOrder.user_id == user_id)

        if order_type:
            query = query.where(InvestmentOrder.order_type == order_type)

        result = await self.session.execute(
            query.order_by(InvestmentOrder.submitted_at.desc())
        )

        orders = []
        for order, product in result.all():
            orders.append({
                "order_id": order.id,
                "order_type": order.order_type,
                "product_name": product.name,
                "product_code": product.code,
                "amount": float(order.amount) if order.amount else 0,
                "shares": float(order.shares) if order.shares else 0,
                "remaining_shares": str(order.remaining_shares) if order.remaining_shares is not None else None,
                "nav": float(order.nav) if order.nav else 0,
                "status": order.status,
                "submitted_at": order.submitted_at.isoformat(),
                "confirmed_at": order.confirmed_at.isoformat() if order.confirmed_at else None,
                "settled_at": order.settled_at.isoformat() if order.settled_at else None,
            })

        return orders

    async def assess_risk(
        self,
        user_id: int,
        answers: list[int],  # 每题 1-5 分
    ) -> dict:
        """风险测评（基于问卷打分）"""
        if len(answers) != 5:
            raise BusinessRuleException("风险测评需要 5 道题")
        if any(type(answer) is not int or not 1 <= answer <= 5 for answer in answers):
            raise BusinessRuleException("每道题必须为 1 到 5 的整数")

        avg_score = sum(answers) / len(answers)

        if avg_score <= 1.5:
            risk_score = "C1"
            style = "保守型"
        elif avg_score <= 2.5:
            risk_score = "C2"
            style = "稳健型"
        elif avg_score <= 3.5:
            risk_score = "C3"
            style = "平衡型"
        elif avg_score <= 4.5:
            risk_score = "C4"
            style = "成长型"
        else:
            risk_score = "C5"
            style = "激进型"

        # 更新用户画像
        user_result = await self.session.execute(
            select(User).where(User.id == user_id)
        )
        user = user_result.scalar_one_or_none()
        if user:
            user.risk_score = risk_score
            user.investment_style = style

        await self._audit(
            user_id=user_id,
            action="RISK_ASSESSMENT",
            target_type="USER",
            extra={"answers": answers, "score": avg_score, "risk_score": risk_score},
        )

        return {
            "risk_score": risk_score,
            "style": style,
            "avg_score": avg_score,
        }

    async def get_product_by_id(self, product_id: int) -> Product:
        """通过 ID 找产品"""
        result = await self.session.execute(
            select(Product).where(Product.id == product_id)
        )
        product = result.scalar_one_or_none()
        if not product:
            raise BusinessRuleException("产品不存在")
        return product

    def _product_to_dict(self, product: Product) -> dict:
        """ORM 转 dict"""
        return {
            "id": product.id,
            "code": product.code,
            "name": product.name,
            "type": product.type,
            "risk_level": product.risk_level,
            "yield_rate": float(product.yield_rate) if product.yield_rate else 0,
            "lock_days": product.lock_days,
            "min_purchase": float(product.min_purchase),
            "is_fictional": product.is_fictional,
            "data_date": product.data_date.isoformat() if product.data_date else None,
        }
