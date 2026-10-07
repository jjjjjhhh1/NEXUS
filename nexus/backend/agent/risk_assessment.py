"""Compatibility alias. Implementation: backend.agent.analysis.risk_assessment."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.analysis.risk_assessment', __package__)
_sys.modules[__name__] = _implementation
