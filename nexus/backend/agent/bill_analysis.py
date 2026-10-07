"""Compatibility alias. Implementation: backend.agent.analysis.bill_analysis."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.analysis.bill_analysis', __package__)
_sys.modules[__name__] = _implementation
