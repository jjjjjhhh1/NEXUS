"""Compatibility alias. Implementation: backend.agent.planning.plan_builder."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.planning.plan_builder', __package__)
_sys.modules[__name__] = _implementation
