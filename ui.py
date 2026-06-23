import sys
import os
import threading
import time

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

_ansi_enabled = False

def enable_ansi_windows():
    global _ansi_enabled
    if _ansi_enabled:
        return
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    _ansi_enabled = True

def _supports_color():
    if not _ansi_enabled:
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

def _c(text, color):
    if _supports_color():
        return f"{color}{text}{RESET}"
    return text

def banner():
    enable_ansi_windows()
    art = r"""
   ____ _               _              ____
  / ___| |__   ___  ___| | _____ _ __ / ___|___  _ __ ___
 | |   | '_ \ / _ \/ __| |/ / _ \ '__| |   / _ \| '__/ _ \
 | |___| | | | (_) \__ \   <  __/ |  | |__| (_) | | |  __/
  \____|_| |_|\___/|___/_|\_\___|_|   \____\___/|_|  \___|
"""
    if _supports_color():
        print(f"{CYAN}{BOLD}{art}{RESET}")
    else:
        print(art)
    print(_c("  Local proxy + tunnel for using any model inside Cursor IDE", DIM))
    print()

def box(title, lines, color=CYAN):
    enable_ansi_windows()
    out_lines = []
    out_lines.append("")
    term_width = 70
    try:
        import shutil
        term_width = max(60, shutil.get_terminal_size((70, 20)).columns)
    except Exception:
        pass

    def top():
        label = f" {title} " if title else ""
        pad = max(0, term_width - len(label) - 2)
        return "╭" + label + "─" * pad + "╮"

    def middle(text):
        visible_len = len(text)
        pad = max(0, term_width - visible_len - 2)
        return "│ " + text + " " * pad + "│"

    def bottom():
        return "╰" + "─" * term_width + "╯"

    if _supports_color():
        out_lines.append(_c(top(), color))
    else:
        out_lines.append(top())
    for line in lines:
        if _supports_color():
            out_lines.append(_c(middle(line), color))
        else:
            out_lines.append(middle(line))
    if _supports_color():
        out_lines.append(_c(bottom(), color))
    else:
        out_lines.append(bottom())
    out_lines.append("")
    print("\n".join(out_lines))

def status_tag(kind):
    kind = kind.upper()
    mapping = {"OK": GREEN, "WARN": YELLOW, "ERR": RED, "INFO": CYAN}
    color = mapping.get(kind, CYAN)
    return _c(f"[{kind}]", color)

def log_request(method, path, model_in=None, model_out=None, status=0, duration=0.0):
    enable_ansi_windows()
    ts = time.strftime("%H:%M:%S")
    arrow = ""
    if model_in and model_out and model_in != model_out:
        arrow = f"  {model_in} {DIM}→{RESET} {model_out}"
    elif model_in:
        arrow = f"  {model_in}"

    status_color = GREEN if 200 <= status < 300 else (YELLOW if 400 <= status < 500 else RED)
    status_str = _c(f"{status}", status_color)
    dur_str = _c(f"{duration:.2f}s", DIM)

    line = f"{DIM}{ts}{RESET} {_c(method, CYAN)} {path}{arrow}  {status_str} {dur_str}"
    print(line)

def log_line(message, kind="INFO"):
    enable_ansi_windows()
    ts = time.strftime("%H:%M:%S")
    print(f"{DIM}{ts}{RESET} {status_tag(kind)} {message}")


class Spinner:
    _frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label=""):
        self.label = label
        self._thread = None
        self._stop_flag = threading.Event()
        self._active = False

    def start(self):
        if not _supports_color():
            print(f"[...] {self.label}")
            return
        self._active = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        i = 0
        while not self._stop_flag.is_set():
            frame = self._frames[i % len(self._frames)]
            sys.stdout.write(f"\r{CYAN}{frame}{RESET} {self.label}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.08)
        sys.stdout.write("\r" + " " * (len(self.label) + 4) + "\r")
        sys.stdout.flush()

    def stop(self, final_message=None):
        if not self._active:
            if final_message:
                print(final_message)
            return
        self._stop_flag.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._active = False
        if final_message:
            print(final_message)

    def update(self, label):
        self.label = label
