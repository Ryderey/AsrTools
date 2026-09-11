[CmdletBinding()]
param(
    [string]$FFmpegPath,
    [string]$ModelDirectory,
    [string]$BuildPython,
    [string]$ResumeBuildRoot,
    [ValidateRange(1, 64)][int]$Jobs = 4,
    [switch]$Worker,
    [string]$RunDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RunsRoot = Join-Path $RepoRoot 'build\background'
New-Item -ItemType Directory -Path $RunsRoot -Force | Out-Null

if (-not $Worker) {
    $RunDirectory = Join-Path $RunsRoot ((Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $RunDirectory | Out-Null
    $Request = @{ Jobs = $Jobs }
    foreach ($Name in @('FFmpegPath', 'ModelDirectory', 'BuildPython', 'ResumeBuildRoot')) {
        $Value = Get-Variable -Name $Name -ValueOnly
        if ($Value) { $Request[$Name] = [IO.Path]::GetFullPath($Value) }
    }
    $Request | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RunDirectory 'request.json') -Encoding UTF8
    $Shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $Process = Start-Process -FilePath $Shell -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath),
        '-Worker', '-RunDirectory', ('"{0}"' -f $RunDirectory)
    ) -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $RunDirectory 'stdout.log') `
        -RedirectStandardError (Join-Path $RunDirectory 'stderr.log')
    Set-Content -LiteralPath (Join-Path $RunDirectory 'pid.txt') -Value $Process.Id -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $RunsRoot 'latest.txt') -Value $RunDirectory -Encoding UTF8
    Write-Host "Background build dispatched. PID: $($Process.Id)"
    Write-Host "Logs and status: $RunDirectory"
    Write-Host 'You may close this window. Check status.json for the final result.'
    exit 0
}

if (-not $RunDirectory) { throw 'Worker requires RunDirectory.' }
$RunDirectory = (Resolve-Path -LiteralPath $RunDirectory).Path
if (-not (Split-Path -Parent $RunDirectory).Equals($RunsRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Worker directory must be a direct child of build/background.'
}
$StatusPath = Join-Path $RunDirectory 'status.json'
$State = [ordered]@{
    state = 'starting'; pid = $PID; startedAt = [DateTime]::UtcNow.ToString('o')
    finishedAt = $null; exitCode = $null; deliveryDirectory = $null; error = $null
}
function Save-Status {
    $Temporary = Join-Path $RunDirectory 'status.tmp'
    $State | ConvertTo-Json | Set-Content -LiteralPath $Temporary -Encoding UTF8
    Move-Item -LiteralPath $Temporary -Destination $StatusPath -Force
}
$BuildLock = $null
$ResultCode = 1
try {
    Save-Status
    try {
        $BuildLock = [IO.File]::Open((Join-Path $RunsRoot 'build.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch [IO.IOException] {
        throw 'Another background build is running. No second compilation was started.'
    }
    $State.state = 'running'
    Save-Status
    $Request = Get-Content -LiteralPath (Join-Path $RunDirectory 'request.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $BuildArguments = @{}
    foreach ($Property in $Request.PSObject.Properties) { $BuildArguments[$Property.Name] = $Property.Value }
    Get-ChildItem Env: | Where-Object {
        $_.Name -match '(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)'
    } | ForEach-Object {
        Remove-Item -LiteralPath "Env:$($_.Name)"
    }
    $ResultPath = Join-Path $RunDirectory 'result.json'
    & (Join-Path $PSScriptRoot 'build_release.ps1') @BuildArguments -ResultPath $ResultPath
    $Result = Get-Content -LiteralPath $ResultPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $State.deliveryDirectory = $Result.deliveryDirectory
    $State.state = 'succeeded'
    $ResultCode = 0
}
catch {
    $State.state = 'failed'
    $State.error = $_.ToString()
    [Console]::Error.WriteLine($_.ToString())
}
finally {
    $State.exitCode = $ResultCode
    $State.finishedAt = [DateTime]::UtcNow.ToString('o')
    Save-Status
    if ($null -ne $BuildLock) { $BuildLock.Dispose() }
}
exit $ResultCode
