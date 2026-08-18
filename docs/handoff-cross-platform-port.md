# 交接文档：ASRTools 跨平台化（Windows / macOS / Linux）

> 本文件由 2026-08-18 会话生成，供后续 Agent 直接执行，不必重新推导。
> 关联历史评估：`.trellis/tasks/07-13-cross-platform-assessment/`（仍为 planning 状态，可作为背景参考）。

## 1. 决策（已由项目方确认）

1. 目标范围：**完整发布链**——三平台源码可运行，且 macOS / Linux 达到与 Windows 对齐的发布交付能力（便携包 + 校验 + SHA256SUMS + VALIDATION-REPORT）。
2. 构建脚本组织：**Windows 现有 `build.bat` + `scripts/*.ps1` 链一字不动**（已验证、GPL 合规交付）；**新增跨平台 `scripts/build_release.py` 供 macOS / Linux 使用**。
3. 依赖锁**必须跨平台化**：现行 `requirements-release.lock` 只覆盖 Windows 图谱（无平台 markers、无 mac/linux wheel 哈希），mac/Linux 上的 `uv pip sync --require-hashes` 必然失败。
4. 不做：不改 Windows 发布链；本轮不新增 CI（Nuitka 构建重、FFmpeg 大件，待本地链路跑通后再议 GitHub Actions 矩阵）。
5. Nuitka 不能交叉编译：每个平台必须在自身系统上构建（写入 README 说明）。

## 2. 当前状态审计（执行前必读）

### 2.1 Windows 专属代码（运行时）

- `app_runtime.py`
  - 硬编码 `ffmpeg.exe`（L35）；macOS/Linux 二进制名为 `ffmpeg`。
  - `SUPPORTED_PLATFORM = "Windows 10+ x64"`（L10）。
  - `WINDOWS_FILE_VERSION`（L9）仅 Windows 契约使用。
  - `FFMPEG_SHA256`（L13）为 Windows exe 的校验值，需要分平台字典。
- `asr_gui.py`
  - L14–16：无条件把 `QT_QPA_PLATFORM_PLUGIN_PATH` 设为 Windows 风格 `sys.prefix/Lib/site-packages/PyQt5/Qt5/plugins`。在 POSIX 上会覆盖 PyQt5 正确的插件自发现，导致启动失败。**必须仅在 Windows 设置。**
  - L82–108 `_terminate_ffmpeg`：已有 Windows（taskkill）/ Darwin+Linux（`os.killpg`）分支，方向正确。
  - L1350–1429 `video2audio` 的 `Popen`：仅 Windows 传 `CREATE_NEW_PROCESS_GROUP` + 隐藏窗口 startupinfo；**POSIX 未传 `start_new_session=True`。后果：`_terminate_ffmpeg` 里 `os.getpgid(child_pid)` 得到的是 GUI 自身的进程组，取消任务时会把整个应用杀掉——POSIX 上是潜在致命 bug，必须修复。**
  - L618–623 打开所在目录：已有 `os.startfile` / `open` / `xdg-open` 三分支，无需改动。
  - 报错文案含 `ffmpeg.exe` / "安全软件未隔离"（L1428 等）：需平台中性化。
  - 字体 "Segoe UI"（L1322/1326 等）：Qt 会回退系统字体，非阻塞；可在 Windows 外用默认字体（可选小改）。
- `bk_asr/BaseASR.py`：缓存已用 `tempfile.gettempdir()`，跨平台，无改动。

### 2.2 构建链（仅 Windows，保持不动）

- `build.bat` → `scripts/build_release.ps1`：uv venv + Nuitka standalone + C# 便携启动器（`portable_launcher.cs`）+ 许可证收集（`scripts/collect_licenses.py`，纯 Python 跨平台已可复用）+ 打包 zip + `SHA256SUMS.txt` + `BUILD-MANIFEST.txt`。
- `scripts/validate_release.ps1`：布局校验 + 打包产物 `--release-check` 冒烟。
- `release-assets/`：README-Windows.template.txt、THIRD_PARTY_NOTICES.template.txt、SOURCE_CODE.template.txt、VALIDATION-REPORT.template.md、WINDOWS10-VALIDATION-CHECKLIST.template.md、portable_launcher.cs。
- 图标：`resources/app_icon.ico`（Windows）、`resources/app_icon.png`（可用于 mac/Linux）。

### 2.3 测试

- `tests/test_app_runtime.py`：硬编码 `ffmpeg.exe` 与 `WINDOWS_FILE_VERSION`，需平台化。
- `tests/test_release_helpers.py`：已用 `@unittest.skipUnless(os.name == "nt")` 正确跳过，不改。

### 2.4 依赖锁现状

- `requirements-release.lock` 由 `uv pip compile ... --python-version 3.12.13 --generate-hashes` 生成（无 `--universal`），全文件无平台 marker、wheel 哈希仅 Windows。mac/Linux `uv pip sync --require-hashes` 会找不到匹配分发。

## 3. 执行计划

### A. 运行时平台化（三平台源码可运行）

1. **`app_runtime.py`**
   - 新增 `FFMPEG_FILENAME`（Windows → `ffmpeg.exe`，其余 → `ffmpeg`），由 `platform.system()` 决定。
   - `FFMPEG_SHA256` 改为 `FFMPEG_SHA256_BY_PLATFORM` 字典：`{"Windows": "1326DDE4..."}`（保留现值）；macOS/Linux 首次构建由 `build_release.py` 打印实际哈希并要求固化到字典（保持源码确定性，符合 GPL 交付纪律），未固化则构建报错。
   - `SUPPORTED_PLATFORM` 按平台推导（如 `Windows 10+ x64` / `macOS 11+` / `Linux x64`）。
   - `WINDOWS_FILE_VERSION` 保留导出（PS1 契约依赖）。
   - `resolve_ffmpeg_path()`：按平台取二进制名；报错文案改平台中性（保留"不使用系统 PATH"策略）。
   - 同步更新 `tests/test_app_runtime.py` 断言。

2. **`asr_gui.py`**
   - L14–16 插件路径修复：外层包 `if platform.system() == "Windows":`。
   - `video2audio()` Popen：POSIX 分支传 `start_new_session=True`（使 `os.killpg` 取消安全）；Windows 保持 `CREATE_NEW_PROCESS_GROUP` + startupinfo。仅传平台相关 kwargs。
   - 报错文案平台中性化（`ffmpeg.exe` → `FFmpeg`；"安全软件未隔离"保留仅 Windows 提示或中性化措辞）。

### B. 跨平台依赖锁

3. 重新生成 `requirements-release.lock`：
   `uv pip compile requirements-release.in --universal --python-version 3.12.13 --generate-hashes --output-file requirements-release.lock`
   目的：纳入三平台 wheel 哈希。若个别包通用解析冲突 → 回退为三份分平台锁（`requirements-release-windows/macos/linux.lock`），并同步调整 `build_release.ps1` 与 `build_release.py` 的锁文件引用（PS1 改动仅限"锁文件名引用"这一个点，其余不动）。
   验证：`uv pip compile --universal` 无报错；用 Windows venv 跑既有测试仍绿。

### C. macOS / Linux 构建链（新增）

4. **`scripts/build_release.py`**（Python 3.12，跨平台，对齐 `build_release.ps1` 阶段）：
   - 入参 `--ffmpeg-path`（必填）：校验 `ffmpeg -version` 首行匹配 `ffmpeg version 8.1.2`，且 SHA256 与 `FFMPEG_SHA256_BY_PLATFORM` 对应条目一致；未固化时报错并打印实际哈希。
   - 环境：确保 `.venv` 存在（`uv venv --python 3.12.13`），`uv pip sync --python <venv> --require-hashes <lock>`。
   - Nuitka standalone：`--assume-yes-for-downloads --enable-plugin=pyqt5 --include-package=qfluentwidgets --include-package=bk_asr --lto=yes --output-dir=<build> --output-filename=ASRTools --report=<xml> asr_gui.py`。
     - macOS：用 `app_icon.png` 经 `sips`/`iconutil` 生成 `.icns`；产出 `ASRTools.app` 包装（`Contents/Info.plist`、`MacOS/ASRTools` 启动器、`Resources/app.icns`）。
     - Linux：直接产出可执行文件，打包时 `chmod +x`。
   - 组装便携包 `dist/ASRTools-<OS>-<arch>-v<ver>/`：`_runtime/`（Nuitka dist 内容 + `ffmpeg`）、根启动器（Linux 用 shell 启动脚本 `exec _runtime/ASRTools`）、`docs/`（LICENSE.txt、THIRD_PARTY_NOTICES.txt、SOURCE_CODE.txt、README-<OS>.txt、licenses/）。
   - 许可证：复用现有 `scripts/collect_licenses.py`；FFmpeg 许可材料（`LICENSE`、`README.txt`）约定与 PS1 相同（从 ffmpeg 发布包同目录取）。
   - 模板：新增 `release-assets/README-macOS.template.txt`、`release-assets/README-Linux.template.txt`；复用 THIRD_PARTY_NOTICES / SOURCE_CODE / VALIDATION-REPORT 模板（模板占位符约定沿用 PS1 的 `{APP_VERSION}` 等）。
   - 产出：`SHA256SUMS.txt`、`BUILD-MANIFEST.txt`、归档 `.zip` + `.tar.gz`。

5. **`scripts/validate_release.py`**（跨平台）：布局校验（对照 PS1 的校验字段）、用打包产物跑 `--release-check ffmpeg` 与 `--release-check convert` 冒烟、核对 SHA256SUMS 与许可证齐全，产出 `VALIDATION-REPORT.md`。`validate_release.ps1` 保持不动。

### D. 文档

6. `README.md`：新增 macOS / Linux 的源码运行步骤与便携包用法、各平台构建命令与"目标平台构建"说明。
7. `CONTEXT.md`：更新"正式支持平台"表述（保持其原有概念与证据要求：原生构建 + 冒烟证据）。
8. `.gitignore`：按需补充平台产物（`*.app` 等；`dist/`、`build/` 已在忽略列表）。

### E. 本机验证（在 Windows 上可完成的）

- 跑通 `tests/`（`python -m unittest discover tests` 或等价）；`test_app_runtime.py` 用分平台断言。
- `uv pip compile --universal` 无冲突。
- `app_runtime` 冒烟导入（`python -c "from app_runtime import ..."`）。
- macOS / Linux 实际构建必须在对应系统上执行（不能在本机验证，README/交付文档中明确）。

## 4. 验收标准

- [ ] 三平台源码可运行（Windows 沿用现有链回归；mac/Linux 需目标机验证）。
- [ ] `requirements-release.lock` 为 `--universal`，三平台 wheel 哈希齐全；Windows 测试仍绿。
- [ ] `scripts/build_release.py` 在 macOS 与 Linux 各产出：便携包 + `SHA256SUMS.txt` + `BUILD-MANIFEST.txt` + VALIDATION 报告。
- [ ] `scripts/validate_release.py`（或等价的 mac/Linux 校验）通过。
- [ ] Windows PS1 链在 mac/Linux 改造前后完全未变（git diff 为空）。
- [ ] README / CONTEXT.md / 错误文案不再声明"仅 Windows"。
- [ ] 未固化平台 FFmpeg SHA256 时构建明确报错（防静默替换未校验二进制）。

## 5. 关键引用

- 入口：`asr_gui.py`；运行时代理：`app_runtime.py`；发布契约：`app_runtime.py` 常量。
- Windows 构建参考实现：`scripts/build_release.ps1`（阶段与产物核对清单）、`scripts/validate_release.ps1`、`scripts/release_helpers.ps1`。
- 许可证收集：`scripts/collect_licenses.py`（已跨平台）。
- 模板占位符约定：`release-assets/*.template.*`。
- 锁定依赖：`requirements-release.in` / `requirements-release.lock`。