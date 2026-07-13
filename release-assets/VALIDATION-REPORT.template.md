# ASRTools v{APP_VERSION} 开发侧验证记录

- 目标平台：Windows 10+ x64
- 开发侧环境：Windows 11 x64
- Windows 10 x64：待用户对同一哈希产物手动复验
- FFmpeg：{FFMPEG_VERSION}
- FFmpeg SHA-256：`{FFMPEG_SHA256}`

构建后的自动验证结果由 `scripts/validate_release.ps1` 写入本文件，包括便携包根目录布局、启动器参数透传、FFmpeg 和转换检查。任何源码、依赖或 FFmpeg 输入变化都会使本记录和现有产物哈希失效。
