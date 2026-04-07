# AsrTools API

AsrTools API 提供简单易用的编程接口，用于批量处理音视频文件的语音识别和字幕生成。

## 环境要求

- **操作系统**: Ubuntu (或其他Linux发行版)
- **Python版本**: >= 3.8
- **包管理器**: [uv](https://github.com/astral-sh/uv) (推荐) 或 pip
- **系统依赖**: ffmpeg (用于视频转音频)

### 安装系统依赖 (Ubuntu)

```bash
# 安装ffmpeg
sudo apt update
sudo apt install -y ffmpeg

# 安装uv (如果尚未安装)
curl -LsSf https://astral.sh/uv/install.sh | sh
# 或者使用其他安装方法: https://github.com/astral-sh/uv#installation
```

## 安装AsrTools API

### 方法1: 使用 uv (推荐)

```bash
# 克隆项目 (如果尚未克隆)
git clone https://github.com/WEIFENG2333/AsrTools.git
cd AsrTools

# 创建虚拟环境并安装依赖
uv venv
uv pip install -e .

# 如果需要GUI功能 (可选)
uv pip install -e ".[gui]"
```

### 方法2: 使用 pip

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate

# 安装依赖
pip install -e .

# 如果需要GUI功能 (可选)
pip install -e ".[gui]"
```

## API 使用方法

### 1. 编程接口使用

#### 单文件处理

```python
from API.asr_api import process_file

# 处理单个文件
result = process_file("video.mp4", output_format="srt")
if result:
    print(f"字幕文件已生成: {result}")
```

#### 批量处理

```python
from API.asr_api import batch_process

# 批量处理多个文件
input_files = ["video1.mp4", "video2.mp4", "audio1.mp3"]
results = batch_process(input_files, output_format="srt", output_dir="./subtitles")

print(f"成功: {len(results['success'])}, 失败: {len(results['failed'])}")
```

#### 目录处理

```python
from API.asr_api import process_directory

# 处理整个目录
results = process_directory("./videos", output_format="txt", recursive=True)
```

#### 高级用法

```python
from API.asr_api import ASRAPI

# 自定义配置
api = ASRAPI(use_cache=True, max_workers=5)

# 处理文件
result = api.process_file("input.mp4", output_format="ass", output_path="custom_output.ass")

# 批量处理到指定目录
results = api.batch_process(
    ["file1.mp4", "file2.mp4"], 
    output_format="srt", 
    output_dir="./output"
)
```

### 2. 命令行使用

安装后可以直接使用命令行工具：

```bash
# 单文件处理
python -m API.asr_api video.mp4 -f srt -o ./output

# 目录处理 (递归)
python -m API.asr_api ./videos -f txt -o ./subtitles --recursive

# 禁用缓存，增加并发数
python -m API.asr_api input.mp4 --no-cache --workers 5
```

## 支持的格式

### 输入格式
- **视频**: mp4, avi, mkv, mov, wmv, flv, webm
- **音频**: mp3, wav, m4a, flac, aac

### 输出格式
- **srt**: 标准字幕格式 (带时间戳)
- **txt**: 纯文本格式 (无时间戳)
- **ass**: 高级字幕格式 (支持样式和布局)

## 配置选项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `use_cache` | `True` | 启用缓存避免重复处理相同文件 |
| `max_workers` | `3` | 最大并发线程数 |
| `output_dir` | `None` | 自定义输出目录 |

## 示例脚本

查看完整示例：

```bash
# 运行示例脚本
python API/examples.py
```

## 错误处理

API会自动处理以下情况：
- 文件不存在
- 不支持的文件格式
- 网络连接问题
- API调用失败

所有错误都会记录到日志，并返回适当的错误信息。

## 性能优化建议

1. **并发设置**: 根据网络带宽调整 `max_workers` (建议3-5)
2. **缓存启用**: 对于可能重复处理的文件，保持 `use_cache=True`
3. **批量处理**: 尽量使用批量处理而不是逐个处理
4. **输出目录**: 指定专门的输出目录避免文件混乱

## 注意事项

- 需要稳定的网络连接 (调用Bilibili必剪API)
- API有速率限制，请合理设置并发数
- 视频文件会自动转换为音频进行处理
- 缓存文件存储在系统临时目录，可安全删除

## 贡献和反馈

如遇到问题或有改进建议，请在GitHub上提交Issue。