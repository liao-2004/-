"""卡死守护自定义动作

功能：
  1. 检测游戏"卡死"：当流水线持续识别失败（连续失败达 stuck_seconds 秒）时判定卡死。
  2. 发邮件通知：通过 QQ 邮箱 SMTP 发送邮件（发件邮箱 / SMTP 授权码 / 收件人）。
  3. 自动关机：卡死后延迟 shutdown_minutes 分钟自动关机（Windows shutdown /s /t）。

配置通过流水线节点的 custom_action_param（JSON 字符串）传入，
通常由 interface.json 的 global_option 输入框注入。

默认不启用（enable=false），只有用户在界面开启开关后才生效。
"""

import json
import subprocess
import threading
import time
import traceback

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

# 跨多次调用保留的状态
_STATE = {
    "first_error": None,  # 连续失败起始时间戳
    "last_call": None,    # 上次调用时间戳
    "triggered": False,   # 本次卡死是否已触发过动作
    "lock": threading.Lock(),
}

_MAIL_SMTP_HOST = "smtp.qq.com"
_MAIL_SMTP_PORT = 465


def _load_cfg(raw: str) -> dict:
    """解析 custom_action_param，失败时返回空 dict。"""
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _as_float(cfg: dict, key: str, default: float) -> float:
    try:
        val = cfg.get(key, default)
        return float(val)
    except Exception:
        return default


def _as_str(cfg: dict, key: str) -> str:
    val = cfg.get(key, "")
    return str(val).strip() if val is not None else ""


def _send_mail(sender: str, smtp_code: str, recipient: str) -> bool:
    """通过 QQ 邮箱 SMTP 发送邮件。任何异常都吞掉并返回 False。"""
    try:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        subject = "【游戏卡死通知】请及时处理"
        body = (
            f"您的游戏自动化在 {now_str} 判定为卡死。\n"
            "可能原因：游戏出现了无法识别的界面，自动化无法继续。\n"
            "请及时检查游戏状态并处理。\n"
        )

        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = Header(subject, "utf-8")

        with smtplib.SMTP_SSL(_MAIL_SMTP_HOST, _MAIL_SMTP_PORT, timeout=20) as smtp:
            smtp.login(sender, smtp_code)
            smtp.sendmail(sender, [recipient], msg.as_string())
        return True
    except Exception:
        print("[卡死守护] 发送邮件失败：")
        traceback.print_exc()
        return False


def _do_shutdown(minutes: float) -> bool:
    """排定 Windows 自动关机。minutes<=0 表示不关机。"""
    if not minutes or minutes <= 0:
        return False
    try:
        seconds = int(round(minutes * 60))
        subprocess.run(
            ["shutdown", "/s", "/t", str(seconds), "/f"],
            check=False,
            timeout=15,
        )
        print(f"[卡死守护] 已排定 {minutes:.0f} 分钟后自动关机（shutdown /s /t {seconds}）")
        return True
    except Exception:
        print("[卡死守护] 排定自动关机失败：")
        traceback.print_exc()
        return False


@AgentServer.custom_action("卡死检测")
class StuckGuardAction(CustomAction):
    """卡死检测自定义动作。

    挂载在流水线的 on_error 兜底链上：当流水线持续识别失败并反复进入
    on_error 时，本动作会被反复调用，从而累计"连续失败时长"。
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        cfg = _load_cfg(argv.custom_action_param)

        enable = bool(cfg.get("enable", False))
        stuck_seconds = _as_float(cfg, "stuck_seconds", 60.0)
        shutdown_minutes = _as_float(cfg, "shutdown_minutes", 0.0)
        sender = _as_str(cfg, "sender_email")
        smtp_code = _as_str(cfg, "smtp_code")
        recipient = _as_str(cfg, "recipient")

        now = time.time()
        with _STATE["lock"]:
            st = _STATE

            # 判定"连续失败"的间隔阈值：超过该间隔再次被调用，视为一次新的卡死周期
            gap_reset = max(stuck_seconds * 1.5, 30.0)

            # 与上次调用间隔过大 => 中间大概率恢复过正常识别，重置
            if st["last_call"] is not None and (now - st["last_call"]) > gap_reset:
                st["first_error"] = None
                st["triggered"] = False

            if st["first_error"] is None:
                st["first_error"] = now
            st["last_call"] = now

            # 是否已连续失败达到阈值
            stuck_now = (now - st["first_error"]) >= stuck_seconds
            should_trigger = enable and stuck_now and not st["triggered"]
            if should_trigger:
                st["triggered"] = True

        if should_trigger:
            print(
                f"[卡死守护] 检测到游戏卡死（连续失败 ≥ {stuck_seconds:.0f} 秒）。"
                f"enable={enable}, 关机延迟={shutdown_minutes:.0f} 分钟"
            )
            if sender and smtp_code and recipient:
                _send_mail(sender, smtp_code, recipient)
            if enable:
                _do_shutdown(shutdown_minutes)

        # 始终返回 True，保证不影响原有流水线流程
        return True
