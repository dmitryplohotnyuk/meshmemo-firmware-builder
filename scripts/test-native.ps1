param(
    [string]$TBeamWorkspace = '.work/portable-tbeam',
    [string]$HeltecWorkspace = '.work/portable-heltec',
    [string]$OutputDirectory = '.work/native-portable',
    [string[]]$AdditionalWorkspaces = @()
)

# Run from a Visual Studio Developer PowerShell with cl.exe on PATH.
$ErrorActionPreference = 'Stop'
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    throw 'Run this script in Visual Studio Developer PowerShell (MSVC cl.exe required).'
}
$builderRoot = Split-Path $PSScriptRoot -Parent
Push-Location $builderRoot
try {
    $outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
    $boards = @(
        @{ Name = 'tbeam'; Workspace = $TBeamWorkspace; Board = 'tbeam-s3-core'; Model = 12; Transport = 1 },
        @{ Name = 'heltec'; Workspace = $HeltecWorkspace; Board = 'heltec-v3'; Model = 43; Transport = 2 }
    )
    $catalog = Get-Content -LiteralPath registry/catalog.json -Raw | ConvertFrom-Json
    foreach ($extra in $AdditionalWorkspaces) {
        $prepared = Get-Content -LiteralPath (Join-Path $extra 'prepare.json') -Raw | ConvertFrom-Json
        $entry = $catalog.boards.PSObject.Properties[$prepared.plan.board]
        if (-not $entry) { throw "Unknown board in workspace: $extra" }
        $hardware = $entry.Value
        $transport = switch ($hardware.transport) {
            'native-usb-cdc' { 1 }
            'usb-uart-console' { 2 }
            default { throw "Unsupported native transport: $($hardware.transport)" }
        }
        $boards += @{ Name = $prepared.plan.board; Workspace = $extra; Board = $prepared.plan.board;
                      Model = [int]$hardware.hardware_model; Transport = $transport }
    }
    $harnesses = @()
    foreach ($board in $boards) {
        $workspace = [IO.Path]::GetFullPath($board.Workspace)
        $prepared = Get-Content -LiteralPath (Join-Path $workspace 'prepare.json') -Raw | ConvertFrom-Json
        if ($prepared.status -ne 'prepared' -or $prepared.plan.board -ne $board.Board -or
            $prepared.plan.hardware.hardware_model -ne $board.Model) {
            throw "Unexpected prepared workspace for $($board.Board)"
        }
        $output = Join-Path $outputRoot $board.Name
        New-Item -ItemType Directory -Path $output -Force | Out-Null
        $configuration = "#pragma once`n#define HW_VENDOR $($board.Model)`n#define MESHMEMO_LOCAL_SERIAL $($board.Transport)`n"
        if ($board.Transport -eq 1) { $configuration += "#define ARDUINO_USB_CDC_ON_BOOT 1`n" }
        Set-Content -LiteralPath (Join-Path $output 'configuration.h') -Value $configuration -Encoding ascii
        $mesh = Join-Path $workspace 'firmware/src/mesh'
        & cl.exe /nologo /std:c++17 /EHsc /W4 "/I$output" "/I$mesh" `
            tests/native/bridge_harness.cpp (Join-Path $mesh 'UsbSfBridge.cpp') `
            "/Fe:$output/bridge.exe" "/Fo:$output/"
        if ($LASTEXITCODE -ne 0) { throw "Native bridge compilation failed: $($board.Board)" }
        $harnesses += @{ path = (Join-Path $output 'bridge.exe'); model = $board.Model;
                         transport = $board.Transport; build_id = $prepared.plan.build_id }
    }
    $policy = Join-Path $outputRoot 'policy'
    New-Item -ItemType Directory -Path $policy -Force | Out-Null
    $cases = @(
        'EXPECT_ALLOWED=1 MESHMEMO_LOCAL_SERIAL=1 ARDUINO_USB_CDC_ON_BOOT=1',
        'EXPECT_ALLOWED=1 MESHMEMO_LOCAL_SERIAL=2',
        'EXPECT_ALLOWED=0 MESHMEMO_LOCAL_SERIAL=1',
        'EXPECT_ALLOWED=0 MESHMEMO_LOCAL_SERIAL=2 ARDUINO_USB_CDC_ON_BOOT=1',
        'EXPECT_ALLOWED=0 MESHMEMO_LOCAL_SERIAL=2 USER_DEBUG_PORT=1',
        'EXPECT_ALLOWED=0 MESHMEMO_LOCAL_SERIAL=1 ARDUINO_USB_CDC_ON_BOOT=1 RP2040_SLOW_CLOCK=1',
        'EXPECT_ALLOWED=0'
    )
    for ($index = 0; $index -lt $cases.Count; $index++) {
        $definitions = @($cases[$index].Split(' ') | ForEach-Object { "/D$_" })
        & cl.exe /nologo /std:c++17 /EHsc /W4 /Ipayloads/portable @definitions `
            tests/native/transport_policy.cpp "/Fe:$policy/case$index.exe" "/Fo:$policy/"
        if ($LASTEXITCODE -ne 0) { throw "Transport policy compilation failed: $index" }
    }
    $harnesses | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $outputRoot 'harnesses.json') -Encoding utf8
    Write-Output "Compiled $($boards.Count) bridge harnesses and seven transport policies in $outputRoot"
} finally {
    Pop-Location
}
