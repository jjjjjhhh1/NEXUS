"""Closed intent schema. The set of intents the agent can produce, and the
slot shape each intent accepts.

This module owns the *vocabulary* of write/read actions. Every other module
that mentions an intent string imports it from here, so changing a name is a
single-file edit. The schema is intentionally closed (extra="forbid") — the
model cannot invent new intents at runtime.

This layer does NOT own:
  - Whether the message is safe to send to the model       -> guard.py
  - Whether the intent is in scope of the demo             -> boundary.py
  - Whether the slots are present and well-formed          -> grounder.py
  - Whether the user typed a precise instruction           -> policy.py
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# The subset of intents that produce a confirmation card before mutation.
# Anything in here must be preceded by an explicit user click to execute.
WRITE_INTENTS = frozenset({
    "transfer",
    "lock_card",
    "unlock_card",
    "report_lost",
    "cancel_subscription",
    "revoke_mandate",
})


class Proposal(BaseModel):
    """A typed user request. Produced by the model (or a local fast-path)
    and then validated by grounder.ground() before any write is offered."""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent: Literal[
        "transfer",
        "lock_card",
        "unlock_card",
        "report_lost",
        "cancel_subscription",
        "revoke_mandate",
        "overview",
        "financial_analysis",
        "bill_analysis",
        "explain",
        "off_topic",
        "unsupported_financial",
        "blocked",
        "clarify",
    ]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    # Slots — all optional, but the grounder requires the relevant ones
    # to appear literally in the message (no invention).
    recipient: str | None = Field(default=None, max_length=30)
    amount: str | None = Field(default=None, max_length=30)
    amount_evidence: str | None = Field(default=None, max_length=50)
    last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    merchant: str | None = Field(default=None, max_length=40)

    # Used by FAQ / explain paths.
    topic: Literal[
        "capabilities",
        "confirmation",
        "subscription_vs_mandate",
        "card_lock_vs_loss",
        "security",
        "transfer",
    ] | None = None


__all__ = ["Proposal", "WRITE_INTENTS"]