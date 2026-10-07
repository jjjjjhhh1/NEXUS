"""Compatibility alias. Implementation: backend.agent.integrations.market_data."""
from importlib import import_module as _import_module
import sys as _sys
_implementation = _import_module('.integrations.market_data', __package__)
_sys.modules[__name__] = _implementation
