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
    NEEDED_FILES_64 = ["WinDivert.dll", "WinDivert64.sys"]
    NEEDED_FILES_32 = ["WinDivert.dll", "WinDivert32.sys"]

    @classmethod
    def is_driver_installed(cls) -> bool:
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
        def log(msg):
            if progress_cb:
                progress_cb(msg)

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

        if not cls.is_driver_installed():
            base = app_dir()
            zip_path = os.path.join(base, "windivert_tmp.zip")
            extract_dir = os.path.join(base, "windivert_tmp")

            log("正在下载 WinDivert 驱动...")
            try:
                urlretrieve(cls.WINDIVERT_URL, zip_path)
            except (URLError, OSError) as e:
                return f"下载失败: {e}"

            log("正在解压...")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

                is_64 = sys.maxsize > 2**32
                needed = cls.NEEDED_FILES_64 if is_64 else cls.NEEDED_FILES_32

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
                    shutil.copy2(src_path, os.path.join(base, fname))

            except zipfile.BadZipFile:
                return "下载的文件损坏，请重试"
            except OSError as e:
                return f"文件操作失败: {e}"
            finally:
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

# 每个方向的动作
ACTION_PASS = "pass"        # 放行
ACTION_DROP = "drop"        # 丢弃
ACTION_THROTTLE = "throttle"  # 限速


class NetworkController:
    """
    优先使用 WinDivert 拦截数据包 (即时生效)。
    WinDivert 不可用时降级为 Windows 防火墙 (仅封锁模式)。
    """

    RULE_PREFIX = "GameNetTool_"

    def __init__(self):
        self.active_rules: List[str] = []
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._divert_handle = None

    # ── 统一启动接口 ──

    def start(self, pid: int, exe_path: str,
              up_action: str, down_action: str,
              up_kbps: float = 0, down_kbps: float = 0) -> str:
        """
        启动网络控制。返回空字符串=成功，否则返回错误/提示信息。
        up_action/down_action: ACTION_PASS / ACTION_DROP / ACTION_THROTTLE
        """
        self.stop()

        if HAS_WINDIVERT:
            return self._start_divert(pid, up_action, down_action, up_kbps, down_kbps)
        else:
            # 降级: 防火墙只能做封锁，不能限速
            if up_action == ACTION_THROTTLE or down_action == ACTION_THROTTLE:
                return "限速功能需要 WinDivert，请先点击「一键安装」"
            return self._start_firewall(exe_path, up_action, down_action)

    # ── WinDivert 模式 (拦截数据包，即时生效) ──

    def _start_divert(self, pid: int,
                      up_action: str, down_action: str,
                      up_kbps: float, down_kbps: float) -> str:
        try:
            conns = psutil.Process(pid).net_connections()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return "无法读取进程网络连接，请检查进程是否存在"

        ports: Set[int] = {c.laddr.port for c in conns if c.laddr}
        if not ports:
            return "该进程当前没有活跃的网络连接"

        filt = self._port_filter(ports)
        if not filt:
            return "无法构建过滤规则"

        self._active = True
        self._thread = threading.Thread(
            target=self._divert_loop,
            args=(pid, ports, filt, up_action, down_action, up_kbps, down_kbps),
            daemon=True,
        )
        self._thread.start()
        return ""

    def _divert_loop(self, pid: int, ports: Set[int], filt: str,
                     up_action: str, down_action: str,
                     up_kbps: float, down_kbps: float):
        # 创建限速桶 (仅 throttle 模式需要)
        up_bucket = (
            TokenBucket(up_kbps * 1024)
            if up_action == ACTION_THROTTLE and up_kbps > 0 else None
        )
        down_bucket = (
            TokenBucket(down_kbps * 1024)
            if down_action == ACTION_THROTTLE and down_kbps > 0 else None
        )

        try:
            with pydivert.WinDivert(filt) as w:
                self._divert_handle = w
                last_refresh = time.monotonic()

                while self._active:
                    # 定期刷新端口列表
                    if time.monotonic() - last_refresh > 3:
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
                        if not self._active:
                            break
                        continue

                    # 判断方向并执行动作
                    if pkt.is_outbound:
                        action = up_action
                        bucket = up_bucket
                    else:
                        action = down_action
                        bucket = down_bucket

                    if action == ACTION_DROP:
                        continue  # 直接丢弃，不 send

                    if action == ACTION_THROTTLE and bucket:
                        bucket.consume(len(pkt.raw))

                    # pass 或 throttle 后放行
                    try:
                        w.send(pkt)
                    except Exception:
                        pass

        except Exception as e:
            print(f"WinDivert error: {e}")
        finally:
            self._divert_handle = None

    # ── 防火墙降级模式 ──

    def _start_firewall(self, exe_path: str,
                        up_action: str, down_action: str) -> str:
        self._clear_fw_rules()
        name = os.path.basename(exe_path)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if up_action == ACTION_DROP:
                rule = f"{self.RULE_PREFIX}OUT_{name}"
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "add", "rule",
                     f"name={rule}", "dir=out", f"program={exe_path}", "action=block"],
                    capture_output=True, creationflags=no_window,
                )
                if r.returncode != 0:
                    return f"防火墙添加失败: {r.stderr.decode(errors='ignore')}"
                self.active_rules.append(rule)

            if down_action == ACTION_DROP:
                rule = f"{self.RULE_PREFIX}IN_{name}"
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "add", "rule",
                     f"name={rule}", "dir=in", f"program={exe_path}", "action=block"],
                    capture_output=True, creationflags=no_window,
                )
                if r.returncode != 0:
                    return f"防火墙添加失败: {r.stderr.decode(errors='ignore')}"
                self.active_rules.append(rule)

            self._active = True
            return "[防火墙模式] 已建立的连接可能不会立即断开，建议安装 WinDivert"
        except Exception as e:
            return f"防火墙操作异常: {e}"

    def _clear_fw_rules(self):
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for rule in self.active_rules:
            try:
                subprocess.run(
                    ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"],
                    capture_output=True, creationflags=no_window,
                )
            except Exception:
                pass
        self.active_rules.clear()

    # ── 停止 ──

    def stop(self):
        self._active = False
        if self._divert_handle:
            try:
                self._divert_handle.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._clear_fw_rules()

    # ── 工具 ──

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
        self.geometry("560x750")
        self.minsize(480, 660)

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

        self._list_frame = ctk.CTkScrollableFrame(pf, height=150)
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

        # 速度设置 (内嵌 cf，默认隐藏)
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

        # ── WinDivert 状态 (所有模式都需要) ──
        wd_frame = ctk.CTkFrame(root)
        wd_frame.pack(fill="x", pady=(0, 8))

        wd_hdr = ctk.CTkFrame(wd_frame, fg_color="transparent")
        wd_hdr.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(wd_hdr, text="WinDivert 驱动", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")

        self._wd_status_frame = ctk.CTkFrame(wd_frame, fg_color="transparent")
        self._wd_status_frame.pack(fill="x", padx=20, pady=(0, 8))
        self._refresh_windivert_status()

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

        # 根据模式确定每个方向的动作
        if mode == "block_both":
            up_act, down_act = ACTION_DROP, ACTION_DROP
            up_kbps, down_kbps = 0, 0
        elif mode == "block_up":
            up_act, down_act = ACTION_DROP, ACTION_PASS
            up_kbps, down_kbps = 0, 0
        elif mode == "block_down":
            up_act, down_act = ACTION_PASS, ACTION_DROP
            up_kbps, down_kbps = 0, 0
        else:  # throttle
            up_act = ACTION_PASS if self._up_unlim.get() else ACTION_THROTTLE
            down_act = ACTION_PASS if self._down_unlim.get() else ACTION_THROTTLE
            up_kbps = 0 if self._up_unlim.get() else self._parse_float(self._up_speed.get())
            down_kbps = 0 if self._down_unlim.get() else self._parse_float(self._down_speed.get())

        err = self.ctrl.start(
            self.selected.pid, exe,
            up_act, down_act, up_kbps, down_kbps,
        )

        if not err:
            # 成功
            self.active = True
            self._btn_start.configure(state="disabled")
            self._btn_stop.configure(state="normal")

            labels = {
                "block_both": "完全断网",
                "block_up": "禁止上行",
                "block_down": "禁止下行",
            }
            if mode == "throttle":
                up_desc = "无限制" if up_act == ACTION_PASS else f"{up_kbps} KB/s"
                dn_desc = "无限制" if down_act == ACTION_PASS else f"{down_kbps} KB/s"
                self._set_status(f"限速中: 上行 {up_desc} | 下行 {dn_desc}", "#66ff66")
            else:
                engine = "WinDivert" if HAS_WINDIVERT else "防火墙"
                self._set_status(
                    f"已启用 [{engine}]: {labels[mode]} - {self.selected.name}",
                    "#66ff66",
                )
        elif err.startswith("[防火墙模式]"):
            # 防火墙降级成功但有警告
            self.active = True
            self._btn_start.configure(state="disabled")
            self._btn_stop.configure(state="normal")
            self._set_status(err, "#ffaa33")
        else:
            self._set_status(f"失败: {err}", "#ff6666")

    def _deactivate(self):
        self.ctrl.stop()
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
        for w in self._wd_status_frame.winfo_children():
            w.destroy()

        has_pydivert = WinDivertInstaller.is_pydivert_installed()
        has_driver = WinDivertInstaller.is_driver_installed()

        if has_pydivert and has_driver:
            ctk.CTkLabel(
                self._wd_status_frame,
                text="已就绪 - 所有功能可用",
                text_color="#66ff66", font=ctk.CTkFont(size=12),
            ).pack(side="left")
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
                parts.append("pydivert")
            if not has_driver:
                parts.append("驱动文件")
            ctk.CTkLabel(
                self._wd_status_frame,
                text=f"未安装 ({'+'.join(parts)}) - 封锁功能将降级为防火墙模式",
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
            self._wd_install_btn.pack(side="right")

    def _install_windivert(self):
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
        self.ctrl.stop()
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
