[CmdletBinding()]
param(
    [string]$Version = "dev",
    [string]$PythonExe = "python",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
$specPath = Join-Path $repoRoot "camApp.spec"
$distDir = Join-Path $repoRoot "dist"
$buildDir = Join-Path $repoRoot "build"
$releaseDir = Join-Path $repoRoot "release"
$exePath = Join-Path $distDir "CamApp.exe"
$safeVersion = ($Version -replace '[^A-Za-z0-9._-]', '-').Trim('-')
if ([string]::IsNullOrWhiteSpace($safeVersion)) {
    $safeVersion = "dev"
}
$artifactStem = "CamApp-$safeVersion-windows-x64"
$zipPath = Join-Path $releaseDir "$artifactStem.zip"
$hashPath = Join-Path $releaseDir "$artifactStem.sha256"
$warnSource = Join-Path $buildDir "camApp\warn-camApp.txt"
$warnTarget = Join-Path $releaseDir "$artifactStem-warn.txt"

if (-not (Test-Path $specPath)) {
    throw "Spec file not found: $specPath"
}

if ($Clean) {
    foreach ($path in @($distDir, $buildDir, $releaseDir)) {
        if (Test-Path $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
}

New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

$pyInstallerArgs = @("-m", "PyInstaller", "--noconfirm")
if ($Clean) {
    $pyInstallerArgs += "--clean"
}
$pyInstallerArgs += $specPath

Write-Host "Building CamApp with $PythonExe"
& $PythonExe @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller exited with code $LASTEXITCODE"
}

if (-not (Test-Path $exePath)) {
    throw "Build did not produce $exePath"
}

if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Compress-Archive -Path $exePath -DestinationPath $zipPath -Force
$hash = Get-FileHash -LiteralPath $zipPath -Algorithm SHA256
Set-Content -LiteralPath $hashPath -Value ("{0} *{1}" -f $hash.Hash.ToLowerInvariant(), (Split-Path -Leaf $zipPath)) -Encoding ascii

if (Test-Path $warnSource) {
    Copy-Item -LiteralPath $warnSource -Destination $warnTarget -Force
} else {
    Set-Content -LiteralPath $warnTarget -Value "No PyInstaller warnings captured." -Encoding utf8
}

Write-Host "Build complete"
Write-Host "Executable: $exePath"
Write-Host "Release zip: $zipPath"
Write-Host "Checksum: $hashPath"
if (Test-Path $warnTarget) {
    Write-Host "Warnings: $warnTarget"
}
