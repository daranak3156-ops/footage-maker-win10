$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Find-RealBinary([string]$name) {
    $command = Get-Command $name -ErrorAction Stop
    $path = $command.Source
    # Chocolatey often exposes a shim, which cannot be copied on its own.
    if ($env:ChocolateyInstall -and $path.StartsWith((Join-Path $env:ChocolateyInstall 'bin'), [StringComparison]::OrdinalIgnoreCase)) {
        $lib = Join-Path $env:ChocolateyInstall 'lib\ffmpeg'
        $real = Get-ChildItem $lib -Filter "$name.exe" -Recurse -File -ErrorAction Stop |
            Where-Object { $_.DirectoryName -match '[\\/]bin$' } |
            Select-Object -First 1
        if (-not $real) { throw "Real $name.exe was not found in $lib" }
        return $real.FullName
    }
    return $path
}

try {
    $python = (Get-Command python -ErrorAction Stop).Source
    $ffmpeg = Find-RealBinary 'ffmpeg'
    $ffprobe = Find-RealBinary 'ffprobe'
    & $python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed' }
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed --name FootageMaker desktop_app.pyw
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }

    $target = Join-Path $PSScriptRoot 'dist\FootageMaker'
    $tools = Join-Path $target '_ffmpeg'
    New-Item -ItemType Directory -Path $tools -Force | Out-Null
    Copy-Item $ffmpeg (Join-Path $tools 'ffmpeg.exe') -Force
    Copy-Item $ffprobe (Join-Path $tools 'ffprobe.exe') -Force
    # Include adjacent codec DLLs when using a shared FFmpeg distribution.
    Get-ChildItem (Split-Path $ffmpeg) -Filter '*.dll' -File | Copy-Item -Destination $tools -Force
    Copy-Item 'config.json' (Join-Path $target 'config.json') -Force
    Copy-Item 'demo_script.txt' (Join-Path $target 'demo_script.txt') -Force
    & (Join-Path $tools 'ffmpeg.exe') -version | Select-Object -First 1
    if ($LASTEXITCODE -ne 0) { throw 'Bundled FFmpeg failed to start' }
    Write-Host "Application built: $target"
} catch {
    Write-Error $_
    exit 1
}
