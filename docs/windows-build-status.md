# Windows 构建盘点（2026-09-10）

## 2026-09-11 收包脚本修复

最新运行 `20260910-225951-365-103cbec0` 已成功编译 5,015 个 C 文件，
`20260910-225952-617/portable-compilation-report.xml` 为 `completion=yes`。
随后 FunASR 路径读取将单行字符串的 `[0]` 当成第一行，实际取到首字符，
调用 `.Trim()` 导致 `System.Char` 错误；后续 site-packages 查询存在相同问题。
两处均改为处理完整字符串。回归测试直接执行真实脚本的两个查询片段，
修复前两处失败，修复后通过。旧测试强制禁用 clcache 的断言已移除：
这只是历史诊断开关，最新构建已使用 clcache 完成编译，不能将其当作发布契约。
9 项发布相关测试、PowerShell 语法、UTF-8 BOM、差异空白检查通过，
78 个依赖兼容。重新计算编译指纹与旧产物完全一致。
续打包运行 `20260911-111852-557-81f187ef` 已复用旧 EXE，越过两处原报错，
完成许可证、模型收集及两个 ZIP。用户随后确认验证通过；本轮代理未启动应用。
后台任务最终仍为 failed：写构建清单时 Git 报 `missing config key GIT_CONFIG_KEY_0`，
随后发生 Null 方法调用错误。构建清单与 SHA256SUMS 收尾尚未完成，不能将
应用验证通过表述为整条发布流水线成功；此项未纳入本次字符串修复。

## 当前重建失败记录

9 月 9 日的 ZIP 是旧产物，不包含 9 月 10 日的 FunASR 注册器兼容修补，
不能作为本次修复后的交付包。

| 后台运行目录（`build/background/` 下） | 已确认的结果 |
|---|---|
| `20260910-172430-943-6fd55eec` | 写用户目录中的 Nuitka module-cache 时 PermissionError |
| `20260910-173648-715-e232041e` | SCons C 编译失败，日志未给出具体编译诊断；根因尚未确定 |
| `20260910-183645-675-55cf91da` | 项目缓存缺少 Dependency Walker，非交互下载默认拒绝，18:51 退出 |

最后一次失败由缓存目录迁移后未携带已下载工具引起。发布脚本现在在编译前执行
`release_payload.py build-tools`：优先使用项目中的工具，缺失则复制用户已有的
`depends.exe` 与 `depends.dll`；两处都不完整时立即报所需路径，不联网下载。
禁用 clcache 仍只是前一次 SCons 失败的排查措施，不能据此认定其根因。

本次验证：相同 Nuitka resolver 在修补前立即复现缺工具错误，修补后解析成功；
`build/toolchain-probe/` 中的 sqlite3 最小 standalone 已完成编译、链接并运行，
使用与正式构建相同的项目缓存、MSVC、LTO 和禁用 clcache 参数。
63 项单元测试全部通过，Windows PowerShell 语法及 UTF-8 BOM 检查通过。
这些结果不等同于完整 FunASR 应用构建成功，也不验证 GUI/离线推理。

19:45 已启动后台重建 `20260910-194550-063-bbd3ff64`（PID 11556），
已通过工具预检并进入编译；最终结果以该目录中的 `status.json` 为准。

## 历史成功包（2026-09-09）

本次以磁盘产物及 Nuitka 报告为依据，未启动成品应用。既有构建目录：
`build/release-v1.2.0/20260909-112201-752`。

| 环节 | 当前状态 | 后续全量构建的工作 |
|---|---|---|
| 应用与 GUI：asr_gui、bk_asr、Qt | 已完成 | 编译应用及 GUI 依赖 |
| 离线识别：sherpa-onnx、SenseVoice、Silero | 已完成 | 收集原生库，复制模型 |
| 对齐：FunASR、PyTorch、Numba、SciPy 等 | 已完成 | 编译 Python 模块并收集 CPU DLL |
| 原生编译与链接 | 5,013 个 C 文件对应目标文件已存在；运行 EXE 已生成 | MSVC 编译、链接；检查报告 completion=yes |
| 启动器与运行库 | 已完成 | 编译 C# 启动器、收集 FFmpeg 与依赖 |
| 模型权重 | 已收包 | 仅校验和复制 9 个模型/配置文件，无需编译或下载 |
| 文档、许可证、便携 ZIP、源码 ZIP、SHA-256 | 已生成 | 收集、压缩、写清单和校验文件 |
| 解压后 GUI / 转录验证 | 用户验证，未执行 | 不在构建脚本中运行 |

已有便携包大小为 734,883,644 字节，位于：
`dist/ASRTools-Windows-x64-v1.2.0/20260909-112201-752/ASRTools-Windows-x64-v1.2.0-portable.zip`。

上述成功状态仅对应 9 月 9 日的旧输入；FunASR 修补需要重新编译应用。
`build.bat` 通过 `scripts/start_build.ps1`
独立启动完整流水线，并将输出、错误、状态保存到 `build/background/`。
操作说明见 [Windows 一键交付](windows-delivery.md)。
