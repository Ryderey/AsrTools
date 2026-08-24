#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AsrTools API 使用示例
"""

import os
from API.asr_api import ASRAPI, process_file, batch_process, process_directory


def example_single_file():
    """单文件处理示例"""
    print("=== 单文件处理示例 ===")
    
    # 方法1: 使用ASRAPI类
    api = ASRAPI(use_cache=True, max_workers=3)
    result = api.process_file("example_video.mp4", output_format="srt")
    if result:
        print(f"处理成功: {result}")
    
    # 方法2: 使用简化函数
    result2 = process_file("example_audio.mp3", output_format="txt", use_cache=True)
    if result2:
        print(f"处理成功: {result2}")


def example_batch_files():
    """批量文件处理示例"""
    print("\n=== 批量文件处理示例 ===")
    
    # 准备文件列表
    input_files = [
        "video1.mp4",
        "video2.avi", 
        "audio1.mp3",
        "audio2.wav"
    ]
    
    # 过滤存在的文件
    existing_files = [f for f in input_files if os.path.exists(f)]
    
    if existing_files:
        # 方法1: 使用ASRAPI类
        api = ASRAPI(use_cache=True, max_workers=3)
        results = api.batch_process(existing_files, output_format="srt", output_dir="./output")
        print(f"批量处理结果 - 成功: {len(results['success'])}, 失败: {len(results['failed'])}")
        
        # 方法2: 使用简化函数
        results2 = batch_process(existing_files, output_format="ass", use_cache=True, max_workers=2)
        print(f"简化函数结果 - 成功: {len(results2['success'])}, 失败: {len(results2['failed'])}")


def example_directory():
    """目录处理示例"""
    print("\n=== 目录处理示例 ===")
    
    input_dir = "./videos"  # 替换为你的视频目录
    
    if os.path.exists(input_dir):
        # 方法1: 使用ASRAPI类
        api = ASRAPI(use_cache=True, max_workers=3)
        results = api.process_directory(
            input_dir, 
            output_format="srt", 
            output_dir="./subtitles",
            recursive=True
        )
        print(f"目录处理结果 - 成功: {len(results['success'])}, 失败: {len(results['failed'])}")
        
        # 方法2: 使用简化函数
        results2 = process_directory(
            input_dir, 
            output_format="txt", 
            use_cache=True,
            max_workers=2
        )
        print(f"简化函数结果 - 成功: {len(results2['success'])}, 失败: {len(results2['failed'])}")


def example_custom_settings():
    """自定义设置示例"""
    print("\n=== 自定义设置示例 ===")
    
    # 创建自定义配置的API实例
    custom_api = ASRAPI(
        use_cache=False,      # 禁用缓存
        max_workers=3         # 最大并发数
    )
    
    if os.path.exists("test_video.mp4"):
        result = custom_api.process_file(
            "test_video.mp4", 
            output_format="ass",
            output_path="./custom_output.ass"
        )
        if result:
            print(f"自定义处理成功: {result}")


if __name__ == "__main__":
    print("AsrTools API 使用示例")
    print("=" * 50)
    
    # 运行各个示例
    example_single_file()
    example_batch_files() 
    example_directory()
    example_custom_settings()
    
    print("\n示例运行完成！")
    print("注意：请将示例中的文件路径替换为实际存在的文件路径")
