"""Local AA collection requests used to demonstrate split-bill orchestration."""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select

from ..core.exceptions import BusinessRuleException
from ..core.models import AACollection, AuditLog


class AACollectionService:
    def __init__(self, session):
        self.session = session

    async def create(self, user_id: int, participant_count: int, total: Decimal, purpose: str) -> AACollection:
        if not 2 <= participant_count <= 50:
            raise BusinessRuleException("AA 人数需在 2 至 50 人之间")
        if total <= 0 or total > Decimal("1000000"):
            raise BusinessRuleException("AA 总金额超出可发起范围")
        per_person = (total / Decimal(participant_count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        row = AACollection(user_id=user_id, participant_count=participant_count, total_amount=total, per_person_amount=per_person, purpose=purpose.strip(), status="COLLECTING")
        self.session.add(row)
        await self.session.flush()
        self.session.add(AuditLog(user_id=user_id, action="CREATE_AA_COLLECTION", target_type="aa_collection", target_id=row.id, after_state=f"{participant_count}人/{total:.2f}/{purpose}"))
        await self.session.flush()
        return row

    async def list_user_collections(self, user_id: int) -> list[dict]:
        rows = list((await self.session.scalars(select(AACollection).where(AACollection.user_id == user_id).order_by(AACollection.id.desc()))).all())
        return [{"id": row.id, "participant_count": row.participant_count, "total": str(row.total_amount), "per_person": str(row.per_person_amount), "purpose": row.purpose, "status": row.status, "collected_count": 0, "progress_pct": 0, "created_at": row.created_at.isoformat()} for row in rows]
