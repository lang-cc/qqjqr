"""qqjqr 看门狗：盯着排队代理(8878)、AstrBot(6185)、SnowLuma(3000)，挂了自动拉起。

- 每 20 秒体检一次
- 代理挂了 → 重起 tools/gw_proxy.py
- AstrBot 挂了 → 重起 astrbot.exe run（窗口隐藏）
- SnowLuma(3000) 挂了 → 只记日志告警（QQ 需要桌面会话，不自动重启，免得把你挤下线）
- 单实例：靠 8899 端口占位，重复启动会自己退出
- 日志：data/logs/watchdog.log（超过 5MB 自动截断）
"""
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
ASTRBOT = os.path.join(ROOT, ".venv", "Scripts", "astrbot.exe")
LOGDIR = os.path.join(ROOT, "data", "logs")
LOG = os.path.join(LOGDIR, "watchdog.log")
LOCK_PORT = 8899
CHECK_INTERVAL = 20
CREATE_NO_WINDOW = 0x08000000

TARGETS = {
    "proxy": {"port": 8878, "desc": "排队代理"},
    "astrbot": {"port": 6185, "desc": "AstrBot"},
    "snowluma": {"port": 3000, "desc": "SnowLuma"},
}
restarts = {"proxy": 0, "astrbot": 0, "snowluma": 0}


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 5 * 1024 * 1024:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_alive(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def acquire_lock() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def spawn(cmd: list[str], out_name: str) -> None:
    out = os.path.join(LOGDIR, out_name)
    with open(out, "ab") as f:
        subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=f,
            stderr=f,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )


def start_proxy() -> None:
    spawn([PY, os.path.join(ROOT, "tools", "gw_proxy.py")], "gw_proxy_restart.log")
    log("已拉起排队代理 (gw_proxy.py)")


def start_astrbot() -> None:
    spawn([ASTRBOT, "run"], "astrbot_watchdog.log")
    log("已拉起 AstrBot (astrbot.exe run)")


def main() -> None:
    lock = acquire_lock()
    if lock is None:
        print("[watchdog] 已有实例在跑（8899 被占用），退出", flush=True)
        return

    log("=" * 50)
    log(f"看门狗启动：监控 8878/6185/3000，每 {CHECK_INTERVAL}s 一次")
    down_count = {k: 0 for k in TARGETS}

    while True:
        try:
            for name, info in TARGETS.items():
                port = info["port"]
                if port_alive(port):
                    if down_count[name]:
                        log(f"{info['desc']}({port}) 恢复正常")
                        down_count[name] = 0
                    continue

                down_count[name] += 1
                log(f"⚠ {info['desc']}({port}) 不在监听，第 {down_count[name]} 次探测")

                if name == "proxy":
                    if not port_alive(8878):
                        start_proxy()
                        restarts["proxy"] += 1
                        time.sleep(3)
                elif name == "astrbot":
                    if not port_alive(6185):
                        start_astrbot()
                        restarts["astrbot"] += 1
                        time.sleep(20)
                else:  # snowluma
                    if down_count[name] == 3:
                        log("SnowLuma 不在线：需要桌面会话里 QQ 还开着。"
                            "可在会话里执行：Start-Process explorer.exe '\"C:\\Program Files\\Tencent\\QQNT\\QQ.exe\"'")
                        down_count[name] = 0
        except Exception as e:  # noqa: BLE001
            log(f"看门狗自身异常：{type(e).__name__}: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("看门狗退出（手动中断）")
        sys.exit(0)
