"""Compatibility alias. Implementation: backend.agent.security.untrusted."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.security.untrusted', __package__)
_sys.modules[__name__] = _implementation
