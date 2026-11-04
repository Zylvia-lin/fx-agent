import base64
import hashlib
import inspect
from email.message import EmailMessage

import pytest
from mcp.server.fastmcp import FastMCP

from fx_agent.task_gates import install_task_gates
from fx_agent.task_tools import GATE_TOOLS
from fx_agent.template_extract import extract_hip, extract_nuke


def mime(sections):
    message = EmailMessage()
    message.make_mixed()
    for name, body in sections:
        part = EmailMessage()
        part['Content-Type'] = 'application/octet-stream'
        part['Content-Disposition'] = f'attachment; filename="{name}"'
        part['Content-Transfer-Encoding'] = 'base64'
        part.set_payload(base64.b64encode(body).decode())
        message.attach(part)
    return b'MIME-Version: 1.0\n' + message.as_bytes()


def test_mime_static_decode_legacy_chinese_and_binary():
    binary = bytes(range(256))
    data = mime([
        ('obj/geo1.init', b'type = geo\n'),
        ('obj/geo1/source.init', b'type = sphere\n'),
        ('obj/geo1/source.parm', b'rad ( 2 )\n'),
        ('obj/geo1/source.def', 'comment "手上拖尾"\nposition 2 3\n'.encode('gb18030')),
        ('obj/__netbox1.netboxinit', 'comment := 手上拖尾;\n'.encode('gb18030')),
        ('.OPdummydefs', binary),
    ])
    result = extract_hip(data)
    assert result['parse_status'] == 'parsed'
    node = next(n for n in result['nodes'] if n['path'] == '/obj/geo1/source')
    assert node['parameters']['rad'] == '2'
    assert node['comment'] == '手上拖尾'
    assert result['netboxes'][0]['label'] == '手上拖尾'
    section = next(s for s in result['sections'] if s['name'] == '.OPdummydefs')
    assert section['sha256'] == hashlib.sha256(binary).hexdigest()


def test_mime_rejects_duplicate_and_truncated():
    with pytest.raises(ValueError, match='duplicate'):
        extract_hip(mime([('obj/geo1.init', b'type = geo'), ('obj/geo1.init', b'type = geo')]))
    data = mime([('obj/geo1.init', b'type = geo')])
    with pytest.raises(ValueError, match='Malformed'):
        extract_hip(data[:data.rfind(b'--')-50])


def test_houdini_independently_padded_base64_lines():
    data = mime([('obj/geo1.init', b'type = geo\n'), ('.OPdummydefs', b'abcd')])
    data = data.replace(b'YWJjZA==', b'YQ==\nYmNk')
    result = extract_hip(data)
    section = next(s for s in result['sections'] if s['name'] == '.OPdummydefs')
    assert section['sha256'] == hashlib.sha256(b'abcd').hexdigest()


def test_nuke_ofx_dotted_type_preserves_input_stack():
    result = extract_nuke(b'Read {\n inputs 0\n name Read1\n}\n'
                          b'OFXcom.genarts.sapphire.blursharpen.s_blurmotion_v1 {\n name BlurMotion1\n}\n'
                          b'Grade {\n name Grade1\n}\n')
    assert result['parse_status'] == 'parsed'
    assert len(result['nodes']) == 3
    assert len(result['connections']) == 2


def test_nuke_mask_inputs_are_literal_socket_counts():
    result = extract_nuke(b'Read {\n inputs 0\n name Mask\n}\n'
                          b'Read {\n inputs 0\n name Image\n}\n'
                          b'Grade {\n inputs 1+1\n name Grade1\n}\n')
    assert result['parse_status'] == 'parsed'
    assert {c['src_path'] for c in result['connections']} == {'/Mask', '/Image'}


def test_task_gate_handlers_are_async_for_upstream_dispatch(tmp_path):
    mcp = FastMCP('test')
    async def target(context):
        return '127.0.0.1:18186', str(tmp_path/'scene.hip')
    install_task_gates(mcp, 'houdini', lambda: tmp_path/'data', target)
    for name in GATE_TOOLS:
        assert inspect.iscoroutinefunction(mcp._tool_manager.get_tool(name).fn), name
