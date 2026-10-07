"""Compatibility alias. Implementation: backend.agent.planning.cross_scene."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.planning.cross_scene', __package__)
_sys.modules[__name__] = _implementation
