"""
Nexus 统一异常体系
所有业务异常继承自 NexusException，HTTP 层统一捕获返回
"""


class NexusException(Exception):
    """Nexus 业务异常基类"""

    code: str = "NEXUS_ERROR"
    message: str = "未知错误"
    status_code: int = 500
    safe_to_user: bool = True  # 是否可以安全展示给用户

    def __init__(self, message: str | None = None, **kwargs):
        self.message = message or self.message
        self.extra = kwargs
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "extra": self.extra,
        }


# ============ 认证异常 ============
class AuthException(NexusException):
    code = "AUTH_ERROR"
    message = "认证失败"
    status_code = 401


class TokenExpiredException(AuthException):
    code = "TOKEN_EXPIRED"
    message = "登录已过期，请重新登录"


class PermissionDeniedException(AuthException):
    code = "PERMISSION_DENIED"
    message = "没有权限"
    status_code = 403


# ============ 账户异常 ============
class AccountException(NexusException):
    code = "ACCOUNT_ERROR"
    message = "账户操作失败"
    status_code = 400


class InsufficientBalanceException(AccountException):
    code = "INSUFFICIENT_BALANCE"
    message = "余额不足"


class AccountNotFoundException(AccountException):
    code = "ACCOUNT_NOT_FOUND"
    message = "账户不存在"
    status_code = 404


# ============ 登录鉴权 ============
class AuthFailedException(NexusException):
    """用户名不存在、口令错误、账号停用——三者共用这一条。

    分开报等于送出一个账号枚举器：先扫出哪些用户名存在，再集中爆破那一个。
    """
    code = "AUTH_FAILED"
    message = "用户名或密码不正确"
    status_code = 401


class AuthLockedException(NexusException):
    """失败次数超限。

    单独一个 code，是为了让前端能把"等一会儿再来"和"密码不对"显示成不同的话。
    锁住不是猜测结果，所以这里如实说明原因。
    """
    code = "AUTH_LOCKED"
    message = "登录尝试过多，请稍后再试"
    status_code = 429


class AccountFrozenException(AccountException):
    code = "ACCOUNT_FROZEN"
    message = "账户已冻结"


# ============ 转账异常 ============
class TransferException(NexusException):
    status_code = 400
    code = "TRANSFER_ERROR"
    message = "转账失败"


class RecipientNotFoundException(TransferException):
    code = "RECIPIENT_NOT_FOUND"
    message = "未找到收款人"
    status_code = 404


class DuplicateTransferException(TransferException):
    code = "DUPLICATE_TRANSFER"
    message = "重复转账（幂等键已存在）"


class TransferTimeoutException(TransferException):
    code = "TRANSFER_TIMEOUT"
    message = "转账处理超时，请查单"
    status_code = 504


# ============ 卡片异常 ============
class CardException(NexusException):
    status_code = 400
    code = "CARD_ERROR"
    message = "卡片操作失败"


class CardLockedException(CardException):
    code = "CARD_LOCKED"
    message = "卡片已锁定，操作被拒绝"


class CardLostException(CardException):
    code = "CARD_LOST"
    message = "卡片已挂失，无法普通解锁"


# ============ 风险拦截异常（特殊：用于反诈拦截）============
class RiskBlockedException(NexusException):
    """风险拦截异常 - 不视为错误，而是业务结果"""
    code = "RISK_BLOCKED"
    message = "转账被风控拦截"
    status_code = 200  # 不是 HTTP 错误
    safe_to_user = True


class HighRiskTransferException(RiskBlockedException):
    code = "HIGH_RISK_TRANSFER"
    message = "高风险转账，已拦截"


class SuspiciousRecipientException(RiskBlockedException):
    code = "SUSPICIOUS_RECIPIENT"
    message = "可疑收款人，请核实"


# ============ 业务规则异常 ============
class BusinessRuleException(NexusException):
    code = "BUSINESS_RULE_ERROR"
    message = "业务规则校验失败"
    status_code = 400


class InsufficientSubscriptionPeriodException(BusinessRuleException):
    code = "INSUFFICIENT_PERIOD"
    message = "产品在锁定期内，无法赎回"


class PlanConflictException(BusinessRuleException):
    code = "PLAN_CONFLICT"
    message = "资金目标冲突，无法操作"


# ============ 系统异常 ============
class LLMException(NexusException):
    code = "LLM_ERROR"
    message = "LLM 调用失败"
    status_code = 503


class LLMTimeoutException(LLMException):
    code = "LLM_TIMEOUT"
    message = "LLM 调用超时"


class ExternalAPIException(NexusException):
    code = "EXTERNAL_API_ERROR"
    message = "外部 API 调用失败"
    status_code = 503


class DatabaseException(NexusException):
    code = "DATABASE_ERROR"
    message = "数据库操作失败"
    status_code = 500
