"""
游戏网络工具箱 - Game Network Toolbox
Windows 10 游戏网络调整与测试工具
功能: 进程选择、网络封锁(上行/下行)、自定义速度限制、全局快捷键
"""

import customtkinter as ctk
import psutil
import subprocess
import threading
import time
import os
import sys
import ctypes
import zipfile
import shutil
from urllib.request import urlretrieve
from urllib.error import URLError
from typing import Optional, List, Set

# 可选依赖
try:
    import pydivert
    HAS_WINDIVERT = True
except ImportError:
    HAS_WINDIVERT = False

try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False


# ─── 工具函数 ───────────────────────────────────────────────

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def request_admin():
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1
    )
    sys.exit()


def app_dir() -> str:
    """程序所在目录 (兼容 PyInstaller 打包)"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ─── WinDivert 一键安装 ────────────────────────────────────

class WinDivertInstaller:
    """自动下载并安装 WinDivert 驱动 + pydivert 库"""

    WINDIVERT_URL = (
        "https://github.com/basil00/WinDivert/releases/download/v2.2.2/WinDivert-2.2.2-A.zip"
    )
    WINDIVERT_DIR = "WinDivert-2.2.2-A"
    NEEDED_FILES_64 = ["WinDivert.dll", "WinDivert64.sys"]
    NEEDED_FILES_32 = ["WinDivert.dll", "WinDivert32.sys"]

    @classmethod
    def is_driver_installed(cls) -> bool:
        """检查 WinDivert DLL/SYS 是否在程序目录"""
        base = app_dir()
        return os.path.isfile(os.path.join(base, "WinDivert.dll")) and (
            os.path.isfile(os.path.join(base, "WinDivert64.sys"))
            or os.path.isfile(os.path.join(base, "WinDivert32.sys"))
        )

    @classmethod
    def is_pydivert_installed(cls) -> bool:
        try:
            import pydivert  # noqa: F811
            return True
        except ImportError:
            return False

    @classmethod
    def install_all(cls, progress_cb=None) -> str:
        """
        一键安装 pydivert + WinDivert 驱动文件。
        progress_cb(message: str) 用于回报进度。
        返回空字符串表示成功，否则返回错误信息。
        """
        def log(msg):
            if progress_cb:
                progress_cb(msg)

        # 1) pip install pydivert
        if not cls.is_pydivert_installed():
            log("正在安装 pydivert 库...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "pydivert"],
                    capture_output=True, check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.CalledProcessError as e:
                return f"pip install pydivert 失败: {e.stderr.decode(errors='ignore')}"
            except FileNotFoundError:
                return "未找到 pip，请确认 Python 环境完整"

        # 2) 下载 WinDivert
        if not cls.is_driver_installed():
            base = app_dir()
            zip_path = os.path.join(base, "windivert_tmp.zip")
            extract_dir = os.path.join(base, "windivert_tmp")

            log("正在下载 WinDivert 驱动...")
            try:
                urlretrieve(cls.WINDIVERT_URL, zip_path)
            except (URLError, OSError) as e:
                return f"下载失败: {e}"

            # 3) 解压并复制文件
            log("正在解压...")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

                # 找到正确的子目录 (x64 或 x86)
                is_64 = sys.maxsize > 2**32
                needed = cls.NEEDED_FILES_64 if is_64 else cls.NEEDED_FILES_32

                # 在解压目录中搜索所需文件
                found = {}
                for root_d, _dirs, files in os.walk(extract_dir):
                    for f in files:
                        if f in needed:
                            found[f] = os.path.join(root_d, f)

                if len(found) < len(needed):
                    missing = set(needed) - set(found.keys())
                    return f"解压后未找到: {', '.join(missing)}"

                log("正在复制驱动文件...")
                for fname, src_path in found.items():
                    dst = os.path.join(base, fname)
                    shutil.copy2(src_path, dst)

            except zipfile.BadZipFile:
                return "下载的文件损坏，请重试"
            except OSError as e:
                return f"文件操作失败: {e}"
            finally:
                # 清理临时文件
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
                try:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                except OSError:
                    pass

        log("安装完成!")
        return ""


# ─── Token Bucket 限速器 ────────────────────────────────────

class TokenBucket:
    """令牌桶算法，用于带宽限速"""

    def __init__(self, rate_bytes_per_sec: float):
        self.rate = rate_bytes_per_sec
        self.tokens = rate_bytes_per_sec
        self.max_tokens = max(rate_bytes_per_sec * 2, 2048)
        self.last_time = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, amount: int):
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.max_tokens,
                    self.tokens + (now - self.last_time) * self.rate,
                )
                self.last_time = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
            time.sleep(0.001)


# ─── 网络控制器 ─────────────────────────────────────────────

class NetworkController:
    """通过 Windows 防火墙封锁 + WinDivert 限速"""

    RULE_PREFIX = "GameNetTool_"

    def __init__(self):
        self.active_rules: List[str] = []
        self.throttle_active = False
        self._throttle_thread: Optional[threading.Thread] = None
        self._divert_handle = None

    # ── 防火墙封锁 ──

    def block(self, exe_path: str, block_up: bool, block_down: bool) -> bool:
        self.unblock()
        name = os.path.basename(exe_path)
        try:
            if block_up:
                rule = f"{self.RULE_PREFIX}OUT_{name}"
                self._fw_add(rule, "out", exe_path)
                self.active_rules.append(rule)
            if block_down:
                rule = f"{self.RULE_PREFIX}IN_{name}"
                self._fw_add(rule, "in", exe_path)
                self.active_rules.append(rule)
            return True
        except Exception as e:
            print(f"Firewall error: {e}")
            return False

    def unblock(self):
        for rule in self.active_rules:
            try:
                subprocess.run(
                    ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass
        self.active_rules.clear()

    @staticmethod
    def _fw_add(rule_name: str, direction: str, exe_path: str):
        subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={rule_name}", f"dir={direction}",
                f"program={exe_path}", "action=block",
            ],
            capture_output=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    # ── WinDivert 限速 ──

    def start_throttle(self, pid: int, up_kbps: float, down_kbps: float) -> bool:
        if not HAS_WINDIVERT:
            return False
        self.stop_throttle()

        try:
            conns = psutil.Process(pid).net_connections()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

        ports: Set[int] = {c.laddr.port for c in conns if c.laddr}
        if not ports:
            return False

        self.throttle_active = True
        self._throttle_thread = threading.Thread(
            target=self._throttle_loop,
            args=(pid, ports, up_kbps, down_kbps),
            daemon=True,
        )
        self._throttle_thread.start()
        return True

    def _throttle_loop(self, pid: int, ports: Set[int],
                       up_kbps: float, down_kbps: float):
        up_bucket = TokenBucket(up_kbps * 1024) if up_kbps > 0 else None
        down_bucket = TokenBucket(down_kbps * 1024) if down_kbps > 0 else None

        filt = self._port_filter(ports)
        if not filt:
            return

        try:
            with pydivert.WinDivert(filt) as w:
                self._divert_handle = w
                last_refresh = time.monotonic()
                while self.throttle_active:
                    # 定期刷新端口列表
                    if time.monotonic() - last_refresh > 5:
                        try:
                            new_ports = {
                                c.laddr.port
                                for c in psutil.Process(pid).net_connections()
                                if c.laddr
                            }
                            ports.update(new_ports)
                        except Exception:
                            pass
                        last_refresh = time.monotonic()

                    try:
                        pkt = w.recv()
                    except Exception:
                        if not self.throttle_active:
                            break
                        continue

                    if pkt.is_outbound and up_bucket:
                        up_bucket.consume(len(pkt.raw))
                    elif not pkt.is_outbound and down_bucket:
                        down_bucket.consume(len(pkt.raw))

                    try:
                        w.send(pkt)
                    except Exception:
                        pass
        except Exception as e:
            print(f"WinDivert error: {e}")
        finally:
            self._divert_handle = None

    @staticmethod
    def _port_filter(ports: Set[int]) -> str:
        if not ports:
            return ""
        conds = []
        for p in ports:
            conds += [
                f"tcp.SrcPort == {p}", f"tcp.DstPort == {p}",
                f"udp.SrcPort == {p}", f"udp.DstPort == {p}",
            ]
        return f"({' or '.join(conds)})"

    def stop_throttle(self):
        self.throttle_active = False
        if self._divert_handle:
            try:
                self._divert_handle.close()
            except Exception:
                pass
        if self._throttle_thread and self._throttle_thread.is_alive():
            self._throttle_thread.join(timeout=3)
        self._throttle_thread = None

    def stop_all(self):
        self.unblock()
        self.stop_throttle()


# ─── 进程信息 ───────────────────────────────────────────────

class ProcInfo:
    def __init__(self, pid: int, name: str, exe: str):
        self.pid = pid
        self.name = name
        self.exe = exe


# ─── 主界面 ─────────────────────────────────────────────────

class App(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("游戏网络工具箱")
        self.geometry("560x720")
        self.minsize(480, 640)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.ctrl = NetworkController()
        self.procs: List[ProcInfo] = []
        self.selected: Optional[ProcInfo] = None
        self.active = False
        self.hotkey_str = "ctrl+f9"
        self._hk_registered = False

        self._build_ui()
        self._load_procs()
        self._bind_hotkey()
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # ── 界面构建 ──

    def _build_ui(self):
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=16, pady=16)

        # 标题
        ctk.CTkLabel(
            root, text="游戏网络工具箱",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(0, 12))

        # ── 进程选择 ──
        pf = ctk.CTkFrame(root)
        pf.pack(fill="x", pady=(0, 8))

        hdr = ctk.CTkFrame(pf, fg_color="transparent")
        hdr.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(hdr, text="选择目标进程", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="刷新", width=56, command=self._load_procs).pack(side="right")

        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._render_list())
        ctk.CTkEntry(pf, placeholder_text="输入关键词筛选...", textvariable=self._search_var).pack(
            fill="x", padx=10, pady=4
        )

        self._list_frame = ctk.CTkScrollableFrame(pf, height=160)
        self._list_frame.pack(fill="x", padx=10, pady=(0, 10))
        self._list_btns: List[ctk.CTkButton] = []

        # ── 控制模式 ──
        cf = ctk.CTkFrame(root)
        cf.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(cf, text="控制模式", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 4)
        )

        self._mode = ctk.StringVar(value="block_both")
        for label, val in [
            ("完全断网 (上行+下行)", "block_both"),
            ("仅禁止上行", "block_up"),
            ("仅禁止下行", "block_down"),
            ("自定义速度限制", "throttle"),
        ]:
            ctk.CTkRadioButton(
                cf, text=label, variable=self._mode, value=val,
                command=self._mode_changed,
            ).pack(anchor="w", padx=20, pady=2)

        # 速度设置 (内嵌 cf)
        self._speed_frame = ctk.CTkFrame(cf, fg_color="transparent")

        sg = ctk.CTkFrame(self._speed_frame, fg_color="transparent")
        sg.pack(fill="x", padx=20, pady=(4, 8))

        ctk.CTkLabel(sg, text="上行:").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self._up_speed = ctk.StringVar(value="1")
        ctk.CTkEntry(sg, textvariable=self._up_speed, width=70).grid(row=0, column=1, padx=4, pady=4)
        ctk.CTkLabel(sg, text="KB/s").grid(row=0, column=2, padx=4, pady=4)
        self._up_unlim = ctk.BooleanVar()
        ctk.CTkCheckBox(sg, text="无限制", variable=self._up_unlim).grid(row=0, column=3, padx=8, pady=4)

        ctk.CTkLabel(sg, text="下行:").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        self._down_speed = ctk.StringVar(value="1")
        ctk.CTkEntry(sg, textvariable=self._down_speed, width=70).grid(row=1, column=1, padx=4, pady=4)
        ctk.CTkLabel(sg, text="KB/s").grid(row=1, column=2, padx=4, pady=4)
        self._down_unlim = ctk.BooleanVar()
        ctk.CTkCheckBox(sg, text="无限制", variable=self._down_unlim).grid(row=1, column=3, padx=8, pady=4)

        # WinDivert 安装状态区域
        self._wd_status_frame = ctk.CTkFrame(self._speed_frame, fg_color="transparent")
        self._wd_status_frame.pack(fill="x", padx=20, pady=(2, 4))
        self._refresh_windivert_status()

        # 速度面板默认隐藏
        # (不 pack self._speed_frame)

        # ── 快捷键 ──
        hf = ctk.CTkFrame(root)
        hf.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(hf, text="全局快捷键", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 4)
        )
        hk_row = ctk.CTkFrame(hf, fg_color="transparent")
        hk_row.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(hk_row, text="切换开关:").pack(side="left", padx=4)
        self._hk_var = ctk.StringVar(value=self.hotkey_str)
        ctk.CTkEntry(hk_row, textvariable=self._hk_var, width=120).pack(side="left", padx=4)
        ctk.CTkButton(hk_row, text="应用", width=50, command=self._apply_hotkey).pack(side="left", padx=4)
        if not HAS_KEYBOARD:
            ctk.CTkLabel(
                hf, text="* 快捷键需要 keyboard 库: pip install keyboard",
                text_color="orange", font=ctk.CTkFont(size=11),
            ).pack(padx=20, anchor="w", pady=(0, 6))

        # ── 操作按钮 ──
        bf = ctk.CTkFrame(root, fg_color="transparent")
        bf.pack(fill="x", pady=(4, 8))

        self._btn_start = ctk.CTkButton(
            bf, text="  启用  ", width=200, height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#27864a", hover_color="#1f6e3c",
            command=self._activate,
        )
        self._btn_start.pack(side="left", expand=True, padx=4)

        self._btn_stop = ctk.CTkButton(
            bf, text="  停止  ", width=200, height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#862727", hover_color="#6e1f1f",
            command=self._deactivate, state="disabled",
        )
        self._btn_stop.pack(side="right", expand=True, padx=4)

        # ── 状态栏 ──
        sf = ctk.CTkFrame(root, height=32)
        sf.pack(fill="x")
        self._status = ctk.CTkLabel(
            sf, text="就绪 - 请选择进程",
            font=ctk.CTkFont(size=12), text_color="#888888",
        )
        self._status.pack(padx=10, pady=6)

    # ── 进程管理 ──

    def _load_procs(self):
        self.procs.clear()
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                info = p.info
                if info["exe"] and info["name"]:
                    self.procs.append(ProcInfo(info["pid"], info["name"], info["exe"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.procs.sort(key=lambda x: x.name.lower())
        self._render_list()

    def _render_list(self):
        for b in self._list_btns:
            b.destroy()
        self._list_btns.clear()

        kw = self._search_var.get().lower().strip()
        for proc in self.procs:
            if kw and kw not in proc.name.lower():
                continue
            is_sel = self.selected and self.selected.pid == proc.pid
            btn = ctk.CTkButton(
                self._list_frame,
                text=f"{proc.name}    PID {proc.pid}",
                anchor="w", height=26,
                fg_color=("#2a4a7a" if is_sel else "transparent"),
                text_color=("#ddd", "#ccc"),
                hover_color="#3a3a3a",
                command=lambda p=proc: self._select(p),
            )
            btn.pack(fill="x", pady=1)
            self._list_btns.append(btn)

    def _select(self, proc: ProcInfo):
        self.selected = proc
        self._render_list()
        self._set_status(f"已选择: {proc.name} (PID {proc.pid})")

    # ── 模式切换 ──

    def _mode_changed(self):
        if self._mode.get() == "throttle":
            self._speed_frame.pack(fill="x", pady=(2, 6))
        else:
            self._speed_frame.pack_forget()

    # ── 激活 / 停用 ──

    def _activate(self):
        if not self.selected:
            self._set_status("请先选择一个进程!", "#ff6666")
            return

        try:
            exe = psutil.Process(self.selected.pid).exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self._set_status("进程已退出，请刷新!", "#ff6666")
            return

        mode = self._mode.get()
        ok = False

        if mode == "throttle":
            if not HAS_WINDIVERT:
                self._set_status("请先点击「一键安装」安装 WinDivert!", "#ff6666")
                return
            up = 0 if self._up_unlim.get() else self._parse_float(self._up_speed.get())
            dn = 0 if self._down_unlim.get() else self._parse_float(self._down_speed.get())
            ok = self.ctrl.start_throttle(self.selected.pid, up, dn)
            if ok:
                desc = (
                    f"上行 {'无限制' if up == 0 else f'{up} KB/s'} | "
                    f"下行 {'无限制' if dn == 0 else f'{dn} KB/s'}"
                )
                self._set_status(f"限速中: {desc}", "#66ff66")
        else:
            up_block = mode in ("block_both", "block_up")
            dn_block = mode in ("block_both", "block_down")
            ok = self.ctrl.block(exe, up_block, dn_block)
            if ok:
                labels = {"block_both": "完全断网", "block_up": "禁止上行", "block_down": "禁止下行"}
                self._set_status(f"已启用: {labels[mode]} - {self.selected.name}", "#66ff66")

        if ok:
            self.active = True
            self._btn_start.configure(state="disabled")
            self._btn_stop.configure(state="normal")
        else:
            self._set_status("操作失败，请确认管理员权限!", "#ff6666")

    def _deactivate(self):
        self.ctrl.stop_all()
        self.active = False
        self._btn_start.configure(state="normal")
        self._btn_stop.configure(state="disabled")
        self._set_status("已停止", "#888888")

    def _toggle(self):
        if self.active:
            self._deactivate()
        else:
            self._activate()

    # ── 快捷键 ──

    def _bind_hotkey(self):
        if not HAS_KEYBOARD:
            return
        try:
            if self._hk_registered:
                keyboard.unhook_all_hotkeys()
            keyboard.add_hotkey(self.hotkey_str, lambda: self.after(0, self._toggle))
            self._hk_registered = True
        except Exception as e:
            print(f"Hotkey bind error: {e}")

    def _apply_hotkey(self):
        val = self._hk_var.get().strip()
        if val:
            self.hotkey_str = val
            self._bind_hotkey()
            self._set_status(f"快捷键已设为: {val}")

    # ── WinDivert 安装 ──

    def _refresh_windivert_status(self):
        """刷新 WinDivert 安装状态显示"""
        for w in self._wd_status_frame.winfo_children():
            w.destroy()

        has_pydivert = WinDivertInstaller.is_pydivert_installed()
        has_driver = WinDivertInstaller.is_driver_installed()

        if has_pydivert and has_driver:
            ctk.CTkLabel(
                self._wd_status_frame,
                text="WinDivert 已就绪",
                text_color="#66ff66", font=ctk.CTkFont(size=11),
            ).pack(side="left")
            # 热加载 pydivert
            global HAS_WINDIVERT
            if not HAS_WINDIVERT:
                try:
                    import pydivert  # noqa: F811
                    HAS_WINDIVERT = True
                except ImportError:
                    pass
        else:
            parts = []
            if not has_pydivert:
                parts.append("pydivert 库")
            if not has_driver:
                parts.append("WinDivert 驱动")
            ctk.CTkLabel(
                self._wd_status_frame,
                text=f"缺少: {' + '.join(parts)}",
                text_color="orange", font=ctk.CTkFont(size=11),
            ).pack(side="left", padx=(0, 8))

            self._wd_install_btn = ctk.CTkButton(
                self._wd_status_frame,
                text="一键安装",
                width=80, height=26,
                font=ctk.CTkFont(size=12),
                fg_color="#1a6b8a", hover_color="#155a74",
                command=self._install_windivert,
            )
            self._wd_install_btn.pack(side="left")

    def _install_windivert(self):
        """在后台线程中安装 WinDivert"""
        self._wd_install_btn.configure(state="disabled", text="安装中...")
        self._set_status("正在安装 WinDivert...", "#aaaaff")

        def worker():
            def on_progress(msg):
                self.after(0, lambda m=msg: self._set_status(m, "#aaaaff"))

            err = WinDivertInstaller.install_all(progress_cb=on_progress)

            def on_done():
                if err:
                    self._set_status(f"安装失败: {err}", "#ff6666")
                else:
                    self._set_status("WinDivert 安装成功!", "#66ff66")
                self._refresh_windivert_status()

            self.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    # ── 工具 ──

    def _set_status(self, text: str, color: str = "#888888"):
        self._status.configure(text=text, text_color=color)

    @staticmethod
    def _parse_float(s: str) -> float:
        try:
            return max(0, float(s))
        except ValueError:
            return 0

    def _quit(self):
        self.ctrl.stop_all()
        if HAS_KEYBOARD and self._hk_registered:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        self.destroy()


# ─── 入口 ───────────────────────────────────────────────────

def main():
    if sys.platform == "win32" and not is_admin():
        request_admin()
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
