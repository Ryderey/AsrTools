#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AsrTools API Interface
提供简单易用的语音识别编程接口

支持的功能：
- 单文件语音识别
- 批量文件处理
- 多种输出格式（SRT/TXT/ASS）
- 缓存机制避免重复处理
- 并发处理提高效率
"""

import os
import sys
import logging
from pathlib import Path
from typing import Dict, List, Optional
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from bk_asr.BcutASR import (
    BcutASR,
    BcutPollingTimeoutError,
    BcutRateLimitedError,
)


MIN_WORKERS = 1
MAX_WORKERS = 3


class ASRAPI:
    """AsrTools语音识别API主类"""
    
    def __init__(self, use_cache: bool = True, max_workers: int = 3):
        """
        初始化ASR API
        
        Args:
            use_cache (bool): 是否启用缓存，默认True
            max_workers (int): 并发线程数，范围1-3，默认3
        """
        if (
            not isinstance(max_workers, int)
            or not MIN_WORKERS <= max_workers <= MAX_WORKERS
        ):
            raise ValueError("max_workers 必须在 1 到 3 之间")
        self.use_cache = use_cache
        self.max_workers = max_workers
        self._setup_logging()
    
    def _setup_logging(self):
        """设置日志配置"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def process_file(
        self, 
        input_path: str, 
        output_format: str = 'srt',
        output_path: Optional[str] = None,
        _propagate_pause: bool = False,
    ) -> Optional[str]:
        """
        处理单个音视频文件
        
        Args:
            input_path (str): 输入文件路径（支持视频和音频）
            output_format (str): 输出格式 ('srt', 'txt', 'ass')
            output_path (str, optional): 自定义输出路径
            
        Returns:
            str: 输出文件路径，失败返回None
        """
        try:
            # 验证输入文件
            if not os.path.exists(input_path):
                self.logger.error(f"输入文件不存在: {input_path}")
                return None
            
            # 创建ASR实例
            asr = BcutASR(input_path, use_cache=self.use_cache)
            
            # 执行语音识别
            self.logger.info(f"开始处理: {input_path}")
            result = asr.run()
            
            # 确定输出路径
            if output_path is None:
                output_path = f"{os.path.splitext(input_path)[0]}.{output_format}"
            
            # 保存结果
            if output_format == 'srt':
                result.to_srt(output_path)
            elif output_format == 'txt':
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(result.to_txt())
            elif output_format == 'ass':
                result.to_ass(save_path=output_path)
            else:
                raise ValueError(f"不支持的输出格式: {output_format}")
            
            self.logger.info(f"处理完成: {output_path}")
            return output_path
            
        except (BcutRateLimitedError, BcutPollingTimeoutError) as exc:
            if _propagate_pause:
                raise
            self.logger.error(f"处理失败 {input_path}: {str(exc)}")
            return None
        except Exception as e:
            self.logger.error(f"处理失败 {input_path}: {str(e)}")
            return None
    
    def batch_process(
        self,
        input_paths: List[str],
        output_format: str = 'srt',
        output_dir: Optional[str] = None
    ) -> dict:
        """
        批量处理多个文件
        
        Args:
            input_paths (List[str]): 输入文件路径列表
            output_format (str): 输出格式
            output_dir (str, optional): 输出目录
            
        Returns:
            dict: 处理结果统计 {'success': [...], 'failed': [...], 'paused': [...]}
        """
        if not input_paths:
            self.logger.warning("没有输入文件")
            return {'success': [], 'failed': [], 'paused': []}
        
        self.logger.info(f"开始批量处理 {len(input_paths)} 个文件")
        
        outcomes: Dict[int, tuple[str, str]] = {}
        next_index = 0
        paused = False
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures: Dict[Future, int] = {}

            def submit_available():
                nonlocal next_index
                while not paused and len(futures) < self.max_workers and next_index < len(input_paths):
                    index = next_index
                    next_index += 1
                    future = executor.submit(
                        self._process_single_with_output,
                        input_paths[index],
                        output_format,
                        output_dir,
                    )
                    futures[future] = index

            submit_available()
            while futures:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = futures.pop(future)
                    input_path = input_paths[index]
                    try:
                        output_path = future.result()
                    except (BcutRateLimitedError, BcutPollingTimeoutError) as exc:
                        paused = True
                        outcomes[index] = ("paused", input_path)
                        self.logger.warning("批量处理已暂停：%s", exc)
                    except Exception as exc:
                        self.logger.error("处理异常 %s: %s", input_path, exc)
                        outcomes[index] = ("failed", input_path)
                    else:
                        outcomes[index] = (
                            ("success", output_path)
                            if output_path
                            else ("failed", input_path)
                        )

                if paused:
                    for future, index in list(futures.items()):
                        if future.cancel():
                            outcomes[index] = ("paused", input_paths[index])
                            futures.pop(future)
                else:
                    submit_available()

        if paused:
            for index in range(next_index, len(input_paths)):
                outcomes[index] = ("paused", input_paths[index])

        results: Dict[str, List[str]] = {
            'success': [],
            'failed': [],
            'paused': [],
        }
        for index in range(len(input_paths)):
            category, value = outcomes[index]
            results[category].append(value)
        
        self.logger.info(
            "批量处理完成 - 成功: %s, 失败: %s, 暂停: %s",
            len(results['success']),
            len(results['failed']),
            len(results['paused']),
        )
        return results
    
    def _process_single_with_output(
        self, 
        input_path: str, 
        output_format: str, 
        output_dir: Optional[str]
    ) -> Optional[str]:
        """内部方法：处理单个文件并处理输出目录"""
        try:
            if output_dir:
                # 确保输出目录存在
                os.makedirs(output_dir, exist_ok=True)
                filename = os.path.basename(input_path)
                output_path = os.path.join(output_dir, f"{os.path.splitext(filename)[0]}.{output_format}")
            else:
                output_path = None
            
            return self.process_file(
                input_path,
                output_format,
                output_path,
                _propagate_pause=True,
            )
            
        except (BcutRateLimitedError, BcutPollingTimeoutError):
            raise
        except Exception as e:
            self.logger.error(f"处理失败 {input_path}: {str(e)}")
            return None
    
    def process_directory(
        self,
        input_dir: str,
        output_format: str = 'srt',
        output_dir: Optional[str] = None,
        recursive: bool = False
    ) -> dict:
        """
        处理整个目录中的音视频文件
        
        Args:
            input_dir (str): 输入目录路径
            output_format (str): 输出格式
            output_dir (str, optional): 输出目录
            recursive (bool): 是否递归子目录
            
        Returns:
            dict: 处理结果统计
        """
        # 支持的文件扩展名
        video_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'}
        audio_extensions = {'.mp3', '.wav', '.m4a', '.flac', '.aac'}
        supported_extensions = video_extensions | audio_extensions
        
        # 获取所有支持的文件
        input_files = []
        input_path_obj = Path(input_dir)
        
        if recursive:
            pattern = "**/*"
        else:
            pattern = "*"
            
        for file_path in input_path_obj.glob(pattern):
            if file_path.is_file() and file_path.suffix.lower() in supported_extensions:
                input_files.append(str(file_path))
        
        if not input_files:
            self.logger.warning(f"在目录 {input_dir} 中未找到支持的音视频文件")
            return {'success': [], 'failed': [], 'paused': []}
        
        self.logger.info(f"找到 {len(input_files)} 个文件待处理")
        return self.batch_process(input_files, output_format, output_dir)


# 全局API实例（方便直接调用）
asr_api = ASRAPI()


def process_file(input_path: str, output_format: str = 'srt', **kwargs) -> Optional[str]:
    """
    简化版单文件处理函数
    
    Args:
        input_path (str): 输入文件路径
        output_format (str): 输出格式 ('srt', 'txt', 'ass')
        **kwargs: 其他参数传递给ASRAPI构造函数
        
    Returns:
        str: 输出文件路径，失败返回None
    """
    api = ASRAPI(**kwargs)
    return api.process_file(input_path, output_format)


def batch_process(
    input_paths: List[str],
    output_format: str = 'srt',
    output_dir: Optional[str] = None,
    **kwargs,
) -> dict:
    """
    简化版批量处理函数
    
    Args:
        input_paths (List[str]): 输入文件路径列表
        output_format (str): 输出格式
        output_dir (str, optional): 输出目录
        **kwargs: 其他参数传递给ASRAPI构造函数
        
    Returns:
        dict: 处理结果统计
    """
    api = ASRAPI(**kwargs)
    return api.batch_process(input_paths, output_format, output_dir)


def process_directory(
    input_dir: str,
    output_format: str = 'srt',
    output_dir: Optional[str] = None,
    recursive: bool = False,
    **kwargs,
) -> dict:
    """
    简化版目录处理函数
    
    Args:
        input_dir (str): 输入目录路径
        output_format (str): 输出格式
        output_dir (str, optional): 输出目录
        recursive (bool): 是否递归处理子目录
        **kwargs: 其他参数传递给ASRAPI构造函数
        
    Returns:
        dict: 处理结果统计
    """
    api = ASRAPI(**kwargs)
    return api.process_directory(input_dir, output_format, output_dir, recursive)


if __name__ == "__main__":
    # 命令行使用示例
    import argparse
    
    parser = argparse.ArgumentParser(description="AsrTools API - 语音识别工具")
    parser.add_argument("input", help="输入文件或目录路径")
    parser.add_argument("-f", "--format", default="srt", choices=["srt", "txt", "ass"], 
                       help="输出格式 (默认: srt)")
    parser.add_argument("-o", "--output", help="输出目录")
    parser.add_argument("-r", "--recursive", action="store_true", 
                       help="递归处理子目录")
    parser.add_argument("--no-cache", action="store_true", 
                       help="禁用缓存")
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(MIN_WORKERS, MAX_WORKERS + 1),
        default=3,
        help="并发线程数，范围 1-3 (默认: 3)",
    )
    
    args = parser.parse_args()
    
    # 创建API实例
    api = ASRAPI(
        use_cache=not args.no_cache,
        max_workers=args.workers
    )
    
    # 判断输入是文件还是目录
    if os.path.isfile(args.input):
        result = api.process_file(args.input, args.format, 
                                os.path.join(args.output, f"{os.path.splitext(os.path.basename(args.input))[0]}.{args.format}") if args.output else None)
        if result:
            print(f"处理成功: {result}")
        else:
            print("处理失败")
            sys.exit(1)
    elif os.path.isdir(args.input):
        results = api.process_directory(
            args.input, 
            args.format, 
            args.output, 
            args.recursive
        )
        print(
            f"处理完成 - 成功: {len(results['success'])}, "
            f"失败: {len(results['failed'])}, 暂停: {len(results['paused'])}"
        )
        if results['failed']:
            print("失败文件:", results['failed'])
        if results['paused']:
            print("暂停文件:", results['paused'])
        if results['failed'] or results['paused']:
            sys.exit(1)
    else:
        print(f"错误: {args.input} 不存在")
        sys.exit(1)
