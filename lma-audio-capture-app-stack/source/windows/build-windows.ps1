<#
.SYNOPSIS
  Build (and optionally install) the LMA Windows audio-capture client.

.DESCRIPTION
  Restores and publishes LMAAudioClient.Windows as a win-x64 executable, runs
  the offline SRP self-test, and — with -Install — copies it to a stable
  location and adds a Start Menu (and optional Desktop) shortcut so you don't
  have to dig into the publish folder to launch it.

  Build modes:
    -SelfContained  : bundles the .NET 8 Desktop runtime (no prerequisites on
                      the target machine; larger output). Recommended.
    (default)       : framework-dependent (smaller; requires the .NET 8 Desktop
                      Runtime installed on the target machine).

  Install location:
    (default)       : per-user  %LOCALAPPDATA%\Programs\LMA Audio Capture
                      — NO admin needed; appears in the Start Menu for you.
    -ProgramFiles   : machine-wide  %ProgramFiles%\LMA Audio Capture
                      — needs admin; the script re-launches elevated for the copy.

.EXAMPLE
  ./build-windows.ps1 -SelfContained -Install
      # build standalone, install to %LOCALAPPDATA%\Programs, add Start Menu shortcut

.EXAMPLE
  ./build-windows.ps1 -SelfContained -Install -ProgramFiles -DesktopShortcut
      # install machine-wide (elevates) and also add a Desktop shortcut

.EXAMPLE
  ./build-windows.ps1                    # framework-dependent build only, no install

.EXAMPLE
  ./build-windows.ps1 -Uninstall
      # remove the installed app + Start Menu/Desktop shortcuts (does not build)
#>
param(
    [string]$Configuration = "Release",
    [switch]$SelfContained,
    [switch]$SkipSelfTest,
    [switch]$Install,
    [switch]$ProgramFiles,
    [switch]$DesktopShortcut,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- Uninstall: remove installed copies + shortcuts, then exit (no build) ----
if ($Uninstall) {
    $targets = @(
        (Join-Path $env:LOCALAPPDATA "Programs\LMA Audio Capture"),
        (Join-Path $env:ProgramFiles "LMA Audio Capture")
    )
    $shortcuts = @(
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\LMA Audio Capture.lnk"),
        (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\LMA Audio Capture.lnk"),
        (Join-Path ([Environment]::GetFolderPath('Desktop')) "LMA Audio Capture.lnk")
    )

    # Stop a running instance so files aren't locked.
    Get-Process -Name LMAAudioClient -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

    $removedAny = $false
    foreach ($t in $targets) {
        if (Test-Path $t) {
            try {
                Remove-Item $t -Recurse -Force -ErrorAction Stop
                Write-Host "==> Removed $t"
                $removedAny = $true
            } catch {
                Write-Warning "Couldn't remove $t ($($_.Exception.Message)). If it's the Program Files copy, re-run from an elevated (admin) PowerShell."
            }
        }
    }
    foreach ($s in $shortcuts) {
        if (Test-Path $s) {
            try { Remove-Item $s -Force -ErrorAction Stop; Write-Host "==> Removed shortcut $s"; $removedAny = $true }
            catch { Write-Warning "Couldn't remove $s ($($_.Exception.Message))." }
        }
    }

    # Per-user settings (remembered email) + start-at-login entry left by the app.
    Remove-Item "HKCU:\Software\AmazonLMA\AudioCapture" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "LMAAudioCapture" -ErrorAction SilentlyContinue

    if ($removedAny) { Write-Host "Uninstall complete." }
    else { Write-Host "Nothing to uninstall (no installed copy found)." }
    return
}

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

# --- Optional install: copy to a stable folder + create shortcuts -----------
function New-Shortcut {
    param([string]$LinkPath, [string]$TargetPath, [string]$Arguments = "", [string]$Description = "")
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($LinkPath)
    $sc.TargetPath = $TargetPath
    $sc.Arguments = $Arguments
    $sc.WorkingDirectory = Split-Path $TargetPath
    $sc.Description = $Description
    $sc.IconLocation = "$TargetPath,0"
    $sc.Save()
}

function Install-App {
    param([string]$SourceDir, [bool]$MachineWide)

    if ($MachineWide) {
        $installDir = Join-Path $env:ProgramFiles "LMA Audio Capture"
        $startMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"
    } else {
        $installDir = Join-Path $env:LOCALAPPDATA "Programs\LMA Audio Capture"
        $startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    }

    Write-Host "==> Installing to $installDir"
    if (Test-Path $installDir) { Remove-Item $installDir -Recurse -Force }
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    Copy-Item (Join-Path $SourceDir '*') $installDir -Recurse -Force

    $installedExe = Join-Path $installDir "LMAAudioClient.exe"

    $lnk = Join-Path $startMenuDir "LMA Audio Capture.lnk"
    New-Shortcut -LinkPath $lnk -TargetPath $installedExe -Arguments "--gui" -Description "LMA Audio Capture"
    Write-Host "==> Start Menu shortcut: $lnk"

    if ($DesktopShortcut) {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $dlnk = Join-Path $desktop "LMA Audio Capture.lnk"
        New-Shortcut -LinkPath $dlnk -TargetPath $installedExe -Arguments "--gui" -Description "LMA Audio Capture"
        Write-Host "==> Desktop shortcut: $dlnk"
    }

    return $installedExe
}

$launchExe = $exe
if ($Install) {
    if ($ProgramFiles) {
        # Program Files needs admin — re-launch this copy step elevated.
        $isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            Write-Host "==> -ProgramFiles requires admin; re-launching the install step elevated…"
            $installScript = @"
`$src = '$publishDir'
`$dst = Join-Path `$env:ProgramFiles 'LMA Audio Capture'
if (Test-Path `$dst) { Remove-Item `$dst -Recurse -Force }
New-Item -ItemType Directory -Path `$dst -Force | Out-Null
Copy-Item (Join-Path `$src '*') `$dst -Recurse -Force
`$sm = Join-Path `$env:ProgramData 'Microsoft\Windows\Start Menu\Programs\LMA Audio Capture.lnk'
`$sh = New-Object -ComObject WScript.Shell
`$s = `$sh.CreateShortcut(`$sm)
`$s.TargetPath = Join-Path `$dst 'LMAAudioClient.exe'
`$s.Arguments = '--gui'
`$s.WorkingDirectory = `$dst
`$s.IconLocation = (Join-Path `$dst 'LMAAudioClient.exe') + ',0'
`$s.Save()
"@
            $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($installScript))
            Start-Process powershell -Verb RunAs -Wait -ArgumentList `
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $enc
            $launchExe = Join-Path (Join-Path $env:ProgramFiles "LMA Audio Capture") "LMAAudioClient.exe"
            Write-Host "==> Installed (machine-wide): $launchExe"
        } else {
            $launchExe = Install-App -SourceDir $publishDir -MachineWide $true
        }
    } else {
        $launchExe = Install-App -SourceDir $publishDir -MachineWide $false
    }

    Write-Host ""
    Write-Host "Installed. Launch it from the Start Menu: search 'LMA Audio Capture'."
    Write-Host "Or run:                                   `"$launchExe`""
} else {
    Write-Host ""
    Write-Host "Done (not installed). Run the tray app:   `"$exe`""
    Write-Host "To install + add a Start Menu shortcut:   ./build-windows.ps1 -SelfContained -Install"
    Write-Host "Headless CLI, e.g.:                       `"$exe`" --debug-wav out.wav --username you@example.com"
}
