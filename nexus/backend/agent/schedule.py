"""Compatibility alias for the shared business scheduling contract."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module("..core.scheduling", __package__)
