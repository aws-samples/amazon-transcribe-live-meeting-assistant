<#
.SYNOPSIS
  Build the LMA Windows audio-capture client.

.DESCRIPTION
  Restores and publishes LMAAudioClient.Windows as a win-x64 executable.

  Two modes:
    -SelfContained  : bundles the .NET 8 Desktop runtime (no prerequisites on
                      the target machine; larger output). Recommended for a
                      download a non-developer will run.
    (default)       : framework-dependent (smaller; requires the .NET 8 Desktop
                      Runtime installed on the target machine).

  After building it runs the offline SRP self-test so you never ship a binary
  whose crypto can't reproduce the pycognito known-answer.

.EXAMPLE
  ./build-windows.ps1                    # framework-dependent Release build
  ./build-windows.ps1 -SelfContained     # standalone Release build
  ./build-windows.ps1 -Configuration Debug -SkipSelfTest
#>
param(
    [string]$Configuration = "Release",
    [switch]$SelfContained,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Building LMA Windows client ($Configuration, self-contained=$SelfContained)"

$publishArgs = @(
    "publish",
    "LMAAudioClient.Windows.csproj",
    "-c", $Configuration,
    "-r", "win-x64",
    "--self-contained", $($SelfContained.IsPresent.ToString().ToLower())
)
dotnet @publishArgs

$publishDir = Join-Path $PSScriptRoot "bin/$Configuration/net8.0-windows/win-x64/publish"
$exe = Join-Path $publishDir "LMAAudioClient.exe"
if (-not (Test-Path $exe)) { throw "Build did not produce $exe" }
Write-Host "==> Built: $exe"

if (-not $SkipSelfTest) {
    Write-Host "==> Running SRP self-test (known-answer vectors, offline)"
    & $exe --selftest
    if ($LASTEXITCODE -ne 0) { throw "SRP self-test FAILED - do not ship this build." }
    Write-Host "==> Self-test passed."
}

Write-Host ""
Write-Host "Done. Run the tray app:      $exe"
Write-Host "Or headless CLI, e.g.:       $exe --debug-wav out.wav --username you@example.com"
