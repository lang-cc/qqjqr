"""qqjqr 看门狗面板 v7（三栏 1180x520）

左栏：监控（7 项状态 + 代理统计 + 三个开关）
中栏：登录信息 + 入站消息 / 发送成功率 / 今日 429
右栏：运行日志（整列，18 行）
底部：7 个操作按钮

监控项：A 网关可达性 / B 今日 429 / C 入站消息心跳 / D 发送成功率 / F 面板 HTTP 健康
        + 排队代理、OneBot 接入、SnowLuma、QQ 账号

用 pythonw.exe 运行，无控制台窗口。
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("qqjqr.console.1")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, ".venv", "Lib", "site-packages")
if SITE not in sys.path:
    sys.path.insert(0, SITE)

PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PYW = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
ASTRBOT = os.path.join(ROOT, ".venv", "Scripts", "astrbot.exe")
PROXY = os.path.join(ROOT, "tools", "gw_proxy.py")
CONFIG = os.path.join(ROOT, "data", "cmd_config.json")
AB_LOG = os.path.join(ROOT, "data", "logs", "astrbot.log")
STATS_FILE = os.path.join(ROOT, "data", "logs", "console_stats.json")
SELF = os.path.abspath(__file__)
ICO = os.path.join(ROOT, "tools", "qqbot.ico")
APP_ID = "qqjqr.console.1"
LOG = os.path.join(ROOT, "data", "logs", "watchdog_gui.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "qqjqr-watchdog"
# ---- 本地密钥：优先环境变量，其次 tools/console.local.json（已被 .gitignore 排除）----
def _load_local_secret(name: str) -> str:
    if os.environ.get(name.upper()):
        return os.environ[name.upper()]
    try:
        import json as _json
        cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "console.local.json"), encoding="utf-8"))
        return str(cfg.get(name.lower(), ""))
    except Exception:
        return ""


SNOW_TOKEN = _load_local_secret("snowluma_token") or ""
GW_KEY = _load_local_secret("gw_key") or ""

QQ_EXE = r"C:\Program Files\Tencent\QQNT\QQ.exe"
CREATE_NO_WINDOW = 0x08000000
NEW_PW = "Miantuan2026"
SILENT_MIN = 30          # 无入站消息超过这么多分钟 → 黄灯
LOG_LINES = 18

W, H = 1180, 520
BG = "#16171a"
CARD = "#202227"
CARD_EDGE = "#2c2f36"
ROW_LINE = "#282b31"
FG = "#e9eaee"
DIM = "#8d919a"
FAINT = "#5f646d"
GREEN = "#3ddc97"
RED = "#ff6b6b"
AMBER = "#ffc857"
ACCENT = "#6b7cff"
ACCENT_DK = "#5563d6"
BTN = "#2b2e35"
BTN_HOVER = "#363a43"
FONT = "Microsoft YaHei UI"


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 2 * 1024 * 1024:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_alive(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_status(url: str, timeout: float = 8.0, headers: dict | None = None) -> int:
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}),
                                      timeout=timeout).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _netstat() -> str:
    try:
        return subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                              timeout=8, creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return ""


def est_connections(port: int) -> int:
    n = 0
    for line in _netstat().splitlines():
        p = line.split()
        if len(p) >= 4 and f":{port}" in p[1] and "ESTABLISHED" in line:
            n += 1
    return n


def pids_on_port(port: int) -> list[int]:
    out = []
    for line in _netstat().splitlines():
        p = line.split()
        if len(p) >= 5 and p[1].endswith(f":{port}") and "LISTENING" in line:
            try:
                out.append(int(p[4]))
            except ValueError:
                pass
    return sorted(set(out))


def astrbot_process_alive() -> bool:
    """进程还在不在（启动中的 AstrBot 端口还没监听，但进程已经在了）。

    少了这个判断，AstrBot 启动那 25 秒会被当成「离线」再拉一个，
    两个实例抢锁，QQ 那边就表现为反复掉线重连。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq astrbot.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
        return "astrbot.exe" in (out or "")
    except Exception:
        return False


def spawn(cmd: list[str], tag: str) -> None:
    out = os.path.join(ROOT, "data", "logs", f"{tag}.log")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "ab") as f:
        subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def snowluma_account() -> str:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:3000/get_login_info", data=b"{}",
            headers={"Authorization": f"Bearer {SNOW_TOKEN}", "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=2.5).read().decode()).get("data") or {}
        uid = d.get("user_id")
        return f"{d.get('nickname','')}({uid})" if uid else ""
    except Exception:
        return ""


def proxy_health() -> dict:
    try:
        return json.loads(urllib.request.urlopen("http://127.0.0.1:8878/health", timeout=1.5).read().decode())
    except Exception:
        return {}


def log_tail_lines(max_bytes: int = 400_000) -> list[str]:
    try:
        size = os.path.getsize(AB_LOG)
        with open(AB_LOG, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            return f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
HIT_429 = ("Non-streaming text concurrency", "RateLimitError", "Error code: 429",
           "concurrency limit exceeded")


def _blank(today: str) -> dict:
    return {"date": today, "offset": 0, "in": 0, "send": 0, "fail": 0, "r429": 0,
            "last_in": "", "last_429": ""}


def today_stats() -> dict:
    """增量扫 astrbot.log：只读上次之后新增的字节，统计今天的入站/发送/429。

    状态存在 data/logs/console_stats.json，跨重启累计；日志轮转或跨天自动归零。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    st = _blank(today)
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            st.update(saved)
    except Exception:
        pass
    if st.get("date") != today:
        st = _blank(today)

    try:
        size = os.path.getsize(AB_LOG)
    except OSError:
        size = 0
    if size < st["offset"]:            # 日志被轮转/清空
        st = _blank(today)

    if size > st["offset"]:
        try:
            with open(AB_LOG, "rb") as f:
                f.seek(st["offset"])
                data = f.read()
            parts = data.split(bytes([10]))
            partial = parts.pop()                      # 最后一行可能没写完
            st["offset"] += len(data) - len(partial)
            for raw in parts:
                line = raw.decode("utf-8", "replace")
                m = _TS.match(line)
                if not m or not m.group(1).startswith(today):
                    continue
                if "core.event_bus:117" in line:
                    st["in"] += 1
                    st["last_in"] = m.group(1)
                elif "Prepare to send" in line:
                    st["send"] += 1
                elif "Failed to send" in line:
                    st["fail"] += 1
                if any(k in line for k in HIT_429):
                    st["r429"] += 1
                    st["last_429"] = m.group(1)
        except Exception as e:  # noqa: BLE001
            print(f"[console] 统计失败 {type(e).__name__}: {e}", flush=True)

    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass

    def _dt(v):
        try:
            return datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    return {"last_in": _dt(st["last_in"]), "in": st["in"], "send": st["send"],
            "fail": st["fail"], "r429": st["r429"], "last_429": _dt(st["last_429"])}


def already_running() -> bool:
    """单实例：第二次点快捷方式时不再开一个看门狗（否则会互相抢着重启服务）"""
    global _LOCK
    try:
        _LOCK = socket.socket()
        _LOCK.bind(("127.0.0.1", 8898))
        _LOCK.listen(1)
        return False
    except OSError:
        return True


_LOCK = None


class Panel(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("控制台")
        self.configure(bg=BG)
        self.resizable(False, False)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{W}x{H}+{max(20, (sw - W) // 2)}+{max(20, (sh - H) // 2 - 40)}")

        self.auto = True
        self.autostart = self._autostart_on()
        self.topmost = True
        self.cool = {"proxy": 0.0, "astrbot": 0.0}
        self.miss = {"proxy": 0, "astrbot": 0}
        self.alive = True
        self.slow = {"gw": (0.0, 0), "panel": (0.0, 0), "stats": (0.0, {})}

        for setter in (lambda: self.iconbitmap(default=ICO), lambda: self.iconbitmap(ICO)):
            try:
                setter()
                break
            except Exception:
                continue

        self.cv = tk.Canvas(self, width=W, height=H, bg=BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.attributes("-topmost", self.topmost)
        self.deiconify()
        self.state("normal")
        self.lift()
        for _ms in (300, 1200, 3000, 6000):
            self.after(_ms, self._raise_win)

        threading.Thread(target=self._monitor, daemon=True).start()
        log("面板启动")

    def _raise_win(self) -> None:
        """把窗口从“最小化/屏幕外”救回来（用 Win32 强制还原，Tk 自己救不回来）"""
        try:
            self.deiconify()
            self.state("normal")
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            x, y = self.winfo_x(), self.winfo_y()
            if x < 0 or y < 0 or x > sw - 200 or y > sh - 200:
                self.geometry(f"{W}x{H}+{max(20, (sw - W) // 2)}+{max(20, (sh - H) // 2 - 40)}")
            self.update_idletasks()

            import ctypes
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)          # SW_RESTORE
            u.ShowWindow(hwnd, 5)              # SW_SHOW
            self.attributes("-topmost", self.topmost)
            u.SetForegroundWindow(hwnd)
            self.lift()
            self.focus_force()
        except Exception:
            pass

    # ---------------- 绘图工具 ----------------
    def rr(self, x1, y1, x2, y2, r=12, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.cv.create_polygon(pts, smooth=True, **kw)

    # ---------------- 界面 ----------------
    def _build(self) -> None:
        cv = self.cv

        # 顶栏
        cv.create_text(24, 26, text="控制台", anchor="w", fill=FG, font=(FONT, 15, "bold"))
        cv.create_text(24, 48, text="监控 · 登录信息 · 日志", anchor="w", fill=DIM, font=(FONT, 8))
        self.pill_bg = self.rr(1052, 14, 1164, 42, 14, fill=CARD, outline=CARD_EDGE)
        self.pill_dot = cv.create_oval(1068, 26, 1074, 32, fill=AMBER, outline="")
        self.pill_txt = cv.create_text(1082, 28, text="检测中", anchor="w", fill=DIM, font=(FONT, 9, "bold"))
        self.clock = cv.create_text(1164, 58, text="", anchor="e", fill=FAINT, font=(FONT, 8))

        # ============ 左栏：监控 ============
        cv.create_text(20, 72, text="监控", anchor="w", fill=FAINT, font=(FONT, 8))
        self.rr(16, 82, 420, 380, 14, fill=CARD, outline=CARD_EDGE)
        specs = [
            ("proxy", "排队代理", "127.0.0.1:8878 · 网关并发锁 2"),
            ("astrbot", "AstrBot", "面板 :6185 · 反向WS :6199"),
            ("onebot", "OneBot 接入", "SnowLuma → AstrBot"),
            ("snowluma", "SnowLuma", "注入 QQNT · HTTP :3000"),
            ("account", "QQ 账号", "当前登录"),
            ("gateway", "模型网关", "经排队代理探测 /v1/models"),
            ("inbound", "入站消息", "最近一条 QQ 消息"),
        ]
        self.rows = {}
        y = 94
        for i, (key, name, sub) in enumerate(specs):
            cv.create_oval(34, y + 8, 44, y + 18, fill=FAINT, outline="", tags=f"dot_{key}")
            cv.create_text(56, y + 1, text=name, anchor="w", fill=FG, font=(FONT, 9, "bold"))
            cv.create_text(56, y + 21, text=sub, anchor="w", fill=FAINT, font=(FONT, 7))
            box = self.rr(326, y + 1, 404, y + 25, 12, fill="#26282e", outline="")
            txt = cv.create_text(365, y + 13, text="…", fill=DIM, font=(FONT, 8, "bold"))
            self.rows[key] = {"dot": f"dot_{key}", "box": box, "txt": txt}
            if i < len(specs) - 1:
                cv.create_line(34, y + 32, 404, y + 32, fill=ROW_LINE)
            y += 42

        # 左栏下：代理统计
        self.stat_txt = {}
        x = 16
        for key, label in (("req", "累计请求"), ("queued", "排队次数"), ("err", "出错次数")):
            self.rr(x, 388, x + 129, 434, 12, fill=CARD, outline=CARD_EDGE)
            cv.create_text(x + 10, 399, text=label, anchor="w", fill=FAINT, font=(FONT, 7))
            self.stat_txt[key] = cv.create_text(x + 10, 422, text="0", anchor="w",
                                                fill=FG, font=(FONT, 12, "bold"))
            x += 137

        # 左栏下：开关
        self.sw = {}
        sw_specs = [("auto", "自动拉起", self.auto, 20), ("boot", "开机自启", self.autostart, 158),
                    ("top", "窗口置顶", self.topmost, 296)]
        for key, label, val, x in sw_specs:
            cv.create_text(x, 450, text=label, anchor="w", fill=DIM, font=(FONT, 8))
            track = self.rr(x + 60, 441, x + 98, 459, 9, fill="#2b2e35", outline="", tags=f"sw_{key}")
            knob = cv.create_oval(x + 62, 443, x + 76, 457, fill="#5f646d", outline="", tags=f"sw_{key}")
            self.sw[key] = {"track": track, "knob": knob, "x": x, "on": val}
            cv.tag_bind(f"sw_{key}", "<Button-1>", lambda e, k=key: self._toggle(k))
        self._paint_switches()

        # ============ 中栏：登录信息 ============
        cv.create_text(436, 72, text="登录信息", anchor="w", fill=FAINT, font=(FONT, 8))
        self.rr(432, 82, 790, 282, 14, fill=CARD, outline=CARD_EDGE)
        info = [
            ("AstrBot 面板", "http://127.0.0.1:6185",
             "用户 miantuan · 密码已加密，「重置密码」→ " + NEW_PW),
            ("SnowLuma WebUI", "http://127.0.0.1:5099",
             "密码已加密存储，配置文件里只有哈希"),
            ("SnowLuma OneBot", "127.0.0.1:3000",
             f"Token：{SNOW_TOKEN[:4] + '***' if SNOW_TOKEN else '（未配置）'}　（AstrBot 走反向 WS :6199）"),
        ]
        y = 100
        for name, value, sub in info:
            cv.create_text(448, y, text=name, anchor="w", fill=FG, font=(FONT, 9, "bold"))
            cv.create_text(448, y + 20, text=value, anchor="w", fill=ACCENT, font=("Consolas", 9))
            cv.create_text(448, y + 38, text=sub, anchor="w", fill=FAINT, font=(FONT, 7))
            cv.create_line(448, y + 48, 774, y + 48, fill=ROW_LINE)
            y += 56

        # ============ 中栏：消息与限流 ============
        cv.create_text(436, 290, text="消息与限流", anchor="w", fill=FAINT, font=(FONT, 8))
        self.rr(432, 300, 790, 470, 14, fill=CARD, outline=CARD_EDGE)
        cv.create_text(448, 318, text="今日入站消息", anchor="w", fill=FAINT, font=(FONT, 7))
        self.lbl_in = cv.create_text(448, 338, text="—", anchor="w", fill=FG, font=(FONT, 9))
        cv.create_text(448, 362, text="发送成功率", anchor="w", fill=FAINT, font=(FONT, 7))
        self.lbl_send = cv.create_text(448, 382, text="—", anchor="w", fill=FG, font=(FONT, 9))
        cv.create_line(448, 398, 774, 398, fill=ROW_LINE)
        cv.create_text(448, 406, text="今日网关 429 限流", anchor="w", fill=FAINT, font=(FONT, 7))
        self.lbl_429 = cv.create_text(448, 426, text="0", anchor="w", fill=GREEN, font=(FONT, 18, "bold"))
        self.lbl_429_hint = cv.create_text(448, 461, text="有代理挡着，正常应为 0", anchor="w",
                                          fill=FAINT, font=(FONT, 7))

        # ============ 右栏：日志 ============
        cv.create_text(806, 72, text="运行日志", anchor="w", fill=FAINT, font=(FONT, 8))
        self.rr(802, 82, 1164, 470, 14, fill="#141519", outline=CARD_EDGE)
        self.log_lines = [cv.create_text(818, 100 + i * 20, text="", anchor="w", fill="#7d838d",
                                         font=("Consolas", 7)) for i in range(LOG_LINES)]

        # ============ 底部按钮 ============
        self._button(16, 480, 173, 506, "全部启动", self.start_all, ACCENT, ACCENT_DK, "b_start")
        self._button(181, 480, 338, 506, "全部停止", self.stop_all, BTN, BTN_HOVER, "b_stop")
        self._button(346, 480, 503, 506, "重启 AstrBot", self.restart_astrbot, BTN, BTN_HOVER, "b_restart")
        self._button(519, 480, 676, 506, "打开面板",
                     lambda: webbrowser.open("http://127.0.0.1:6185"), BTN, BTN_HOVER, "b_p1")
        self._button(684, 480, 841, 506, "SnowLuma WebUI",
                     lambda: webbrowser.open("http://127.0.0.1:5099"), BTN, BTN_HOVER, "b_p2")
        self._button(849, 480, 1006, 506, "扫码登录", self.login_scan, BTN, BTN_HOVER, "b_qq")
        self._button(1014, 480, 1164, 506, "重置密码", self.reset_password, BTN, BTN_HOVER, "b_pw")

    def _button(self, x1, y1, x2, y2, text, cb, color, hover, tag):
        box = self.rr(x1, y1, x2, y2, 11, fill=color, outline="", tags=tag)
        self.cv.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=text, fill="white",
                            font=(FONT, 8, "bold"), tags=tag)
        self.cv.tag_bind(tag, "<Button-1>", lambda e: cb())
        self.cv.tag_bind(tag, "<Enter>", lambda e: self.cv.itemconfig(box, fill=hover))
        self.cv.tag_bind(tag, "<Leave>", lambda e: self.cv.itemconfig(box, fill=color))
        return box

    def _paint_switches(self) -> None:
        for s in self.sw.values():
            on, x = s["on"], s["x"]
            self.cv.itemconfig(s["track"], fill=ACCENT if on else "#2b2e35")
            self.cv.coords(s["knob"], (x + 82, 443, x + 96, 457) if on else (x + 62, 443, x + 76, 457))
            self.cv.itemconfig(s["knob"], fill="white" if on else "#5f646d")

    def _toggle(self, key: str) -> None:
        s = self.sw[key]
        s["on"] = not s["on"]
        if key == "auto":
            self.auto = s["on"]
            log(f"自动拉起：{'开' if self.auto else '关'}")
        elif key == "boot":
            self.autostart = s["on"]
            self._set_autostart(self.autostart)
        else:
            self.topmost = s["on"]
            self.attributes("-topmost", self.topmost)
        self._paint_switches()

    # ---------------- 监控 ----------------
    def _monitor(self) -> None:
        while self.alive:
            try:
                now = time.time()
                p_proxy, p_astr = port_alive(8878), port_alive(6185)
                p_bot, p_snow = port_alive(6199), port_alive(3000)
                conns = est_connections(6199) if p_bot else 0
                acct = snowluma_account() if p_snow else ""
                health = proxy_health() if p_proxy else {}

                if now - self.slow["panel"][0] > 15:          # F 面板 HTTP
                    self.slow["panel"] = (now, http_status("http://127.0.0.1:6185/", 5) if p_astr else 0)
                if now - self.slow["gw"][0] > 30:             # A 网关可达性
                    st = 0
                    if p_proxy:
                        st = http_status("http://127.0.0.1:8878/v1/models", 12,
                                         {"Authorization": f"Bearer {GW_KEY}"})
                    self.slow["gw"] = (now, st)
                if now - self.slow["stats"][0] > 10:          # B/C/D 日志统计
                    self.slow["stats"] = (now, today_stats())

                gw_st, panel_st = self.slow["gw"][1], self.slow["panel"][1]
                st = self.slow["stats"][1] or {}
                last_in = st.get("last_in")
                silence = (datetime.now() - last_in).total_seconds() / 60 if last_in else None
                astr_ok = bool(p_astr) and bool(panel_st) and panel_st < 500

                state = {
                    "proxy": (p_proxy, "运行中" if p_proxy else "已停止"),
                    "astrbot": (astr_ok, "运行中" if astr_ok else (f"HTTP {panel_st}" if p_astr else "已停止")),
                    "onebot": (conns > 0, f"已接入 {conns}" if conns else ("待连接" if p_bot else "未监听")),
                    "snowluma": (p_snow, "在线" if p_snow else "离线"),
                    "account": (bool(acct), (acct.split("(")[0] if acct else "未登录")[:6]),
                    "gateway": (gw_st == 200, "正常" if gw_st == 200 else (f"HTTP {gw_st}" if gw_st else "连不上")),
                    "inbound": (silence is not None and silence < SILENT_MIN,
                                f"{int(silence)}分前" if silence is not None else "无记录"),
                }

                self.miss["proxy"] = 0 if p_proxy else self.miss["proxy"] + 1
                self.miss["astrbot"] = 0 if p_astr else self.miss["astrbot"] + 1

                if self.auto:
                    if self.miss["proxy"] >= 2 and now - self.cool["proxy"] > 30:
                        self.cool["proxy"] = now
                        log("代理离线，自动拉起")
                        spawn([PY, PROXY], "gw_proxy_restart")
                    if self.miss["astrbot"] >= 3 and now - self.cool["astrbot"] > 60:
                        if astrbot_process_alive():
                            # 进程在跑、只是还没监听端口 = 正在启动，别插一脚
                            self.miss["astrbot"] = 0
                            self.cool["astrbot"] = now - 45
                        else:
                            self.cool["astrbot"] = now
                            log("AstrBot 离线，自动拉起")
                            spawn([ASTRBOT, "run"], "astrbot_watchdog")

                ok_all = (p_proxy and astr_ok and conns > 0 and p_snow and gw_st == 200
                          and silence is not None and silence < SILENT_MIN)
                self.after(0, lambda: self._paint(state, health, ok_all, st, silence))
            except Exception as e:  # noqa: BLE001
                log(f"监控异常 {type(e).__name__}: {e}")
            time.sleep(3)

    def _paint(self, state: dict, health: dict, ok_all: bool, st: dict, silence) -> None:
        cv = self.cv
        for key, (ok, text) in state.items():
            r = self.rows[key]
            color = GREEN if ok else (AMBER if key == "inbound" else RED)
            cv.itemconfig(r["dot"], fill=color)
            cv.itemconfig(r["txt"], text=text[:7], fill=color)
            cv.itemconfig(r["box"], fill="#1d2a24" if ok else "#2c1f22")

        if ok_all:
            cv.itemconfig(self.pill_bg, fill="#1d2a24", outline="#2b4a3c")
            cv.itemconfig(self.pill_dot, fill=GREEN)
            cv.itemconfig(self.pill_txt, text="全部正常", fill=GREEN)
        else:
            cv.itemconfig(self.pill_bg, fill="#2c1f22", outline="#4a2b2f")
            cv.itemconfig(self.pill_dot, fill=RED)
            cv.itemconfig(self.pill_txt, text="有异常", fill=RED)

        for k in ("req", "queued", "err"):
            v = health.get(k, 0) if health else 0
            if k == "err":
                color = FG if not v else (AMBER if v < 5 else RED)
            else:
                color = FG
            cv.itemconfig(self.stat_txt[k], text=str(v), fill=color)

        last_in = st.get("last_in")
        if last_in:
            warn = (silence or 0) >= SILENT_MIN
            cv.itemconfig(self.lbl_in,
                          text=f"{st.get('in', 0)} 条 · 最后 {last_in.strftime('%H:%M:%S')}"
                               f"（{int(silence or 0)} 分钟前）{'⚠ 静默' if warn else ''}",
                          fill=(AMBER if warn else FG))
        else:
            cv.itemconfig(self.lbl_in, text="今天还没有收到 QQ 消息", fill=AMBER)

        send, fail = st.get("send", 0), st.get("fail", 0)
        if send:
            rate = max(0.0, (send - fail) / send * 100)
            cv.itemconfig(self.lbl_send,
                          text=f"准备 {send} · 失败 {fail} · 成功 {rate:.1f}%",
                          fill=(FG if rate >= 90 else (AMBER if rate >= 60 else RED)))
        else:
            cv.itemconfig(self.lbl_send, text="今天还没有发送记录", fill=DIM)

        n429, last429 = st.get("r429", 0), st.get("last_429")
        cv.itemconfig(self.lbl_429, text=str(n429), fill=GREEN if n429 == 0 else FG)
        if last429:
            mins = (datetime.now() - last429).total_seconds() / 60
            color = GREEN if mins > 60 else (AMBER if mins > 10 else RED)
            cv.itemconfig(self.lbl_429_hint,
                          text=f"最近一次 {last429.strftime('%H:%M')}（{int(mins)} 分钟前）", fill=color)
        else:
            cv.itemconfig(self.lbl_429_hint, text="今天一次都没触发 ✅", fill=GREEN)

        cv.itemconfig(self.clock, text=datetime.now().strftime("%H:%M:%S"))

        try:
            lines = []
            if os.path.exists(LOG):
                with open(LOG, encoding="utf-8", errors="replace") as f:
                    lines = [l.rstrip() for l in f.readlines()[-LOG_LINES:]]
            for i, lbl in enumerate(self.log_lines):
                cv.itemconfig(lbl, text=lines[i][:150] if i < len(lines) else "")
        except Exception:
            pass

    # ---------------- 操作 ----------------
    def start_all(self) -> None:
        if not port_alive(8878):
            log("手动启动代理")
            spawn([PY, PROXY], "gw_proxy_restart")
            time.sleep(2)
        if not port_alive(6185):
            log("手动启动 AstrBot")
            spawn([ASTRBOT, "run"], "astrbot_watchdog")
            self.cool["astrbot"] = time.time()
        log("已请求启动全部服务")

    def stop_all(self) -> None:
        for port in (6185, 8878):
            for pid in pids_on_port(port):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
                log(f"已停止 {port} 上的进程 {pid}")

    def restart_astrbot(self) -> None:
        for pid in pids_on_port(6185):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
            log(f"结束 AstrBot 进程 {pid}")
        time.sleep(1)
        spawn([ASTRBOT, "run"], "astrbot_watchdog")
        self.cool["astrbot"] = time.time()
        log("AstrBot 已重新拉起")

    # ---------- 扫码登录 ----------
    def _qq_pids(self) -> list[int]:
        pids = []
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=CREATE_NO_WINDOW).stdout
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    pids.append(int(parts[1]))
        except Exception:
            pass
        return pids

    def _focus_qq(self, pid: int) -> bool:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def cb(hwnd, _):
                p = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            user32.EnumWindows(cb, 0)
            if not found:
                return False
            user32.ShowWindow(found[0], 9)
            user32.SetForegroundWindow(found[0])
            return True
        except Exception:
            return False

    def login_scan(self) -> None:
        acct = snowluma_account()
        if acct:
            log(f"已经登录了：{acct}，不用再扫码")
            return
        pids = self._qq_pids()
        if not pids:
            if os.path.exists(QQ_EXE):
                subprocess.Popen(["explorer.exe", f'"{QQ_EXE}"'], creationflags=CREATE_NO_WINDOW)
                log("QQ 未登录：已打开 QQ，请用手机 QQ 扫描它弹出的二维码")
            else:
                log(f"没找到 QQ 客户端：{QQ_EXE}")
            return
        if self._focus_qq(pids[0]):
            log("QQ 已打开但没登录：已切到前台，用手机 QQ 扫那个二维码")
        else:
            log("QQ 进程在，但没找到它的窗口，手动切到 QQ 看看二维码")

    def reset_password(self) -> None:
        try:
            from astrbot.core.utils.auth_password import hash_dashboard_password
            h = hash_dashboard_password(NEW_PW)
            with open(CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("dashboard", {})
            cfg["dashboard"]["password"] = h
            cfg["dashboard"]["pbkdf2_password"] = h
            cfg["dashboard"]["password_change_required"] = False
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            log(f"面板密码重置为 {NEW_PW}（用户 miantuan），正在重启 AstrBot 生效")
            self.restart_astrbot()
        except Exception as e:  # noqa: BLE001
            log(f"重置密码失败：{e}")

    # ---------------- 开机自启 ----------------
    def _autostart_on(self) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                v, _ = winreg.QueryValueEx(k, RUN_NAME)
                return SELF.lower() in str(v).lower()
        except Exception:
            return False

    def _set_autostart(self, on: bool) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{PYW}" "{SELF}"')
                    log("开机自启：已开启")
                else:
                    try:
                        winreg.DeleteValue(k, RUN_NAME)
                    except FileNotFoundError:
                        pass
                    log("开机自启：已关闭")
        except Exception as e:  # noqa: BLE001
            log(f"开机自启设置失败：{e}")
            self.sw["boot"]["on"] = self._autostart_on()
            self._paint_switches()

    def _close(self) -> None:
        self.alive = False
        log("面板关闭")
        self.destroy()


if __name__ == "__main__":
    if already_running():
        log("已经有一个控制台在跑了，这次不再开第二个")
        raise SystemExit(0)
    Panel().mainloop()
