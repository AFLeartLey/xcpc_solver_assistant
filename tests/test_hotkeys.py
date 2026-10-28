"""快捷键监听器测试（不做真实键盘读取，避免污染测试进程的 stdin）。"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.hotkeys import CTRL_O, HotkeyListener  # noqa: E402


class HotkeyTest(unittest.TestCase):
    def test_disabled_listener_is_noop(self):
        calls: list[int] = []
        listener = HotkeyListener(lambda: calls.append(1), enabled=False)
        self.assertFalse(listener.available)
        self.assertFalse(listener.start())
        self.assertFalse(listener.running)
        listener.stop()  # 幂等，不应抛异常
        self.assertEqual(calls, [])

    def test_ctrl_o_codepoint(self):
        self.assertEqual(CTRL_O, "\x0f")

    def test_callback_failure_is_isolated(self):
        listener = HotkeyListener(lambda: (_ for _ in ()).throw(RuntimeError("boom")), enabled=False)
        listener._fire()  # 内部吞掉异常，监听线程不会死

    @unittest.skipUnless(os.name == "nt", "Windows 专用：msvcrt 键盘轮询")
    def test_windows_ctrl_o_triggers_callback(self):
        import msvcrt

        calls: list[str] = []
        listener = HotkeyListener(lambda: calls.append("toggle"), enabled=False)
        listener.available = True  # 绕过 TTY 检查，直接测轮询逻辑
        keys = ["\x0f", "\x0f"]
        with patch.object(msvcrt, "kbhit", lambda: bool(keys)), patch.object(
            msvcrt, "getwch", lambda: keys.pop(0)
        ):
            worker = threading.Thread(target=listener._run_windows, daemon=True)
            worker.start()
            deadline = time.time() + 2.0
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.02)
            listener._stop.set()
            worker.join(timeout=1.0)
        self.assertEqual(len(calls), 2)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
