"""HMAC-signed audit chain with commit anchors kept outside the database.

The local key/anchor files protect against database-only tampering, not an
attacker controlling the application host. Production uses a separate signer.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from contextlib import contextmanager
if os.name == "nt":
    import msvcrt
else:
    import fcntl
from pathlib import Path

from sqlalchemy import select

from ..core.config import settings
from ..core.models import AgentEvidence, User, AuditLog


@contextmanager
def _exclusive_lock(stream):
    """Serialize file access on Windows and POSIX hosts."""
    position = stream.tell()
    if os.name == "nt":
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        stream.seek(position)
    else:
        fcntl.flock(stream, fcntl.LOCK_EX)
    try:
        yield
    finally:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream, fcntl.LOCK_UN)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _paths(session):
    root = Path(settings.audit_directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    scope = hashlib.sha256(str(session.bind.url).encode()).hexdigest()[:24]
    return root / "signing.key", root / f"{scope}.anchors.jsonl"


def _key(session) -> bytes:
    path, _ = _paths(session)
    # Lock covers initial creation and reading; concurrent processes cannot
    # see an empty key file before its creator writes the random secret.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as stream, _exclusive_lock(stream):
        key = stream.read()
        if not key:
            key = secrets.token_bytes(32)
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        if len(key) != 32:
            raise RuntimeError("审计签名密钥无效")
        return key


def sign(session, value) -> str:
    return hmac.new(_key(session), canonical(value).encode(), hashlib.sha256).hexdigest()


async def append_evidence(session, user_id: int, event: str, body: dict):
    await session.scalar(select(User).where(User.id == user_id).with_for_update())
    latest = await session.scalar(select(AgentEvidence).where(AgentEvidence.user_id == user_id).order_by(AgentEvidence.sequence.desc()).limit(1))
    sequence, previous = (latest.sequence+1, latest.record_hash) if latest else (1, "0"*64)
    data = {"user_id":user_id, "sequence":sequence, "event":event, "body":body, "previous_hash":previous}
    record_hash = digest(data)
    row = AgentEvidence(**data, record_hash=record_hash, signature=sign(session, record_hash))
    session.add(row)
    await session.flush()
    # Commit anchor is prepared in the same transaction and published only
    # after commit by database.session_scope. Rolled-back events are not anchored.
    _, path = _paths(session)
    anchor = {"user_id":user_id, "sequence":sequence, "record_hash":record_hash}
    anchor["signature"] = sign(session, anchor)
    session.info.setdefault("evidence_anchors", {})[user_id] = (str(path), anchor)
    return row


def publish_anchors(session) -> None:
    anchors = session.info.pop("evidence_anchors", {})
    for path, anchor in anchors.values():
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream, _exclusive_lock(stream):
            stream.write(canonical(anchor)+"\n")
            stream.flush()
            os.fsync(stream.fileno())


async def verify_chain(session, user_id: int) -> dict:
    rows = list((await session.scalars(select(AgentEvidence).where(AgentEvidence.user_id == user_id).order_by(AgentEvidence.sequence))).all())
    previous, sequence = "0"*64, 0
    for row in rows:
        data = {"user_id":row.user_id,"sequence":row.sequence,"event":row.event,"body":row.body,"previous_hash":row.previous_hash}
        if row.sequence != sequence+1 or row.previous_hash != previous or row.record_hash != digest(data) or not hmac.compare_digest(row.signature, sign(session, row.record_hash)):
            return {"ok":False,"status":"tampered","checked":sequence,"failed_sequence":row.sequence}
        if row.event == "BUSINESS_AUDIT":
            audit = await session.get(AuditLog,row.body.get("audit_id"))
            actual = digest({"user_id":audit.user_id,"action":audit.action,"target_type":audit.target_type,
                "target_id":audit.target_id,"before_state":audit.before_state,"after_state":audit.after_state,"evidence_ids":audit.evidence_ids}) if audit else None
            if actual != row.body.get("audit_hash"):
                return {"ok":False,"status":"business_record_tampered","checked":sequence,"failed_sequence":row.sequence}
        previous, sequence = row.record_hash, row.sequence
    _, path = _paths(session)
    highest = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for line in lines:
            anchor = json.loads(line)
            signature = anchor.pop("signature")
            if anchor["user_id"] != user_id:
                continue
            if not hmac.compare_digest(signature, sign(session, anchor)):
                return {"ok":False,"status":"anchor_tampered","checked":sequence}
            if highest is None or anchor["sequence"] > highest["sequence"]:
                highest = anchor
    except (ValueError, KeyError, TypeError):
        return {"ok":False,"status":"anchor_tampered","checked":sequence}
    if highest and (highest["sequence"] > sequence or not any(r.sequence == highest["sequence"] and r.record_hash == highest["record_hash"] for r in rows)):
        return {"ok":False,"status":"truncated","checked":sequence}
    anchored = bool(highest and highest["sequence"] == sequence)
    return {"ok":anchored or not rows,"status":"verified" if anchored else ("empty" if not rows else "unanchored"),"checked":sequence,"anchored_sequence":highest["sequence"] if highest else 0,"protection_scope":"database_tampering; local signer and anchors"}
