<#
.SYNOPSIS
  Build (and optionally install) the LMA Windows audio-capture client.

.DESCRIPTION
  Restores and publishes LMACaptureClient.Windows as a win-x64 executable, runs
  the offline SRP self-test, and - with -Install - copies it to a stable
  location and adds a Start Menu (and optional Desktop) shortcut, so you don't
  have to dig into the publish folder to launch it.

  Note: the script deliberately does NOT try to pin to the taskbar. Windows 10+
  removed the supported pin API, the shell verb is absent on current Win11
  builds, and probing for it via Shell.Application loads third-party shell
  extensions that spew their own errors (e.g. "log4net:ERROR ... lockingModel")
  into the install output, making a successful install look broken. The app
  lives in the system tray; users can pin it manually from the Start Menu.

  Build modes:
    -SelfContained  : bundles the .NET 8 Desktop runtime (no prerequisites on
                      the target machine; larger output). Recommended.
    (default)       : framework-dependent (smaller; requires the .NET 8 Desktop
                      Runtime installed on the target machine).

  Install location:
    (default)       : per-user  %LOCALAPPDATA%\Programs\LMA Capture Client (<Stack>)
                      - NO admin needed; appears in the Start Menu for you.
    -ProgramFiles   : machine-wide  %ProgramFiles%\LMA Capture Client (<Stack>)
                      - needs admin; the script re-launches elevated for the copy.

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

# --- Per-stack identity -------------------------------------------------------
# The client is namespaced by the LMA stack it was downloaded from so the apps
# for multiple LMA deployments can be installed side by side (separate install
# dir, shortcut, registry key, start-at-login entry, single-instance mutex).
# Derived from lma-config.json exactly as AppIdentity.cs / make-app.sh do.
$stackName = ""
$cfgPath = Join-Path $PSScriptRoot "lma-config.json"
if (Test-Path $cfgPath) {
    try { $stackName = (Get-Content $cfgPath -Raw | ConvertFrom-Json).stackName } catch { $stackName = "" }
}
if (-not $stackName) { $stackName = "" }
$stackSlug = ($stackName.ToLowerInvariant() -replace '[^a-z0-9-]', '-') -replace '-+', '-'
$stackSlug = $stackSlug.Trim('-')
if ($stackSlug) {
    $appDisplayName = "LMA Capture Client ($stackName)"
    $installFolder  = "LMA Capture Client ($stackName)"
    $runValueName   = "LMACaptureClient-$stackSlug"
    $settingsKey    = "HKCU:\Software\AmazonLMA\CaptureClient\$stackSlug"
    $arpKey         = "LMACaptureClient-$stackSlug"
} else {
    $appDisplayName = "LMA Capture Client"
    $installFolder  = "LMA Capture Client"
    $runValueName   = "LMACaptureClient"
    $settingsKey    = "HKCU:\Software\AmazonLMA\CaptureClient"
    $arpKey         = "LMACaptureClient"
}

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- Uninstall: remove installed copies + shortcuts, then exit (no build) ----
if ($Uninstall) {
    $targets = @(
        (Join-Path $env:LOCALAPPDATA "Programs\$installFolder"),
        (Join-Path $env:ProgramFiles "$installFolder")
    )
    $shortcuts = @(
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$appDisplayName.lnk"),
        (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\$appDisplayName.lnk"),
        (Join-Path ([Environment]::GetFolderPath('Desktop')) "$appDisplayName.lnk")
    )

    # Stop a running instance so files aren't locked.
    Get-Process -Name LMACaptureClient -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

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
    Remove-Item $settingsKey -Recurse -Force -ErrorAction SilentlyContinue
    Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $runValueName -ErrorAction SilentlyContinue

    # Apps & features (ARP) entries - HKCU (per-user) always; HKLM (machine-wide)
    # only removable when elevated (warn otherwise).
    Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$arpKey" -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$arpKey") {
        try { Remove-Item "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$arpKey" -Recurse -Force -ErrorAction Stop }
        catch { Write-Warning "Couldn't remove the machine-wide Apps & features entry - re-run from an elevated (admin) PowerShell." }
    }

    if ($removedAny) { Write-Host "Uninstall complete." }
    else { Write-Host "Nothing to uninstall (no installed copy found)." }
    return
}

Write-Host "==> Building $appDisplayName ($Configuration, self-contained=$SelfContained)"

$publishArgs = @(
    "publish",
    "LMACaptureClient.Windows.csproj",
    "-c", $Configuration,
    "-r", "win-x64",
    "--self-contained", $($SelfContained.IsPresent.ToString().ToLower())
)
dotnet @publishArgs

$publishDir = Join-Path $PSScriptRoot "bin/$Configuration/net8.0-windows/win-x64/publish"
$exe = Join-Path $publishDir "LMACaptureClient.exe"
if (-not (Test-Path $exe)) { throw "Build did not produce $exe" }
Write-Host "==> Built: $exe"

if (-not $SkipSelfTest) {
    Write-Host "==> Running SRP self-test (known-answer vectors, offline)"

    # LMACaptureClient.exe is a WinExe (GUI subsystem) so it can be a tray app with
    # no console window. Consequences we must handle here:
    #   * `& $exe` does NOT block - PowerShell launches it and moves on, leaving
    #     $LASTEXITCODE empty. A plain `& $exe --selftest; if ($LASTEXITCODE -ne 0)`
    #     gate therefore never actually gated anything, and a FAILING self-test
    #     would not have stopped the build.
    #   * Its console writes go straight to the inherited console handle, so they
    #     land out of order with PowerShell's own output (self-test results
    #     appeared after later "==> Installing ..." lines).
    # Start-Process -Wait -PassThru with redirected stdout/stderr fixes both: we
    # get a real exit code to gate on, and we print the output in order ourselves.
    $stOut = [IO.Path]::GetTempFileName()
    $stErr = [IO.Path]::GetTempFileName()
    $proc = Start-Process $exe -ArgumentList "--selftest" -Wait -PassThru -NoNewWindow `
                -RedirectStandardOutput $stOut -RedirectStandardError $stErr
    # -Encoding UTF8 matches the app's Console.OutputEncoding, so any non-ASCII in
    # a failure message (e.g. an exception string) reads back correctly.
    Get-Content $stOut -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
    $stErrText = (Get-Content $stErr -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)
    Remove-Item $stOut, $stErr -Force -ErrorAction SilentlyContinue

    if ($proc.ExitCode -ne 0) {
        if ($stErrText) { Write-Host $stErrText }
        throw "SRP self-test FAILED (exit $($proc.ExitCode)) - do not ship this build."
    }
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

# Register (or refresh) the Windows "Apps & features" / Add-Remove-Programs entry
# so the app is listed in Settings > Apps and can be uninstalled from there. The
# UninstallString is a self-contained encoded PowerShell command (it does NOT
# call back into build-windows.ps1, which may be deleted after install).
function Register-Uninstall {
    param([string]$InstallDir, [string]$InstalledExe, [bool]$MachineWide)

    $arpRoot = if ($MachineWide) { "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$arpKey" }
               else { "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$arpKey" }

    # Removal script baked into UninstallString: kills a running instance, deletes
    # the install dir + shortcuts + per-user settings, then removes this ARP key.
    $userStart = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$appDisplayName.lnk"
    $machineStart = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\$appDisplayName.lnk"
    $desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) "$appDisplayName.lnk"
    $removal = @"
Get-Process -Name LMACaptureClient -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$InstallDir' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$userStart' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$machineStart' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$desktopLnk' -Force -ErrorAction SilentlyContinue
Remove-Item '$settingsKey' -Recurse -Force -ErrorAction SilentlyContinue
Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '$runValueName' -ErrorAction SilentlyContinue
Remove-Item '$arpRoot' -Recurse -Force -ErrorAction SilentlyContinue
"@
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($removal))
    $uninstallCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $enc"

    # DisplayVersion from the built exe's file version, when available.
    $ver = try { (Get-Item $InstalledExe).VersionInfo.ProductVersion } catch { $null }
    if (-not $ver) { $ver = "1.0" }
    # Rough install size in KB for the size column.
    $sizeKb = try { [int]((Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum / 1024) } catch { 0 }

    New-Item -Path $arpRoot -Force | Out-Null
    Set-ItemProperty $arpRoot DisplayName        $appDisplayName
    Set-ItemProperty $arpRoot DisplayVersion     $ver
    Set-ItemProperty $arpRoot Publisher          "Amazon Web Services"
    Set-ItemProperty $arpRoot DisplayIcon        $InstalledExe
    Set-ItemProperty $arpRoot InstallLocation    $InstallDir
    Set-ItemProperty $arpRoot UninstallString    $uninstallCmd
    Set-ItemProperty $arpRoot QuietUninstallString $uninstallCmd
    Set-ItemProperty $arpRoot NoModify           1 -Type DWord
    Set-ItemProperty $arpRoot NoRepair           1 -Type DWord
    if ($sizeKb -gt 0) { Set-ItemProperty $arpRoot EstimatedSize $sizeKb -Type DWord }
    Write-Host "==> Registered in Apps & features (uninstall from Settings > Apps)"
}

function Install-App {
    param([string]$SourceDir, [bool]$MachineWide)

    if ($MachineWide) {
        $installDir = Join-Path $env:ProgramFiles "$installFolder"
        $startMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"
    } else {
        $installDir = Join-Path $env:LOCALAPPDATA "Programs\$installFolder"
        $startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    }

    Write-Host "==> Installing to $installDir"

    # Upgrading over a RUNNING copy fails with "Access to the path ... is denied"
    # because the live process holds its own DLLs open. Close it first (this is an
    # upgrade of the same app, so stopping it is expected), then wait briefly for
    # Windows to release the file handles.
    $running = Get-Process -Name LMACaptureClient -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "==> Closing the running $appDisplayName to upgrade it..."
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        $running | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
    }

    if (Test-Path $installDir) {
        # Retry: even after the process exits, handles can linger a moment.
        $removed = $false
        foreach ($attempt in 1..5) {
            try { Remove-Item $installDir -Recurse -Force -ErrorAction Stop; $removed = $true; break }
            catch { Start-Sleep -Milliseconds 500 }
        }
        if (-not $removed) {
            throw "Couldn't replace $installDir - a file there is still in use. Close $appDisplayName (right-click the tray icon > Quit) and re-run. If you installed machine-wide with -ProgramFiles, re-run from an elevated (admin) PowerShell."
        }
    }
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    Copy-Item (Join-Path $SourceDir '*') $installDir -Recurse -Force

    $installedExe = Join-Path $installDir "LMACaptureClient.exe"

    $lnk = Join-Path $startMenuDir "$appDisplayName.lnk"
    New-Shortcut -LinkPath $lnk -TargetPath $installedExe -Arguments "--gui" -Description $appDisplayName
    Write-Host "==> Start Menu shortcut: $lnk"

    if ($DesktopShortcut) {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $dlnk = Join-Path $desktop "$appDisplayName.lnk"
        New-Shortcut -LinkPath $dlnk -TargetPath $installedExe -Arguments "--gui" -Description $appDisplayName
        Write-Host "==> Desktop shortcut: $dlnk"
    }

    Register-Uninstall -InstallDir $installDir -InstalledExe $installedExe -MachineWide $MachineWide

    return $installedExe
}

$launchExe = $exe
if ($Install) {
    if ($ProgramFiles) {
        # Program Files needs admin - re-launch this copy step elevated.
        $isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            Write-Host "==> -ProgramFiles requires admin; re-launching the install step elevated..."
            $installScript = @"
`$src = '$publishDir'
`$dst = Join-Path `$env:ProgramFiles '$installFolder'
# Close a running copy first, else its own DLLs are locked and the delete fails.
`$running = Get-Process -Name LMACaptureClient -ErrorAction SilentlyContinue
if (`$running) { `$running | Stop-Process -Force -ErrorAction SilentlyContinue; `$running | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue }
if (Test-Path `$dst) {
  `$ok = `$false
  foreach (`$i in 1..5) { try { Remove-Item `$dst -Recurse -Force -ErrorAction Stop; `$ok = `$true; break } catch { Start-Sleep -Milliseconds 500 } }
  if (-not `$ok) { throw "Couldn't replace `$dst - a file there is still in use. Quit $appDisplayName and re-run." }
}
New-Item -ItemType Directory -Path `$dst -Force | Out-Null
Copy-Item (Join-Path `$src '*') `$dst -Recurse -Force
`$exe = Join-Path `$dst 'LMACaptureClient.exe'
`$sm = Join-Path `$env:ProgramData 'Microsoft\Windows\Start Menu\Programs\$appDisplayName.lnk'
`$sh = New-Object -ComObject WScript.Shell
`$s = `$sh.CreateShortcut(`$sm)
`$s.TargetPath = `$exe
`$s.Arguments = '--gui'
`$s.WorkingDirectory = `$dst
`$s.IconLocation = `$exe + ',0'
`$s.Save()
# Apps & features (ARP) entry under HKLM (machine-wide install).
`$arp = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$arpKey'
`$rm = "Get-Process -Name LMACaptureClient -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath '`$dst' -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath '`$sm' -Force -ErrorAction SilentlyContinue; Remove-Item '`$arp' -Recurse -Force -ErrorAction SilentlyContinue"
`$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(`$rm))
`$ucmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand `$enc"
`$ver = try { (Get-Item `$exe).VersionInfo.ProductVersion } catch { '1.0' }
New-Item -Path `$arp -Force | Out-Null
Set-ItemProperty `$arp DisplayName '$appDisplayName'
Set-ItemProperty `$arp DisplayVersion `$ver
Set-ItemProperty `$arp Publisher 'Amazon Web Services'
Set-ItemProperty `$arp DisplayIcon `$exe
Set-ItemProperty `$arp InstallLocation `$dst
Set-ItemProperty `$arp UninstallString `$ucmd
Set-ItemProperty `$arp QuietUninstallString `$ucmd
Set-ItemProperty `$arp NoModify 1 -Type DWord
Set-ItemProperty `$arp NoRepair 1 -Type DWord
"@
            $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($installScript))
            Start-Process powershell -Verb RunAs -Wait -ArgumentList `
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $enc
            $launchExe = Join-Path (Join-Path $env:ProgramFiles "$installFolder") "LMACaptureClient.exe"
            Write-Host "==> Installed (machine-wide): $launchExe"
        } else {
            $launchExe = Install-App -SourceDir $publishDir -MachineWide $true
        }
    } else {
        $launchExe = Install-App -SourceDir $publishDir -MachineWide $false
    }

    Write-Host ""
    Write-Host "=============================================================="
    Write-Host " INSTALL SUCCEEDED"
    Write-Host "=============================================================="
    Write-Host "Launch it from the Start Menu: press the Windows key, type '$appDisplayName'."
    Write-Host "Or run: `"$launchExe`""
    Write-Host ""
    Write-Host "When it starts there is no window: a gray LMA icon appears in the system tray"
    Write-Host "(bottom-right, next to the clock). Left-click that icon to sign in and start."
    Write-Host "It turns red while recording."
    Write-Host "Uninstall any time from Settings > Apps > Installed apps, or: ./build-windows.ps1 -Uninstall"
} else {
    Write-Host ""
    Write-Host "Done (not installed). Run the tray app:   `"$exe`""
    Write-Host "To install + add a Start Menu shortcut:   ./build-windows.ps1 -SelfContained -Install"
    Write-Host "Headless CLI, e.g.:                       `"$exe`" --debug-wav out.wav --username you@example.com"
}
