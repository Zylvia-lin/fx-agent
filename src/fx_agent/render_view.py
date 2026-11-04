"""Native Render View MCP helpers using the upstream main-thread bridge."""
from pathlib import Path


async def render_view_command(bridge, operation: str, **arguments) -> dict:
    if operation not in {'start', 'status', 'save', 'stop'}:
        raise ValueError('Unknown Render View operation')
    module = Path(__file__).resolve().parents[2] / 'integrations/houdini/fx_agent_render_view.py'
    code = (
        'import runpy\n'
        f'_fx_render_view_api = runpy.run_path({str(module)!r})\n'
        f'_fx_render_view_result = _fx_render_view_api[{operation!r}](**{arguments!r})\n'
    )
    payload = await bridge.execute('code.execute_python', {
        'code': code, 'return_expression': '_fx_render_view_result',
    })
    if not isinstance(payload, dict) or not payload.get('success'):
        raise RuntimeError(f'Render View operation failed: {payload}')
    return payload['return_value']
