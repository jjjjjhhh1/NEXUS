"""HTTP input contracts; no business execution or session state."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

class LoginInput(BaseModel):
    """Credentials. Bounded so an oversized body cannot reach the hasher."""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=500)
    request_id: UUID


class RatingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=36, max_length=36)
    stars: int = Field(ge=1, le=5)


class RiskAnswers(BaseModel):
    # A questionnaire can legitimately be half filled, so empty is allowed here
    # and rejected upstream by the scorer — that way the customer is told *which*
    # question they left blank instead of receiving a schema validation dump.
    # ``extra="forbid"`` still blocks anything the UI never rendered.
    model_config = ConfigDict(extra="forbid")
    experience: str = Field(default="", max_length=40)
    max_loss: str = Field(default="", max_length=40)
    horizon: str = Field(default="", max_length=40)
    purpose: str = Field(default="", max_length=40)


class StepUpPasscode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passcode: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class ActionEdit(BaseModel):
    """The terms a customer may still change on a plan they have not authorised.

    Every field is optional and ``None`` means "leave it as it is" — an editor
    that forces every field to be re-entered is how people re-confirm things
    they meant to change one digit of. The payee is not present by design; see
    ``demo_agent.edit_action`` for why re-pointing a live confirmation is the
    one edit that must not exist.
    """

    model_config = ConfigDict(extra="forbid")
    amount: Optional[str] = Field(default=None, max_length=20)
    purpose: Optional[str] = Field(default=None, max_length=100)
    run_date: Optional[str] = Field(default=None, max_length=10)
    day_of_month: Optional[int] = Field(default=None, ge=1, le=31)
    recurrence: Optional[Literal["once", "weekly", "monthly", "quarterly", "yearly"]] = None
    occurrences: Optional[int] = Field(default=None, ge=1, le=60)


class StepUpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    echoes: dict[str, str] = Field(default_factory=dict)
    passcode: Optional[str] = Field(default=None, max_length=32)


class DemoClockInput(BaseModel):
    now: datetime


