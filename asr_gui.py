import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
import webbrowser
import signal
import tempfile
import threading

# FIX: 修复中文路径报错 https://github.com/WEIFENG2333/AsrTools/issues/18  设置QT_QPA_PLATFORM_PLUGIN_PATH 
plugin_path = os.path.join(sys.prefix, 'Lib', 'site-packages', 'PyQt5', 'Qt5', 'plugins')
os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = plugin_path

from PyQt5.QtCore import Qt, QRunnable, QThreadPool, QObject, pyqtSignal as Signal, pyqtSlot as Slot, QSize, QThread, \
    pyqtSignal, QSettings
from PyQt5.QtGui import QCursor, QColor, QFont
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFileDialog,
                             QTableWidgetItem, QHeaderView, QSizePolicy, QCheckBox, QSpinBox)
from qfluentwidgets import (ComboBox, PushButton, LineEdit, TableWidget, FluentIcon as FIF,
                            Action, RoundMenu, InfoBar, InfoBarPosition,
                            FluentWindow, BodyLabel, MessageBox)

from bk_asr.BcutASR import BcutASR

# 设置日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


class WorkerSignals(QObject):
    finished = Signal(str, str)
    errno = Signal(str, str)
    cancelled = Signal(str)  # 新增：任务被取消信号




class ASRWorker(QRunnable):
    """ASR处理工作线程（支持强制终止和临时文件管理）"""
    _workers = {}  # 类变量：跟踪所有活动的worker
    _lock = threading.Lock()
    
    def __init__(self, file_path, asr_engine, export_format, delete_temp_audio=False):
        super().__init__()
        self.file_path = file_path
        self.asr_engine = asr_engine
        self.export_format = export_format
        self.delete_temp_audio = delete_temp_audio  # 是否自动删除临时音频文件
        self.signals = WorkerSignals()
        
        self.audio_path = None
        self.is_temp_audio = False  # 标记是否为临时音频文件
        self._is_cancelled = False
        self._cancel_lock = threading.Lock()
        self._ffmpeg_process = None
        
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
                    os.killpg(os.getpgid(self._ffmpeg_process.pid), signal.SIGTERM)
                    # 给进程一点时间优雅退出，然后强制终止
                    try:
                        self._ffmpeg_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(os.getpgid(self._ffmpeg_process.pid), signal.SIGKILL)
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
    def remove_worker(file_path):
        """从活动worker字典中移除"""
        with ASRWorker._lock:
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
                        raise Exception("音频转换失败，确保安装ffmpeg")
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
            asr = BcutASR(self.audio_path, use_cache=use_cache)

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
            ASRWorker.remove_worker(self.file_path)
            
            self.signals.finished.emit(self.file_path, result_text)
            
        except Exception as e:
            # 从活动worker中移除
            ASRWorker.remove_worker(self.file_path)
            
            if self.is_cancelled():
                # 用户取消时，强制删除临时文件
                self.cleanup_temp_audio(force_delete=True)
                self.signals.cancelled.emit(self.file_path)
            else:
                # 处理失败时，根据用户设置决定是否清理
                self.cleanup_temp_audio()
                logging.error(f"处理文件 {self.file_path} 时出错: {str(e)}")
                self.signals.errno.emit(self.file_path, f"处理时出错: {str(e)}")

class UpdateCheckerThread(QThread):
    msg = pyqtSignal(str, str, str)  # 用于发送消息的信号

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            from check_update import check_update, check_internet_connection
            # 检查互联网连接
            if not check_internet_connection():
                self.msg.emit("错误", "无法连接到互联网，请检查网络连接。", "")
                return
            # 检查更新
            config = check_update(self)
            if config:
                if config['fource']:
                    self.msg.emit("更新", "检测到新版本，请下载最新版本。", config['update_download_url'])
                else:
                    self.msg.emit("可更新", "检测到新版本，请下载最新版本。", config['update_download_url'])
        except Exception as e:
            pass


class ASRWidget(QWidget):
    """ASR处理界面"""

    def __init__(self):
        super().__init__()
        self.max_threads = 3  # 设置最大线程数
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(self.max_threads)
        self.processing_queue = []
        self.workers = {}  # 维护文件路径到worker的映射（用于信号连接跟踪）

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
        self.max_threads = max(1, min(10, self.max_threads))

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
        self.force_cleanup_temp_audio(file_path)
        
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
        self.combo_box.addItems(['B 接口'])
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
        self.thread_spinbox.setMinimum(1)
        self.thread_spinbox.setMaximum(10)
        self.thread_spinbox.setValue(self.max_threads)
        self.thread_spinbox.setFixedWidth(60)
        self.thread_spinbox.setToolTip("设置同时处理的音频文件数量 (1-10)")
        self.thread_spinbox.valueChanged.connect(self.on_thread_count_changed)
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
        if status_item and status_item.text() == "处理中":
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
        if status == "处理中":
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
        self.processing_queue.append(file_path)
        self.process_next_in_queue()

    def process_files(self):
        """处理所有未处理的文件"""
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 2)
            filename_item = self.table.item(row, 1)
            if status_item is None or filename_item is None:
                continue
            if status_item.text() == "未处理":
                file_path = filename_item.data(Qt.UserRole)
                if file_path:
                    self.processing_queue.append(file_path)
        self.process_next_in_queue()

    def process_next_in_queue(self):
        """处理队列中的下一个文件"""
        while self.thread_pool.activeThreadCount() < self.max_threads and self.processing_queue:
            file_path = self.processing_queue.pop(0)
            if file_path not in self.workers:
                self.process_file(file_path)

    def process_file(self, file_path):
        """处理单个文件"""
        if not file_path:
            return
            
        selected_engine = self.combo_box.currentText()
        selected_format = self.format_combo.currentText()
        delete_temp = self.delete_temp_checkbox.isChecked()
        worker = ASRWorker(file_path, selected_engine, selected_format, delete_temp_audio=delete_temp)
        worker.signals.finished.connect(self.update_table)
        worker.signals.errno.connect(self.handle_error)
        worker.signals.cancelled.connect(self.handle_cancelled)
        self.thread_pool.start(worker)
        self.workers[file_path] = worker

        row = self.find_row_by_file_path(file_path)
        if row != -1 and 0 <= row < self.table.rowCount():
            status_item = self.create_non_editable_item("处理中")
            status_item.setForeground(QColor("orange"))
            self.table.setItem(row, 2, status_item)
            self.update_start_button_state()
    
    def handle_cancelled(self, file_path):
        """处理任务被取消的情况"""
        row = self.find_row_by_file_path(file_path)
        if row != -1 and 0 <= row < self.table.rowCount():
            # 更新状态为"已取消"
            item_status = self.create_non_editable_item("已取消")
            item_status.setForeground(QColor("gray"))
            self.table.setItem(row, 2, item_status)
        
        # 清理引用
        if file_path in self.workers:
            self.workers.pop(file_path, None)
        
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
        self.process_next_in_queue()
        self.update_start_button_state()

    def handle_error(self, file_path, error_message):
        """处理错误信息"""
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

        if file_path:
            self.workers.pop(file_path, None)
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
            elif status == "处理中":
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
            self.processing_queue.append(file_path)
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
            elif status == "处理中":
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
            self.processing_queue.append(file_path)
        
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
            self.processing_queue.append(file_path)
        
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
            if status_item and status_item.text() == "处理中":
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


class InfoWidget(QWidget):
    """个人信息界面"""

    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        # GitHub URL 和仓库描述
        GITHUB_URL = "https://github.com/Ryderey/AsrTools"
        REPO_DESCRIPTION = """
    🚀 无需复杂配置：无需 GPU 和繁琐的本地配置，小白也能轻松使用。
    🖥️ 高颜值界面：基于 PyQt5 和 qfluentwidgets，界面美观且用户友好。
    ⚡ 效率超人：多线程并发 + 批量处理，文字转换快如闪电。
    📄 多格式支持：支持生成 .srt 和 .txt 字幕文件，满足不同需求。
        """
        
        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignTop)
        # main_layout.setSpacing(50)

        # 标题
        title_label = BodyLabel("  ASRTools", self)
        title_label.setFont(QFont("Segoe UI", 30, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title_label)

        # 仓库描述区域
        desc_label = BodyLabel(REPO_DESCRIPTION, self)
        desc_label.setFont(QFont("Segoe UI", 12))
        main_layout.addWidget(desc_label)

        github_button = PushButton("GitHub 仓库", self)
        github_button.setIcon(FIF.GITHUB)
        github_button.setIconSize(QSize(20, 20))
        github_button.setMinimumHeight(42)
        github_button.clicked.connect(lambda _: webbrowser.open(GITHUB_URL))
        main_layout.addWidget(github_button)


class MainWindow(FluentWindow):
    """主窗口"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle('ASR Processing Tool')

        # ASR 处理界面
        self.asr_widget = ASRWidget()
        self.asr_widget.setObjectName("main")
        self.addSubInterface(self.asr_widget, FIF.ALBUM, 'ASR Processing')

        # 个人信息界面
        self.info_widget = InfoWidget()
        self.info_widget.setObjectName("info")  # 设置对象名称
        self.addSubInterface(self.info_widget, FIF.GITHUB, 'About')

        self.navigationInterface.setExpandWidth(200)
        self.resize(800, 600)

        self.update_checker = UpdateCheckerThread(self)
        self.update_checker.msg.connect(self.show_msg)
        self.update_checker.start()

    def show_msg(self, title, content, update_download_url):
        w = MessageBox(title, content, self)
        if w.exec() and update_download_url:
            webbrowser.open(update_download_url)
        if title == "更新":
            sys.exit(0)

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
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output = str(output)

    cmd = [
        'ffmpeg',
        '-i', input_file,
        '-ac', '1',
        '-f', 'mp3',
        '-af', 'aresample=async=1',
        '-y',
        output
    ]
    
    try:
        # Windows使用CREATE_NEW_PROCESS_GROUP以便能够终止进程树
        startupinfo = None
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            # 配置Windows启动信息以隐藏命令行窗口
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            startupinfo=startupinfo,
            encoding='utf-8',
            errors='replace'
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
        else:
            return False
            
    except subprocess.CalledProcessError as e:
        logging.error(f"ffmpeg转换失败: {e}")
        return False
    except Exception as e:
        logging.error(f"ffmpeg执行出错: {e}")
        return False

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
    start()


