// apps/web/app/install/win/route.ts
//
// Returns a self-contained PowerShell installer for Windows. The buyer
// pastes:
//
//   iwr https://delfibot.com/install/win -UseBasicParsing | iex
//
// in PowerShell (or wraps it in `powershell -Command ...` from cmd)
// and the script:
//   1. Stops any running Delfi processes from a prior install
//   2. Downloads the current NSIS installer via the delfibot.com
//      proxy (the user rule says no GitHub URL ever in front of the
//      buyer)
//   3. Strips the Mark-of-the-Web with Unblock-File so SmartScreen
//      doesn't block the silent install
//   4. Runs the NSIS installer with /S (silent) - same behaviour as
//      ticking through the default GUI installer
//   5. Launches Delfi from the installed location
//
// Symmetric with the macOS curl|bash installer at /install/mac.
//
// On Windows the Tauri GUI itself spawns the sidecar (per the
// non-macOS branch in `Delfibot/bot/src-tauri/src/main.rs`), so no
// equivalent of the macOS LaunchAgent bootstrap is needed - just
// install + run is enough.

import { NextResponse } from "next/server";

export const runtime = "nodejs";

export async function GET(): Promise<NextResponse> {
  const script = `# Delfi Windows installer. Paste this in PowerShell:
#   iwr https://delfibot.com/install/win -UseBasicParsing | iex
$ErrorActionPreference = 'Stop'
$PSDefaultParameterValues['*:UseBasicParsing'] = $true

function Say([string]$msg) {
    Write-Host "[delfi] $msg" -ForegroundColor Cyan
}

# 1. Stop any running Delfi from a prior install. Tauri's NSIS
#    installer won't replace files held open by a running process,
#    so /S would silently fail to write the new binary.
Say 'Stopping any running Delfi...'
Get-Process -Name 'Delfi','delfi-sidecar' -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# 2. Download the installer via the proxy. Force TLS 1.2+ for older
#    Windows 10 builds that default to TLS 1.0/1.1 and would otherwise
#    fail the GitHub redirect.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13
} catch {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

$installer = Join-Path $env:TEMP 'Delfi-Setup.exe'
Say "Downloading installer to $installer..."
Invoke-WebRequest -Uri 'https://delfibot.com/api/download/win' -OutFile $installer

# Sanity-check the file is actually an installer, not an error page.
$size = (Get-Item $installer).Length
if ($size -lt 10000000) {
    throw "Download is suspiciously small ($size bytes). Try again in a minute."
}

# 3. Strip the Mark-of-the-Web so Windows SmartScreen doesn't pop a
#    "Windows protected your PC" dialog mid-silent-install.
Say 'Removing the Mark-of-the-Web flag...'
Unblock-File -Path $installer -ErrorAction SilentlyContinue

# 4. Silent NSIS install. /S is Tauri's NSIS bundler's silent flag;
#    end state is identical to clicking through the default install
#    wizard.
Say 'Installing Delfi (silent)...'
$proc = Start-Process -FilePath $installer -ArgumentList '/S' -PassThru -Wait
if ($proc.ExitCode -ne 0) {
    throw "Installer exited with code $($proc.ExitCode). Try downloading the .exe manually from your email."
}
Remove-Item $installer -Force -ErrorAction SilentlyContinue

# 5. Find Delfi.exe and launch it. Tauri NSIS defaults to per-user
#    install under \$env:LOCALAPPDATA\\Delfi but older bundles landed
#    under \\Programs\\Delfi and machine-wide installs go to Program
#    Files - check all three.
$candidates = @(
    (Join-Path $env:LOCALAPPDATA 'Delfi\\Delfi.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\\Delfi\\Delfi.exe'),
    (Join-Path $env:ProgramFiles 'Delfi\\Delfi.exe')
)
$pf86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
if ($pf86) { $candidates += (Join-Path $pf86 'Delfi\\Delfi.exe') }

$exe = $null
foreach ($p in $candidates) {
    if (Test-Path $p) { $exe = $p; break }
}

if ($exe) {
    Say "Launching $exe ..."
    Start-Process -FilePath $exe
    Say 'Done. Delfi should be opening now. Your license email has the key for the first-launch screen.'
} else {
    Say 'Installed, but could not auto-locate Delfi.exe.'
    Say 'Open Delfi from the Start menu - it should appear right after install.'
}
`;
  return new NextResponse(script, {
    status: 200,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      // Same reasoning as the mac installer: each curl|iex pull must
      // get the latest script.
      "Cache-Control": "no-store",
    },
  });
}
