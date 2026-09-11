# Windows 一键打包与交付

在项目根目录双击 `build.bat`，脚本会在独立后台进程中构建包含离线模型的 Windows 完整便携 ZIP。
最终用户解压后双击 `ASRTools.exe`，无需安装 Python、FFmpeg 或另行下载模型。

本期仅构建 Windows x64。脚本不启动应用、不运行识别测试；解压后的使用验证由交付方手动完成。

## 打包机器准备

- Windows x64，安装 `uv` 和 CPython 3.12.13；首次编译还需要 Nuitka 可用的 C/C++ 编译器。
- 优先复用项目 `.venv-alignment`：脚本逐项核对 Python 版本及 `requirements-release.lock` 中的全部包版本。
  不修改该环境；缺少包或版本不一致时停止并显示错误。
- 如果没有上述环境，脚本创建 `.venv-release`，使用哈希锁、本机 uv 缓存以及
  `build/offline-wheels` 中手动下载的 CPU 安装文件进行离线安装。
  缓存不全时需要先准备依赖；脚本不会自动下载大型依赖或模型。
- 默认 FFmpeg：`build/ffmpeg-build-input/bin/ffmpeg.exe`。
  同时保留 `build/ffmpeg-build-input/LICENSE` 和 `README.txt`。
  版本和 SHA-256 必须与 `app_runtime.py` 一致。
- 模型放在项目的 `models` 目录，下载链接见 [模型说明](offline-models.md)。

```text
models/
  sensevoice-small-int8/
    model.int8.onnx
    tokens.txt
    LICENSE
    README.md
  silero-vad/
    silero_vad.onnx
  fa-zh/
    model.pt
    config.yaml
    configuration.json
    tokens.json
    seg_dict
    am.mvn
    README.md
```

模型文件按 `release-assets/offline-models.json` 的固定哈希检查；缺失或不匹配立即停止。
模型仍不提交 Git，但会自动收进最终 ZIP。模型样例音频和导出脚本不会收进便携包。

## 执行方式

双击根目录 `build.bat` 即可。窗口显示后台进程 ID 和日志目录后即可关闭，关闭终端或结束 AI 对话不会主动停止该独立进程。
显示 `Background build dispatched` 仅表示已启动，最终是否成功以状态文件为准。
也可以在项目目录的 PowerShell 运行：

```powershell
.\build.bat
```

自定义 FFmpeg 位置：

```powershell
.\build.bat "D:\ffmpeg\bin\ffmpeg.exe"
```

需要自定义模型目录、构建环境或并行编译数时，直接调用底层脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_build.ps1 -ModelDirectory "D:\ASRModels" -BuildPython ".\.venv-alignment\Scripts\python.exe" -Jobs 4
```

此处 `-ExecutionPolicy Bypass` 仅对本次 PowerShell 进程生效，不修改系统执行策略。
`-Jobs` 影响编译并行度，与应用内识别并发无关。编译时长取决于 CPU、磁盘和编译缓存。

如果编译已经成功、但后续收包尚未开始，可以使用同一份源码、依赖和模型恢复收包：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_build.ps1 -ResumeBuildRoot ".\build\release-v1.2.0\实际构建时间戳"
```

仅在原编译进程已结束，且目录内有运行程序和 `portable-compilation-report.xml` 时使用。
恢复期间不要修改应用源码或依赖；如果已有便携包暂存目录或交付文件，脚本会拒绝覆盖。
编译中断、源码变更或依赖变更时，重新运行 `build.bat` 完整构建。

## 查看后台结果

`build/background/latest.txt` 记录最近一次后台任务目录，其中包含：

- `stdout.log`、`stderr.log`：构建输出和错误信息。
- `status.json`：`starting` / `running` / `succeeded` / `failed`，以及进程 ID、退出码、错误和最终交付目录。
- `request.json`：本次构建参数；`result.json`：成功后生成的压缩包路径。

无需保持 AI 对话或实时读取日志。稍后手动打开状态文件即可。
后台任务使用文件锁防止重复编译，重复启动的第二个任务会记录失败，不会再编译一份。
电脑必须保持开机且不休眠；关机、强制终止进程等情况不会自动恢复，状态可能停留在 `running`。
这时先确认 `pid.txt` 中的进程已结束，再重新运行脚本。遗留的 `build.lock` 文件无需删除，进程结束即释放锁。
直接调用 `scripts/build_release.ps1` 仍是前台模式，用于人工排查；不要与后台构建同时运行。

## 输出位置与交付文件

版本号统一取自 `app_runtime.py`。每次构建使用新的时间戳目录，旧包不会被清空：

```text
dist/ASRTools-Windows-x64-v1.2.0/年月日-时分秒-毫秒/
  ASRTools-Windows-x64-v1.2.0-portable.zip
  ASRTools-v1.2.0-source.zip
  SHA256SUMS.txt
  BUILD-MANIFEST.txt
  README-Windows.txt
  LICENSE.txt
  THIRD_PARTY_NOTICES.txt
  SOURCE_CODE.txt
  licenses/
  WINDOWS10-VALIDATION-CHECKLIST.md
  VALIDATION-REPORT.md
```

- `*-portable.zip`：用户实际使用的完整包，包含运行库、FFmpeg 和全部离线模型。
- `*-source.zip`：对应源码包，按项目现有 GPLv3 源码交付安排一并提供。
- `SHA256SUMS.txt`：两个 ZIP 的 SHA-256，随交付文件提供。
- `BUILD-MANIFEST.txt`：版本、构建环境、Git 状态、FFmpeg 等信息，供交付追踪。
- `VALIDATION-REPORT.md`：明确记录运行验证未执行，留给人工验证。

便携 ZIP 解压后的根目录为：

```text
ASRTools.exe
README-Windows.txt
_runtime/
  ASRTools-runtime.exe
  ffmpeg.exe
  ...运行库与 DLL...
  models/
    sensevoice-small-int8/
    silero-vad/
    fa-zh/
docs/
  MODEL-CHECKSUMS.txt
  ...许可证、模型来源和授权文件...
```

使用 ZIP64 压缩以支持大文件。用户必须完整解压，不能仅复制启动器 EXE。
功能验证完成后发送这次构建对应的文件；不要把旧版本 ZIP 和新校验文件混用。

`_runtime/funasr/` 下的 `.py` 源码与 `_runtime/*.dist-info` 元数据是构建期有意收进包的：
Nuitka 把 funasr 编译进 EXE 后磁盘上没有源码，funasr 1.2.6 注册类时会调用
`inspect.getsourcelines()`（缺失则模块体在类定义处中断，导致类后的
`load_seg_dict` 等全局缺失）；运行期的 `importlib.metadata` 查询（离线任务读取
funasr/torch/sherpa-onnx 版本）也只解析磁盘上的 `*.dist-info`。这两类文件体积
合计约 10 MB，属正常交付内容，勿在收包时删除。

## 增量编译（改动门控）

构建开始时对“编译输入”（仓库源码、Nuitka 参数、Python/Nuitka 版本、安装的依赖版本、
venv 内 Python 源码内容）计算指纹。若与 `build\release-v<版本>\<时间戳>\compile-fingerprint.json`
记录的最新成功编译一致，`build.bat` 会跳过 Nuitka 编译阶段（[1/5]），直接复用它编译好的
运行库并收包，通常几分钟完成；任一编译相关输入有改动，则正常触发编译。
有改动需要重编时，Nuitka 的模块级 C 对象缓存（clcache，MSVC 内置）会把未改动模块的
`.obj` 按源内容命中，只有改动的模块真正重编，最后链接与收包必跑。
指纹只在编译成功后写入上述 sidecar（失败或中断的构建不会写）。强制重新编译的办法：
改动任一编译输入（如 `asr_gui.py`/`bk_asr`），或删除
`build\release-v<版本>\` 下最新匹配目录里的 `compile-fingerprint.json`。
`BUILD-MANIFEST.txt` 会记录本次是“rebuilt”还是“reused（指纹匹配）”。

## 常见问题

| 提示 | 处理方式 |
|---|---|
| 找不到 uv / Python | 在打包机器安装对应开发工具后重试 |
| Missing local model / checksum mismatch | 按模型说明补齐原文件，确认目录、文件名和版本 |
| Missing model notice / FFmpeg notice | 恢复下载包里的 LICENSE、README |
| Build dependency mismatch | 将构建环境恢复到锁文件版本，或指定另一套完整虚拟环境 |
| 离线缓存缺包 | 补齐提示中的依赖到本机缓存/`build/offline-wheels`，再运行；无需重新下载模型 |
| Nuitka 缺少编译工具 | 根据编译器提示准备 Windows C/C++ 工具链；本脚本不默认同意大型下载 |
| 编译或收集失败 | 根据终端最后的错误修复后重试；只有显示 Release build complete 的目录才表示流程完成 |

脚本不会提交、推送或合并 Git 分支，也不会自动发布到 GitHub。
