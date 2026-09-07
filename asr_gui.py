import logging
import os
from pathlib import Path
import argparse
import json
import platform
import subprocess
import sys
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar, Dict
from bk_asr.offline_alignment import prepare_native_runtime

# On Windows the installed Qt/sentencepiece combination is load-order sensitive.
# Only preload the small extension, not torch/FunASR or model weights on the UI thread.
prepare_native_runtime()

# FIX: 修复中文路径报错 https://github.com/WEIFENG2333/AsrTools/issues/18  设置QT_QPA_PLATFORM_PLUGIN_PATH
if platform.system() == "Windows":
    plugin_path = os.path.join(sys.prefix, 'Lib', 'site-packages', 'PyQt5', 'Qt5', 'plugins')
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = plugin_path

from PyQt5.QtCore import Qt, QRunnable, QThreadPool, QObject, QTimer, pyqtSignal as Signal, pyqtSlot as Slot, QSettings
from PyQt5.QtGui import QCursor, QColor, QFont
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFileDialog,
                             QTableWidgetItem, QHeaderView, QSizePolicy, QCheckBox, QSpinBox)
from qfluentwidgets import (ComboBox, PushButton, LineEdit, TableWidget, FluentIcon as FIF,
                            Action, RoundMenu, InfoBar, InfoBarPosition,
                            FluentWindow, BodyLabel, MessageBox)

from app_runtime import APP_VERSION, FFmpegUnavailableError, resolve_ffmpeg_path
from bk_asr.BcutASR import BcutASR, BcutPollingTimeoutError, BcutRateLimitedError
from bk_asr.OfflineASR import OfflineASR, OFFLINE_ENGINE, ONLINE_ENGINE, atomic_text

MIN_THREAD_COUNT = 1
MAX_THREAD_COUNT = 3


def clamp_thread_count(value):
    return max(MIN_THREAD_COUNT, min(MAX_THREAD_COUNT, int(value)))

# 设置日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


class WorkerSignals(QObject):
    finished = Signal(str, str)
    errno = Signal(str, str, str, float)
    cancelled = Signal(str)  # 新增：任务被取消信号
    progress = Signal(str, str, float, float)




class ASRWorker(QRunnable):
    """ASR处理工作线程（支持强制终止和临时文件管理）"""
    _workers: ClassVar[Dict[str, "ASRWorker"]] = {}  # 跟踪所有活动的worker
    _lock = threading.Lock()
    
    def __init__(self, file_path, asr_engine, export_format, delete_temp_audio=False, offline_engine=None):
        super().__init__()
        self.file_path = file_path
        self.asr_engine = asr_engine
        self.offline_engine = offline_engine
        self.export_format = export_format
        self.delete_temp_audio = delete_temp_audio  # 是否自动删除临时音频文件
        self.signals = WorkerSignals()
        
        self.audio_path = None
        self.is_temp_audio = False  # 标记是否为临时音频文件
        self._is_cancelled = False
        self._cancel_lock = threading.Lock()
        self._ffmpeg_process = None
        self._offline_stage = None
        
        # 注册到活动worker字典
        with ASRWorker._lock:
            ASRWorker._workers[file_path] = self
    
    def is_cancelled(self):
        """检查是否被取消"""
        with self._cancel_lock:
            return self._is_cancelled
    
    def cancel(self):
        """取消任务"""
        with self._cancel_lock:
            self._is_cancelled = True
        
        # 终止ffmpeg进程
        self._terminate_ffmpeg()
        
        logging.info(f"任务已取消: {self.file_path}")
    
    def _terminate_ffmpeg(self):
        """终止ffmpeg进程"""
        if self._ffmpeg_process and self._ffmpeg_process.poll() is None:
            try:
                # Windows使用TASKKILL强制终止进程树
                if platform.system() == "Windows":
                    # 配置Windows启动信息以隐藏命令行窗口
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = subprocess.SW_HIDE
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(self._ffmpeg_process.pid)],
                        capture_output=True,
                        check=False,
                        startupinfo=startupinfo
                    )
                else:
                    # Unix系统使用进程组终止
                    killpg = getattr(os, "killpg")
                    getpgid = getattr(os, "getpgid")
                    killpg(getpgid(self._ffmpeg_process.pid), signal.SIGTERM)
                    # 给进程一点时间优雅退出，然后强制终止
                    try:
                        self._ffmpeg_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        killpg(
                            getpgid(self._ffmpeg_process.pid),
                            getattr(signal, "SIGKILL"),
                        )
                logging.info(f"已终止ffmpeg进程: {self.file_path}")
            except Exception as e:
                logging.warning(f"终止ffmpeg进程时出错: {e}")
    
    def cleanup_temp_audio(self, force_delete=False):
        """清理临时音频文件的健壮实现
        
        Args:
            force_delete: 是否强制删除（用于用户手动取消/删除/重新处理时）
        
        Returns:
            bool: 是否成功删除
        """
        # 健壮性检查1: 确保是临时音频文件
        if not self.is_temp_audio or not self.audio_path:
            return False
        
        # 健壮性检查2: 检查文件是否存在
        if not os.path.exists(self.audio_path):
            return False
        
        # 健壮性检查3: 只有在用户启用自动删除或强制删除时才执行
        should_delete = force_delete or self.delete_temp_audio
        
        if should_delete:
            try:
                # 健壮性检查4: 确保文件不是正在使用状态
                with open(self.audio_path, 'rb') as f:
                    f.read(1)  # 尝试读取，如果文件被占用会抛出异常
                
                os.remove(self.audio_path)
                logging.info(f"临时音频文件已删除: {self.audio_path}")
                return True
                
            except (OSError, IOError, PermissionError) as e:
                logging.warning(f"无法删除临时文件 {self.audio_path}: {str(e)}")
                # 健壮性处理5: 如果删除失败，记录日志但不崩溃
                return False
        
        return False
    
    @staticmethod
    def get_worker(file_path):
        """获取指定文件的worker"""
        with ASRWorker._lock:
            return ASRWorker._workers.get(file_path)
    
    @staticmethod
    def remove_worker(file_path, worker=None):
        """从活动worker字典中移除"""
        with ASRWorker._lock:
            if worker is None or ASRWorker._workers.get(file_path) is worker:
                ASRWorker._workers.pop(file_path, None)
    
    @Slot()
    def run(self):
        try:
            # 检查是否已被取消
            if self.is_cancelled():
                # 用户取消时，强制删除临时文件
                self.cleanup_temp_audio(force_delete=True)
                self.signals.cancelled.emit(self.file_path)
                return
            
            if self.asr_engine == OFFLINE_ENGINE:
                try:
                    if self.offline_engine is None:
                        raise RuntimeError("离线模型管理器未初始化")
                    result = self.offline_engine.transcribe(
                        self.file_path, should_stop=self.is_cancelled,
                        progress=self.report_offline_stage)
                    result_text = {"SRT": result.to_srt, "TXT": result.to_txt, "ASS": result.to_ass}[self.export_format]()
                    atomic_text(Path(self.file_path).with_suffix('.' + self.export_format.lower()), result_text, self.is_cancelled)
                    self.signals.finished.emit(self.file_path, result_text)
                finally:
                    ASRWorker.remove_worker(self.file_path, self)
                return
            if self.asr_engine != ONLINE_ENGINE:
                raise ValueError("未知识别引擎")
            use_cache = True
            
            # 检查文件类型,如果不是音频则转换
            logging.info("[+]正在进行ffmpeg转换")
            audio_exts = ['.mp3', '.wav']
            if not any(self.file_path.lower().endswith(ext) for ext in audio_exts):
                temp_audio = self.file_path.rsplit(".", 1)[0] + ".mp3"
                
                if not video2audio(self.file_path, temp_audio, self):
                    if self.is_cancelled():
                        # 用户取消时，强制删除临时文件
                        self.cleanup_temp_audio(force_delete=True)
                        self.signals.cancelled.emit(self.file_path)
                    else:
                        raise RuntimeError("FFmpeg 音频转换未完成，请重新处理；若持续失败，请重新获取完整应用包。")
                    return
                self.audio_path = temp_audio
                self.is_temp_audio = True  # 标记为临时文件
            else:
                self.audio_path = self.file_path
                self.is_temp_audio = False  # 不是临时文件
            
            # 再次检查是否被取消
            if self.is_cancelled():
                # 用户取消时，强制删除临时文件
                self.cleanup_temp_audio(force_delete=True)
                self.signals.cancelled.emit(self.file_path)
                return
            
            # 使用B接口进行ASR识别
            asr = BcutASR(
                self.audio_path,
                use_cache=use_cache,
                should_stop=self.is_cancelled,
            )

            logging.info(f"开始处理文件: {self.file_path} 使用引擎: {self.asr_engine}")
            result = asr.run()
            
            # 检查是否被取消
            if self.is_cancelled():
                # 用户取消时，强制删除临时文件
                self.cleanup_temp_audio(force_delete=True)
                self.signals.cancelled.emit(self.file_path)
                return
            
            # 根据导出格式选择转换方法
            save_ext = self.export_format.lower()
            if save_ext == 'srt':
                result_text = result.to_srt()
            elif save_ext == 'ass':
                result_text = result.to_ass()
            elif save_ext == 'txt':
                result_text = result.to_txt()
                
            logging.info(f"完成处理文件: {self.file_path} 使用引擎: {self.asr_engine}")
            save_path = self.file_path.rsplit(".", 1)[0] + "." + save_ext
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(result_text)
            
            # 成功完成后，根据用户设置决定是否删除临时文件
            self.cleanup_temp_audio()
            
            # 从活动worker中移除
            ASRWorker.remove_worker(self.file_path, self)
            
            self.signals.finished.emit(self.file_path, result_text)
            
        except Exception as e:
            # 从活动worker中移除
            ASRWorker.remove_worker(self.file_path, self)
            
            if self.is_cancelled():
                # 用户取消时，强制删除临时文件
                self.cleanup_temp_audio(force_delete=True)
                self.signals.cancelled.emit(self.file_path)
            else:
                # 处理失败时，根据用户设置决定是否清理
                self.cleanup_temp_audio()
                logging.error(f"处理文件 {self.file_path} 时出错: {str(e)}")
                if isinstance(e, BcutRateLimitedError):
                    category = "rate_limited"
                    retry_after = e.retry_after
                elif isinstance(e, BcutPollingTimeoutError):
                    category = "stalled"
                    retry_after = 0.0
                else:
                    category = "error"
                    retry_after = 0.0
                self.signals.errno.emit(
                    self.file_path,
                    category,
                    f"处理时出错: {str(e)}",
                    retry_after,
                )

    def report_offline_stage(self, stage, current, total):
        # Filter in the worker before crossing threads: no per-segment/percentage
        # updates or repeated UI repaints during recognition and alignment.
        if stage in ("扫描音频", "识别中", "逐字对齐"):
            stage = "识别中"
        if stage != self._offline_stage:
            self._offline_stage = stage
            self.signals.progress.emit(self.file_path, stage, 0, 0)

class ASRWidget(QWidget):
    """ASR处理界面"""

    def __init__(self):
        super().__init__()
        self.max_threads = 3  # 设置最大线程数
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(self.max_threads)
        self.processing_queue = []
        self.workers = {}  # 维护文件路径到worker的映射（用于信号连接跟踪）
        self.offline_engine = OfflineASR()
        # Native CPU inference must run on a persistent Python-owned thread.
        # Observed Windows heap corruption after native inference on Qt workers.
        self.offline_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-asr")
        self.batch_state = "running"
        self.probe_file = None
        self.circuit_deadline = 0.0
        self.cooldown_timer = QTimer(self)
        self.cooldown_timer.setSingleShot(True)
        self.cooldown_timer.timeout.connect(self.start_recovery_probe)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self.update_circuit_notice)

        # 加载设置
        self.load_settings()

        # 更新线程池设置
        self.update_thread_pool()

        # 初始化UI
        self.init_ui()
    
    def load_settings(self):
        """加载用户设置"""
        settings = QSettings("AsrTools", "Preferences")
        self.delete_temp_audio_default = settings.value("delete_temp_audio", False, type=bool)
        self.max_threads = settings.value("max_threads", 3, type=int)
        # 确保线程数在有效范围内
        self.max_threads = clamp_thread_count(self.max_threads)
        settings.setValue("max_threads", self.max_threads)

    def update_thread_pool(self):
        """更新线程池设置"""
        self.thread_pool.setMaxThreadCount(self.max_threads)

    def save_settings(self):
        """保存用户设置"""
        settings = QSettings("AsrTools", "Preferences")
        settings.setValue("delete_temp_audio", self.delete_temp_checkbox.isChecked())
        settings.setValue("max_threads", self.max_threads)

    def on_thread_count_changed(self, value):
        """线程数变化时的处理"""
        self.max_threads = value
        self.update_thread_pool()
        self.save_settings()

    def on_engine_changed(self, text):
        online = text == ONLINE_ENGINE
        self.thread_spinbox.blockSignals(True)
        self.thread_spinbox.setValue(self.max_threads if online else 1)
        self.thread_spinbox.blockSignals(False)
        self.thread_spinbox.setEnabled(online)
        self.thread_pool.setMaxThreadCount(self.max_threads if online else 1)
    
    def _terminate_and_cleanup_task(self, file_path, status_item=None):
        """终止任务并清理资源（用户手动操作时使用）
        
        Args:
            file_path: 文件路径
            status_item: 状态项，用于判断是否需要删除临时文件
        
        Returns:
            bool: 是否成功终止
        """
        if not file_path:
            return False
        
        terminated = False
        was_probe = self.batch_state == "probing" and file_path == self.probe_file
        
        # 1. 从队列中移除（如果存在）
        if file_path in self.processing_queue:
            try:
                self.processing_queue.remove(file_path)
                logging.info(f"已从队列移除: {file_path}")
            except ValueError:
                pass
        
        # 2. 强制终止正在运行的worker
        worker = ASRWorker.get_worker(file_path)
        if worker:
            worker.cancel()  # cancel方法会触发cleanup_temp_audio(force_delete=True)
            terminated = True
            logging.info(f"已取消任务: {file_path}")
        
        # 3. 断开信号连接
        if file_path in self.workers:
            try:
                old_worker = self.workers[file_path]
                old_worker.signals.finished.disconnect(self.update_table)
                old_worker.signals.errno.disconnect(self.handle_error)
                old_worker.signals.cancelled.disconnect(self.handle_cancelled)
            except Exception:
                pass
            self.workers.pop(file_path, None)
        
        # 4. 强制清理临时文件（用户手动操作时）
        if self.combo_box.currentText() != OFFLINE_ENGINE:
            self.force_cleanup_temp_audio(file_path)

        if was_probe:
            if self.processing_queue:
                self.enter_manual_pause(
                    "恢复探测已取消，批次仍保持暂停。请稍后点击“继续处理”。"
                )
            else:
                self.finish_probe()

        self.process_next_in_queue()

        return terminated
    
    def force_cleanup_temp_audio(self, original_file_path):
        """强制清理与原始文件关联的临时音频文件（用户手动操作时使用）
        
        Args:
            original_file_path: 原始文件路径（视频文件）
        """
        if not original_file_path:
            return
        
        try:
            # 推断临时文件路径
            temp_audio_path = original_file_path.rsplit(".", 1)[0] + ".mp3"
            
            # 健壮性检查：确认这是临时生成的文件（不是用户原有的MP3）
            # 并且不是原始文件本身
            if (os.path.exists(temp_audio_path) and 
                temp_audio_path != original_file_path and
                not original_file_path.lower().endswith('.mp3')):
                
                # 检查文件是否被占用
                try:
                    with open(temp_audio_path, 'rb') as f:
                        f.read(1)
                except (OSError, IOError):
                    logging.warning(f"临时文件被占用，无法删除: {temp_audio_path}")
                    return
                
                os.remove(temp_audio_path)
                logging.info(f"强制删除临时文件: {temp_audio_path}")
                
        except (OSError, IOError) as e:
            logging.warning(f"强制删除失败: {str(e)}")


    def init_ui(self):
        layout = QVBoxLayout(self)

        # ASR引擎选择区域
        engine_layout = QHBoxLayout()
        engine_label = BodyLabel("选择接口:", self)
        engine_label.setFixedWidth(70)
        self.combo_box = ComboBox(self)
        self.combo_box.addItems([OFFLINE_ENGINE, ONLINE_ENGINE])
        engine_layout.addWidget(engine_label)
        engine_layout.addWidget(self.combo_box)
        layout.addLayout(engine_layout)

        # 导出格式选择区域 
        format_layout = QHBoxLayout()
        format_label = BodyLabel("导出格式:", self)
        format_label.setFixedWidth(70)
        self.format_combo = ComboBox(self)
        self.format_combo.addItems(['SRT', 'TXT', 'ASS'])
        format_layout.addWidget(format_label)
        format_layout.addWidget(self.format_combo)
        layout.addLayout(format_layout)

        # 临时文件设置区域
        temp_layout = QHBoxLayout()
        self.delete_temp_checkbox = QCheckBox("自动删除临时音频文件", self)
        self.delete_temp_checkbox.setChecked(self.delete_temp_audio_default)
        self.delete_temp_checkbox.setToolTip("勾选后会在任务完成时自动删除转换过程中生成的临时MP3文件")
        self.delete_temp_checkbox.stateChanged.connect(self.save_settings)
        temp_layout.addWidget(self.delete_temp_checkbox)
        temp_layout.addStretch()
        layout.addLayout(temp_layout)

        # 线程数设置区域
        thread_layout = QHBoxLayout()
        thread_label = BodyLabel("并发线程数:", self)
        thread_label.setFixedWidth(80)
        self.thread_spinbox = QSpinBox(self)
        self.thread_spinbox.setMinimum(MIN_THREAD_COUNT)
        self.thread_spinbox.setMaximum(MAX_THREAD_COUNT)
        self.thread_spinbox.setValue(self.max_threads)
        self.thread_spinbox.setFixedWidth(60)
        self.thread_spinbox.setToolTip("设置同时处理的音频文件数量 (1-3)")
        self.thread_spinbox.valueChanged.connect(self.on_thread_count_changed)
        self.thread_spinbox.setEnabled(False)
        self.thread_spinbox.setToolTip("离线模式逐文件处理并复用模型；并发设置仅用于 B 接口")
        self.combo_box.currentTextChanged.connect(self.on_engine_changed)
        self.on_engine_changed(self.combo_box.currentText())
        thread_layout.addWidget(thread_label)
        thread_layout.addWidget(self.thread_spinbox)
        thread_layout.addStretch()
        layout.addLayout(thread_layout)

        # 文件选择区域
        file_layout = QHBoxLayout()
        self.file_input = LineEdit(self)
        self.file_input.setPlaceholderText("拖拽文件或文件夹到这里")
        self.file_input.setReadOnly(True)
        self.file_button = PushButton("选择文件", self)
        self.file_button.clicked.connect(self.select_file)
        file_layout.addWidget(self.file_input)
        file_layout.addWidget(self.file_button)
        layout.addLayout(file_layout)

        # 文件列表表格
        self.table = TableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(['', '文件名', '状态'])
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.table)

        # 设置表格列的拉伸模式
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(2, 100)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # 连接表格item变化信号，用于监听复选框状态变化
        self.table.itemChanged.connect(self.on_table_item_changed)

        # 批量操作按钮区域
        batch_layout = QHBoxLayout()
        
        # 全选/取消全选按钮
        self.select_all_button = PushButton("全选", self)
        self.select_all_button.clicked.connect(self.toggle_select_all)
        self.select_all_button.setEnabled(False)
        batch_layout.addWidget(self.select_all_button)
        
        # 批量处理选中任务按钮
        self.batch_process_button = PushButton("批量处理选中任务", self)
        self.batch_process_button.clicked.connect(self.process_selected_files)
        self.batch_process_button.setEnabled(False)
        batch_layout.addWidget(self.batch_process_button)
        
        # 批量重新处理按钮
        self.batch_reprocess_button = PushButton("批量重新处理", self)
        self.batch_reprocess_button.setIcon(FIF.SYNC)
        self.batch_reprocess_button.clicked.connect(self.batch_reprocess_selected)
        self.batch_reprocess_button.setEnabled(False)
        batch_layout.addWidget(self.batch_reprocess_button)
        
        # 批量删除按钮
        self.batch_delete_button = PushButton("批量删除", self)
        self.batch_delete_button.setIcon(FIF.DELETE)
        self.batch_delete_button.clicked.connect(self.batch_delete_selected)
        self.batch_delete_button.setEnabled(False)
        batch_layout.addWidget(self.batch_delete_button)
        
        batch_layout.addStretch()
        layout.addLayout(batch_layout)

        # 清空已完成按钮
        self.clear_completed_button = PushButton("清空已完成", self)
        self.clear_completed_button.setIcon(FIF.BROOM)  # 使用扫帚图标表示清理
        self.clear_completed_button.clicked.connect(self.clear_completed_tasks)
        self.clear_completed_button.setEnabled(False)
        layout.addWidget(self.clear_completed_button)

        # 处理按钮
        self.process_button = PushButton("开始处理全部", self)
        self.process_button.clicked.connect(self.process_files)
        self.process_button.setEnabled(False)  # 初始禁用
        layout.addWidget(self.process_button)

        self.circuit_notice = BodyLabel("", self)
        self.circuit_notice.setWordWrap(True)
        self.circuit_notice.hide()
        layout.addWidget(self.circuit_notice)

        self.setAcceptDrops(True)

    def select_file(self):
        """选择文件对话框"""
        files, _ = QFileDialog.getOpenFileNames(self, "选择音频或视频文件", "",
                                                "Media Files (*.mp3 *.wav *.ogg *.mp4 *.avi *.mov *.ts)")
        for file in files:
            self.add_file_to_table(file)
        self.update_start_button_state()

    def add_file_to_table(self, file_path):
        """将文件添加到表格中"""
        if self.find_row_by_file_path(file_path) != -1:
            InfoBar.warning(
                title='文件已存在',
                content=f"文件 {os.path.basename(file_path)} 已经添加到列表中。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
            return

        row_count = self.table.rowCount()
        self.table.insertRow(row_count)
        
        # 复选框列
        checkbox_item = QTableWidgetItem()
        checkbox_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        checkbox_item.setCheckState(Qt.Unchecked)
        self.table.setItem(row_count, 0, checkbox_item)
        
        # 文件名列
        item_filename = self.create_non_editable_item(os.path.basename(file_path))
        item_filename.setData(Qt.UserRole, file_path)
        self.table.setItem(row_count, 1, item_filename)
        
        # 状态列
        item_status = self.create_non_editable_item("未处理")
        item_status.setForeground(QColor("gray"))
        self.table.setItem(row_count, 2, item_status)

    def create_non_editable_item(self, text):
        """创建不可编辑的表格项"""
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def show_context_menu(self, pos):
        """显示右键菜单"""
        current_row = self.table.rowAt(pos.y())
        if current_row < 0:
            return

        self.table.selectRow(current_row)

        menu = RoundMenu(parent=self)
        reprocess_action = Action(FIF.SYNC, "重新处理")
        delete_action = Action(FIF.DELETE, "删除任务")
        open_dir_action = Action(FIF.FOLDER, "打开文件目录")
        menu.addActions([reprocess_action, delete_action, open_dir_action])

        delete_action.triggered.connect(self.delete_selected_row)
        open_dir_action.triggered.connect(self.open_file_directory)
        reprocess_action.triggered.connect(self.reprocess_selected_file)

        menu.exec(QCursor.pos())

    def delete_selected_row(self):
        """删除选中的行（右键菜单用，强制终止并清理资源）"""
        current_row = self.table.currentRow()
        if current_row < 0 or current_row >= self.table.rowCount():
            return
            
        filename_item = self.table.item(current_row, 1)
        status_item = self.table.item(current_row, 2)
        if filename_item is None:
            return
            
        file_path = filename_item.data(Qt.UserRole)
        if not file_path:
            return
        
        # 如果任务正在处理中，显示确认对话框
        if status_item and status_item.text() in ("处理中", "恢复探测"):
            w = MessageBox('确认删除', 
                          '该任务正在处理中，强制终止并删除吗？\n（将清理所有相关资源）',
                          self)
            w.yesButton.setText('确定')
            w.cancelButton.setText('取消')
            if not w.exec():
                return
        
        # 强制终止任务并清理资源
        self._terminate_and_cleanup_task(file_path, status_item)
        
        # 删除行
        self.table.removeRow(current_row)
        self.update_start_button_state()
        
        InfoBar.success(
            title='删除成功',
            content="任务已删除",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1500,
            parent=self
        )

    def open_file_directory(self):
        """打开文件所在目录"""
        current_row = self.table.currentRow()
        if current_row < 0 or current_row >= self.table.rowCount():
            return
            
        current_item = self.table.item(current_row, 1)
        if current_item:
                file_path = current_item.data(Qt.UserRole)
                directory = os.path.dirname(file_path)
                try:
                    if platform.system() == "Windows":
                        os.startfile(directory)
                    elif platform.system() == "Darwin":
                        subprocess.Popen(["open", directory])
                    else:
                        subprocess.Popen(["xdg-open", directory])
                except Exception as e:
                    InfoBar.error(
                        title='无法打开目录',
                        content=str(e),
                        orient=Qt.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=3000,
                        parent=self
                    )

    def reprocess_selected_file(self):
        """重新处理选中的文件（右键菜单用，支持强制终止）"""
        current_row = self.table.currentRow()
        if current_row < 0 or current_row >= self.table.rowCount():
            return
            
        filename_item = self.table.item(current_row, 1)
        status_item = self.table.item(current_row, 2)
        
        if filename_item is None or status_item is None:
            return
            
        file_path = filename_item.data(Qt.UserRole)
        if not file_path:
            return
            
        status = status_item.text()
        if status in ("处理中", "恢复探测"):
            # 如果正在处理中，询问是否强制终止
            w = MessageBox('确认重新处理', 
                          '该任务正在处理中，强制终止并重新处理吗？\n（将清理所有相关资源）',
                          self)
            w.yesButton.setText('确定')
            w.cancelButton.setText('取消')
            if not w.exec():
                return
            
            # 强制终止任务并清理资源
            self._terminate_and_cleanup_task(file_path, status_item)
        else:
            # 对于已完成的任务，断开旧连接
            if file_path in self.workers:
                try:
                    worker = self.workers[file_path]
                    worker.signals.finished.disconnect(self.update_table)
                    worker.signals.errno.disconnect(self.handle_error)
                    worker.signals.cancelled.disconnect(self.handle_cancelled)
                except Exception:
                    pass
                self.workers.pop(file_path, None)
        
        # 更新状态为"未处理"
        new_status = self.create_non_editable_item("未处理")
        new_status.setForeground(QColor("gray"))
        self.table.setItem(current_row, 2, new_status)
        
        # 添加到队列
        self.add_to_queue(file_path)
        
        InfoBar.success(
            title='已添加到队列',
            content="任务已重新添加到处理队列",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1500,
            parent=self
        )

    def add_to_queue(self, file_path):
        """将文件添加到处理队列并更新状态"""
        self.enqueue_file(file_path)
        self.process_next_in_queue()

    def set_file_status(self, file_path, status, color="gray"):
        row = self.find_row_by_file_path(file_path)
        if row != -1 and 0 <= row < self.table.rowCount():
            item = self.create_non_editable_item(status)
            item.setForeground(QColor(color))
            self.table.setItem(row, 2, item)

    def enqueue_file(self, file_path, front=False):
        if not file_path or file_path in self.processing_queue:
            return
        if front:
            self.processing_queue.insert(0, file_path)
        else:
            self.processing_queue.append(file_path)
        status = "风控暂停" if self.batch_state != "running" else "排队中"
        self.set_file_status(file_path, status, "darkorange" if self.batch_state != "running" else "gray")

    def process_files(self):
        """处理所有未处理的文件"""
        if self.batch_state == "manual_pause":
            self.start_recovery_probe()
            return
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 2)
            filename_item = self.table.item(row, 1)
            if status_item is None or filename_item is None:
                continue
            if status_item.text() == "未处理":
                file_path = filename_item.data(Qt.UserRole)
                if file_path:
                    self.enqueue_file(file_path)
        self.process_next_in_queue()

    def process_next_in_queue(self):
        """处理队列中的下一个文件"""
        if self.batch_state != "running":
            return
        effective_threads = 1 if self.combo_box.currentText() == OFFLINE_ENGINE else self.max_threads
        while len(self.workers) < effective_threads and self.processing_queue:
            file_path = self.processing_queue.pop(0)
            if file_path not in self.workers:
                self.process_file(file_path)

    def process_file(self, file_path, is_probe=False):
        """处理单个文件"""
        if not file_path:
            return
            
        selected_engine = self.combo_box.currentText()
        selected_format = self.format_combo.currentText()
        delete_temp = self.delete_temp_checkbox.isChecked()
        worker = ASRWorker(file_path, selected_engine, selected_format, delete_temp_audio=delete_temp)
        worker.offline_engine = self.offline_engine
        worker.signals.finished.connect(self.update_table)
        worker.signals.errno.connect(self.handle_error)
        worker.signals.cancelled.connect(self.handle_cancelled)
        worker.signals.progress.connect(self.handle_progress)
        self.workers[file_path] = worker
        if selected_engine == OFFLINE_ENGINE:
            self.offline_executor.submit(worker.run)
        else:
            self.thread_pool.start(worker)

        row = self.find_row_by_file_path(file_path)
        if row != -1 and 0 <= row < self.table.rowCount():
            status = "恢复探测" if is_probe else "处理中"
            status_item = self.create_non_editable_item(status)
            status_item.setForeground(QColor("darkorange" if is_probe else "orange"))
            self.table.setItem(row, 2, status_item)
            self.update_start_button_state()

    def handle_progress(self, file_path, stage, current, total):
        if file_path not in self.workers:
            return
        worker = self.workers[file_path]
        if self.sender() is not None and self.sender() is not worker.signals:
            return
        if worker.is_cancelled():
            self.set_file_status(file_path, "取消中", "gray")
            return
        # Preserve the established state text used by queue/context-menu handlers.
        row = self.find_row_by_file_path(file_path)
        if row >= 0:
            item = self.table.item(row, 2)
            if item is not None:
                item.setText("处理中")
                item.setToolTip(stage)
                self.circuit_notice.setText(f"{Path(file_path).name}：{stage}，排队 {len(self.processing_queue)} 个")
                self.circuit_notice.show()

    def mark_queue_paused(self):
        for file_path in self.processing_queue:
            self.set_file_status(file_path, "风控暂停", "darkorange")

    def cancel_active_workers(self):
        for file_path, worker in list(self.workers.items()):
            self.set_file_status(file_path, "风控暂停", "darkorange")
            worker.cancel()

    def paused_task_count(self):
        return sum(
            1
            for row in range(self.table.rowCount())
            if self.table.item(row, 2) is not None
            and self.table.item(row, 2).text() == "风控暂停"
        )

    def update_circuit_notice(self):
        if self.batch_state != "cooldown":
            return
        remaining = max(0, int(self.circuit_deadline - time.monotonic() + 0.999))
        self.circuit_notice.setText(
            f"必剪接口触发风控，剩余 {self.paused_task_count()} 个任务已暂停。"
            f"{remaining} 秒后尝试恢复。"
        )
        self.circuit_notice.show()

    def open_batch_circuit(self, retry_after):
        self.batch_state = "cooldown"
        self.circuit_deadline = time.monotonic() + max(0.0, retry_after)
        self.cancel_active_workers()
        self.mark_queue_paused()
        self.update_circuit_notice()
        self.countdown_timer.start()
        self.cooldown_timer.start(max(0, int(retry_after * 1000 + 0.999)))
        self.update_start_button_state()

    def start_recovery_probe(self):
        if self.batch_state not in ("cooldown", "manual_pause"):
            return
        self.cooldown_timer.stop()
        self.countdown_timer.stop()
        if not self.processing_queue:
            self.batch_state = "running"
            self.circuit_notice.hide()
            self.update_start_button_state()
            return
        self.batch_state = "probing"
        self.probe_file = self.processing_queue.pop(0)
        self.circuit_notice.setText("正在用一个任务探测必剪接口是否恢复…")
        self.circuit_notice.show()
        self.process_file(self.probe_file, is_probe=True)
        self.update_start_button_state()

    def enter_manual_pause(self, message):
        self.batch_state = "manual_pause"
        self.probe_file = None
        self.cooldown_timer.stop()
        self.countdown_timer.stop()
        self.mark_queue_paused()
        self.circuit_notice.setText(message)
        self.circuit_notice.show()
        self.update_start_button_state()

    def finish_probe(self):
        self.batch_state = "running"
        self.probe_file = None
        self.circuit_notice.hide()
        for file_path in self.processing_queue:
            self.set_file_status(file_path, "排队中", "gray")
    
    def handle_cancelled(self, file_path):
        """处理任务被取消的情况"""
        was_probe = self.batch_state == "probing" and file_path == self.probe_file
        batch_paused = self.batch_state in ("cooldown", "probing", "manual_pause")
        row = self.find_row_by_file_path(file_path)
        if batch_paused:
            self.enqueue_file(file_path, front=was_probe)
        elif row != -1 and 0 <= row < self.table.rowCount():
            # 更新状态为"已取消"
            item_status = self.create_non_editable_item("已取消")
            item_status.setForeground(QColor("gray"))
            self.table.setItem(row, 2, item_status)
        
        # 清理引用
        if file_path in self.workers:
            self.workers.pop(file_path, None)

        if was_probe:
            self.enter_manual_pause(
                "恢复探测已取消，批次仍保持暂停。请稍后点击“继续处理”。"
            )
            return

        # 继续处理队列
        self.process_next_in_queue()
        self.update_start_button_state()

    def update_table(self, file_path, result):
        """更新表格中文件的处理状态"""
        row = self.find_row_by_file_path(file_path)
        if row != -1 and 0 <= row < self.table.rowCount():
            item_status = self.create_non_editable_item("已处理")
            item_status.setForeground(QColor("green"))
            self.table.setItem(row, 2, item_status)
            
            filename_item = self.table.item(row, 1)
            if filename_item:
                InfoBar.success(
                    title='处理完成',
                    content=f"文件 {filename_item.text()} 已处理完成",
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=1500,
                    parent=self
                )

        if file_path:
            self.workers.pop(file_path, None)
        if self.batch_state == "probing" and file_path == self.probe_file:
            self.finish_probe()
        self.process_next_in_queue()
        self.update_start_button_state()

    def handle_error(self, file_path, category, error_message, retry_after):
        """处理错误信息"""
        self.workers.pop(file_path, None)
        if category == "rate_limited":
            was_probe = self.batch_state == "probing" and file_path == self.probe_file
            self.enqueue_file(file_path, front=True)
            if was_probe:
                self.enter_manual_pause(
                    "接口仍受限制，已停止自动重试。请稍后点击“继续处理”。"
                )
            elif self.batch_state == "running":
                self.open_batch_circuit(retry_after)
            else:
                self.mark_queue_paused()
                self.update_circuit_notice()
            return

        if category == "stalled":
            self.enqueue_file(file_path, front=True)
            self.cancel_active_workers()
            self.enter_manual_pause(
                "必剪云端识别长时间未完成，批次已暂停。请稍后点击“继续处理”。"
            )
            return

        row = self.find_row_by_file_path(file_path)
        if row != -1 and 0 <= row < self.table.rowCount():
            item_status = self.create_non_editable_item("错误")
            item_status.setForeground(QColor("red"))
            self.table.setItem(row, 2, item_status)

            InfoBar.error(
                title='处理出错',
                content=error_message,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )

        if self.batch_state == "probing" and file_path == self.probe_file:
            if self.processing_queue:
                self.enter_manual_pause(
                    "恢复探测未成功，批次仍保持暂停。请稍后点击“继续处理”。"
                )
            else:
                self.finish_probe()
            return
        self.process_next_in_queue()
        self.update_start_button_state()

    def find_row_by_file_path(self, file_path):
        """根据文件路径查找表格中的行号（带健壮性检查）"""
        if not file_path:
            return -1
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item is not None and item.data(Qt.UserRole) == file_path:
                return row
        return -1

    def update_start_button_state(self):
        busy = bool(self.workers or self.processing_queue)
        self.combo_box.setEnabled(not busy)
        if not busy and self.batch_state == "running":
            self.circuit_notice.hide()
        """根据文件列表更新按钮的状态"""
        has_files = self.table.rowCount() > 0
        
        # 检查是否有未处理的任务
        has_unprocessed = False
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 2)
            if status_item is not None and status_item.text() == "未处理":
                has_unprocessed = True
                break
        
        # 获取选中的行
        selected_rows = self.get_selected_rows()
        has_selected = len(selected_rows) > 0
        
        # 检查选中的任务中是否有可重新处理的（已处理或错误的）
        has_reprocessable = False
        for row in selected_rows:
            if not (0 <= row < self.table.rowCount()):
                continue
            status_item = self.table.item(row, 2)
            if status_item is not None and status_item.text() in ["已处理", "错误"]:
                has_reprocessable = True
                break
        
        # 检查是否有已完成的任务（用于清空按钮）
        has_completed = False
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 2)
            if status_item is not None and status_item.text() == "已处理":
                has_completed = True
                break
        
        # 更新按钮状态
        if self.batch_state == "manual_pause":
            self.process_button.setText("继续处理")
            self.process_button.setEnabled(bool(self.processing_queue))
        elif self.batch_state == "cooldown":
            self.process_button.setText("风控冷却中")
            self.process_button.setEnabled(False)
        elif self.batch_state == "probing":
            self.process_button.setText("恢复探测中")
            self.process_button.setEnabled(False)
        else:
            self.process_button.setText("开始处理全部")
            self.process_button.setEnabled(has_unprocessed)
        self.select_all_button.setEnabled(has_files)
        self.batch_process_button.setEnabled(has_selected)
        self.batch_reprocess_button.setEnabled(has_reprocessable)
        self.batch_delete_button.setEnabled(has_selected)
        self.clear_completed_button.setEnabled(has_completed)

    def on_table_item_changed(self, item):
        """处理表格项变化（复选框状态变化）"""
        if item is None:
            return
            
        # 只处理第0列（复选框列）的变化
        if item.column() == 0:
            self.update_start_button_state()
            # 更新全选按钮文本
            if self.table.rowCount() > 0:
                all_checked = True
                for row in range(self.table.rowCount()):
                    checkbox_item = self.table.item(row, 0)
                    if checkbox_item is None or checkbox_item.checkState() != Qt.Checked:
                        all_checked = False
                        break
                self.select_all_button.setText("取消全选" if all_checked else "全选")

    def toggle_select_all(self):
        """全选/取消全选"""
        # 健壮性检查：空表格直接返回
        if self.table.rowCount() == 0:
            return
            
        # 检查当前是否全选
        all_checked = True
        for row in range(self.table.rowCount()):
            checkbox_item = self.table.item(row, 0)
            if checkbox_item is None or checkbox_item.checkState() != Qt.Checked:
                all_checked = False
                break
        
        # 切换状态
        new_state = Qt.Unchecked if all_checked else Qt.Checked
        
        # 暂时断开信号，避免频繁触发更新
        # 添加异常处理，防止信号未连接时崩溃
        signal_was_connected = False
        try:
            self.table.itemChanged.disconnect(self.on_table_item_changed)
            signal_was_connected = True
        except TypeError:
            # 信号未连接，忽略异常
            pass
        
        try:
            for row in range(self.table.rowCount()):
                checkbox_item = self.table.item(row, 0)
                if checkbox_item is not None and checkbox_item.flags() & Qt.ItemIsUserCheckable:
                    checkbox_item.setCheckState(new_state)
        finally:
            # 恢复信号连接（仅在之前成功断开的情况下）
            if signal_was_connected:
                try:
                    self.table.itemChanged.connect(self.on_table_item_changed)
                except TypeError:
                    # 防止重复连接导致的问题
                    pass
        
        self.select_all_button.setText("取消全选" if not all_checked else "全选")
        self.update_start_button_state()

    def process_selected_files(self):
        """批量处理选中的文件"""
        selected_files = []
        for row in range(self.table.rowCount()):
            checkbox_item = self.table.item(row, 0)
            if checkbox_item is None or checkbox_item.checkState() != Qt.Checked:
                continue
            
            status_item = self.table.item(row, 2)
            filename_item = self.table.item(row, 1)
            if status_item is None or filename_item is None:
                continue
                
            status = status_item.text()
            if status == "未处理":
                file_path = filename_item.data(Qt.UserRole)
                if file_path:
                    selected_files.append(file_path)
            elif status in ("处理中", "恢复探测"):
                file_path = filename_item.data(Qt.UserRole)
                if file_path:
                    InfoBar.warning(
                        title='文件正在处理中',
                        content=f"文件 {os.path.basename(file_path)} 正在处理中，已跳过。",
                        orient=Qt.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=2000,
                        parent=self
                    )
        
        if not selected_files:
            InfoBar.warning(
                title='没有可处理的文件',
                content="请勾选未处理的文件。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
            return
        
        for file_path in selected_files:
            self.enqueue_file(file_path)
        self.process_next_in_queue()

    def get_selected_rows(self):
        """获取所有选中的行索引（带健壮性检查）"""
        selected_rows = []
        row_count = self.table.rowCount()
        
        for row in range(row_count):
            checkbox_item = self.table.item(row, 0)
            # 健壮性检查：确保单元格存在且是复选框类型
            if (checkbox_item is not None and 
                checkbox_item.flags() & Qt.ItemIsUserCheckable and
                checkbox_item.checkState() == Qt.Checked):
                selected_rows.append(row)
        
        return selected_rows

    def batch_reprocess_selected(self):
        """批量重新处理选中的任务（强制终止正在处理的任务并清理资源）"""
        # 1. 获取所有选中的行
        selected_rows = self.get_selected_rows()
        
        # 2. 健壮性验证
        if not selected_rows:
            InfoBar.warning(
                title='未选择任务',
                content="请先勾选需要重新处理的任务。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
            return
        
        # 3. 统计各类状态的任务
        reprocessable_rows = []  # 已处理或错误的任务
        processing_rows = []     # 正在处理中的任务
        
        for row in selected_rows:
            if not (0 <= row < self.table.rowCount()):
                continue
                
            status_item = self.table.item(row, 2)
            filename_item = self.table.item(row, 1)
            
            if status_item is None or filename_item is None:
                continue
                
            status = status_item.text()
            if status in ["已处理", "错误", "已取消"]:
                reprocessable_rows.append(row)
            elif status in ("处理中", "恢复探测"):
                processing_rows.append(row)
        
        total_count = len(reprocessable_rows) + len(processing_rows)
        
        if total_count == 0:
            InfoBar.warning(
                title='没有可重新处理的任务',
                content="请勾选已处理、处理失败或处理中的任务。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
            return
        
        # 4. 确认对话框（当包含正在处理的任务时）
        if processing_rows:
            confirm_msg = f'选中任务中有 {len(processing_rows)} 个正在处理中，强制终止并重新处理吗？'
            w = MessageBox('确认重新处理', confirm_msg, self)
            w.yesButton.setText('确定')
            w.cancelButton.setText('取消')
            if not w.exec():
                # 用户取消，只处理已完成的任务
                if not reprocessable_rows:
                    return
                processing_rows = []
        
        # 5. 处理正在处理中的任务（强制终止并清理）
        terminated_count = 0
        for row in processing_rows:
            filename_item = self.table.item(row, 1)
            status_item = self.table.item(row, 2)
            if filename_item is None:
                continue
                
            file_path = filename_item.data(Qt.UserRole)
            if not file_path:
                continue
            
            # 强制终止任务并清理资源
            if self._terminate_and_cleanup_task(file_path, status_item):
                terminated_count += 1
            
            # 更新状态为"未处理"
            if status_item:
                new_status = self.create_non_editable_item("未处理")
                new_status.setForeground(QColor("gray"))
                self.table.setItem(row, 2, new_status)
            
            # 添加到队列
            self.enqueue_file(file_path)
        
        # 6. 处理已完成的任务（断开连接并重新加入队列）
        for row in reprocessable_rows:
            filename_item = self.table.item(row, 1)
            if filename_item is None:
                continue
                
            file_path = filename_item.data(Qt.UserRole)
            if not file_path:
                continue
            
            # 断开可能存在的旧worker连接
            if file_path in self.workers:
                try:
                    worker = self.workers[file_path]
                    worker.signals.finished.disconnect(self.update_table)
                    worker.signals.errno.disconnect(self.handle_error)
                    worker.signals.cancelled.disconnect(self.handle_cancelled)
                except Exception:
                    pass
                self.workers.pop(file_path, None)
            
            # 更新状态为"未处理"
            status_item = self.table.item(row, 2)
            if status_item:
                new_status = self.create_non_editable_item("未处理")
                new_status.setForeground(QColor("gray"))
                self.table.setItem(row, 2, new_status)
            
            # 添加到队列
            self.enqueue_file(file_path)
        
        # 7. 启动处理流程
        processed_count = len(reprocessable_rows) + len(processing_rows)
        if processed_count > 0:
            self.process_next_in_queue()
            self.update_start_button_state()
            msg = f"已将 {processed_count} 个任务添加到处理队列"
            if terminated_count > 0:
                msg += f"（其中 {terminated_count} 个已强制终止）"
            InfoBar.success(
                title='已添加到队列',
                content=msg,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )

    def batch_delete_selected(self):
        """批量删除选中的任务（强制终止并清理资源）"""
        # 1. 获取所有选中的行
        selected_rows = self.get_selected_rows()
        
        # 2. 健壮性验证
        if not selected_rows:
            InfoBar.warning(
                title='未选择任务',
                content="请先勾选需要删除的任务。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
            return
        
        # 3. 确认对话框（当选择多个任务时）
        processing_count = 0
        for row in selected_rows:
            if not (0 <= row < self.table.rowCount()):
                continue
            status_item = self.table.item(row, 2)
            if status_item and status_item.text() in ("处理中", "恢复探测"):
                processing_count += 1
        
        confirm_msg = f'确定要删除 {len(selected_rows)} 个选中的任务吗？'
        if processing_count > 0:
            confirm_msg += f'\n\n注意：其中有 {processing_count} 个任务正在处理中，强制终止可能会产生临时文件。'
        
        if len(selected_rows) > 1 or processing_count > 0:
            w = MessageBox('确认删除', confirm_msg, self)
            w.yesButton.setText('确定')
            w.cancelButton.setText('取消')
            if not w.exec():
                return
        
        # 4. 逆序删除（避免索引错乱）
        selected_rows.sort(reverse=True)
        deleted_count = 0
        terminated_count = 0
        
        for row in selected_rows:
            if not (0 <= row < self.table.rowCount()):
                continue
            
            filename_item = self.table.item(row, 1)
            status_item = self.table.item(row, 2)
            if filename_item is None:
                continue
                
            file_path = filename_item.data(Qt.UserRole)
            if not file_path:
                continue
            
            # 强制终止任务并清理资源
            if self._terminate_and_cleanup_task(file_path, status_item):
                terminated_count += 1
            
            # 删除行
            self.table.removeRow(row)
            deleted_count += 1
        
        # 5. 更新按钮状态
        self.update_start_button_state()
        
        if deleted_count > 0:
            msg = f"已删除 {deleted_count} 个任务"
            if terminated_count > 0:
                msg += f"（其中 {terminated_count} 个已强制终止）"
            InfoBar.success(
                title='删除成功',
                content=msg,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )

    def clear_completed_tasks(self):
        """清空所有已完成的任务（健壮性实现）"""
        try:
            # 健壮性检查1: 确保表格有数据
            if self.table.rowCount() == 0:
                return
            
            # 收集需要删除的行索引（逆序存储以避免索引错乱）
            rows_to_delete = []
            
            for row in range(self.table.rowCount()):
                # 健壮性检查2: 确保状态列存在
                status_item = self.table.item(row, 2)
                if status_item is not None and status_item.text() == "已处理":
                    rows_to_delete.append(row)
            
            # 健壮性检查3: 如果没有已完成的任务，直接返回
            if not rows_to_delete:
                return
            
            # 逆序删除以避免索引错乱
            rows_to_delete.sort(reverse=True)
            
            # 批量删除操作，暂时禁用表格更新以提高性能
            self.table.setUpdatesEnabled(False)
            try:
                for row in rows_to_delete:
                    # 清理worker引用（如果存在）
                    filename_item = self.table.item(row, 1)
                    if filename_item:
                        file_path = filename_item.data(Qt.UserRole)
                        if file_path and file_path in self.workers:
                            try:
                                worker = self.workers[file_path]
                                worker.signals.finished.disconnect(self.update_table)
                                worker.signals.errno.disconnect(self.handle_error)
                                worker.signals.cancelled.disconnect(self.handle_cancelled)
                            except Exception:
                                pass
                            self.workers.pop(file_path, None)
                    
                    # 删除行
                    self.table.removeRow(row)
            finally:
                # 恢复表格更新
                self.table.setUpdatesEnabled(True)
            
            # 更新所有按钮状态
            self.update_start_button_state()
            
            # 显示成功提示
            InfoBar.success(
                title='清空成功',
                content=f'已清空 {len(rows_to_delete)} 个已完成的任务',
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
            
        except Exception as e:
            # 健壮性处理: 记录错误但不崩溃程序
            logging.error(f"清空已完成任务时出错: {str(e)}")
            InfoBar.error(
                title='清空失败',
                content='清空操作遇到错误，请稍后重试',
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )

    def dragEnterEvent(self, event):
        """拖拽进入事件"""
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        """拖拽释放事件"""
        supported_formats = ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma',  # 音频格式
                           '.mp4', '.avi', '.mov', '.ts', '.mkv', '.wmv', '.flv', '.webm', '.rmvb')  # 视频格式
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file in files:
            if os.path.isdir(file):
                for root, dirs, files_in_dir in os.walk(file):
                    for f in files_in_dir:
                        if f.lower().endswith(supported_formats):
                            self.add_file_to_table(os.path.join(root, f))
            elif file.lower().endswith(supported_formats):
                self.add_file_to_table(file)
        self.update_start_button_state()


class GuideWidget(QWidget):
    """面向最终用户的应用内使用说明。"""

    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        guide_text = (
            "1. 设置输出：选择 SRT、TXT 或 ASS；并发数可设为 1–3。\n"
            "   视频等输入会生成同目录临时 MP3，可按需开启自动清理。\n\n"
            "2. 添加媒体：点击“选择文件”，或把媒体文件、文件夹拖入窗口。\n\n"
            "3. 开始处理：可处理全部任务，也可勾选后批量处理。\n"
            "   需要中止时可删除正在处理的任务；使用“重新处理”重试。\n\n"
            "   如果接口触发风控，剩余任务会暂停；自动探测仍失败后可稍后点击“继续处理”。\n\n"
            "4. 获取结果：结果保存在原媒体目录，文件名不变，仅替换扩展名。\n"
            "   识别过程需要联网访问 B 站相关服务，且原目录必须可写。"
        )

        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignTop)
        main_layout.setContentsMargins(36, 28, 36, 28)
        main_layout.setSpacing(24)

        title_label = BodyLabel(f"使用说明 · v{APP_VERSION}", self)
        title_font = QFont("Segoe UI", 24, QFont.Bold) if platform.system() == "Windows" else QFont()
        if platform.system() != "Windows":
            title_font.setPointSize(24)
            title_font.setBold(True)
        title_label.setFont(title_font)
        main_layout.addWidget(title_label)

        guide_label = BodyLabel(guide_text, self)
        guide_font = QFont("Segoe UI", 11) if platform.system() == "Windows" else QFont()
        if platform.system() != "Windows":
            guide_font.setPointSize(11)
        guide_label.setFont(guide_font)
        guide_label.setWordWrap(True)
        main_layout.addWidget(guide_label)


class MainWindow(FluentWindow):
    """主窗口"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"ASRTools v{APP_VERSION}")

        # ASR 处理界面
        self.asr_widget = ASRWidget()
        self.asr_widget.setObjectName("main")
        self.addSubInterface(self.asr_widget, FIF.ALBUM, 'ASR Processing')

        # 使用说明界面
        self.guide_widget = GuideWidget()
        self.guide_widget.setObjectName("guide")
        self.addSubInterface(self.guide_widget, FIF.HELP, '使用说明')

        self.navigationInterface.setExpandWidth(200)
        self.resize(800, 600)

    def closeEvent(self, event):
        # POSIX 上 ffmpeg 运行在独立会话，不随应用退出被回收；关闭窗口时主动终止存活进程，避免孤儿。
        with ASRWorker._lock:
            workers = list(ASRWorker._workers.values())
        for worker in workers:
            worker.cancel()
        self.asr_widget.offline_executor.shutdown(wait=True)
        super().closeEvent(event)

def convert_failure_message(stderr: str, returncode: int, input_file: str) -> str:
    """把 FFmpeg 转换失败输出转成可操作的中文提示。"""
    if "does not contain any stream" in stderr:
        return f"输入文件 {input_file} 不含音频流，无法提取音频（纯视频文件）。"
    detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"退出码 {returncode}"
    return f"{detail}。请检查输入文件是否完整且原目录可写。"


def video2audio(input_file: str, output: str = "", worker=None) -> bool:
    """使用ffmpeg将视频转换为音频（支持取消）
    
    Args:
        input_file: 输入视频文件路径
        output: 输出音频文件路径
        worker: ASRWorker实例，用于检查取消状态和设置ffmpeg进程
    """
    # 检查是否已取消
    if worker and worker.is_cancelled():
        return False
    
    # 创建output目录
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = str(output_path)

    ffmpeg_path = resolve_ffmpeg_path()
    cmd = [
        str(ffmpeg_path),
        '-i', input_file,
        '-ac', '1',
        '-f', 'mp3',
        '-af', 'aresample=async=1',
        '-y',
        output
    ]
    
    try:
        # Windows使用CREATE_NEW_PROCESS_GROUP以便能够终止进程树；
        # POSIX使用start_new_session隔离进程组，避免取消任务时连带终止整个应用
        popen_kwargs: Dict[str, Any] = {}
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            # 配置Windows启动信息以隐藏命令行窗口
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            popen_kwargs["startupinfo"] = startupinfo
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding='utf-8',
            errors='replace',
            **popen_kwargs
        )
        
        # 保存进程引用到worker
        if worker:
            worker._ffmpeg_process = process
        
        # 等待进程完成，同时检查取消状态
        stdout, stderr = process.communicate()
        
        # 检查是否被取消
        if worker and worker.is_cancelled():
            # 尝试删除未完成的输出文件
            try:
                if os.path.exists(output):
                    os.remove(output)
                    logging.info(f"已删除未完成的音频文件: {output}")
            except Exception as e:
                logging.warning(f"删除未完成文件失败: {e}")
            return False
        
        if process.returncode == 0 and Path(output).is_file():
            return True
        raise RuntimeError(f"FFmpeg 转换失败：{convert_failure_message(stderr, process.returncode, input_file)}")

    except FFmpegUnavailableError:
        raise
    except OSError as e:
        logging.error(f"FFmpeg 启动失败: {e}")
        raise FFmpegUnavailableError(
            "随应用提供的 FFmpeg 无法启动。请重新解压完整便携包，"
            "并确认 FFmpeg 二进制未被安全软件或系统策略移除；若持续失败，请向销售方重新获取应用包。"
        ) from e


def run_release_check(arguments: list[str]) -> int | None:
    """Run a non-interactive check against the exact packaged executable."""

    if "--release-check" not in arguments:
        return None

    parser = argparse.ArgumentParser(description="ASRTools release verification")
    parser.add_argument(
        "--release-check",
        choices=("ffmpeg", "convert", "recognize", "workflow", "offline-workflow"),
        required=True,
    )
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--models", help="Local model directory for offline-workflow")
    parser.add_argument("--report", required=True)
    options = parser.parse_args(arguments)

    report_path = Path(options.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "app_version": APP_VERSION,
        "check": options.release_check,
        "status": "failed",
    }
    exit_code = 1

    try:
        if options.release_check == "ffmpeg":
            ffmpeg_path = resolve_ffmpeg_path()
            completed = subprocess.run(
                [str(ffmpeg_path), "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            report.update(
                status="passed",
                ffmpeg_path=str(ffmpeg_path),
                ffmpeg_version=completed.stdout.splitlines()[0],
            )
        elif options.release_check == "convert":
            if not options.input or not options.output:
                raise ValueError("convert 检查需要 --input 和 --output")
            if not video2audio(options.input, options.output):
                raise RuntimeError("FFmpeg 转换未完成")
            output_path = Path(options.output).resolve()
            report.update(
                status="passed",
                input=str(Path(options.input).resolve()),
                output=str(output_path),
                output_bytes=output_path.stat().st_size,
            )
        elif options.release_check == "recognize":
            if not options.input or not options.output:
                raise ValueError("recognize 检查需要 --input 和 --output")
            result_text = BcutASR(options.input, use_cache=False).run().to_srt()
            if not result_text.strip():
                raise RuntimeError("B 接口返回了空识别结果")
            output_path = Path(options.output).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(result_text, encoding="utf-8")
            report.update(
                status="passed",
                input=str(Path(options.input).resolve()),
                output=str(output_path),
                output_bytes=output_path.stat().st_size,
            )
        else:
            if not options.input:
                raise ValueError("workflow 检查需要 --input")
            application = QApplication.instance() or QApplication([])
            widget = ASRWidget()
            offline_check = options.release_check == "offline-workflow"
            widget.combo_box.setCurrentText(OFFLINE_ENGINE if offline_check else ONLINE_ENGINE)
            if offline_check:
                # An isolated cache proves this candidate can actually load and run
                # its native dependencies, rather than return a prior source result.
                widget.offline_engine = OfflineASR(
                    model_dir=options.models,
                    cache_dir=report_path.parent / "offline-check-cache",
                )
            widget.format_combo.setCurrentText("SRT")
            widget.add_file_to_table(options.input)
            widget.process_files()

            deadline = time.monotonic() + 180
            task_status = "处理中"
            while time.monotonic() < deadline:
                application.processEvents()
                status_item = widget.table.item(0, 2)
                task_status = status_item.text() if status_item else "状态缺失"
                if task_status in ("已处理", "错误", "已取消"):
                    break
                time.sleep(0.05)

            widget.thread_pool.waitForDone(5000)
            application.processEvents()
            status_item = widget.table.item(0, 2)
            task_status = status_item.text() if status_item else "状态缺失"
            output_path = Path(options.input).resolve().with_suffix(".srt")
            if task_status != "已处理":
                raise RuntimeError(f"GUI 任务未完成，最终状态：{task_status}")
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                raise RuntimeError("GUI 任务已处理，但没有生成非空 SRT 结果")
            report.update(
                status="passed",
                input=str(Path(options.input).resolve()),
                output=str(output_path),
                output_bytes=output_path.stat().st_size,
                task_status=task_status,
                engine=OFFLINE_ENGINE if offline_check else ONLINE_ENGINE,
            )
            widget.close()
        exit_code = 0
    except Exception as error:
        report["error"] = str(error)

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code

def start():
    # enable dpi scale
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    # setTheme(Theme.DARK)  # 如果需要深色主题，取消注释此行
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    release_check_exit_code = run_release_check(sys.argv[1:])
    if release_check_exit_code is None:
        start()
    sys.exit(release_check_exit_code)


