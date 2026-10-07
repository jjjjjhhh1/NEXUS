"""
Nexus ORM：业务数据 + 本地演示会话、确认、对话和理财档案
基于 SQLAlchemy 2.0 异步 ORM
"""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import String, Integer, Float, Boolean, DateTime, Date, ForeignKey, UniqueConstraint, Index, JSON, Text, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .database import Base


# ============ 1. users（用户表）============
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(100))
    id_card_hash: Mapped[Optional[str]] = mapped_column(String(64))
    risk_score: Mapped[Optional[str]] = mapped_column(String(10))  # C1-C5
    investment_style: Mapped[Optional[str]] = mapped_column(String(20))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_users_phone", "phone"),)


# ============ 2. accounts（账户表）============
class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # checking/savings/credit
    balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    available_balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    reserved_balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_accounts_user", "user_id", "type"),)


# ============ 3. cards（卡片表）============
class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(50), nullable=False)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE/TEMP_LOCKED/LOST/CLOSED
    single_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    daily_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    daily_used: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    card_type: Mapped[str] = mapped_column(String(20))  # DEBIT/CREDIT
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (Index("idx_cards_account", "account_id", "status"),)


# ============ 4. transactions（转账流水表）============
class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    from_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"))
    to_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"))
    to_recipient_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recipients.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    type: Mapped[str] = mapped_column(String(20))  # TRANSFER/PAYMENT/REFUND
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    remark: Mapped[Optional[str]] = mapped_column(String(200))
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_tx_status", "status", "created_at"),
        Index("idx_tx_account", "from_account_id"),
    )


# ============ 5. recipients（收款人表）============
class Recipient(Base):
    __tablename__ = "recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    linked_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"))
    account_no: Mapped[Optional[str]] = mapped_column(String(50))
    bank_name: Mapped[Optional[str]] = mapped_column(String(50))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    alias: Mapped[Optional[str]] = mapped_column(String(50))
    history_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (Index("idx_recipients_user", "user_id", "name"),)


# ============ 6. merchants（商户表）============
class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(50))
    cancel_endpoint: Mapped[Optional[str]] = mapped_column(String(200))
    logo_url: Mapped[Optional[str]] = mapped_column(String(200))


# ============ 7. subscriptions（订阅表）============
class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    detected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    period: Mapped[str] = mapped_column(String(20))  # MONTHLY/QUARTERLY/YEARLY
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    last_charged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    next_charge_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


# ============ 8. payment_mandates（代扣授权表）============
class PaymentMandate(Base):
    __tablename__ = "payment_mandates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    granted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ============ 9. merchant_contracts（商户合同表）============
class MerchantContract(Base):
    __tablename__ = "merchant_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    subscription_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subscriptions.id"))
    merchant_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    terminated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ============ 10. products（理财产品表）============
class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(20))  # CURRENCY_FUND/BOND/STABLE
    risk_level: Mapped[str] = mapped_column(String(10))  # R1-R5
    yield_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    lock_days: Mapped[int] = mapped_column(Integer, default=0)
    min_purchase: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)
    data_date: Mapped[Optional[date]] = mapped_column(Date)


# ============ 11. investment_orders（申赎订单表）============
class InvestmentOrder(Base):
    __tablename__ = "investment_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10))  # SUBSCRIBE/REDEEM
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    shares: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 4))
    remaining_shares: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 4))
    source_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("investment_orders.id"))
    nav: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ProductPerformance(Base):
    """Daily simulated net-value history for one product.

    A single ``yield_rate`` cannot answer the question users actually ask about
    a wealth product — "is it up or down lately, and by how much". This table is
    what makes a trend chart possible. Every row is simulated and belongs to a
    product whose ``is_fictional`` flag is set, so any view or chart reading it
    must label itself as demo data rather than real market history.

    Layer contract:
      owns      — the simulated price path of a product
      does NOT own — product catalogue facts (Product), user orders
                   (InvestmentOrder), or the chart shape (agent/charts.py)
    """
    __tablename__ = "product_performance"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    nav: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    __table_args__ = (
        UniqueConstraint("product_id", "trade_date"),
        Index("idx_product_performance_product", "product_id", "trade_date"),
    )


# ============ 12. plans（计划表）============
class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(20))  # BIRTHDAY/ANNIVERSARY/HOLIDAY
    title: Mapped[str] = mapped_column(String(100))
    event_date: Mapped[date] = mapped_column(Date)
    budget: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    order_lead_days: Mapped[int] = mapped_column(Integer, default=2)
    delivery_address: Mapped[Optional[str]] = mapped_column(Text)
    categories: Mapped[Optional[str]] = mapped_column(String(200))  # JSON
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ============ 13. plan_orders（计划内订单表）============
class PlanOrder(Base):
    __tablename__ = "plan_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=False)
    product_name: Mapped[str] = mapped_column(String(100))
    product_price: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    order_id_at_merchant: Mapped[Optional[str]] = mapped_column(String(50))
    tracking_no: Mapped[Optional[str]] = mapped_column(String(50))


# ============ 14. risk_reservations（资金预留表）============
class RiskReservation(Base):
    __tablename__ = "risk_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    reserved_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ============ 15. tickets（工单表）============
class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="CREATED")
    payload: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    result: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (Index("idx_tickets_user", "user_id", "status"),)


# ============ 16. audit_logs（审计日志表）============
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(30))
    target_id: Mapped[Optional[int]] = mapped_column(Integer)
    before_state: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    after_state: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    evidence_ids: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    device_id: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_audit_user", "user_id", "created_at"),)


# ============ 17. risk_events（反诈事件表）============
class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(10))  # HIGH/MEDIUM/LOW
    risk_type: Mapped[str] = mapped_column(String(50))
    trigger_keywords: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    action: Mapped[str] = mapped_column(String(20))  # BLOCKED/WARNED/PASSED
    user_overrode: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback_received: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ============ 18. user_profiles（用户画像表）============
class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    avg_transfer_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    max_transfer_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    transfer_frequency_daily: Mapped[Optional[float]] = mapped_column(Float)
    active_hours: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    preferred_merchants: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    risk_baseline: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# ============ 19. import_batches（账单导入批次表）============
class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    file_hash: Mapped[Optional[str]] = mapped_column(String(64))
    file_name: Mapped[Optional[str]] = mapped_column(String(200))
    file_type: Mapped[Optional[str]] = mapped_column(String(20))
    total_rows: Mapped[Optional[int]] = mapped_column(Integer)
    imported_rows: Mapped[Optional[int]] = mapped_column(Integer)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PROCESSING")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ============ 20. statement_transactions（账单导入交易表）============
class StatementTransaction(Base):
    __tablename__ = "statement_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    txn_date: Mapped[Optional[date]] = mapped_column(Date)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    merchant_name: Mapped[Optional[str]] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(50))
    # 付款方附言。真实账单里这一栏决定了支出归类：同一家商户，"买菜"和
    # "给同事生日礼物"不该落进同一项。没有附言时才回退到商户规则。
    note: Mapped[Optional[str]] = mapped_column(String(120))
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    anomaly_reason: Mapped[Optional[str]] = mapped_column(String(200))
    source_row: Mapped[Optional[int]] = mapped_column(Integer)
    raw_data: Mapped[Optional[str]] = mapped_column(Text)  # JSON


def register_all_models() -> None:
    """注册所有模型（在 init_db 时调用）"""
    # 这个函数本身不需要实现任何代码
    # 因为导入模块时 SQLAlchemy 会自动发现 Base 的子类
    # 保留这个函数用于将来动态加载扩展模型
    pass


# 所有模型（方便外部引用）
ALL_MODELS = [
    User, Account, Card, Transaction, Recipient,
    Merchant, Subscription, PaymentMandate, MerchantContract,
    Product, InvestmentOrder, ProductPerformance, Plan, PlanOrder, RiskReservation,
    Ticket, AuditLog, RiskEvent, UserProfile,
    ImportBatch, StatementTransaction,
]


class DemoSession(Base):
    """A browser session. Minted by login on a public address, auto-issued
    locally.

    ``operator_id`` is set only when a real login produced this session, and
    ``auth_time`` records when. On a public address a session without one is
    refused — otherwise the old "POST /api/session and you are in" behaviour
    would still be a way in, with a login screen sitting in front of it.
    """
    __tablename__ = "demo_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    operator_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auth_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)


class OperatorAccount(Base):
    """Someone authorised to *use* the demo — a reviewer, an operator.

    Deliberately not the ``User`` row: ``User`` is the fictional customer whose
    money is being moved, and merging the two would make every audit line
    ambiguous about which of the two it was talking about.
    """

    __tablename__ = "operator_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # bcrypt digest, cost 12. Never the plaintext, never logged, never returned.
    password_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("idx_operator_username", "username"),)


class DemoAction(Base):
    __tablename__ = "demo_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("demo_sessions.id"))
    request_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(30))
    # PENDING -> AWAITING_STEP_UP -> COMPLETED/CANCELLED/EXPIRED。
    # 资金类写操作必须先过二次核验才会进入 AWAITING_STEP_UP 之后的执行路径。
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[Optional[dict]] = mapped_column(JSON)
    authorization_contract: Mapped[Optional[dict]] = mapped_column(JSON)
    contract_signature: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("session_id", "request_id"),)


ALL_MODELS.extend([DemoSession, DemoAction])


class AgentTurn(Base):
    """Request replay and bounded, validated slot context. No model reasoning stored."""
    __tablename__ = "agent_turns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("demo_sessions.id"))
    request_id: Mapped[str] = mapped_column(String(36))
    input_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    context: Mapped[Optional[dict]] = mapped_column(JSON)
    memory_recorded: Mapped[Optional[bool]] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("session_id", "request_id"),)


class UserMemory(Base):
    """Bounded, user-scoped preferences; never financial authority or consent."""
    __tablename__ = "user_memories"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    recent: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=0)
    compression_count: Mapped[int] = mapped_column(Integer, default=0)
    forget_before_turn_id: Mapped[int] = mapped_column(Integer, default=0)
    signature: Mapped[Optional[str]] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentEvidence(Base):
    """Signed decision/authorization chain; independent signed commit anchors."""
    __tablename__ = "agent_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(50), nullable=False)
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("user_id", "sequence"),)


ALL_MODELS.append(AgentTurn)


class FinancialProfile(Base):
    """Durable, user-editable planning facts; never inferred from account activity."""
    __tablename__ = "financial_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    monthly_income: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    essential_expenses: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    debt_balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    monthly_debt_payment: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    goal_name: Mapped[str] = mapped_column(String(80), nullable=False)
    goal_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    goal_saved: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    horizon_months: Mapped[int] = mapped_column(Integer, nullable=False)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    income_stability: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


ALL_MODELS.append(FinancialProfile)


class FinancialSnapshot(Base):
    """User-declared assets outside the local demo account and debt pricing facts."""
    __tablename__ = "financial_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    liquid_savings: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    investment_assets: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    # 客户自报的总资产（含自住房、车辆等不产生现金流的资产）。有房贷的人如果只
    # 填现金和基金，负债永远大于可见资产，净资产厚度会被算成 0。
    declared_assets: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    debt_interest_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    annual_income: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    annual_expenses: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    seasonal_income: Mapped[Optional[list]] = mapped_column(JSON)
    seasonal_expenses: Mapped[Optional[list]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


ALL_MODELS.append(FinancialSnapshot)


class RiskAssessment(Base):
    """A dated investor-risk assessment, kept as its own record.

    A risk grade is not a profile field. It expires, it has to be re-earned
    when the numbers behind it move, and the regulations require the customer's
    own stated appetite to sit next to their measured capacity — so both halves
    and the reasoning that combined them are stored together.

    ``grade`` is the *final* C1–C5 label actually used for suitability, which
    under the prudence rule can be lower than either half on its own.
    ``objective_grade`` and ``subjective_grade`` are kept so the decision can be
    explained and challenged later rather than being a bare string.

    Layer contract:
      owns      — the assessment result and the evidence behind it
      does NOT own — the scoring rules (agent/risk_assessment.py) or the
                   suitability mapping (services/product_service.py)
    """
    __tablename__ = "risk_assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    objective_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    subjective_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    final_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    objective_grade: Mapped[str] = mapped_column(String(4), nullable=False)
    subjective_grade: Mapped[str] = mapped_column(String(4), nullable=False)
    grade: Mapped[str] = mapped_column(String(4), nullable=False)
    binding: Mapped[str] = mapped_column(String(20), nullable=False, default="SUBJECTIVE")
    # No raw identity or credential is ever written here — only the answers the
    # customer themselves selected.
    objective_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    subjective_answers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    diagnosis: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Assessments go stale; a grade older than this cannot be used to sell.
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (Index("idx_risk_assessment_user", "user_id", "status", "valid_until"),)


ALL_MODELS.append(RiskAssessment)


class AnswerRating(Base):
    """How a customer judged one answer, kept with the layout that produced it.

    A rating on its own is noise. What makes it useful later is the pairing with
    the question, the answer shape and the layout decision, because the thing
    worth reusing is "this question, ordered this way, landed well" — not just
    a number. That pairing is what turns a demo session into a benchmark to
    polish against.

    Layer contract:
      owns      — the score and the context needed to reproduce the answer
      does NOT own — the answer itself (AgentTurn holds that) or any rendering
    """
    __tablename__ = "answer_ratings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("demo_sessions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    answer_type: Mapped[str] = mapped_column(String(40), nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    # The layout as decided, so a five-star answer can be replayed as-is.
    layout: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (Index("idx_answer_rating_user_stars", "user_id", "stars"),)


ALL_MODELS.append(AnswerRating)


class DeclaredSubscription(Base):
    """Recurring expense entered by the demo user; separate from bank mandates."""
    __tablename__ = "declared_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False, default="MONTHLY")
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_charge_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (Index("idx_declared_subscription_user", "user_id", "status"),)


class ScheduledTransferPlan(Base):
    """A local demo schedule. Creation never moves money by itself.

    ``frequency`` and ``total_occurrences`` are what stop "转这一次" from
    becoming a standing order. ``ONCE`` plans complete themselves after their
    single run; a repeating plan with ``total_occurrences`` set retires itself
    after that many payments instead of charging the customer forever.
    """
    __tablename__ = "scheduled_transfer_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("recipients.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False, default="MONTHLY")
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_run_on: Mapped[date] = mapped_column(Date, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    purpose: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    # NULL means "until the customer pauses it". 1 on an ONCE plan is the same
    # thing expressed explicitly, and is what the confirmation card shows.
    total_occurrences: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (Index("idx_scheduled_transfer_user", "user_id", "status", "next_run_at"),)


ALL_MODELS.extend([DeclaredSubscription, ScheduledTransferPlan, OperatorAccount])


class AACollection(Base):
    """A demo split-bill collection request; it does not contact real participants."""
    __tablename__ = "aa_collections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    per_person_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    purpose: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="COLLECTING")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (Index("idx_aa_collection_user", "user_id", "status"),)


ALL_MODELS.append(AACollection)


class ContextEvent(Base):
    """User-scoped simulated facts supplied by mock tools for advanced demos."""
    __tablename__ = "context_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    __table_args__ = (UniqueConstraint("user_id", "event_type", "title"), Index("idx_context_event_user", "user_id", "event_type", "status"),)


class OrchestratedPlan(Base):
    """Persisted result of a confirmed multi-tool simulation."""
    __tablename__ = "orchestrated_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (Index("idx_orchestrated_plan_user", "user_id", "scenario_id", "status"),)


ALL_MODELS.extend([ContextEvent, OrchestratedPlan])
