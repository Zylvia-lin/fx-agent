[CmdletBinding()]
param(
    [ValidateSet('Both', 'Houdini', 'Nuke')][string]$Components = 'Both',
    [string]$HoudiniPrefDir,
    [string]$NukeExe,
    [ValidateRange(1, 65535)][int]$HoudiniPort = 18186,
    [ValidateRange(1, 65535)][int]$NukePort = 8187,
    [switch]$Plan
)

$script:FxAgentRoot = Split-Path -Parent $PSScriptRoot

function Invoke-FxChecked {
    param([string]$Executable, [string[]]$Arguments, [string]$Label)
    Write-Host "`n[$Label]" -ForegroundColor Cyan
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label 失败，退出码 $LASTEXITCODE。已完成步骤的备份保留在用户目录 .fx-agent-install-backups 中。"
    }
}

function Resolve-InstallerDependency {
    param([string]$Name, [string]$PackageId)
    $found = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "未找到 $Name 或 WinGet。请先安装 $Name 并重新运行；脚本不会执行网络下载的 PowerShell 代码。"
    }
    Invoke-FxChecked -Executable $winget.Source -Arguments @(
        'install', '--id', $PackageId, '--exact', '--source', 'winget',
        '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
    ) -Label "安装依赖 $Name" | Out-Host
    $env:Path = $env:Path + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $found = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue
    if (-not $found) { throw "$Name 安装后尚未出现在 PATH，请重开终端或重新双击 BAT。" }
    return $found.Source
}

function Assert-FxPortAvailable {
    param([int]$Port)
    $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $Port
    try { $listener.Start() }
    catch { throw "端口 $Port 已被占用。请先保存工程并关闭对应桥接，或通过参数选择其他端口；不会自动换端口。" }
    finally { $listener.Stop() }
}

function Find-FxNukeExecutable {
    param([string[]]$Roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}))
    $candidates = foreach ($directory in $Roots) {
        if (-not $directory -or -not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
        foreach ($folder in Get-ChildItem -LiteralPath $directory -Directory -Filter 'Nuke*') {
            if ($folder.Name -match '^Nuke(\d+\.\d+)v(\d+)$' -and [version]$Matches[1] -ge [version]'15.2') {
                $version = [version]"$($Matches[1]).$($Matches[2])"
                $exe = Join-Path $folder.FullName "Nuke$($Matches[1]).exe"
                if (Test-Path -LiteralPath $exe -PathType Leaf) {
                    [pscustomobject]@{ Path = $exe; Version = $version; Folder = $folder.Name }
                }
            }
        }
    }
    $selected = $candidates | Sort-Object Version, Folder -Descending | Select-Object -First 1
    if (-not $selected) { throw '未找到 Nuke 15.2+。请先安装 Nuke，或用 -NukeExe 指定实际程序路径。' }
    return $selected.Path
}

function Find-FxHoudiniPrefDir {
    param([string[]]$Roots = @([Environment]::GetFolderPath('MyDocuments'), (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Documents')))
    if ($env:HOUDINI_USER_PREF_DIR -and (Test-Path -LiteralPath $env:HOUDINI_USER_PREF_DIR -PathType Container)) {
        return $env:HOUDINI_USER_PREF_DIR
    }
    $candidates = foreach ($directory in $Roots | Select-Object -Unique) {
        if (-not $directory -or -not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
        foreach ($folder in Get-ChildItem -LiteralPath $directory -Directory -Filter 'houdini*') {
            if ($folder.Name -match '^houdini(\d+\.\d+)$') {
                [pscustomobject]@{ Path = $folder.FullName; Version = [version]$Matches[1] }
            }
        }
    }
    $selected = $candidates | Sort-Object Version -Descending | Select-Object -First 1
    if (-not $selected) { throw '未找到 Houdini 用户偏好目录。请先启动 Houdini 一次，或用 -HoudiniPrefDir 指定目录。' }
    return $selected.Path
}

function Invoke-FxInstall {
    param(
        [string]$AgentRoot = $script:FxAgentRoot,
        [ValidateSet('Both', 'Houdini', 'Nuke')][string]$Components = 'Both',
        [string]$HoudiniPrefDir, [string]$NukeExe,
        [ValidateRange(1, 65535)][int]$HoudiniPort = 18186,
        [ValidateRange(1, 65535)][int]$NukePort = 8187,
        [string]$UserProfileRoot = [Environment]::GetFolderPath('UserProfile'),
        [switch]$Plan
    )
    $AgentRoot = (Resolve-Path -LiteralPath $AgentRoot -ErrorAction Stop).Path
    foreach ($required in @('pyproject.toml', 'uv.lock')) {
        if (-not (Test-Path -LiteralPath (Join-Path $AgentRoot $required) -PathType Leaf)) { throw "工程缺少 $required。" }
    }
    $withHoudini = $Components -ne 'Nuke'
    $withNuke = $Components -ne 'Houdini'
    if ($withHoudini) {
        if ($HoudiniPort -in @(8100, 54321)) { throw '不能使用原 MCP 默认端口 8100 或 54321，请选择独立端口。' }
        if (-not $HoudiniPrefDir) { $HoudiniPrefDir = Find-FxHoudiniPrefDir }
        $HoudiniPrefDir = (Resolve-Path -LiteralPath $HoudiniPrefDir -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath $HoudiniPrefDir -PathType Container)) { throw 'Houdini 偏好路径必须是目录。' }
    }
    if ($withNuke) {
        if (-not $NukeExe) { $NukeExe = Find-FxNukeExecutable }
        $NukeExe = (Resolve-Path -LiteralPath $NukeExe -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath $NukeExe -PathType Leaf)) { throw 'NukeExe 必须是已有程序文件。' }
        if ($NukePort -in @(8100, 8186, 18186, 54321) -or $NukePort -eq $HoudiniPort) { throw 'Nuke 端口必须与 Houdini、原版 8100 和 54321 不同。' }
    }
    Write-Host "工程：$AgentRoot`n组件：$Components`n记忆：$(Join-Path $AgentRoot 'data')"
    if ($withHoudini) { Write-Host "Houdini 偏好：$HoudiniPrefDir；端口：$HoudiniPort" }
    if ($withNuke) { Write-Host "Nuke：$NukeExe；端口：$NukePort" }
    if ($Plan) {
        Write-Host '仅预览：未安装依赖、未修改配置。实际安装前会检查端口。'
        return
    }
    if ($withHoudini) { Assert-FxPortAvailable $HoudiniPort }
    if ($withNuke) { Assert-FxPortAvailable $NukePort }
    if ($withNuke) { $null = Resolve-InstallerDependency -Name 'git' -PackageId 'Git.Git' }
    $uv = Resolve-InstallerDependency -Name 'uv' -PackageId 'astral-sh.uv'
    $syncArgs = @('--cache-dir', (Join-Path $AgentRoot '.cache/uv'), 'sync', '--project', $AgentRoot,
        '--frozen', '--inexact', '--no-dev', '--python', '3.12')
    if ($withNuke) { $syncArgs += @('--group', 'nuke') }
    $previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $AgentRoot '.venv'
        Invoke-FxChecked -Executable $uv -Arguments $syncArgs -Label '同步依赖'
    }
    finally { $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment }
    $python = Join-Path $AgentRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw '依赖同步后未找到工程 Python 环境。' }
    $houArgs = @((Join-Path $AgentRoot 'scripts/install_codex.py'), '--houdini-pref', $HoudiniPrefDir, '--port', "$HoudiniPort")
    $nukeArgs = @((Join-Path $AgentRoot 'scripts/install_nuke.py'), '--port', "$NukePort")
    if ($Components -eq 'Nuke') { $nukeArgs += '--require-shared-agent' }
    if ($withHoudini) { Invoke-FxChecked -Executable $python -Arguments $houArgs -Label '检查Houdini配置' }
    if ($withNuke) {
        Invoke-FxChecked -Executable $python -Arguments ($nukeArgs + @('--prepare')) -Label '准备Nuke桥接'
    }
    $data = Join-Path $AgentRoot 'data'
    $legacy = Join-Path $UserProfileRoot '.fx-agent'
    if ((Test-Path -LiteralPath (Join-Path $legacy 'memory.sqlite3') -PathType Leaf) -and
        (Test-Path -LiteralPath $data) -and -not (Test-Path -LiteralPath (Join-Path $data 'memory.sqlite3') -PathType Leaf)) {
        throw '工程 data 目录已存在但没有数据库；检测到旧记忆，停止自动合并。请先核对数据并使用 migrate_data.py。'
    }
    if (-not (Test-Path -LiteralPath $data) -and (Test-Path -LiteralPath (Join-Path $legacy 'memory.sqlite3') -PathType Leaf)) {
        Invoke-FxChecked -Executable $python -Arguments @((Join-Path $AgentRoot 'scripts/migrate_data.py'), $legacy, $data) -Label '迁移旧记忆'
    }
    if ($withHoudini) { Invoke-FxChecked -Executable $python -Arguments ($houArgs + @('--apply')) -Label '安装Houdini插件' }
    if ($withNuke) { Invoke-FxChecked -Executable $python -Arguments ($nukeArgs + @('--apply')) -Label '安装Nuke插件' }
    Write-Host "`n安装配置完成。请启动所选软件，并重启 Codex 或新建聊天。" -ForegroundColor Green
    Write-Host '软件连接与实际效果尚需启动后验证；旧配置备份位于 ~/.fx-agent-install-backups，记忆未删除。'
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    try { Invoke-FxInstall @PSBoundParameters }
    catch {
        Write-Host "安装失败：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host '停止了后续步骤；单个安装器有独立备份和回退，已成功的前置步骤不会统一撤销。'
        exit 1
    }
}
