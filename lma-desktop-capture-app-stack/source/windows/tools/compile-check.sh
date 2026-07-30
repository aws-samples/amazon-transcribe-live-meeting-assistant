#!/usr/bin/env bash
#
# compile-check.sh — type-check the whole Windows client (including the WPF UI)
# on a non-Windows machine, using the .NET SDK in Docker.
#
# Why this exists: this client is developed on macOS, where there is no .NET SDK,
# so changes to it used to ship having never been compiled. `dotnet build`
# normally refuses a `net8.0-windows` / `UseWPF` project off Windows, but
# `EnableWindowsTargeting=true` makes the reference assemblies available and the
# compile succeeds — which catches the class of mistake that matters most here:
# wrong API names and types against the real NuGet packages (ScreenRecorderLib,
# NAudio, WPF).
#
# What it does NOT do: run anything. There is no Windows runtime here, so
# behaviour still needs a smoke test on a real Windows machine. This is a
# compile gate, not a substitute for testing.
#
# Usage:  ./tools/compile-check.sh          (needs docker running)
set -euo pipefail

cd "$(dirname "$0")/.."
SRC="$(pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

cp -R Engine App "${WORK}/"

# A project mirroring LMACaptureClient.Windows.csproj, minus the bits that only
# make sense when producing a real Windows executable (WinExe output, RID,
# StartupObject, the config file copy). Package versions are kept in sync with
# the real csproj by reading them from it.
pkg_version() {
  grep -o "Include=\"$1\" Version=\"[^\"]*\"" LMACaptureClient.Windows.csproj \
    | head -1 | sed 's/.*Version="\([^"]*\)".*/\1/'
}
NAUDIO="$(pkg_version NAudio)"
NOTIFYICON="$(pkg_version Hardcodet.NotifyIcon.Wpf)"
SCREENREC="$(pkg_version ScreenRecorderLib)"
echo "==> package versions from csproj: NAudio=${NAUDIO} NotifyIcon=${NOTIFYICON} ScreenRecorderLib=${SCREENREC}"

cat > "${WORK}/compile-check.csproj" <<EOF
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <!-- Library, not WinExe: we only want the type check, not a runnable app. -->
    <OutputType>Library</OutputType>
    <TargetFramework>net8.0-windows</TargetFramework>
    <UseWPF>true</UseWPF>
    <!-- The flag that makes Windows-targeted builds work off Windows. -->
    <EnableWindowsTargeting>true</EnableWindowsTargeting>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <LangVersion>latest</LangVersion>
    <RootNamespace>LMA</RootNamespace>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <Platforms>x64</Platforms>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="Engine/**/*.cs" />
    <Compile Include="App/**/*.cs" />
  </ItemGroup>
  <ItemGroup>
    <PackageReference Include="NAudio" Version="${NAUDIO}" />
    <PackageReference Include="Hardcodet.NotifyIcon.Wpf" Version="${NOTIFYICON}" />
    <PackageReference Include="ScreenRecorderLib" Version="${SCREENREC}" />
  </ItemGroup>
</Project>
EOF

echo "==> compiling (net8.0-windows, WPF) in docker…"
docker run --rm -v "${WORK}:/w" -w /w mcr.microsoft.com/dotnet/sdk:8.0 \
  dotnet build compile-check.csproj -p:Platform=x64 --nologo \
  > "${WORK}/build.log" 2>&1 || true

if grep -qE "^ *[0-9]+ Error\(s\)" "${WORK}/build.log" \
   && ! grep -qE "^ *0 Error\(s\)" "${WORK}/build.log"; then
  grep -E "error [A-Z]+[0-9]+" "${WORK}/build.log" | head -40
  echo
  echo "✗ compile FAILED"
  exit 1
fi

grep -E "warning [A-Z]+[0-9]+" "${WORK}/build.log" | head -20 || true
echo "✓ compile OK (all Engine/ + App/ sources type-check against the real packages)"
echo "  NOTE: this does not run anything — behaviour still needs a Windows smoke test."
