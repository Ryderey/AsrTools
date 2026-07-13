[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FFmpegPath,

    [int]$Jobs = [Math]::Max(1, [Environment]::ProcessorCount - 1)
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$PythonVersionFile = Join-Path $RepoRoot ".python-version"
$LockFile = Join-Path $RepoRoot "requirements-release.lock"
$EntryPoint = Join-Path $RepoRoot "asr_gui.py"
$IconPath = Join-Path $RepoRoot "resources\app_icon.ico"
$ReleaseAssets = Join-Path $RepoRoot "release-assets"
$LauncherSource = Join-Path $ReleaseAssets "portable_launcher.cs"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

function Reset-KnownDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $FullPath = [IO.Path]::GetFullPath($Path)
    $RepoPrefix = $RepoRoot.TrimEnd('\') + '\'
    if (-not $FullPath.StartsWith($RepoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to reset a directory outside the repository: $FullPath"
    }
    if (Test-Path -LiteralPath $FullPath) {
        Remove-Item -LiteralPath $FullPath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $FullPath -Force | Out-Null
}

function Expand-ReleaseTemplate {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $Content = Get-Content -LiteralPath $Source -Raw -Encoding UTF8
    $Content = $Content.Replace("{APP_VERSION}", $Contract.app_version)
    $Content = $Content.Replace("{FFMPEG_VERSION}", $Contract.ffmpeg_version)
    $Content = $Content.Replace("{FFMPEG_SHA256}", $Contract.ffmpeg_sha256)
    if ($Content -match "\{(?:APP_VERSION|FFMPEG_VERSION|FFMPEG_SHA256)\}") {
        throw "Unexpanded release template token in $Source"
    }
    Set-Content -LiteralPath $Destination -Value $Content -Encoding UTF8
}

function New-PortableLauncher {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Icon,
        [Parameter(Mandatory = $true)][string]$AppVersion,
        [Parameter(Mandatory = $true)][string]$WindowsFileVersion
    )

    $SourceCode = Get-Content -LiteralPath $Source -Raw -Encoding UTF8
    $SourceCode = $SourceCode.Replace("__APP_VERSION__", $AppVersion)
    $SourceCode = $SourceCode.Replace("__WINDOWS_FILE_VERSION__", $WindowsFileVersion)
    if ($SourceCode -match "__(?:APP_VERSION|WINDOWS_FILE_VERSION)__") {
        throw "Unexpanded portable launcher token in $Source"
    }
    # Windows PowerShell 5.1 CodeDom can write its temporary C# file using the
    # active ANSI code page. Escape non-ASCII source characters so the launcher
    # keeps Chinese diagnostics without depending on the build machine locale.
    $SourceCode = [regex]::Replace(
        $SourceCode,
        "[^\x00-\x7F]",
        { param($Match) '\u{0:X4}' -f [int][char]$Match.Value }
    )

    $Provider = New-Object Microsoft.CSharp.CSharpCodeProvider
    $Parameters = New-Object System.CodeDom.Compiler.CompilerParameters
    $Parameters.GenerateExecutable = $true
    $Parameters.GenerateInMemory = $false
    $Parameters.IncludeDebugInformation = $false
    $Parameters.OutputAssembly = $Destination
    $Parameters.CompilerOptions = '/target:winexe /platform:x64 /optimize+ /win32icon:"{0}"' -f $Icon
    [void]$Parameters.ReferencedAssemblies.Add("System.dll")

    $Result = $Provider.CompileAssemblyFromSource($Parameters, $SourceCode)
    if ($Result.Errors.HasErrors) {
        $CompilerErrors = ($Result.Errors | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "Unable to compile the portable launcher:$([Environment]::NewLine)$CompilerErrors"
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw "Portable launcher compilation did not create: $Destination"
    }
}

foreach ($RequiredFile in @($PythonVersionFile, $LockFile, $EntryPoint, $IconPath, $LauncherSource)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required release input is missing: $RequiredFile"
    }
}

$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $UvCommand) {
    throw "uv is required. Install uv, then rerun this script. No packages are installed into system Python."
}

$PythonVersion = (Get-Content -LiteralPath $PythonVersionFile -Raw).Trim()
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Invoke-Checked -FilePath $UvCommand.Source -Arguments @(
        "venv", "--python", $PythonVersion, (Join-Path $RepoRoot ".venv")
    )
}
Invoke-Checked -FilePath $UvCommand.Source -Arguments @(
    "pip", "sync", "--python", $VenvPython, "--require-hashes", $LockFile
)

$ContractValues = @(& $VenvPython -c "from app_runtime import APP_VERSION, WINDOWS_FILE_VERSION, FFMPEG_VERSION, FFMPEG_SHA256; print(APP_VERSION); print(WINDOWS_FILE_VERSION); print(FFMPEG_VERSION); print(FFMPEG_SHA256)")
if ($LASTEXITCODE -ne 0 -or $ContractValues.Count -ne 4) {
    throw "Unable to load the release contract from app_runtime.py"
}
$Contract = [pscustomobject]@{
    app_version = [string]$ContractValues[0]
    windows_file_version = [string]$ContractValues[1]
    ffmpeg_version = [string]$ContractValues[2]
    ffmpeg_sha256 = [string]$ContractValues[3]
}

if (-not (Test-Path -LiteralPath $FFmpegPath -PathType Leaf)) {
    throw "FFmpeg input does not exist: $FFmpegPath. Provide the verified FFmpeg $($Contract.ffmpeg_version) executable explicitly."
}
$FFmpegPath = (Resolve-Path -LiteralPath $FFmpegPath).Path
$ActualFFmpegHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $FFmpegPath).Hash.ToUpperInvariant()
if ($ActualFFmpegHash -ne $Contract.ffmpeg_sha256) {
    throw "FFmpeg SHA-256 mismatch. Expected $($Contract.ffmpeg_sha256), got $ActualFFmpegHash."
}
$FFmpegVersionOutput = & $FFmpegPath -version 2>&1
if ($LASTEXITCODE -ne 0 -or -not $FFmpegVersionOutput) {
    throw "FFmpeg failed to start: $FFmpegPath"
}
$FFmpegVersionLine = [string]$FFmpegVersionOutput[0]
if ($FFmpegVersionLine -notmatch "^ffmpeg version $([regex]::Escape($Contract.ffmpeg_version))(?=[-\s])") {
    throw "FFmpeg version mismatch. Expected $($Contract.ffmpeg_version), got: $FFmpegVersionLine"
}

$AppVersion = $Contract.app_version
$BuildRoot = Join-Path $RepoRoot "build\release-v$AppVersion"
$PortableBuild = Join-Path $BuildRoot "portable"
$DeliveryRoot = Join-Path $RepoRoot "dist\ASRTools-Windows-x64-v$AppVersion"
Reset-KnownDirectory -Path $BuildRoot
Reset-KnownDirectory -Path $DeliveryRoot
New-Item -ItemType Directory -Path $PortableBuild -Force | Out-Null

$CommonNuitkaArguments = @(
    "-m", "nuitka",
    "--assume-yes-for-downloads",
    "--enable-plugin=pyqt5",
    "--include-package=qfluentwidgets",
    "--include-package=bk_asr",
    "--windows-console-mode=disable",
    "--windows-icon-from-ico=$IconPath",
    "--windows-product-name=ASRTools",
    "--windows-file-description=ASRTools Speech Recognition Tool",
    "--windows-company-name=ASRTools",
    "--file-version=$($Contract.windows_file_version)",
    "--product-version=$($Contract.windows_file_version)",
    "--lto=yes",
    "--jobs=$Jobs",
    "--remove-output"
)

Push-Location $RepoRoot
try {
    Write-Host "[1/5] Building portable standalone runtime..."
    Invoke-Checked -FilePath $VenvPython -Arguments @(
        $CommonNuitkaArguments +
        @(
            "--mode=standalone",
            "--output-dir=$PortableBuild",
            "--output-filename=ASRTools-runtime.exe",
            "--report=$(Join-Path $BuildRoot 'portable-compilation-report.xml')",
            $EntryPoint
        )
    )
}
finally {
    Pop-Location
}

$PortableDist = @(Get-ChildItem -LiteralPath $PortableBuild -Directory -Filter "*.dist")
if ($PortableDist.Count -ne 1) {
    throw "Expected exactly one Nuitka standalone .dist directory, found $($PortableDist.Count)."
}
$PortableFolderName = "ASRTools-Windows-x64-v$AppVersion-portable"
$PortableStage = Join-Path $BuildRoot $PortableFolderName
$PortableRuntime = Join-Path $PortableStage "_runtime"
$PortableDocs = Join-Path $PortableStage "docs"
New-Item -ItemType Directory -Path $PortableStage, $PortableRuntime, $PortableDocs -Force | Out-Null
Copy-Item -Path (Join-Path $PortableDist[0].FullName "*") -Destination $PortableRuntime -Recurse
Copy-Item -LiteralPath $FFmpegPath -Destination (Join-Path $PortableRuntime "ffmpeg.exe")
New-PortableLauncher `
    -Source $LauncherSource `
    -Destination (Join-Path $PortableStage "ASRTools.exe") `
    -Icon $IconPath `
    -AppVersion $AppVersion `
    -WindowsFileVersion $Contract.windows_file_version

Write-Host "[2/5] Collecting licenses and release documentation..."
$DeliveryLicenses = Join-Path $DeliveryRoot "licenses"
$PythonLicenses = Join-Path $DeliveryLicenses "python"
$FFmpegLicenses = Join-Path $DeliveryLicenses "ffmpeg"
New-Item -ItemType Directory -Path $PythonLicenses, $FFmpegLicenses -Force | Out-Null
Invoke-Checked -FilePath $VenvPython -Arguments @(
    (Join-Path $RepoRoot "scripts\collect_licenses.py"), "--output", $PythonLicenses
)

$FFmpegRoot = Split-Path -Parent (Split-Path -Parent $FFmpegPath)
foreach ($FFmpegDocument in @("LICENSE", "README.txt")) {
    $Source = Join-Path $FFmpegRoot $FFmpegDocument
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required FFmpeg license material is missing: $Source"
    }
    Copy-Item -LiteralPath $Source -Destination $FFmpegLicenses
}

Copy-Item -LiteralPath (Join-Path $RepoRoot "LICENSE") -Destination (Join-Path $DeliveryRoot "LICENSE.txt")
Expand-ReleaseTemplate -Source (Join-Path $ReleaseAssets "README-Windows.template.txt") -Destination (Join-Path $DeliveryRoot "README-Windows.txt")
Expand-ReleaseTemplate -Source (Join-Path $ReleaseAssets "THIRD_PARTY_NOTICES.template.txt") -Destination (Join-Path $DeliveryRoot "THIRD_PARTY_NOTICES.txt")
Expand-ReleaseTemplate -Source (Join-Path $ReleaseAssets "SOURCE_CODE.template.txt") -Destination (Join-Path $DeliveryRoot "SOURCE_CODE.txt")
Expand-ReleaseTemplate -Source (Join-Path $ReleaseAssets "WINDOWS10-VALIDATION-CHECKLIST.template.md") -Destination (Join-Path $DeliveryRoot "WINDOWS10-VALIDATION-CHECKLIST.md")
Expand-ReleaseTemplate -Source (Join-Path $ReleaseAssets "VALIDATION-REPORT.template.md") -Destination (Join-Path $DeliveryRoot "VALIDATION-REPORT.md")

Copy-Item -LiteralPath (Join-Path $DeliveryRoot "README-Windows.txt") -Destination $PortableStage
foreach ($Sidecar in @("LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "SOURCE_CODE.txt")) {
    Copy-Item -LiteralPath (Join-Path $DeliveryRoot $Sidecar) -Destination $PortableDocs
}
Copy-Item -LiteralPath $DeliveryLicenses -Destination (Join-Path $PortableDocs "licenses") -Recurse

Write-Host "[3/5] Creating portable and corresponding-source ZIP files..."
$PortableZip = Join-Path $DeliveryRoot "$PortableFolderName.zip"
Compress-Archive -Path (Join-Path $PortableStage "*") -DestinationPath $PortableZip -CompressionLevel Optimal

$SourceFolderName = "ASRTools-v$AppVersion-source"
$SourceStage = Join-Path $BuildRoot $SourceFolderName
New-Item -ItemType Directory -Path $SourceStage -Force | Out-Null
$SourceItems = @(
    ".python-version",
    ".gitignore",
    "API",
    "LICENSE",
    "README.md",
    "app_runtime.py",
    "asr_gui.py",
    "bk_asr",
    "build.bat",
    "example.py",
    "release-assets",
    "requirements-release.in",
    "requirements-release.lock",
    "requirements.txt",
    "resources",
    "scripts",
    "tests"
)
foreach ($RelativeItem in $SourceItems) {
    $Source = Join-Path $RepoRoot $RelativeItem
    if (Test-Path -LiteralPath $Source) {
        Copy-Item -LiteralPath $Source -Destination $SourceStage -Recurse
    }
}
$SourceZip = Join-Path $DeliveryRoot "$SourceFolderName.zip"
Compress-Archive -LiteralPath $SourceStage -DestinationPath $SourceZip -CompressionLevel Optimal

Write-Host "[4/5] Writing build manifest and artifact hashes..."
$GitCommit = (& git -C $RepoRoot rev-parse HEAD).Trim()
$GitStatus = (& git -C $RepoRoot status --short) -join [Environment]::NewLine
$PythonIdentity = (& $VenvPython -c "import sys, platform; print(sys.version.replace(chr(10), ' ')); print(platform.architecture()[0])") -join " / "
$NuitkaVersion = (& $VenvPython -m nuitka --version | Select-Object -First 1)
$DependencyFreeze = (& $UvCommand.Source pip freeze --python $VenvPython) -join [Environment]::NewLine
$BuildManifest = @"
ASRTools release build manifest
App version: $AppVersion
Windows file/product version: $($Contract.windows_file_version)
Target: Windows 10+ x64
Built at UTC: $([DateTime]::UtcNow.ToString('o'))
Build OS: $([Environment]::OSVersion.VersionString)
PowerShell: $($PSVersionTable.PSVersion)
uv: $(& $UvCommand.Source --version)
Python: $PythonIdentity
Nuitka: $NuitkaVersion
FFmpeg input: $FFmpegPath
FFmpeg version: $FFmpegVersionLine
FFmpeg SHA-256: $ActualFFmpegHash
Git commit: $GitCommit
Git working tree status at build time:
$GitStatus

Locked Python environment:
$DependencyFreeze

Build entry point: scripts/build_release.ps1 -FFmpegPath <verified-ffmpeg.exe>
"@
Set-Content -LiteralPath (Join-Path $DeliveryRoot "BUILD-MANIFEST.txt") -Value $BuildManifest -Encoding UTF8

$HashTargets = @($PortableZip, $SourceZip)
$HashLines = foreach ($Target in $HashTargets) {
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash.ToUpperInvariant()
    "$Hash  $(Split-Path -Leaf $Target)"
}
Set-Content -LiteralPath (Join-Path $DeliveryRoot "SHA256SUMS.txt") -Value $HashLines -Encoding ASCII

Write-Host "[5/5] Release build complete: $DeliveryRoot"
Get-ChildItem -LiteralPath $DeliveryRoot | Select-Object Name, Length
