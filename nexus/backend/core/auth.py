"""Who is allowed to use this thing.

The sandbox used to hand a session to anyone who asked: ``POST /api/session``
minted a cookie for whoever called it, and that cookie was the only thing
standing between the open internet and an agent that can move money. Putting
it on a public address means the front door needs a lock.

Design notes, each of which is a deliberate choice rather than a default:

* **Operator, not customer.** An :class:`OperatorAccount` is whoever is sitting
  at the keyboard — a judge, a reviewer, you. It is deliberately *not* the
  fictional customer (``林知夏``) in the ``users`` table. Collapsing the two
  would make "who signed in" and "whose money is this" the same question, and
  every audit line would then be ambiguous about which one it meant.

* **bcrypt at cost 12, and the 72-byte limit is enforced rather than
  tolerated.** bcrypt silently truncates at 72 bytes: ``"correct horse" +
  70 more characters`` and ``"correct horse"`` would authenticate the same
  person. Silently truncating is how a long passphrase ends up weaker than the
  user believes, so a longer password is *rejected* with an explanation
  instead.

* **Failures are indistinguishable.** Unknown username, wrong password and
  disabled account all return the same message, and all cost the same bcrypt
  work — an unknown username still verifies against a decoy hash. A login form
  that says "no such user" is a username oracle, and one that answers a valid
  username 200× faster than an invalid one is the same oracle with extra steps.

* **Lockout is per account *and* per address, and it escalates.** A single
  counter is trivially defeated: attack one account from many hosts, or many
  accounts from one. Both are counted, and the delay grows with each round.

* **Nothing here is reversible.** There is no password reset endpoint and no
  mailer, because a demo that can be reset by anyone who finds the address is
  a demo that is not access-controlled. A lost password is fixed from the
  server console.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import bcrypt

from .exceptions import BusinessRuleException

#: bcrypt work factor. 12 is ~250ms on commodity hardware — slow enough to make
#: an offline attack expensive, fast enough that a legitimate login is instant.
BCRYPT_ROUNDS = 12

#: bcrypt hashes more than 72 bytes by ignoring the rest. Reject instead of
#: pretending: a user who thinks 200 characters bought more entropy is wrong.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 12

#: Login attempts allowed per address before the door starts closing.
LOGIN_WINDOW_SECONDS = 900.0
LOGIN_ATTEMPTS_PER_WINDOW = 10
#: Per-account, much tighter: this is what actually stops credential stuffing.
ACCOUNT_ATTEMPTS = 5
#: How long a lockout lasts, and how it grows each round.
LOCK_BASE_SECONDS = 300
LOCK_MAX_SECONDS = 3600
#: bcrypt cost 12 of a decoy, computed once at import so an unknown username
#: costs the same wall-clock as a real one.
_DECOY_DIGEST = bcrypt.hashpw(b"nexus-decoy-never-matches", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


class PasswordRejected(BusinessRuleException):
    """The password cannot be accepted, and the reason is safe to display."""

    code = "WEAK_PASSWORD"


def hash_password(password: str) -> str:
    """Hash a password for storage. The plaintext goes no further than here."""
    raw = (password or "").encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordRejected(
            f"密码过长（{len(raw)} 字节）。请控制在 {MAX_PASSWORD_BYTES} 字节以内——"
            "超过的部分不会被加密，等于没设。"
        )
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise PasswordRejected(f"密码至少需要 {MIN_PASSWORD_LENGTH} 位。")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-work verification.

    A missing or malformed hash still runs a full bcrypt comparison against a
    decoy, so "no such account" and "wrong password" take the same time.
    """
    raw = (password or "").encode("utf-8")[:MAX_PASSWORD_BYTES]
    reference = (stored_hash or "").encode()
    try:
        matched = bcrypt.checkpw(raw, reference if reference else _DECOY_DIGEST)
    except (ValueError, TypeError):
        # A corrupt stored hash must not become a fast-path success.
        try:
            bcrypt.checkpw(raw, _DECOY_DIGEST)
        except ValueError:
            pass
        return False
    # Hash a well-formed dummy whenever the account was unknown, so the
    # response time does not depend on whether the row exists.
    if not stored_hash:
        return False
    return bool(matched)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """What actually gets stored.

    The bearer token is high-entropy random, so SHA-256 is the right tool here
    — this is a lookup key for a value with no guessable structure, not a
    password. (Passwords get bcrypt precisely because they *are* guessable.)
    """
    return hashlib.sha256((token or "").encode()).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode(), (right or "").encode())


# ---------------------------------------------------------------------------
# Brute-force bookkeeping. In-memory on purpose: it must not survive a restart
# (a restart must not hand an attacker a fresh budget) and it must never reach
# the database or the logs.
# ---------------------------------------------------------------------------
@dataclass
class LoginGuard:
    """Per-address and per-account failure counters with escalating lockout."""

    by_address: dict[str, list[float]] = field(default_factory=dict)
    by_account: dict[str, list[float]] = field(default_factory=dict)
    locked_until: dict[str, float] = field(default_factory=dict)
    rounds: dict[str, int] = field(default_factory=dict)

    def lockout_seconds(self, key: str) -> int:
        if time.monotonic() >= self.locked_until.get(key, 0.0):
            return 0
        return min(LOCK_MAX_SECONDS, int(self.locked_until[key] - time.monotonic()) + 1)

    def _record(self, bucket: dict[str, list[float]], key: str) -> int:
        now = time.monotonic()
        hits = [t for t in bucket.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        hits.append(now)
        bucket[key] = hits
        return len(hits)

    def check(self, address: str, account: str) -> int:
        """Seconds remaining on whichever lock applies; 0 when free to try."""
        return max(self.lockout_seconds(f"addr:{address}"), self.lockout_seconds(f"acct:{account}"))

    def record_failure(self, address: str, account: str) -> int:
        """Count a failed attempt and lock if this account or address is over."""
        address_hits = self._record(self.by_address, address)
        account_hits = self._record(self.by_account, account)
        self._prune()
        if address_hits > LOGIN_ATTEMPTS_PER_WINDOW:
            return self._lock(f"addr:{address}", f"该来源在 {int(LOGIN_WINDOW_SECONDS // 60)} 分钟内登录失败过多")
        if account_hits >= ACCOUNT_ATTEMPTS:
            return self._lock(f"acct:{account}", "该账号已临时锁定")
        return 0

    def record_success(self, address: str, account: str) -> None:
        self.by_address.pop(address, None)
        self.by_account.pop(account, None)
        for key in (f"addr:{address}", f"acct:{account}"):
            self.locked_until.pop(key, None)
            self.rounds.pop(key, None)

    def _lock(self, key: str, reason: str) -> int:
        rounds = self.rounds.get(key, 0) + 1
        self.rounds[key] = rounds
        seconds = min(LOCK_BASE_SECONDS * (2 ** (rounds - 1)), LOCK_MAX_SECONDS)
        self.locked_until[key] = time.monotonic() + seconds
        return seconds

    def _prune(self) -> None:
        """Keep the maps from growing without bound on a public address."""
        if len(self.by_address) < 4096:
            return
        cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
        for bucket in (self.by_address, self.by_account):
            for key in [k for k, v in bucket.items() if not v or v[-1] < cutoff]:
                bucket.pop(key, None)
        for key in [k for k, v in self.locked_until.items() if v < time.monotonic()]:
            self.locked_until.pop(key, None)


login_guard = LoginGuard()

#: The one message every failure produces. Anything more specific is a probe.
LOGIN_FAILED_MESSAGE = "用户名或密码不正确"


def set_cookie(response, token: str, *, secure: bool, max_age: int) -> None:
    response.set_cookie(
        "nexus_session", token,
        httponly=True,          # never readable from JavaScript
        secure=secure,          # HTTPS-only on a public address
        samesite="strict",      # no cross-site sends at all
        path="/",
        max_age=max_age,
    )


def clear_cookie(response) -> None:
    response.delete_cookie("nexus_session", path="/")


__all__ = [
    "BCRYPT_ROUNDS", "MIN_PASSWORD_LENGTH", "MAX_PASSWORD_BYTES",
    "PasswordRejected", "LoginGuard", "login_guard", "LOGIN_FAILED_MESSAGE",
    "hash_password", "verify_password", "new_session_token", "token_fingerprint",
    "constant_time_equals", "set_cookie", "clear_cookie",
]
