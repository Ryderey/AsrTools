# ASRTools v{APP_VERSION} Windows 10 x64 手动验收清单

> 开发侧构建和验证环境为 Windows 11 x64。在买家交付前，请在干净 Windows 10 x64 上对同一哈希的文件完成本清单并回传结果。

- [ ] 核对 `SHA256SUMS.txt` 中便携 ZIP 的 SHA-256。
- [ ] 360 与 Microsoft Defender 均未查杀或隔离便携 ZIP 及其解压内容。
- [ ] 若出现 SmartScreen，可通过“更多信息 → 仍要运行”继续；不存在无法绕过的组织策略阻止。
- [ ] 解压后的根目录仅包含 `ASRTools.exe`、`README-Windows.txt`、`_runtime` 和 `docs`。
- [ ] 便携 ZIP 完整解压后可从根目录 `ASRTools.exe` 启动，窗口标题显示 `ASRTools v{APP_VERSION}`。
- [ ] 应用能添加音频和视频文件。
- [ ] 应用能完成视频转音频，不依赖系统安装的 FFmpeg。
- [ ] 仅用交付方提供的测试音频，应用能完成 B 接口识别并生成非空结果。
- [ ] SRT、TXT、ASS 输出均写入原媒体目录。
- [ ] 记录 Windows 版本、360/Defender 版本、SmartScreen 表现、测试时间和异常。

在此清单完成前，v{APP_VERSION} 只能声明“支持 Windows 10+ x64，已在 Windows 11 x64 开发环境验证；Windows 10 x64 待用户复验”。
