[CmdletBinding()]
param(
    [string]$FFmpegPath,

    [string]$ModelDirectory,

    [string]$BuildPython,

    [string]$ResumeBuildRoot,

    [string]$ResultPath,

    [ValidateRange(1, 64)]
    [int]$Jobs = [Math]::Min(4, [Math]::Max(1, [Environment]::ProcessorCount - 1))
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$VenvPython = Join-Path $RepoRoot ".venv-release\Scripts\python.exe"
$ReuseEnvironment = $false
if (-not $BuildPython -and (Test-Path -LiteralPath (Join-Path $RepoRoot ".venv-alignment\Scripts\python.exe"))) {
    $BuildPython = Join-Path $RepoRoot ".venv-alignment\Scripts\python.exe"
}
if ($BuildPython) {
    $VenvPython = (Resolve-Path -LiteralPath $BuildPython).Path
    $ReuseEnvironment = $true
}
Set-Location -LiteralPath $RepoRoot
$env:PYTHONIOENCODING = "utf-8"
$env:UV_CACHE_DIR = Join-Path $RepoRoot "build\uv-cache"
$env:NUITKA_CACHE_DIR = Join-Path $RepoRoot "build\nuitka-cache"
if (-not $FFmpegPath) { $FFmpegPath = Join-Path $RepoRoot "build\ffmpeg-build-input\bin\ffmpeg.exe" }
if (-not $ModelDirectory) { $ModelDirectory = Join-Path $RepoRoot "models" }
$PayloadScript = Join-Path $PSScriptRoot "release_payload.py"
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
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowExistingEmpty
    )

    $FullPath = [IO.Path]::GetFullPath($Path)
    $RepoPrefix = $RepoRoot.TrimEnd('\') + '\'
    if (-not $FullPath.StartsWith($RepoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to reset a directory outside the repository: $FullPath"
    }
    if (Test-Path -LiteralPath $FullPath) {
        $ExistingItem = Get-Item -LiteralPath $FullPath
        if ($AllowExistingEmpty -and $ExistingItem.PSIsContainer -and -not (Get-ChildItem -LiteralPath $FullPath -Force | Select-Object -First 1)) {
            return
        }
        throw "Build output already exists; refusing to overwrite: $FullPath"
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
        "venv", "--offline", "--python", $PythonVersion, (Join-Path $RepoRoot ".venv-release")
    )
}
Invoke-Checked -FilePath $VenvPython -Arguments @($PayloadScript, "models", "--source", $ModelDirectory)

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
$ResumingBuild = -not [string]::IsNullOrWhiteSpace($ResumeBuildRoot)
if ($ResumingBuild) {
    if (-not (Test-Path -LiteralPath $ResumeBuildRoot -PathType Container)) {
        throw "Resume build directory does not exist: $ResumeBuildRoot"
    }
    $BuildRoot = (Resolve-Path -LiteralPath $ResumeBuildRoot).Path
    $ExpectedBuildParent = [IO.Path]::GetFullPath((Join-Path $RepoRoot "build\release-v$AppVersion"))
    if (-not (Split-Path -Parent $BuildRoot).Equals($ExpectedBuildParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Resume build directory must be under: $ExpectedBuildParent"
    }
    $BuildId = Split-Path -Leaf $BuildRoot
    if ($BuildId -notmatch '^\d{8}-\d{6}-\d{3}$') {
        throw "Resume build directory must use a release build identifier: $BuildRoot"
    }
    $PortableBuild = Join-Path $BuildRoot "portable"
    if (-not (Test-Path -LiteralPath $PortableBuild -PathType Container)) {
        throw "Resume build has no portable compilation directory: $PortableBuild"
    }
}
else {
    $BuildId = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $BuildRoot = Join-Path $RepoRoot "build\release-v$AppVersion\$BuildId"
    $PortableBuild = Join-Path $BuildRoot "portable"
}
$DeliveryRoot = Join-Path $RepoRoot "dist\ASRTools-Windows-x64-v$AppVersion\$BuildId"
foreach ($Document in @("LICENSE", "README.txt")) {
    $DocumentPath = Join-Path (Split-Path -Parent (Split-Path -Parent $FFmpegPath)) $Document
    if (-not (Test-Path -LiteralPath $DocumentPath -PathType Leaf)) { throw "Missing FFmpeg notice: $DocumentPath" }
}
$LocalLock = Join-Path $BuildRoot "requirements-local.lock"
if ($ResumingBuild) {
    if (-not (Test-Path -LiteralPath $LocalLock -PathType Leaf)) {
        throw "Resume build is missing its dependency lock: $LocalLock"
    }
}
else {
    Reset-KnownDirectory -Path $BuildRoot
    Invoke-Checked -FilePath $VenvPython -Arguments @($PayloadScript, "local-lock", "--source", $LockFile, "--destination", $LocalLock)
    if (-not $ReuseEnvironment) {
        Invoke-Checked -FilePath $UvCommand.Source -Arguments @(
        "pip", "sync", "--python", $VenvPython, "--require-hashes",
        "--offline", "--find-links", (Join-Path $RepoRoot "build\offline-wheels"), $LocalLock
        )
    }
}
Invoke-Checked -FilePath $VenvPython -Arguments @($PayloadScript, "environment")

$CommonNuitkaArguments = @(
    "-m", "nuitka",
    "--enable-plugin=pyqt5",
    "--include-package=qfluentwidgets",
    "--include-package=bk_asr",
    "--include-package=funasr",
    "--include-package=sherpa_onnx",
    "--include-package=sentencepiece",
    "--include-package-data=funasr",
    "--include-package-data=jieba",
    "--include-distribution-metadata=funasr",
    "--include-distribution-metadata=torch",
    "--include-distribution-metadata=sherpa_onnx",
    "--module-parameter=torch-disable-jit=yes",
    "--module-parameter=numba-disable-jit=no",
    "--windows-console-mode=disable",
    "--windows-icon-from-ico=$IconPath",
    "--windows-product-name=ASRTools",
    "--windows-file-description=ASRTools Speech Recognition Tool",
    "--windows-company-name=ASRTools",
    "--file-version=$($Contract.windows_file_version)",
    "--product-version=$($Contract.windows_file_version)",
    "--lto=yes",
    "--jobs=$Jobs"
)

# Change-gated compilation: fingerprint everything the frozen executable is built
# from (repo sources, Nuitka arguments, Python/Nuitka versions, installed package
# versions, and the venv's Python sources). When it matches the newest previous
# successful compile, skip [1/5] entirely and reuse its .dist; packaging still
# runs. Anything compile-affecting yields a different fingerprint and rebuilds.
$ReusedCompileRoot = $null
$SkipCompilation = $false
if ($ResumingBuild) {
    # Resume pins the compile root; reuse its recorded fingerprint for the manifest.
    $ResumeSidecar = Join-Path $BuildRoot "compile-fingerprint.json"
    $CurrentFingerprint = ""
    if (Test-Path -LiteralPath $ResumeSidecar -PathType Leaf) {
        $ResumeFingerprint = (& $VenvPython -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['fingerprint'])" $ResumeSidecar)
        if ($LASTEXITCODE -eq 0) {
            $CurrentFingerprint = ([string]$ResumeFingerprint).Trim()
        }
    }
}
else {
    $CompileArgsFile = Join-Path $BuildRoot "compile-args.json"
    $CurrentFingerprintFile = Join-Path $BuildRoot "compile-fingerprint.current.json"
    [IO.File]::WriteAllText(
        $CompileArgsFile,
        (@{ appVersion = $AppVersion; args = $CommonNuitkaArguments } | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    $FingerprintResult = & $VenvPython $PayloadScript compile-fingerprint --args-file $CompileArgsFile --out $CurrentFingerprintFile
    if ($LASTEXITCODE -ne 0 -or -not $FingerprintResult) {
        throw "Unable to compute the compile-inputs fingerprint."
    }
    $CurrentFingerprint = [regex]::Match(([string[]]$FingerprintResult | Select-Object -Last 1), '([0-9a-f]{64})').Groups[1].Value

    $ReleaseRoot = Join-Path $RepoRoot "build\release-v$AppVersion"
    $Candidates = @(Get-ChildItem -LiteralPath $ReleaseRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^\d{8}-\d{6}-\d{3}$' -and
            (Test-Path -LiteralPath (Join-Path $_.FullName "compile-fingerprint.json") -PathType Leaf)
        } | Sort-Object LastWriteTimeUtc -Descending)
    foreach ($Candidate in $Candidates) {
        $Sidecar = Join-Path $Candidate.FullName "compile-fingerprint.json"
        $CandidateFingerprint = (& $VenvPython -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['fingerprint'])" $Sidecar)
        if ($LASTEXITCODE -ne 0) { continue }
        $CandidateDist = Get-ChildItem -LiteralPath (Join-Path $Candidate.FullName "portable") -Directory -Filter "*.dist" -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "ASRTools-runtime.exe") -PathType Leaf } | Select-Object -First 1
        $CandidateReport = Join-Path $Candidate.FullName "portable-compilation-report.xml"
        $HasCompleteReport = (Test-Path -LiteralPath $CandidateReport -PathType Leaf) -and
            (Select-String -LiteralPath $CandidateReport -Pattern 'completion="yes"' -Quiet)
        if (-not $CandidateDist -or -not $HasCompleteReport) { continue }
        if ($CandidateFingerprint -eq $CurrentFingerprint) {
            $ReusedCompileRoot = $Candidate.FullName
            break
        }
    }

    if ($ReusedCompileRoot) {
        $PortableBuild = Join-Path $ReusedCompileRoot "portable"
        $SkipCompilation = $true
    }
    else {
        Invoke-Checked -FilePath $VenvPython -Arguments @($PayloadScript, "build-tools")
    }
}
Reset-KnownDirectory -Path $DeliveryRoot -AllowExistingEmpty:$ResumingBuild
if (-not $ResumingBuild -and -not $SkipCompilation) {
    New-Item -ItemType Directory -Path $PortableBuild -Force | Out-Null
}

if ($ResumingBuild) {
    Write-Host "[1/5] Reusing completed portable standalone runtime..."
}
elseif ($SkipCompilation) {
    Write-Host "[1/5] Skipping Nuitka compilation (inputs unchanged), reusing: $ReusedCompileRoot"
}
else {
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
}

$PortableDist = @(Get-ChildItem -LiteralPath $PortableBuild -Directory -Filter "*.dist")
if ($PortableDist.Count -ne 1) {
    throw "Expected exactly one Nuitka standalone .dist directory, found $($PortableDist.Count)."
}
$CompiledRuntime = Join-Path $PortableDist[0].FullName "ASRTools-runtime.exe"
if (-not (Test-Path -LiteralPath $CompiledRuntime -PathType Leaf)) {
    throw "Compilation is incomplete; runtime executable is missing: $CompiledRuntime"
}
$CompileRoot = Split-Path -Parent $PortableBuild
if (-not (Test-Path -LiteralPath (Join-Path $CompileRoot 'portable-compilation-report.xml') -PathType Leaf)) {
    throw "Compilation is incomplete; Nuitka completion report is missing."
}
$CompilationReport = [xml](Get-Content -LiteralPath (Join-Path $CompileRoot 'portable-compilation-report.xml') -Raw -Encoding UTF8)
if ($CompilationReport.DocumentElement.GetAttribute('completion') -ne 'yes') {
    throw "Compilation report does not indicate successful completion; refusing to package."
}
# Persist the fingerprint that produced this .dist so later builds can skip
# recompilation while inputs are unchanged. Skip/reuse runs leave the sidecar
# in the compile root they reused.
if (-not $ResumingBuild -and -not $SkipCompilation) {
    Copy-Item -LiteralPath $CurrentFingerprintFile -Destination (Join-Path $CompileRoot 'compile-fingerprint.json') -Force
}

# Nuitka compiles funasr into the executable, leaving no module sources on disk.
# funasr 1.2.6's register decorator runs inspect.getsourcelines() for every
# @tables.register class; without readable sources it aborts the module body at
# the class statement, so module-level helpers defined after the class (e.g.
# char_tokenizer.load_seg_dict) never exist at runtime. Ship the package tree
# so inspect/linecache resolve sources; imports still come from the executable.
Write-Host "Bundling funasr module sources for runtime inspect support..."
$FunasrRootOutput = & $VenvPython -c "import funasr, os; print(os.path.dirname(funasr.__file__))"
if ($LASTEXITCODE -ne 0 -or -not $FunasrRootOutput) {
    throw "Unable to locate the bundled funasr package in the build environment."
}
$FunasrRoot = [IO.Path]::GetFullPath(([string]$FunasrRootOutput).Trim())
if (-not (Test-Path -LiteralPath $FunasrRoot -PathType Container)) {
    throw "funasr package directory is missing: $FunasrRoot"
}
$FunasrTarget = Join-Path $PortableDist[0].FullName "funasr"
New-Item -ItemType Directory -Path $FunasrTarget -Force | Out-Null
Get-ChildItem -LiteralPath $FunasrRoot -Recurse -File |
    Where-Object { $_.FullName -notmatch "__pycache__" } |
    ForEach-Object {
        $Relative = $_.FullName.Substring($FunasrRoot.Length).TrimStart('\')
        $Destination = Join-Path $FunasrTarget $Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
    }

# The frozen runtime's importlib.metadata lookups (the offline identity check
# reads funasr/torch/sherpa-onnx versions) only resolve distributions whose
# metadata Nuitka embedded; sherpa-onnx was not found at runtime. Ship the
# venv's *.dist-info folders so version lookups resolve off the disk.
Write-Host "Bundling distribution metadata for runtime importlib.metadata..."
$SitePackagesOutput = & $VenvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
if ($LASTEXITCODE -ne 0 -or -not $SitePackagesOutput) {
    throw "Unable to locate the build environment site-packages directory."
}
$SitePackages = [IO.Path]::GetFullPath(([string]$SitePackagesOutput).Trim())
if (-not (Test-Path -LiteralPath $SitePackages -PathType Container)) {
    throw "Build environment site-packages directory is missing: $SitePackages"
}
Get-ChildItem -LiteralPath $SitePackages -Directory -Filter "*.dist-info" |
    Copy-Item -Destination $PortableDist[0].FullName -Recurse -Force

$PortableFolderName = "ASRTools-Windows-x64-v$AppVersion-portable"
$PortableStage = Join-Path $BuildRoot $PortableFolderName
$PortableRuntime = Join-Path $PortableStage "_runtime"
$PortableDocs = Join-Path $PortableStage "docs"
if (Test-Path -LiteralPath $PortableStage) {
    throw "Portable staging directory already exists; refusing to overwrite: $PortableStage"
}
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
Invoke-Checked -FilePath $VenvPython -Arguments @(
    $PayloadScript, "models", "--source", $ModelDirectory, "--destination", $PortableStage
)

Write-Host "[3/5] Creating portable and corresponding-source ZIP files..."
$PortableZip = Join-Path $DeliveryRoot "$PortableFolderName.zip"
Invoke-Checked -FilePath $VenvPython -Arguments @($PayloadScript, "zip", "--source", $PortableStage, "--destination", $PortableZip)

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
    "requirements-offline.txt",
    "requirements-alignment-probe.lock",
    "docs",
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
Invoke-Checked -FilePath $VenvPython -Arguments @($PayloadScript, "zip", "--source", $SourceStage, "--destination", $SourceZip, "--include-root")

Write-Host "[4/5] Writing build manifest and artifact hashes..."
$GitCommit = (& git -c "safe.directory=$($RepoRoot.Replace('\', '/'))" -C $RepoRoot rev-parse HEAD).Trim()
$GitStatus = (& git -c "safe.directory=$($RepoRoot.Replace('\', '/'))" -C $RepoRoot status --short) -join [Environment]::NewLine
$PythonIdentity = (& $VenvPython -c "import sys, platform; print(sys.version.replace(chr(10), ' ')); print(platform.architecture()[0])") -join " / "
$NuitkaVersion = (& $VenvPython -m nuitka --version | Select-Object -First 1)
$DependencyFreeze = (& $UvCommand.Source pip freeze --python $VenvPython) -join [Environment]::NewLine
if ($ResumingBuild) {
    $CompileSourceLabel = "reused (resumed build): $BuildRoot"
}
elseif ($SkipCompilation) {
    $CompileSourceLabel = "reused (compile-inputs fingerprint matched): $ReusedCompileRoot"
}
else {
    $CompileSourceLabel = "rebuilt for this delivery"
}
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
Build environment: $VenvPython (reused existing environment: $ReuseEnvironment)
Build collection resumed: $ResumingBuild
Compilation source: $CompileSourceLabel
Compile inputs fingerprint: $CurrentFingerprint
Nuitka: $NuitkaVersion
FFmpeg input: $FFmpegPath
FFmpeg version: $FFmpegVersionLine
FFmpeg SHA-256: $ActualFFmpegHash
Models: bundled in _runtime/models; docs/MODEL-CHECKSUMS.txt records inputs
funasr sources: bundled in _runtime/funasr; required for inspect/linecache in the frozen funasr 1.2.6 environment
distribution metadata: bundled as *.dist-info under _runtime for importlib.metadata lookups (funasr/torch/sherpa-onnx)
Application execution tests: not run; unpacked application verification is user-owned
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

if ($ResultPath) {
    @{ deliveryDirectory = $DeliveryRoot; portableZip = $PortableZip; sourceZip = $SourceZip } |
        ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding UTF8
}

Write-Host "[5/5] Release build complete: $DeliveryRoot"
Write-Host "Application was not launched. Unpack the portable ZIP and run ASRTools.exe to verify."
Get-ChildItem -LiteralPath $DeliveryRoot | Select-Object Name, Length
