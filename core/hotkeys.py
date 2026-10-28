"""CLI 快捷键监听：Agent 运行期间按 ``Ctrl+O``（多数终端下 ``Ctrl+Shift+O`` 同码）
即时展开/收起流式思考过程。

为什么需要它：思考内容是流式到达的，等模型说完再敲命令就看不到了。
监听线程只在 Agent 执行期间开启（此时终端没有 ``input()`` 在读 stdin），
因此不会和命令行输入抢字符；Linux/macOS 用 ``cbreak`` 模式读取单键。

注意：终端对 ``Ctrl+Shift+O`` 的编码不统一，部分终端会把它转成 ``Ctrl+O``（0x0F），
也有终端直接拦截；所以还提供 ``/think`` 命令作为稳定后备。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable

CTRL_O = "\x0f"
CTRL_C = "\x03"
FUNCTION_KEY_PREFIXES = ("\x00", "\xe0")


class HotkeyListener:
    """Agent 运行期间的非阻塞快捷键监听器（可用性依赖 TTY）。"""

    def __init__(self, on_toggle: Callable[[], None], *, enabled: bool = True):
        self.on_toggle = on_toggle
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old_term: list | None = None
        self.available = bool(enabled and _is_tty())

    # ---------------------------- 生命周期 ---------------------------- #
    def start(self) -> bool:
        if not self.available:
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="xcpc-hotkeys", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=0.5)
        self._thread = None
        self._restore_terminal()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------- 平台实现 ---------------------------- #
    def _run(self) -> None:
        try:
            if os.name == "nt":
                self._run_windows()
            else:
                self._run_posix()
        finally:
            self._restore_terminal()

    def _run_windows(self) -> None:
        import msvcrt  # 仅 Windows 可用

        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue
            char = msvcrt.getwch()
            if char == CTRL_O:
                self._fire()
            elif char == CTRL_C:
                self._interrupt_main()
            elif char in FUNCTION_KEY_PREFIXES and msvcrt.kbhit():
                msvcrt.getwch()  # 方向键 / 功能键的第二个字节，丢弃

    def _run_posix(self) -> None:
        import select
        import termios
        import tty

        try:
            self._fd = sys.stdin.fileno()
            self._old_term = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except (OSError, ValueError, termios.error):
            self.available = False
            return

        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                char = sys.stdin.read(1)
            except (OSError, ValueError):
                break
            if char == CTRL_O:
                self._fire()
            elif char == CTRL_C:
                self._interrupt_main()

    # ---------------------------- 内部 ---------------------------- #
    def _fire(self) -> None:
        try:
            self.on_toggle()
        except Exception:  # noqa: BLE001 - 快捷键回调失败不应杀掉监听线程
            pass

    @staticmethod
    def _interrupt_main() -> None:
        """把 Ctrl+C 转成主线程 KeyboardInterrupt，避免被监听线程吞掉。"""
        try:
            import _thread

            _thread.interrupt_main()
        except Exception:  # noqa: BLE001
            pass

    def _restore_terminal(self) -> None:
        if self._fd is None or self._old_term is None:
            return
        try:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_term)
        except (OSError, ValueError):
            pass
        finally:
            self._fd = None
            self._old_term = None


def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False
