"""Exercise the real selected-file GUI queue and its event loop in a subprocess."""
import argparse
import ctypes
import faulthandler
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == 'win32':
    ctypes.windll.kernel32.SetErrorMode(3)
faulthandler.enable()
import asr_gui
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QApplication

parser = argparse.ArgumentParser()
parser.add_argument('--videos', action='store_true')
parser.add_argument('--inject-failure', action='store_true')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
app = QApplication([])
window = asr_gui.MainWindow()
widget = window.asr_widget
temporary = tempfile.TemporaryDirectory(prefix='asr-gui-batch-')
directory = Path(temporary.name)
sources = sorted((root / 'example/test').glob('*.mp4'))[:5] if args.videos else [root / 'models/sensevoice-small-int8/test_wavs/zh.wav'] * 5
assert len(sources) == 5
for i, source in enumerate(sources):
    target = directory / f'{i}-中文{source.suffix}'
    shutil.copy2(source, target)
    widget.add_file_to_table(str(target))
    widget.table.item(i, 0).setCheckState(Qt.Checked)
widget.offline_engine.cache_dir = directory / 'cache'
thread_ids = set()
model_ids = set()
transcribe = widget.offline_engine.transcribe
def observed_transcribe(*a, **kw):
    thread_ids.add(threading.get_ident())
    result = transcribe(*a, **kw)
    model_ids.add(tuple(id(m) for m in (widget.offline_engine.recognizer,
                                      widget.offline_engine.vad, widget.offline_engine.aligner)))
    return result
widget.offline_engine.transcribe = observed_transcribe
if args.inject_failure:
    # Ordinary input failure must not abort the rest of the selected batch.
    (directory / ('1-中文' + sources[1].suffix)).unlink()
window.show()
started = time.monotonic()
last = None
done = None
def poll():
    global last, done
    statuses = [widget.table.item(i, 2).text() for i in range(5)]
    if statuses != last:
        print('STATUS', statuses, flush=True)
        last = statuses
    expected = ['已处理'] * 5
    if args.inject_failure:
        expected[1] = '错误'
    if statuses == expected:
        if done is None:
            done = time.monotonic()
        if time.monotonic() - done > 3:
            assert len(list(directory.glob('*.srt'))) == (4 if args.inject_failure else 5)
            assert len(thread_ids) == len(model_ids) == 1
            print('RESIDENCY one-thread one-model-set', flush=True)
            print('BATCH-PASSED window-alive', window.isVisible(), flush=True)
            window.close()
            app.exit(0)
    elif time.monotonic() - started > 240:
        print('BATCH-TIMEOUT', flush=True)
        app.exit(2)
timer = QTimer()
timer.timeout.connect(poll)
timer.start(100)
QTimer.singleShot(100, widget.batch_process_button.click)
exit_code = app.exec()
window.close()
widget.thread_pool.waitForDone()
temporary.cleanup()
sys.exit(exit_code)
