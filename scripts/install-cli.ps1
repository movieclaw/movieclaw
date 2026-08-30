<#
.SYNOPSIS
movieclaw CLI（mclaw）Windows 安装脚本（docs/design/device-auth.md §6.4）。

.DESCRIPTION
在 PowerShell 里一行装好：

    irm https://raw.githubusercontent.com/yipengfei329/movieclaw/main/scripts/install-cli.ps1 | iex

mclaw 是一个静态二进制，装它不需要 Python、Node 或任何包管理器。

装到 %LOCALAPPDATA%\Programs\movieclaw 并把该目录加进用户级 PATH：
Windows 没有 /usr/local/bin 这种「所有进程都读得到」的目录，用户级 PATH 是
不要管理员权限就能让新开的终端、计划任务与 GUI 应用都找到命令的做法。

.PARAMETER Version
指定版本（如 v0.19.0）；缺省装最新版。

.PARAMETER DownloadBase
GitHub 加速镜像地址，国内网络可用。
#>
[CmdletBinding()]
param(
    [string]$Version = $env:MOVIECLAW_CLI_VERSION,
    [string]$DownloadBase = $env:MOVIECLAW_DOWNLOAD_BASE
)

$ErrorActionPreference = 'Stop'

$repo = 'yipengfei329/movieclaw'
if (-not $Version) { $Version = 'latest' }
if (-not $DownloadBase) { $DownloadBase = "https://github.com/$repo/releases" }

# 认出架构。ARM64 的 Windows 设备（Surface Pro X、骁龙本）跑 amd64 二进制
# 要走模拟层，能跑但慢，所以按真实架构取对应产物。
$arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'amd64' }
    'ARM64' { 'arm64' }
    default { throw "不支持的架构：$env:PROCESSOR_ARCHITECTURE（仅 amd64/arm64）" }
}

$asset = "mclaw_windows_$arch.zip"
$url = if ($Version -eq 'latest') {
    "$DownloadBase/latest/download/$asset"
} else {
    "$DownloadBase/download/$Version/$asset"
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("mclaw-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "正在下载 mclaw（windows/$arch）…"
    try {
        Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmp $asset) -UseBasicParsing
    } catch {
        throw "下载失败：$url（网络不通，或该版本没有这个平台的产物）"
    }

    # 校验和随 Release 一起发布；取不到就跳过——网络受限的用户不该因为多取
    # 一个文件失败而装不上，但取得到就必须对得上。
    $checksums = Join-Path $tmp 'checksums.txt'
    try {
        Invoke-WebRequest -Uri ("$($url -replace '/[^/]+$', '')/checksums.txt") `
            -OutFile $checksums -UseBasicParsing
    } catch { $checksums = $null }
    if ($checksums -and (Test-Path $checksums)) {
        $expected = (Get-Content $checksums |
            Where-Object { $_ -match "\s$([regex]::Escape($asset))$" } |
            ForEach-Object { ($_ -split '\s+')[0] }) | Select-Object -First 1
        if ($expected) {
            $actual = (Get-FileHash -Path (Join-Path $tmp $asset) -Algorithm SHA256).Hash
            if ($actual -ne $expected.ToUpper()) {
                throw '校验和不匹配，下载的文件可能被篡改或损坏'
            }
            Write-Host '✓ 校验和通过'
        }
    }

    Expand-Archive -Path (Join-Path $tmp $asset) -DestinationPath $tmp -Force
    $binary = Join-Path $tmp 'mclaw.exe'
    if (-not (Test-Path $binary)) { throw '压缩包里没有 mclaw.exe' }

    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\movieclaw'
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    Copy-Item -Path $binary -Destination (Join-Path $installDir 'mclaw.exe') -Force
    Write-Host "✓ 已安装到 $installDir\mclaw.exe"

    # 用户级 PATH：不需要管理员权限，新开的终端、计划任务与 GUI 应用都读得到
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$installDir*") {
        $updated = if ([string]::IsNullOrEmpty($userPath)) { $installDir } else { "$userPath;$installDir" }
        [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
        Write-Host "✓ 已把 $installDir 加进用户 PATH（新开的终端里生效）"
    }
    # 当前这个会话也能立刻用，不用重开窗口
    $env:Path = "$env:Path;$installDir"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host '下一步：把这台机器配对到你的 movieclaw'
Write-Host '  mclaw login'
Write-Host ''
Write-Host '它会先在局域网里找一遍；找不到（跨网段、VPN，或服务端关了 Jellyfin'
Write-Host '兼容层）就自己给地址：mclaw login --server http://<你的 movieclaw 地址>:3000'
Write-Host ''
Write-Host '命令会显示一段配对码，到网页「设置 → 设备」核对后批准即可。'
