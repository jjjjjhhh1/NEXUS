"""Guard source-layer boundaries and compatibility module identity."""
import ast
import importlib
import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / 'backend'


def test_legacy_paths_preserve_module_identity_and_shared_state():
    mapping = json.loads((BACKEND / 'agent/module_aliases.json').read_text())
    for old, new in mapping.items():
        legacy = importlib.import_module('nexus.' + old)
        canonical = importlib.import_module('nexus.' + new)
        assert legacy is canonical, f'{old} must not create a second singleton or patch seam'


def test_business_services_do_not_depend_on_agent_or_http_layers():
    for path in (BACKEND / 'services').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not any(part in {'agent', 'api'} for part in (node.module or '').split('.')), path


def test_agent_contracts_do_not_import_execution_or_transport():
    forbidden = {'orchestration', 'planning', 'integrations', 'api', 'services', 'security'}
    for path in (BACKEND / 'agent/contracts').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not forbidden.intersection((node.module or '').split('.')), path


def test_shared_frontend_display_primitives_load_before_chat():
    frontend = BACKEND.parent / 'frontend'
    html = (frontend / 'index.html').read_text()
    assert html.index('ui.js') < html.index('app.js')
    shared = (frontend / 'ui.js').read_text()
    assert 'innerHTML' not in shared and 'fetch(' not in shared
    assert 'window.NexusUI' in shared
