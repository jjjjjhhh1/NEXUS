"""Compatibility alias. Implementation: backend.agent.planning.parser."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.planning.parser', __package__)
_sys.modules[__name__] = _implementation
