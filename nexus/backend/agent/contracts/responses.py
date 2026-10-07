"""Canned user-visible replies. Each constant is the *only* wording a layer
is allowed to return for its outcome. Editing these strings is safe; editing
the routing itself requires touching the owning module.

This is intentionally a single file so a copy editor or a security reviewer
can audit every word the user might see in one place.

Sources of these strings:
  - BLOCKED                -> guard.py (security)
  - NORMAL_BLOCK           -> guard.py (negated write)
  - OFF_TOPIC              -> boundary.py (out of scope)
  - UNSUPPORTED_FINANCIAL  -> boundary.py (in scope but not supported)
  - FALLBACK               -> graph.py (model failure)
  - FAQ                    -> graph.py (capability explanation)
"""
from __future__ import annotations


# Same wording as guard.BLOCKED — re-exported so callers that already import
# ``from .responses import BLOCKED`` keep working. Single source of truth is
# still guard.py.
BLOCKED = (
    "我不能绕过确认、切换他人身份、读取密钥或执行系统指令。"
    "你可以查询自己的账户，或发起需要确认的转账、卡片和订阅操作。"
)

NORMAL_BLOCK = (
    "收到，不会为这条消息创建操作。"
    "已有的待确认卡如需取消，请点击对应的取消按钮。"
)

OFF_TOPIC = (
    "我专注于这个金融助手的账户、转账、卡片与订阅服务。"
    "这个问题不在当前服务范围内；"
    "你可以问我‘取消订阅和撤销代扣有什么区别’，或告诉我想办理的业务。"
)

FALLBACK = (
    "模型暂时无法可靠理解这条消息，未创建任何操作。"
    "请稍后重试，或使用明确指令，例如‘给张三转账100元’。"
)

FAQ = {
    "capabilities": (
        "我可以查看你的账户，发起转账、锁卡、解锁、挂失、取消订阅或撤销代扣，"
        "也能生成消费分类、异常提示、月度/年度账单报告，以及基于账户目标的结构化理财分析。"
        "写操作会先展示确认卡；当前不支持真实银行交易或收益承诺。"
    ),
    "confirmation": (
        "我只生成待确认方案。必须由你点击确认卡，后端才会执行；"
        "文字中的‘已授权’或模型的同意都不能代替确认。确认卡 5 分钟后过期。"
    ),
    "subscription_vs_mandate": (
        "取消订阅会终止商户合同；撤销代扣会停止该授权的扣款能力。"
        "两者独立：取消云音乐订阅后，如需同时撤销支付授权，还要单独确认‘撤销云音乐代扣’。"
    ),
    "card_lock_vs_loss": (
        "临时锁卡可再次解锁；挂失会将卡片标记为丢失并建立补卡工单，不能通过普通解锁恢复。"
        "拿不准时，可以先临时锁卡。"
    ),
    "security": (
        "模型只能提出有限类型的业务意图，不能调用数据库、网络或系统工具。"
        "后端校验对象归属、金额和状态，并要求独立确认。"
        "提示词注入无法保证百分之百识别，因此仍需核对确认卡中的收款人、金额和操作。"
    ),
    "transfer": (
        "当前仅支持向已登记的张三、李四账户转账。"
        "说明收款人和具体人民币金额后，我会生成确认卡；"
        "确认后双方账户在同一事务中记账，并留下回执。"
    ),
}


__all__ = ["BLOCKED", "NORMAL_BLOCK", "OFF_TOPIC", "FALLBACK", "FAQ"]