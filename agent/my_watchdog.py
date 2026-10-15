import json
import os
import smtplib
import subprocess
import threading
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

# 本地持久化配置路径（相对 agent 进程 CWD，即项目根目录；config 目录已被 .gitignore 忽略）
CONFIG_PATH = os.path.join("assets", "config", "watchdog_config.json")


def _load_local_config() -> dict:
    """读取本地已保存的配置；不存在或非法时返回空 dict。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_local_config(cfg: dict) -> None:
    """把配置保存到本地文件（含 SMTP 授权码）。"""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[挂机保护] 本地配置保存失败: {exc}")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "y", "true", "1", "开", "是")
    return bool(value)


def _gray_fingerprint(img: np.ndarray, grid: int = 16) -> np.ndarray:
    """把截图降采样为 grid×grid 的灰度指纹（块平均），用于判断画面是否变化。"""
    if img is None:
        return np.zeros((grid, grid), dtype=np.float32)
    gray = img.astype(np.float32).mean(axis=2) if img.ndim == 3 else img.astype(np.float32)
    h, w = gray.shape
    bh, bw = max(1, h // grid), max(1, w // grid)
    fp = np.zeros((grid, grid), dtype=np.float32)
    for gy in range(grid):
        y0 = min(gy * bh, h)
        y1 = min((gy + 1) * bh, h)
        for gx in range(grid):
            x0 = min(gx * bw, w)
            x1 = min((gx + 1) * bw, w)
            block = gray[y0:y1, x0:x1]
            fp[gy, gx] = block.mean() if block.size else 0.0
    return fp


def _send_email(cfg: dict) -> None:
    host = str(cfg.get("smtp_host") or "smtp.qq.com").strip()
    port = _as_int(cfg.get("smtp_port"), 465)
    user = str(cfg.get("smtp_user") or "").strip()
    code = str(cfg.get("smtp_code") or "").strip()
    recipient = str(cfg.get("recipient") or "").strip()
    if not (user and code and recipient):
        print("[挂机保护] 邮件未发送：发件邮箱/授权码/收件邮箱未填全")
        return
    subject = "挂机保护提醒：进程长时间无变化"
    body = (
        "挂机保护检测到游戏画面/进程已超过设定时间没有变化（可能已卡死）。\n\n"
        "请及时查看游戏运行状态。\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("MAA 挂机保护", user))
    msg["To"] = recipient
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        server.login(user, code)
        server.sendmail(user, [recipient], msg.as_string())
        server.quit()
        print(f"[挂机保护] 已发送通知邮件到 {recipient}")
    except Exception as exc:
        print(f"[挂机保护] 邮件发送失败: {exc}")


def _do_shutdown() -> None:
    # 延迟 30 秒关机，留出日志落盘/邮件发送时间
    delay = 30
    try:
        subprocess.run(["shutdown", "/s", "/t", str(delay)], check=False)
        print(f"[挂机保护] 已触发自动关机（{delay} 秒后执行）")
    except Exception as exc:
        print(f"[挂机保护] 触发关机失败: {exc}")


class _IdleWatchdog:
    """后台线程：定时截图对比，判断画面是否长时间无变化。"""

    def __init__(self, context: Context):
        try:
            self._ctx = context.clone()
        except Exception:
            self._ctx = context
        self._cfg: dict = {}
        self._lock = threading.Lock()
        self._last_fp = None
        self._last_change_ts = time.time()
        self._fired_email = False
        self._fired_shutdown = False

    def update_config(self, cfg: dict) -> None:
        with self._lock:
            self._cfg = dict(cfg)

    def run(self) -> None:
        interval = 10  # 每 10 秒检查一次
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(interval)

    def _tick(self) -> None:
        with self._lock:
            cfg = dict(self._cfg)
        try:
            job = self._ctx.tasker.controller.post_screencap()
            job.wait()
            img = job.get()
        except Exception:
            return
        fp = _gray_fingerprint(img)
        now = time.time()
        prev = self._last_fp
        self._last_fp = fp
        if prev is None:
            self._last_change_ts = now
            return
        diff = float(np.abs(fp - prev).mean())
        threshold = float(cfg.get("diff_threshold", 1.5))
        if diff > threshold:
            # 画面有变化：重置计时与触发标记
            self._last_change_ts = now
            self._fired_email = False
            self._fired_shutdown = False
            return
        # 画面持续没变化
        idle_minutes = _as_int(cfg.get("idle_minutes"), 10)
        if (now - self._last_change_ts) / 60.0 < idle_minutes:
            return
        if _as_bool(cfg.get("email_enabled")) and not self._fired_email:
            self._fired_email = True
            self._notify(cfg)
        if _as_bool(cfg.get("shutdown_enabled")) and not self._fired_shutdown:
            self._fired_shutdown = True
            self._shutdown()

    def _notify(self, cfg: dict) -> None:
        _send_email(cfg)

    def _shutdown(self) -> None:
        _do_shutdown()


@AgentServer.custom_action("进程监测")
class IdleMonitorAction(CustomAction):
    _watchdog = None
    _start_lock = threading.Lock()

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = json.loads(argv.custom_action_param) if argv.custom_action_param else {}
        except Exception:
            params = {}
        # 以本地已保存的配置为底，用 GUI 当前传入的值覆盖（GUI 传入优先）
        merged = _load_local_config()
        for key, val in params.items():
            if val not in (None, ""):
                merged[key] = val
        # 保存到本地：输入框内容 + 选项（开关、时长、SMTP、授权码、收件邮箱）全部落盘
        _save_local_config(merged)
        with IdleMonitorAction._start_lock:
            if IdleMonitorAction._watchdog is None:
                watchdog = _IdleWatchdog(context)
                IdleMonitorAction._watchdog = watchdog
                threading.Thread(target=watchdog.run, daemon=True).start()
            IdleMonitorAction._watchdog.update_config(merged)
        return True
