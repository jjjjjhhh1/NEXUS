"""Bounded AI planning contract. Amounts are grounded; tools own calculations."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

MODULES = ('goal_calculation', 'financial_diagnosis', 'cashflow', 'risk_compliance', 'product_matching', 'original_goal_impact')

class FinancialTaskPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Literal['model', 'bounded-fallback'] = 'model'
    objective: str = Field(max_length=160)
    evidence: str = Field(min_length=1, max_length=500)
    amount: str | None = Field(default=None, pattern=r'^\d{1,10}(?:\.\d{1,2})?$')
    months: int | None = Field(default=None, ge=1, le=1200)
    kind: Literal['profit', 'savings', 'allocation'] = 'allocation'
    confidence: float = Field(default=0.0, ge=0, le=1)
    modules: list[Literal['goal_calculation', 'financial_diagnosis', 'cashflow', 'risk_compliance', 'product_matching', 'original_goal_impact']] = Field(default_factory=list, max_length=6)
