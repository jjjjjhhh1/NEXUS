"""A boundary for content this application did not author.

Every string that arrives from outside — an API response field, a provider's
error body — is text somebody else chose. In a personal finance agent that
matters more than usual: one of the feeds is SEC EDGAR, and the company name
returned there is a free-text field the company itself fills in. Render it
verbatim and a company can put arbitrary text in front of a customer, wearing
our layout and our credibility.

This module is that boundary. It does not decide what is *true*; upstream
adapters already own that. It decides what is *safe to show*.

Failure modes this layer owns:
  - Unbounded or control-character-bearing text from a provider
  - Instruction-shaped text ("ignore previous instructions") reaching a
    customer as if it were our own copy
  - Links or markup from a third party smuggled into our own chrome
  - A provider field silently replacing a value we did author

What it deliberately does NOT own:
  - Deciding whether a fetched number is correct   -> the fetch adapter
  - Blocking the request in the first place         -> guard.py
  - Reviewing the finished answer for compliance   -> compliance.py
  - Sanitising HTML the customer themselves typed  -> the renderer (it escapes)
"""
from __future__ import annotations

import re
import unicodedata

# A third-party string longer than this is a payload, not a name. Real company
# names, indicator labels and form types are far shorter.
MAX_EXTERNAL_TEXT = 80

# Control and format characters. Zero-width joiners and bidi overrides are the
# ones that matter: they render as nothing and can reorder what a customer
# believes they were shown.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# Text shaped like an instruction to a model rather than a fact for a human.
# Checked against the *rendered* string, so it also catches a provider that
# tries to smuggle a prompt past this layer.
_INJECTION = re.compile(
    r"(?:忽略|无视|忘记|覆盖).{0,12}(?:以上|之前|前面|系统|指令|提示|规则)"
    r"|(?:ignore|disregard|forget|override).{0,20}(?:previous|prior|above|system|instruction|prompt)"
    r"|<\|?\s*(?:im_start|im_end|system|assistant|user)\s*\|?>"
    r"|\[\s*(?:system|inst)\s*\]"
    r"|you\s+are\s+now|act\s+as\s+an?\s+(?:admin|root|system)"
    r"|你(?:现在)?是(?:一个|个)?(?:系统|管理员|客服|工作人员|银行的人)"
    r"|(?:现在|接下来)?请?(?:你)?(?:立即|马上)?执行(?:转账|支付|操作)"
    r"|(?:系统提示|新的指令|新指令|developer\s+message)",
    re.IGNORECASE,
)

# Anything that would become a live link or a tag once it is in our own markup.
_LINK_OR_MARKUP = re.compile(r"(?:https?://|www\.|ftp://|<\s*/?\s*[a-z][a-z0-9-]*\s*[^>]*>|javascript:)", re.IGNORECASE)


class UntrustedRejected(Exception):
    """Raised when external text cannot be shown. Callers must supply a fallback."""


def is_safe(value: object, *, max_length: int = MAX_EXTERNAL_TEXT) -> bool:
    """True when a third-party string may be rendered as-is."""
    return not _violation(value, max_length=max_length)


def _violation(value: object, *, max_length: int = MAX_EXTERNAL_TEXT) -> str | None:
    """Return a short reason for rejecting this string, or None if it is safe."""
    if value is None:
        return "none"
    text = str(value)
    if not text.strip():
        return "blank"
    if len(text) > max_length:
        return "too_long"
    if _CONTROL.search(text):
        return "control_characters"
    if _INJECTION.search(text):
        return "instruction_shaped"
    if _LINK_OR_MARKUP.search(text):
        return "link_or_markup"
    return None


def clean(value: object, fallback: str, *, field: str = "外部字段") -> str:
    """Return the value if it is safe to show, otherwise the caller's own text.

    Falling back rather than raising is deliberate. A third-party feed being
    hostile is not a reason to fail a customer's question — it is a reason to
    show them something we wrote. The caller must therefore always pass a value
    this application authored, so the boundary degrades to "our copy" instead
    of "no answer".
    """
    if _violation(value) is None:
        return unicodedata.normalize("NFC", str(value)).strip()
    return fallback


def require(value: object, *, field: str = "外部字段") -> str:
    """Return a safe string or refuse.

    For the cases where there is no honest fallback to show — a value whose
    whole purpose is to be that exact text. Refusing is better than
    substituting, because a silently swapped value is worse than an error.
    """
    reason = _violation(value)
    if reason is None:
        return unicodedata.normalize("NFC", str(value)).strip()
    raise UntrustedRejected(f"{field} 不可信：{reason}")


__all__ = [
    "MAX_EXTERNAL_TEXT",
    "UntrustedRejected",
    "clean",
    "is_safe",
    "require",
]
