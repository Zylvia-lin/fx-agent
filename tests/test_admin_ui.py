"""Late responses must not restore the management page after logout."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_logout_ignores_pending_key_and_page_responses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the admin JavaScript state check")
    page = (
        Path(__file__).resolve().parents[1] / "services/data-service/src/fx_data_service/admin.html"
    )
    script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map(), pending = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value:'', hidden:false, textContent:'', querySelectorAll:()=>[],
    replaceChildren(){}, focus(){}
  });
  return elements.get(id);
}
const context = vm.createContext({
  document:{getElementById:element}, DOMException,
  fetch:(url, options)=>new Promise(resolve=>pending.push({url,options,resolve}))
});
vm.runInContext(source, context);
(async()=>{
  element('manage').hidden=false;
  element('newKey').value='previous-key';
  const create=element('createForm').onsubmit({preventDefault(){}});
  const logout=element('logout').onclick();
  assert.equal(pending.length,4);
  pending[3].resolve({ok:true,json:async()=>({ok:true})});
  await logout;
  // Complete both the old page refresh and key creation AFTER logout.
  pending[0].resolve({ok:true,json:async()=>({keys:[]})});
  pending[1].resolve({ok:true,json:async()=>({projects:[]})});
  pending[2].resolve({ok:true,json:async()=>({api_key:'late-key'})});
  await create;
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('manage').hidden,true);
  assert.equal(element('login').hidden,false);
  assert.equal(element('created').hidden,true);
  assert.equal(element('newKey').value,'');
  console.log('LOGOUT_PENDING_RESPONSES_PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script, str(page)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "LOGOUT_PENDING_RESPONSES_PASS" in result.stdout
