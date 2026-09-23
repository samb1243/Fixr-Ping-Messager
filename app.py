"""Fixr Ping Messager - desktop control app.

One window with a status light, Start/Stop buttons, the pages being watched,
and a live activity feed. Launch it via "Fixr Ping Messager.vbs" (or the
desktop shortcut) so it opens with no console window.
"""
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog

import watcher

STATE_DIR = watcher.STATE_DIR
PID_FILE = STATE_DIR / "watcher.pid"

GREEN = "#1a7f37"
RED = "#b00020"
GREY = "#888888"


def _pid_alive(pid):
    """True if a process with this PID is currently running (Windows)."""
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259  # STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def external_watcher_running():
    """True if another process (CLI or another app window) is already watching."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    if pid == os.getpid():
        return False
    return _pid_alive(pid)


class QueueWriter:
    """File-like object that forwards written text to a queue (for the log pane)."""
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fixr Ping Messager")
        self.minsize(580, 500)
        self.configure(padx=16, pady=14)

        self.stop_event = None
        self.thread = None
        self.cfg = None
        self.watching = False
        self.log_q = queue.Queue()

        self._build_ui()
        self._redirect_output()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_pages()
        self.after(150, self._drain_log)

    # ---------- UI ----------
    def _build_ui(self):
        ttk.Label(self, text="Fixr Ping Messager",
                  font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(self, text="Watches your Fixr pages and pings your phone "
                             "when an event is posted.",
                  foreground="#555").pack(anchor="w", pady=(0, 10))

        status = ttk.Frame(self)
        status.pack(fill="x", pady=(0, 8))
        self.dot = tk.Canvas(status, width=18, height=18, highlightthickness=0)
        self.dot.pack(side="left")
        self._dot = self.dot.create_oval(3, 3, 15, 15, fill=RED, outline="")
        self.status_lbl = ttk.Label(status, text="Stopped",
                                    font=("Segoe UI", 12, "bold"))
        self.status_lbl.pack(side="left", padx=(8, 0))

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=(2, 8))
        self.start_btn = ttk.Button(btns, text="▶  Start watching",
                                    command=self.start)
        self.start_btn.pack(side="left", ipadx=12, ipady=6)
        self.stop_btn = ttk.Button(btns, text="■  Stop", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(10, 0), ipadx=12, ipady=6)

        # Extra tools on their own row, so they never get squeezed off-screen.
        tools = ttk.Frame(self)
        tools.pack(fill="x", pady=(0, 8))
        self.auto_var = tk.BooleanVar(value=self._auto_reserve_on())
        ttk.Checkbutton(tools, text="Auto-reserve tickets",
                        variable=self.auto_var,
                        command=self.toggle_auto_reserve
                        ).pack(side="left", padx=(0, 12))
        ttk.Button(tools, text="Log in to Fixr", command=self.fixr_login
                   ).pack(side="left", ipadx=8, ipady=3)
        ttk.Button(tools, text="Test auto-reserve", command=self.test_reserve
                   ).pack(side="left", padx=(8, 0), ipadx=8, ipady=3)
        ttk.Button(tools, text="Test browsers", command=self.test_browsers
                   ).pack(side="left", padx=(8, 0), ipadx=8, ipady=3)

        # One tick box per Fixr account (from "accounts" in config.json).
        self.account_vars = {}
        self.account_boxes = []
        try:
            accounts = watcher.load_config().get("accounts") or []
        except Exception:
            accounts = []
        if accounts:
            acc_row = ttk.Frame(self)
            acc_row.pack(fill="x", pady=(0, 12))
            ttk.Label(acc_row, text="Accounts:").pack(side="left",
                                                      padx=(0, 6))
            for a in accounts:
                name = a["name"]
                var = tk.BooleanVar(
                    value=watcher.reserve.account_enabled(name))
                box = ttk.Checkbutton(
                    acc_row, text=name, variable=var,
                    command=lambda n=name, v=var: self.toggle_account(n, v))
                box.pack(side="left", padx=(0, 10))
                self.account_vars[name] = var
                self.account_boxes.append(box)
            self._update_account_boxes()

        ttk.Label(self, text="Watching", font=("Segoe UI", 9, "bold")).pack(
            anchor="w")
        self.pages_lbl = ttk.Label(self, text="", justify="left",
                                   foreground="#333")
        self.pages_lbl.pack(anchor="w", pady=(0, 10))

        ttk.Label(self, text="Activity", font=("Segoe UI", 9, "bold")).pack(
            anchor="w")
        self.log = scrolledtext.ScrolledText(self, height=13, wrap="word",
                                             state="disabled",
                                             font=("Consolas", 9),
                                             background="#f6f6f6")
        self.log.pack(fill="both", expand=True)

        self.foot = ttk.Label(self, text="", foreground="#666",
                              font=("Segoe UI", 8))
        self.foot.pack(anchor="w", pady=(6, 0))

    def _refresh_pages(self):
        try:
            cfg = watcher.load_config()
        except Exception as e:
            self.pages_lbl.config(text=f"(could not read config.json: {e})")
            return
        enabled = [p for p in cfg["pages"] if p.get("enabled", True)]
        disabled = [p for p in cfg["pages"] if not p.get("enabled", True)]
        if enabled:
            self.pages_lbl.config(text="\n".join(f"  •  {p['name']}"
                                                  for p in enabled))
        else:
            self.pages_lbl.config(text="  (no pages enabled in config.json)")
        topic = cfg.get("ntfy", {}).get("topic", "?")
        extra = f"   ·   {len(disabled)} disabled" if disabled else ""
        self.foot.config(text=f"Pings → ntfy topic '{topic}'   ·   "
                              f"every {cfg.get('interval_seconds', 60)}s{extra}")

    # ---------- output capture ----------
    def _redirect_output(self):
        try:
            logf = open(watcher.ROOT / "watcher.log", "a", encoding="utf-8",
                        buffering=1)
        except OSError:
            logf = None
        writer = QueueWriter(self.log_q)
        sys.stdout = watcher._Tee(logf, writer)
        sys.stderr = watcher._Tee(logf, writer)

    def _drain_log(self):
        try:
            while True:
                text = self.log_q.get_nowait()
                self.log.config(state="normal")
                self.log.insert("end", text)
                # Cap the pane so it can't grow without bound.
                if int(self.log.index("end-1c").split(".")[0]) > 600:
                    self.log.delete("1.0", "120.0")
                self.log.see("end")
                self.log.config(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    # ---------- control ----------
    def start(self):
        if self.watching:
            return
        if external_watcher_running():
            messagebox.showwarning(
                "Already running",
                "A watcher is already running (started elsewhere).\n\n"
                "Stop that one first.")
            return
        try:
            cfg = watcher.load_config()
        except Exception as e:
            messagebox.showerror("Config error",
                                 f"Could not read config.json:\n\n{e}")
            return
        if not [p for p in cfg["pages"] if p.get("enabled", True)]:
            messagebox.showwarning("Nothing to watch",
                                   "No pages are enabled in config.json.")
            return
        self.cfg = cfg
        self.stop_event = threading.Event()
        self._set_watching(True)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _auto_reserve_on(self):
        try:
            return watcher.reserve.settings(watcher.load_config())["enabled"]
        except Exception:
            return False

    def toggle_auto_reserve(self):
        on = self.auto_var.get()
        watcher.reserve.set_enabled(on)
        print("Auto-reserve is now " + (
            "ON - new Timepiece events get a ticket reserved." if on else
            "OFF - new events just open in your browser."))
        self._update_account_boxes()

    def toggle_account(self, name, var):
        on = var.get()
        watcher.reserve.set_account_enabled(name, on)
        print(f"Auto-reserve for {name} is now {'ON' if on else 'OFF'}.")

    def _update_account_boxes(self):
        # Account boxes only matter while auto-reserve itself is on.
        state = "!disabled" if self.auto_var.get() else "disabled"
        for box in self.account_boxes:
            box.state([state])

    def fixr_login(self):
        try:
            cfg = watcher.load_config()
        except Exception as e:
            messagebox.showerror("Config error",
                                 f"Could not read config.json:\n\n{e}")
            return
        if watcher.reserve.settings(cfg)["use_own_window"]:
            watcher.reserve.RESERVER.open_login(cfg)
        else:
            # Log in once per account (each opens in its own Chrome profile).
            for who, open_url in watcher._reserve_targets(cfg,
                                                          include_off=True):
                open_url(watcher.reserve.LOGIN_URL)

    def test_reserve(self):
        try:
            cfg = watcher.load_config()
        except Exception as e:
            messagebox.showerror("Config error",
                                 f"Could not read config.json:\n\n{e}")
            return
        query = simpledialog.askstring(
            "Test auto-reserve",
            "Run auto-reserve now on the event named:\n\n"
            "(this really puts a ticket in your basket)",
            initialvalue="Thursday Indie Night", parent=self)
        if not query:
            return

        def run():
            try:
                watcher.test_reserve(cfg, query)
            except Exception as e:
                print(f"  ! test failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    def test_browsers(self):
        try:
            cfg = watcher.load_config()
        except Exception as e:
            messagebox.showerror("Config error",
                                 f"Could not read config.json:\n\n{e}")
            return
        threading.Thread(target=watcher.test_browsers, args=(cfg,),
                         daemon=True).start()

    def _run(self):
        try:
            watcher.run_loop(self.cfg, self.stop_event, write_pid=True)
        except Exception as e:
            print(f"  ! watcher stopped with an error: {e}")
        finally:
            self.after(0, lambda: self._set_watching(False))

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.stop_btn.config(state="disabled", text="Stopping…")

    def _set_watching(self, on):
        self.watching = on
        if on:
            self.dot.itemconfig(self._dot, fill=GREEN)
            self.status_lbl.config(text="Watching")
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal", text="■  Stop")
        else:
            self.dot.itemconfig(self._dot, fill=RED)
            self.status_lbl.config(text="Stopped")
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled", text="■  Stop")
            self._refresh_pages()

    def _on_close(self):
        if self.watching:
            if not messagebox.askokcancel(
                    "Quit", "Watching is still on.\nStop it and quit?"):
                return
            if self.stop_event:
                self.stop_event.set()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
