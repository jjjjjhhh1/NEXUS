"""Compatibility alias. Implementation: backend.agent.analysis.financial_analysis."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.analysis.financial_analysis', __package__)
_sys.modules[__name__] = _implementation
