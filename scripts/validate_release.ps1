[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FFmpegPath,

    [switch]$RunOnlineRecognition
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "release_helpers.ps1")

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw "Release .venv is missing. Run scripts/build_release.ps1 first."
}

$ContractValues = @(& $VenvPython -c "from app_runtime import APP_VERSION, FFMPEG_VERSION, FFMPEG_SHA256; print(APP_VERSION); print(FFMPEG_VERSION); print(FFMPEG_SHA256)")
if ($LASTEXITCODE -ne 0 -or $ContractValues.Count -ne 3) {
    throw "Unable to load the release contract."
}
$Contract = [pscustomobject]@{
    app_version = [string]$ContractValues[0]
    ffmpeg_version = [string]$ContractValues[1]
    ffmpeg_sha256 = [string]$ContractValues[2]
}
$AppVersion = $Contract.app_version
$DeliveryRoot = Join-Path $RepoRoot "dist\ASRTools-Windows-x64-v$AppVersion"
$ValidationRoot = Join-Path $RepoRoot "build\release-v$AppVersion\validation"

if (-not (Test-Path -LiteralPath $DeliveryRoot -PathType Container)) {
    throw "Release delivery directory is missing: $DeliveryRoot"
}
if (-not (Test-Path -LiteralPath $FFmpegPath -PathType Leaf)) {
    throw "Verified FFmpeg input is missing: $FFmpegPath"
}
$FFmpegPath = (Resolve-Path -LiteralPath $FFmpegPath).Path
$ActualFFmpegHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $FFmpegPath).Hash.ToUpperInvariant()
if ($ActualFFmpegHash -ne $Contract.ffmpeg_sha256) {
    throw "FFmpeg SHA-256 mismatch during validation."
}

$ValidationFullPath = [IO.Path]::GetFullPath($ValidationRoot)
$RepoPrefix = $RepoRoot.TrimEnd('\') + '\'
if (-not $ValidationFullPath.StartsWith($RepoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to reset validation directory outside the repository: $ValidationFullPath"
}
if (Test-Path -LiteralPath $ValidationFullPath) {
    Remove-Item -LiteralPath $ValidationFullPath -Recurse -Force
}
New-Item -ItemType Directory -Path $ValidationFullPath -Force | Out-Null

$PortableZip = Join-Path $DeliveryRoot "ASRTools-Windows-x64-v$AppVersion-portable.zip"
if (-not (Test-Path -LiteralPath $PortableZip -PathType Leaf)) {
    throw "Release artifact is missing: $PortableZip"
}
$SourceZip = Join-Path $DeliveryRoot "ASRTools-v$AppVersion-source.zip"
$ChecksumFile = Join-Path $DeliveryRoot "SHA256SUMS.txt"
foreach ($RequiredDeliveryFile in @($SourceZip, $ChecksumFile)) {
    if (-not (Test-Path -LiteralPath $RequiredDeliveryFile -PathType Leaf)) {
        throw "Release delivery file is missing: $RequiredDeliveryFile"
    }
}

$UnexpectedExecutables = @(Get-ChildItem -LiteralPath $DeliveryRoot -File -Filter "*.exe")
if ($UnexpectedExecutables.Count -ne 0) {
    throw "Delivery directory must not contain a onefile or loose EXE: $($UnexpectedExecutables.Name -join ', ')."
}

$ExpectedHashTargets = @($PortableZip, $SourceZip)
$ExpectedHashNames = @($ExpectedHashTargets | ForEach-Object { Split-Path -Leaf $_ })
$RecordedHashNames = @()
foreach ($ChecksumLine in Get-Content -LiteralPath $ChecksumFile -Encoding ASCII) {
    if ([string]::IsNullOrWhiteSpace($ChecksumLine)) {
        continue
    }
    if ($ChecksumLine -notmatch "^([0-9A-Fa-f]{64})\s{2}([^\\/:]+)$") {
        throw "Invalid SHA256SUMS.txt line: $ChecksumLine"
    }

    $RecordedHash = $Matches[1].ToUpperInvariant()
    $RecordedName = $Matches[2]
    if ($RecordedHashNames -contains $RecordedName) {
        throw "Duplicate SHA256SUMS.txt entry: $RecordedName"
    }
    $RecordedHashNames += $RecordedName

    $HashTarget = Join-Path $DeliveryRoot $RecordedName
    if (-not (Test-Path -LiteralPath $HashTarget -PathType Leaf)) {
        throw "SHA256SUMS.txt references a missing artifact: $RecordedName"
    }
    $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $HashTarget).Hash.ToUpperInvariant()
    if ($ActualHash -ne $RecordedHash) {
        throw "Artifact SHA-256 mismatch for $RecordedName. Expected $RecordedHash, got $ActualHash."
    }
}
$HashNameDiff = @(Compare-Object -ReferenceObject ($ExpectedHashNames | Sort-Object) -DifferenceObject ($RecordedHashNames | Sort-Object))
if ($HashNameDiff.Count -ne 0) {
    throw "SHA256SUMS.txt entries do not match the required release artifacts."
}

$PortableExtract = Join-Path $ValidationRoot "portable-extracted"
Expand-Archive -LiteralPath $PortableZip -DestinationPath $PortableExtract
$PackageRoot = $PortableExtract
$ExpectedRootEntries = @("_runtime", "ASRTools.exe", "docs", "README-Windows.txt")
$ActualRootEntries = @(Get-ChildItem -LiteralPath $PackageRoot -Force | Select-Object -ExpandProperty Name | Sort-Object)
$RootEntryDiff = @(Compare-Object -ReferenceObject ($ExpectedRootEntries | Sort-Object) -DifferenceObject $ActualRootEntries)
if ($RootEntryDiff.Count -ne 0) {
    throw "Portable package root layout is invalid. Expected: $($ExpectedRootEntries -join ', '). Actual: $($ActualRootEntries -join ', ')."
}

$PortableExe = Join-Path $PackageRoot "ASRTools.exe"
$RuntimeExe = Join-Path $PackageRoot "_runtime\ASRTools-runtime.exe"
$BundledFFmpeg = Join-Path $PackageRoot "_runtime\ffmpeg.exe"
$RequiredPackageFiles = @(
    $PortableExe,
    $RuntimeExe,
    $BundledFFmpeg,
    (Join-Path $PackageRoot "README-Windows.txt"),
    (Join-Path $PackageRoot "docs\LICENSE.txt"),
    (Join-Path $PackageRoot "docs\THIRD_PARTY_NOTICES.txt"),
    (Join-Path $PackageRoot "docs\SOURCE_CODE.txt")
)
foreach ($RequiredPackageFile in $RequiredPackageFiles) {
    if (-not (Test-Path -LiteralPath $RequiredPackageFile -PathType Leaf)) {
        throw "Portable package file is missing: $RequiredPackageFile"
    }
}
$BundledFFmpegHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BundledFFmpeg).Hash.ToUpperInvariant()
if ($BundledFFmpegHash -ne $Contract.ffmpeg_sha256) {
    throw "Bundled FFmpeg SHA-256 mismatch. Expected $($Contract.ffmpeg_sha256), got $BundledFFmpegHash."
}

$TestAudio = Join-Path $RepoRoot "resources\test.mp3"
if (-not (Test-Path -LiteralPath $TestAudio -PathType Leaf)) {
    throw "Authorized test audio is missing: $TestAudio"
}
$TestVideo = Join-Path $ValidationRoot "conversion-input.mp4"
& $FFmpegPath -hide_banner -loglevel error -f lavfi -i "color=c=black:s=320x240:d=3" -i $TestAudio -t 3 -c:v libx264 -c:a aac -shortest -y $TestVideo
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $TestVideo -PathType Leaf)) {
    throw "Unable to create the local video conversion fixture."
}

function Invoke-PackagedCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$ReportPath
    )

    $Process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory (Split-Path -Parent $Executable) -WindowStyle Hidden -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        $Failure = if (Test-Path -LiteralPath $ReportPath) { Get-Content -LiteralPath $ReportPath -Raw } else { "No report generated." }
        throw "Packaged check failed for $Executable (exit $($Process.ExitCode)): $Failure"
    }
    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        throw "Packaged check did not create its report: $ReportPath"
    }
    $Report = Read-Utf8Json -Path $ReportPath
    if ($Report.status -ne "passed" -or $Report.app_version -ne $AppVersion) {
        throw "Packaged check report is invalid: $ReportPath"
    }
    return $Report
}

$OriginalPath = $env:PATH
$Results = @()
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    foreach ($Variant in @(
        @{ Name = "portable"; Executable = $PortableExe }
    )) {
        $VariantRoot = Join-Path $ValidationRoot $Variant.Name
        New-Item -ItemType Directory -Path $VariantRoot -Force | Out-Null

        $FFmpegReportPath = Join-Path $VariantRoot "ffmpeg.json"
        $FFmpegReport = Invoke-PackagedCheck -Executable $Variant.Executable -Arguments @(
            "--release-check", "ffmpeg", "--report", $FFmpegReportPath
        ) -ReportPath $FFmpegReportPath
        if ($FFmpegReport.ffmpeg_version -notmatch "^ffmpeg version $([regex]::Escape($Contract.ffmpeg_version))(?=[-\s])") {
            throw "$($Variant.Name) resolved an unexpected FFmpeg version: $($FFmpegReport.ffmpeg_version)"
        }

        $ConvertedAudio = Join-Path $VariantRoot "converted.mp3"
        $ConvertReportPath = Join-Path $VariantRoot "convert.json"
        $ConvertReport = Invoke-PackagedCheck -Executable $Variant.Executable -Arguments @(
            "--release-check", "convert", "--input", $TestVideo, "--output", $ConvertedAudio, "--report", $ConvertReportPath
        ) -ReportPath $ConvertReportPath
        if ($ConvertReport.output_bytes -le 0) {
            throw "$($Variant.Name) generated an empty converted audio file."
        }

        $RecognitionStatus = "not run"
        if ($RunOnlineRecognition) {
            $WorkflowInput = Join-Path $VariantRoot "test.mp3"
            Copy-Item -LiteralPath $TestAudio -Destination $WorkflowInput
            $OriginalTestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $TestAudio).Hash
            $WorkflowInputHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $WorkflowInput).Hash
            if ($WorkflowInputHash -ne $OriginalTestHash) {
                throw "$($Variant.Name) workflow input is not byte-identical to resources/test.mp3."
            }
            $RecognitionReportPath = Join-Path $VariantRoot "workflow.json"
            $RecognitionReport = Invoke-PackagedCheck -Executable $Variant.Executable -Arguments @(
                "--release-check", "workflow", "--input", $WorkflowInput, "--report", $RecognitionReportPath
            ) -ReportPath $RecognitionReportPath
            if ($RecognitionReport.output_bytes -le 0 -or $RecognitionReport.task_status -ne "已处理") {
                throw "$($Variant.Name) GUI workflow did not produce a non-empty result with task status 已处理."
            }
            $RecognitionStatus = "passed ($($RecognitionReport.output_bytes) bytes, task status $($RecognitionReport.task_status))"
        }

        $VersionInfo = (Get-Item -LiteralPath $Variant.Executable).VersionInfo
        if ($VersionInfo.FileVersion -notlike "$AppVersion*") {
            throw "$($Variant.Name) file version mismatch: $($VersionInfo.FileVersion)"
        }
        $RuntimeVersionInfo = (Get-Item -LiteralPath $RuntimeExe).VersionInfo
        if ($RuntimeVersionInfo.FileVersion -notlike "$AppVersion*") {
            throw "$($Variant.Name) runtime file version mismatch: $($RuntimeVersionInfo.FileVersion)"
        }
        $Results += [pscustomobject]@{
            Variant = $Variant.Name
            Executable = $Variant.Executable
            RuntimeExecutable = $RuntimeExe
            FileVersion = $VersionInfo.FileVersion
            ProductVersion = $VersionInfo.ProductVersion
            RuntimeFileVersion = $RuntimeVersionInfo.FileVersion
            FFmpeg = $FFmpegReport.ffmpeg_version
            Conversion = "passed ($($ConvertReport.output_bytes) bytes)"
            Recognition = $RecognitionStatus
        }
    }
}
finally {
    $env:PATH = $OriginalPath
}

$ResultLines = foreach ($Result in $Results) {
    @"
## $($Result.Variant)

- Executable: $($Result.Executable)
- Runtime executable: $($Result.RuntimeExecutable)
- File version: $($Result.FileVersion)
- Product version: $($Result.ProductVersion)
- Runtime file version: $($Result.RuntimeFileVersion)
- Package root layout: ASRTools.exe, README-Windows.txt, _runtime/, docs/
- Bundled FFmpeg: $($Result.FFmpeg)
- Video to audio: $($Result.Conversion)
- GUI B interface workflow using byte-identical resources/test.mp3: $($Result.Recognition)
"@
}
$Signatures = foreach ($Artifact in @($PortableExe, $RuntimeExe)) {
    $Signature = Get-AuthenticodeSignature -LiteralPath $Artifact
    "- $(Split-Path -Leaf $Artifact): $($Signature.Status)"
}
$ValidationReport = @"
# ASRTools v$AppVersion 开发侧验证记录

- Validation time UTC: $([DateTime]::UtcNow.ToString('o'))
- Development OS: $([Environment]::OSVersion.VersionString) (Windows 11 x64 build host)
- Target: Windows 10+ x64
- Windows 10 x64 clean-machine result: pending user validation
- PATH during packaged checks: system Windows directories only; no FFmpeg directory
- Test media uploaded online: $(if ($RunOnlineRecognition) { 'resources/test.mp3 only' } else { 'none (online recognition not run)' })
- Verified FFmpeg input SHA-256: $ActualFFmpegHash

$($ResultLines -join [Environment]::NewLine)

## Authenticode

$($Signatures -join [Environment]::NewLine)

SmartScreen cannot be reproduced without an Internet-origin mark on the locally built files. Windows 10 remains unverified until the user completes WINDOWS10-VALIDATION-CHECKLIST.md against these exact hashes.
"@
Set-Content -LiteralPath (Join-Path $DeliveryRoot "VALIDATION-REPORT.md") -Value $ValidationReport -Encoding UTF8

$Results | Format-Table -AutoSize
Write-Host "Validation report updated: $(Join-Path $DeliveryRoot 'VALIDATION-REPORT.md')"
