"""
服务基类
所有业务服务继承 BaseService，自动获得 session + audit 能力
"""
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import json
from datetime import date, datetime
from decimal import Decimal

from ..core.models import AuditLog
from ..core.logging import logger


def json_value(value):
    """Preserve monetary precision in audit records."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported audit value: {type(value).__name__}")


class BaseService:
    """所有服务基类"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _audit(
        self,
        user_id: Optional[int],
        action: str,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        before_state: Optional[dict] = None,
        after_state: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> None:
        """写审计日志"""
        audit = AuditLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_state=json.dumps(before_state, ensure_ascii=False, default=json_value) if before_state is not None else None,
            after_state=json.dumps(after_state, ensure_ascii=False, default=json_value) if after_state is not None else None,
            evidence_ids=json.dumps(extra, ensure_ascii=False, default=json_value) if extra is not None else None,
        )
        self.session.add(audit)
        await self.session.flush()
        if user_id is not None:
            from .evidence_service import append_evidence, digest
            await append_evidence(self.session,user_id,"BUSINESS_AUDIT",{
                "audit_id":audit.id,"action":action,
                "audit_hash":digest({"user_id":audit.user_id,"action":audit.action,"target_type":audit.target_type,
                    "target_id":audit.target_id,"before_state":audit.before_state,"after_state":audit.after_state,"evidence_ids":audit.evidence_ids})})

        logger.debug(f"[AUDIT] {action} user={user_id} target={target_type}:{target_id}")
