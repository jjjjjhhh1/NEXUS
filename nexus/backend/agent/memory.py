"""Compatibility alias. Implementation: backend.agent.context.memory."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.context.memory', __package__)
_sys.modules[__name__] = _implementation
