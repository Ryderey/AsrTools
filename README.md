
# ASRTools - 语音识别工具

基于原项目：https://github.com/WEIFENG2333/AsrTools 进行修改，使用更方便

## 📖 项目简介

ASRTools 是一款基于 PyQt5 开发的语音识别 GUI 工具。v1.1.0 正式支持 Windows 10 及以上 x64、macOS 11 及以上、Linux x64，提供 B 接口批量识别与 SRT、TXT、ASS 导出。各平台交付包均由各平台本机构建（Nuitka 不支持交叉编译），并附原生构建与冒烟测试证据。

## ✨ 主要优化点

1. **界面简化**：界面上仅保留 B 接口（BcutASR），操作更直观
2. **交互增强**：增加多种交互选项，提升用户体验
   - 支持批量音频文件处理
   - 支持多种导出格式（SRT、TXT、ASS）
   - 支持临时音频文件自动清理
   
3. **功能完善**：
   - 支持任务取消和强制终止
   - 支持中文路径兼容
   - 将ffmpeg打包进程序
   - 去掉任务弹出的终端窗口

## 📸 界面截图

![ASRTools 界面截图](README.assets/image-20260415204159875.png)

## 🚀 使用方式

### 方式一：直接运行便携包（推荐）

**Windows**

1. 从交付方获取便携 ZIP，先按 `SHA256SUMS.txt` 核对哈希。
2. 完整解压后运行根目录的 `ASRTools.exe`；不要单独复制该 EXE。
3. Nuitka 运行时与 `ffmpeg.exe` 收纳在 `_runtime/`，应用不会调用系统 PATH 中的 FFmpeg。

**Linux**

1. 从交付方获取便携包（zip），先按 `SHA256SUMS.txt` 核对哈希（`sha256sum -c SHA256SUMS.txt`）。
2. 完整解压后在终端运行根目录的 `./ASRTools`；不要单独复制启动脚本。
3. Nuitka 运行时与随包 `ffmpeg` 收纳在 `_runtime/`，应用不会调用系统 PATH 中的 FFmpeg。

**macOS**

1. 从交付方获取便携包，先按 `SHA256SUMS.txt` 核对哈希（`shasum -a 256 -c SHA256SUMS.txt`）。
2. 完整解压后运行根目录的 `./ASRTools`（将打开 `_runtime` 内的 `ASRTools.app`）。
3. 未签名与公证，首次运行如被 Gatekeeper 拦截，请在“系统设置 > 隐私与安全性”中确认来源。

### 方式二：源码运行

#### 前置条件

- uv 管理的 CPython 3.12.13 x64（Windows）；macOS/Linux 使用 CPython 3.12 即可
- 经校验的 FFmpeg 8.1.2（Windows 为 `ffmpeg.exe`，macOS/Linux 为 `ffmpeg`，源码运行时放在项目根目录）

#### 安装步骤（Windows）

```bash
# 1. 克隆项目到本地
git clone https://github.com/Ryderey/AsrTools
cd AsrTools

# 2. 创建虚拟环境（推荐使用 uv）
# 如果没有 uv，请先安装：https://github.com/astral-sh/uv
uv venv --python 3.12.13

# 3. 激活虚拟环境（Windows）
# 使用 uv 可直接运行，或手动激活：
# .venv\Scripts\activate

# 4. 安装依赖
uv pip sync --python .venv\Scripts\python.exe --require-hashes requirements-release.lock

# 5. 运行程序
.venv\Scripts\python.exe asr_gui.py
```

#### 安装步骤（macOS / Linux）

```bash
git clone https://github.com/Ryderey/AsrTools
cd AsrTools

uv venv --python 3.12
uv pip sync --python .venv/bin/python --require-hashes requirements-release.lock

# 把经校验的 ffmpeg 放到项目根目录后运行：
.venv/bin/python asr_gui.py
```

Linux 需要桌面环境提供的 xcb 库（一般发行版默认已有）；源码运行时同样不使用系统 PATH 中的 FFmpeg。

## 📦 打包说明

使用 uv 锁定环境和 Nuitka standalone，从同一源码与 FFmpeg 输入生成规整的便携包。onefile 变体因安全软件查杀风险不再交付。**Nuitka 不支持交叉编译：Windows/macOS/Linux 三个平台的交付包必须在对应平台本机构建。**

### 前置准备

打包前请确保以下文件存在：

- `resources/app_icon.ico` - 应用图标（Windows）；macOS 构建会从 `resources/app_icon.png` 生成 `.icns`
- 对应平台的 FFmpeg 二进制，SHA-256 必须与 `app_runtime.FFMPEG_SHA256_BY_PLATFORM` 中的条目一致：
  - Windows：FFmpeg 8.1.2 x64 essentials `ffmpeg.exe`，SHA-256 为 `1326DDE4C84FF1F96FE6B8916C5BED29E163E9B5DCCF995F6F3DB069D143EC5E`
  - Linux：BtbN linux64 GPL 静态构建 `n8.1.2-44-g7c533d0f86`（release/8.1 分支：8.1.2 tag + 上游 bugfix 提交），SHA-256 为 `7E9CBECF3D568A411789EC73F6A28EABE4D37F6D2965B76CBD28AE98F018BA11`
  - 版本校验接受精确 `8.1.2` 或 8.1 分支的 `n8.1.2-*` git-describe 形式
  - 未固化哈希的平台首次构建会报错并打印实际哈希，核验后写入再重新构建
- Linux 构建另需 `patchelf`（Nuitka standalone 依赖），可用 `uv tool install patchelf` 安装

### 一键打包

**Windows**：项目提供可失败、可复现的发布脚本；FFmpeg 路径必须显式传入：

```bash
build.bat "D:\path\to\ffmpeg.exe"
```

**macOS / Linux**：使用 Python 版发布脚本（阶段与 Windows 链对齐）：

```bash
python3 scripts/build_release.py --ffmpeg-path /path/to/ffmpeg
```

打包完成后，Windows 应用包位于 `dist/ASRTools-Windows-x64-v1.1.0/ASRTools-Windows-x64-v1.1.0-portable.zip`；macOS/Linux 应用包位于 `dist/ASRTools-<平台>-v1.1.0/` 下对应的 `.zip`。

发布脚本会校验 FFmpeg 版本和哈希、同步 `requirements-release.lock`、构建 standalone 运行时和轻量根启动器，并组装许可证、源码包、构建清单和 `SHA256SUMS.txt`。缺少或错用 FFmpeg 时立即失败。

发布后可用 `python3 scripts/validate_release.py --ffmpeg-path /path/to/ffmpeg`（Windows 为 `validate_release.ps1`）对打包产物做布局校验与冒烟检查，产出 `VALIDATION-REPORT.md`。

### GitHub Actions 自动构建

`.github/workflows/build-release.yml` 提供 Windows / Linux 双平台自动打包：

- 触发方式：Actions 页面手动运行（workflow_dispatch）或推送 `v*` tag。
- 每个平台先跑单测，再下载各自的定版 FFmpeg（Windows：gyan.dev 8.1.2 essentials；Linux：BtbN `n8.1.2-44` 固化哈希构建），构建脚本校验哈希与版本，错配即失败。
- 产物上传为 Actions 工件（`ASRTools-Windows-x64-v*` / `ASRTools-Linux-x64-v*`）。
- macOS 暂未纳入：公开渠道无 FFmpeg 8.1.2 定版静态二进制，待获得定版源并固化哈希后启用。

便携包根目录固定为：

```text
ASRTools（Windows 为 ASRTools.exe）
README-<平台>.txt
_runtime/
docs/
```

## 📁 项目结构

```
AsrTools/
├── app_runtime.py             # 应用版本和 FFmpeg 运行时合同
├── asr_gui.py                 # 主程序入口
├── requirements-release.lock # uv 发布依赖锁
├── build.bat                  # 发布脚本入口
├── scripts/                   # 构建、许可证和验证脚本
├── release-assets/            # 交付说明模板
├── bk_asr/                    # ASR 引擎实现
├── resources/                 # 图标和授权测试音频
└── dist/                      # 最终交付输出目录
```

## ⚙️ 依赖说明

### Python 依赖

```
requests
PyQt5
PyQt-Fluent-Widgets
```

### 外部依赖

- **FFmpeg 8.1.2**：音频格式转换和处理的锁定组件
  - 下载地址：https://ffmpeg.org/download.html
  - 源码运行时放在项目根目录（Windows 为 `ffmpeg.exe`，macOS/Linux 为 `ffmpeg`）；发布构建通过参数显式传入
  - 应用不回退到系统 PATH

## 💡 使用提示

1. **支持的音频格式**：MP3、WAV、FLAC、M4A 等常见格式
2. **导出格式**：SRT（字幕文件）、TXT（纯文本）、ASS（字幕文件）
3. **批量处理**：可同时选择多个音频文件进行识别
4. **临时文件**：处理过程中会生成临时文件，可选择自动删除

## ⚠️ 注意事项

- 首次运行可能需要联网加载 ASR 服务
- 确保网络连接正常（BcutASR 需要访问 B 站接口）
- 大文件处理可能需要较长时间，请耐心等待
- 识别结果可能受音频质量和网络状况影响

## 📝 常见问题

### Q: 提示找不到 FFmpeg？
A: 请重新完整解压便携包，确认 `_runtime/` 内的 FFmpeg 二进制（Windows 为 `ffmpeg.exe`）未被安全软件隔离或移除。源码运行时把经校验的 FFmpeg 放在项目根目录。应用不会使用系统 PATH。关闭应用窗口时会自动终止仍在运行的随包 FFmpeg 进程；若应用被系统强制杀死（如 kill -9），可能有 FFmpeg 进程残留，需手动结束。

### Q: 打包后运行闪退？
A: 请先核对 `SHA256SUMS.txt`，确认安全软件未隔离文件，并完整解压便携包后从根目录启动入口运行。

### Q: 识别失败怎么办？
A: 请检查网络连接；当前版本只提供 B 接口并依赖 B 站相关服务。

## 📄 许可证

继承原项目的 LICENSE，详见 [LICENSE](LICENSE) 文件。

## 🙏 致谢

- 原项目：[WEIFENG2333/AsrTools](https://github.com/WEIFENG2333/AsrTools)
- ASR 引擎：Bilibili Cut（BcutASR）
- UI 框架：[PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)
- 打包工具：[Nuitka](https://nuitka.net/)

---

如有问题或建议，欢迎提交 [Issue](https://github.com/Ryderey/AsrTools/issues)
