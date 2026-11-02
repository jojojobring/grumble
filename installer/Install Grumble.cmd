@echo off
title Install Grumble
powershell -NoProfile -ExecutionPolicy Bypass -Command "$text = Get-Content -Raw -LiteralPath '%~f0'; Invoke-Expression ([regex]::Split($text, '(?m)^#<installer>\r?$')[1])"
set result=%errorlevel%
echo.
pause
exit /b %result%
#<installer>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$version = '0.2.1'
$uvVersion = '0.13.6'
$uvHash = 'A2A8820D5EEA32F08EF467EF3B7F66883A9B076D5737A2FC2FBB5F458D1BB50F'
$pythonVersion = '3.14'

# The GRUMBLE_INSTALL_* overrides exist so the installer can be tested without touching a real install.
$source = if ($env:GRUMBLE_INSTALL_SOURCE) { $env:GRUMBLE_INSTALL_SOURCE } else { "https://github.com/jojojobring/grumble/archive/refs/tags/v$version.zip" }
$root = if ($env:GRUMBLE_INSTALL_ROOT) { $env:GRUMBLE_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA 'Grumble' }
$desktop = if ($env:GRUMBLE_INSTALL_SHORTCUTS) { $env:GRUMBLE_INSTALL_SHORTCUTS } else { [Environment]::GetFolderPath('Desktop') }
$startMenu = if ($env:GRUMBLE_INSTALL_SHORTCUTS) { $env:GRUMBLE_INSTALL_SHORTCUTS } else { [Environment]::GetFolderPath('Programs') }

try {
    Write-Host 'Installing Grumble. Nothing else on your PC is changed.'
    New-Item -ItemType Directory -Force -Path $root | Out-Null

    $env:UV_PYTHON_INSTALL_DIR = Join-Path $root 'python'
    $env:UV_TOOL_DIR = Join-Path $root 'tools'
    $env:UV_TOOL_BIN_DIR = Join-Path $root 'bin'
    $env:UV_CACHE_DIR = Join-Path $root 'cache'

    $uv = Join-Path $root 'uv.exe'
    if (-not (Test-Path $uv)) {
        Write-Host '[1/3] Getting the installer tool...'
        $zip = Join-Path $root 'uv.zip'
        Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
        if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $uvHash) {
            Remove-Item $zip
            throw 'The download did not match its expected checksum, so it was not used.'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        try {
            $entry = $archive.Entries | Where-Object { $_.Name -eq 'uv.exe' } | Select-Object -First 1
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $uv, $true)
        } finally {
            $archive.Dispose()
        }
        Remove-Item $zip
    }

    $gpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    $extra = if ($gpu) { '[cuda]' } else { '' }
    if ($gpu) {
        Write-Host '[2/3] NVIDIA graphics card found. Installing Grumble with GPU support. This is about a 1.2 GB download.'
    } else {
        Write-Host '[2/3] No NVIDIA graphics card found. Installing the smaller version, which transcribes on your CPU.'
    }

    # Compiling now keeps the first launch from sitting for half a minute with nothing on screen.
    Write-Host 'This can take a few minutes. Progress shows below.'
    & $uv tool install --force --compile-bytecode --python $pythonVersion "grumble$extra @ $source"
    if ($LASTEXITCODE -ne 0) { throw "The install failed with code $LASTEXITCODE. Check your internet connection and run this again." }

    Write-Host 'Removing temporary download files...'
    & $uv cache clean --quiet
    Write-Host '[3/3] Creating shortcuts...'
    $target = Join-Path $root 'bin\grumble-app.exe'
    if (-not (Test-Path $target)) { throw "The install finished without creating $target." }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in @($desktop, $startMenu)) {
        $shortcut = $shell.CreateShortcut((Join-Path $folder 'Grumble.lnk'))
        $shortcut.TargetPath = $target
        $shortcut.IconLocation = Join-Path $root 'tools\grumble\Lib\site-packages\grumble\grumble.ico'
        $shortcut.Description = 'Talk while you playtest and get proposed issues'
        $shortcut.Save()
    }

    Write-Host ''
    Write-Host 'Done. Grumble is on your Desktop and in the Start Menu.'
    Write-Host 'Its first launch can take up to half a minute before the window appears.'
    if (-not $env:GRUMBLE_INSTALL_SHORTCUTS) { Start-Process $target }
} catch {
    Write-Host ''
    Write-Host "Grumble could not be installed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
