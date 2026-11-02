"""Exercise the PowerShell installer without changing real profile settings."""

import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(SHELL is None, reason="Windows PowerShell is required")


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(code):
    result = subprocess.run(
        [SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    marker = next(line for line in result.stdout.splitlines() if line.startswith("FX_TEST_JSON:"))
    return json.loads(marker.removeprefix("FX_TEST_JSON:"))


@pytest.fixture
def setup_paths(tmp_path):
    root = tmp_path / "FX project & 中文"
    pref = tmp_path / "Documents/houdini22.0"
    nuke = tmp_path / "Nuke15.2v1/Nuke15.2.exe"
    for path in (
        root / "pyproject.toml",
        root / "uv.lock",
        root / ".venv/Scripts/python.exe",
        nuke,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    pref.mkdir(parents=True)
    return root, pref, nuke


def harness(paths, tail, *, fail_label=""):
    root, pref, nuke = paths
    return f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
. {quote(ROOT / "scripts/Install-FXAgent.ps1")}
$global:FxTestCalls = New-Object System.Collections.Generic.List[object]
function Resolve-InstallerDependency {{ param($Name, $PackageId) return "C:/mock/$Name.exe" }}
function Assert-FxPortAvailable {{ param($Port) }}
function Invoke-FxChecked {{
    param($Executable, $Arguments, $Label)
    $global:FxTestCalls.Add(@{{ label=$Label; exe=$Executable; argv=@($Arguments) }})
    if ($Label -eq {quote(fail_label)}) {{ throw 'simulated child failure' }}
}}
$fxParams = @{{ AgentRoot={quote(root)}; HoudiniPrefDir={quote(pref)}; NukeExe={quote(nuke)}; UserProfileRoot={quote(root / "profile")} }}
{tail}
"""


def test_double_software_setup_uses_literal_paths_and_checks_before_apply(setup_paths):
    calls = run_ps(
        harness(
            setup_paths,
            """
Invoke-FxInstall @fxParams
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject $global:FxTestCalls.ToArray() -Depth 5 -Compress))
""",
        )
    )
    labels = [call["label"] for call in calls]
    assert labels.index("检查Houdini配置") < labels.index("安装Houdini插件")
    assert labels.index("准备Nuke桥接") < labels.index("安装Houdini插件")
    assert labels[-1] == "安装Nuke插件"
    assert "--group" in calls[0]["argv"] and "nuke" in calls[0]["argv"]
    assert calls[0]["argv"][calls[0]["argv"].index("--project") + 1] == str(setup_paths[0])
    assert "--port" in calls[-1]["argv"] and "8187" in calls[-1]["argv"]


def test_plan_does_not_install_dependencies_or_call_python(setup_paths):
    calls = run_ps(
        harness(
            setup_paths,
            """
function Resolve-InstallerDependency { throw 'Plan attempted dependency installation' }
Invoke-FxInstall @fxParams -Plan
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject $global:FxTestCalls.ToArray() -Compress))
""",
        )
    )
    assert calls == []


def test_child_failure_stops_later_install_steps(setup_paths):
    result = run_ps(
        harness(
            setup_paths,
            """
$fxError = ''
try { Invoke-FxInstall @fxParams } catch { $fxError = $_.Exception.Message }
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject @{ error=$fxError; calls=$global:FxTestCalls.ToArray() } -Depth 5 -Compress))
""",
            fail_label="准备Nuke桥接",
        )
    )
    assert result["error"] == "simulated child failure"
    assert not any(call["label"].startswith("安装") for call in result["calls"])


def test_nuke_only_does_not_change_houdini_registration(setup_paths):
    calls = run_ps(
        harness(
            setup_paths,
            """
Invoke-FxInstall @fxParams -Components Nuke
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject $global:FxTestCalls.ToArray() -Depth 5 -Compress))
""",
        )
    )
    assert not any("install_codex.py" in " ".join(call["argv"]) for call in calls)
    assert calls[-1]["label"] == "安装Nuke插件"


def test_discovery_uses_numeric_versions_including_nuke_patch(tmp_path):
    for folder, exe in (
        ("Nuke13.2v99", "Nuke13.2.exe"),
        ("Nuke15.2v9", "Nuke15.2.exe"),
        ("Nuke15.2v10", "Nuke15.2.exe"),
    ):
        directory = tmp_path / folder
        directory.mkdir()
        (directory / exe).write_bytes(b"fixture")
    result = run_ps(f"""
. {quote(ROOT / "scripts/Install-FXAgent.ps1")}
$fxFound = Find-FxNukeExecutable -Roots @({quote(tmp_path)})
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject $fxFound -Compress))
""")
    assert Path(result).parent.name == "Nuke15.2v10"


def test_conflicting_component_ports_stop_before_dependency_install(setup_paths):
    result = run_ps(
        harness(
            setup_paths,
            """
$fxError = ''
try { Invoke-FxInstall @fxParams -NukePort 8186 } catch { $fxError = $_.Exception.Message }
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject @{ error=$fxError; calls=$global:FxTestCalls.ToArray() } -Depth 5 -Compress))
""",
        )
    )
    assert result["error"]
    assert result["calls"] == []


def test_winget_bootstrap_uses_package_id_and_refreshes_lookup():
    result = run_ps(f"""
. {quote(ROOT / "scripts/Install-FXAgent.ps1")}
$global:FxInstalled = $false
$global:FxBootstrap = @()
function Get-Command {{
    param($Name, $CommandType, $ErrorAction)
    if ($Name -eq 'winget.exe') {{ return [pscustomobject]@{{Source='C:/mock/winget.exe'}} }}
    if ($global:FxInstalled) {{ return [pscustomobject]@{{Source='C:/mock/uv.exe'}} }}
}}
function Invoke-FxChecked {{
    param($Executable, $Arguments, $Label)
    $global:FxBootstrap = $Arguments
    $global:FxInstalled = $true
}}
$fxTool = Resolve-InstallerDependency -Name uv -PackageId astral-sh.uv
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject @{{ path=$fxTool; arguments=$global:FxBootstrap }} -Depth 4 -Compress))
""")
    assert result["path"] == "C:/mock/uv.exe"
    assert result["arguments"][result["arguments"].index("--id") + 1] == "astral-sh.uv"


def test_real_child_exit_code_is_not_reported_as_success():
    result = run_ps(f"""
. {quote(ROOT / "scripts/Install-FXAgent.ps1")}
$fxError = ''
try {{ Invoke-FxChecked -Executable {quote(SHELL)} -Arguments @('-NoProfile', '-Command', 'exit 7') -Label 'child' }} catch {{ $fxError=$_.Exception.Message }}
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject $fxError -Compress))
""")
    assert "7" in result


@pytest.mark.parametrize("valid_pref", [True, False])
def test_bat_runs_from_another_directory_and_preserves_failure_status(setup_paths, valid_pref):
    _, pref, nuke = setup_paths
    selected = pref if valid_pref else pref / "missing"
    result = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            str(ROOT / "Install-FX-Agent.bat"),
            "-Plan",
            "-HoudiniPrefDir",
            str(selected),
            "-NukeExe",
            str(nuke),
        ],
        cwd=pref,
        input="\n",
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == (0 if valid_pref else 1), result.stdout + result.stderr
    if valid_pref:
        assert str(ROOT) in result.stdout


def test_powershell_invokes_python_with_literal_arguments(setup_paths):
    root, _, _ = setup_paths
    probe = root / "echo arguments.py"
    probe.write_text(
        "import json,sys\nprint('FX_TEST_JSON:'+json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    result = run_ps(f"""
. {quote(ROOT / "scripts/Install-FXAgent.ps1")}
Invoke-FxChecked -Executable {quote(ROOT / ".venv/Scripts/python.exe")} -Arguments @({quote(probe)}, {quote(root)}, 'one & two') -Label 'literal args'
""")
    assert result == [str(root), "one & two"]


def test_real_port_preflight_accepts_free_socket_and_blocks_occupied_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = run_ps(f"""
. {quote(ROOT / "scripts/Install-FXAgent.ps1")}
Assert-FxPortAvailable -Port 0
$fxBlocked = $false
try {{ Assert-FxPortAvailable -Port {port} }} catch {{ $fxBlocked = $true }}
Write-Output ('FX_TEST_JSON:' + (ConvertTo-Json -InputObject $fxBlocked -Compress))
""")
    assert result is True
