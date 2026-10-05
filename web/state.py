# -*- coding: utf-8 -*-
"""状态聚合与自检（2.0）

从 1.0 的 ui_server.py 抽出来的「状态聚合 / 自检」部分。

2.0 变化：
  - uptime 改用 ctypes GetProcessTimes（不再调 PowerShell）
  - 自检新增「解释器可解析」「密钥来源」「单实例」三项
  - 密钥展示脱敏
"""

import os
import time

from core import (  # noqa: F401
    alive, audit_config_secrets, base_dir, find_python, is_placeholder,
    mask_key, proc_tree_mem_mb, proc_start_time, resolve_api_key, tail_text,
    ENV_API_KEY,
)

SELF = os.getpid()


def _fmt_uptime(seconds):
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%d天%d小时%d分" % (d, h, m)
    if h:
        return "%d小时%d分" % (h, m)
    if m:
        return "%d分%d秒" % (m, s)
    return "%d秒" % s


def uptime_str(pid):
    """bot 运行时长：纯 ctypes 读进程创建时间，无缓存也无妨（毫秒级）。"""
    start = proc_start_time(pid)
    if not start:
        return "—"
    return _fmt_uptime(time.time() - start)


def python_info():
    """解释器信息（供状态与自检展示）。"""
    py, src = find_python()
    return {"path": py, "source": src, "ok": bool(py)}


def build_status(cfg, ctrl, bot_status, pid, log_paths, extra=None):
    """聚合面板状态。

    cfg         : config.json 内容
    ctrl        : bot_control.json 内容
    bot_status  : data/bot_status.json（bot 心跳）
    pid         : 当前 bot PID 或 None
    log_paths   : [主日志, 控制台日志]
    """
    running = bool(pid) and alive(pid)
    paused = bot_status.get("paused") if bot_status.get("pid") else bool(
        ctrl.get("paused", False))

    py = python_info()
    keys = audit_config_secrets(cfg)
    ai_key, ai_src = resolve_api_key(
        (cfg.get("ai") or {}).get("api_key", ""), ENV_API_KEY)

    st = {
        "running": running,
        "paused": bool(paused),
        "pid": pid,
        "bot_mem_mb": proc_tree_mem_mb(pid) if running else None,
        "panel_mem_mb": proc_tree_mem_mb(SELF),
        "listen": cfg.get("listen", []),
        "control_chat": cfg.get("control_chat", "文件传输助手"),
        "partner_name": cfg.get("partner_name", ""),
        "uptime": uptime_str(pid) if running else "—",
        "wechat": wechat_status(bot_status, log_paths) if running else "未运行",
        "python": py["path"],
        "python_source": py["source"],
        "python_ok": py["ok"],
        "api_key_source": ai_src,
        "api_key_masked": mask_key(ai_key),
        "api_key_ok": not is_placeholder(ai_key),
        "base_dir": base_dir(),
        "version": "3.0",
    }
    if extra:
        st.update(extra)
    return st


def wechat_status(bot_status, log_paths):
    """微信连接状态：优先 bot 心跳自报，日志文本仅兜底。"""
    if bot_status.get("pid") and bot_status.get("wechat_ready"):
        text = "".join(tail_text(p, 100) for p in log_paths)
        if "微信窗口不可见" in text or "发送失败" in text:
            return "已连接（发送受限：微信窗口不可见）"
        return "已连接"
    text = "".join(tail_text(p, 300) for p in log_paths)
    if "后端初始化失败" in text:
        return "异常"
    connected = ("UIA 驱动已启用" in text or "UIA 树不可用" in text
                 or "消息监听线程已启动" in text or "后端就绪" in text)
    if connected:
        return "已连接"
    return "连接中…"


def self_test_items(cfg, ctrl, bot_status, pid, log_paths, control_roundtrip):
    """自检项。control_roundtrip: callable() -> bool（验证控制文件可写）。"""
    items = []
    running = bool(pid) and alive(pid)

    # 1 bot 进程
    if running:
        age = int(time.time() - bot_status.get("ts", 0)) if bot_status.get("pid") else -1
        items.append({"name": "bot 进程存活", "ok": True,
                      "detail": "PID %s（心跳 %s 秒前）" % (pid, age), "fix": "restart"})
    else:
        items.append({"name": "bot 进程存活", "ok": False,
                      "detail": "未检测到运行中的 bot", "fix": "start"})

    # 2 控制文件读写
    try:
        ok = bool(control_roundtrip())
        items.append({"name": "控制文件读写", "ok": ok,
                      "detail": "读→写→读 往返一致" if ok else "往返不一致",
                      "fix": None if ok else "rebuild_control"})
    except Exception as e:
        items.append({"name": "控制文件读写", "ok": False,
                      "detail": "异常: %r" % e, "fix": "rebuild_control"})

    # 3 config.json
    items.append({"name": "config.json", "ok": bool(cfg),
                  "detail": "监听: %s" % "、".join(cfg.get("listen", [])) if cfg else "读取失败",
                  "fix": None if cfg else "rebuild_config"})

    # 4 心跳新鲜度
    heartbeat_age = int(time.time() - bot_status.get("ts", 0)) if bot_status.get("ts") else -1
    try:
        log_age = int(time.time() - max(os.path.getmtime(p) for p in log_paths
                                        if os.path.exists(p)))
    except (OSError, ValueError):
        log_age = -1
    if running and 0 <= heartbeat_age < 120:
        items.append({"name": "日志心跳", "ok": True,
                      "detail": "bot 心跳 %d 秒前" % heartbeat_age, "fix": None})
    else:
        ok = (0 <= log_age < 180)
        items.append({"name": "日志心跳", "ok": ok,
                      "detail": ("日志 %d 秒前有更新" % log_age) if ok
                      else ("日志已 %d 秒未更新" % log_age if log_age >= 0 else "无法读取日志"),
                      "fix": None if ok else "restart"})

    # 5 微信连接
    wc = wechat_status(bot_status, log_paths) if running else "未运行"
    wc_ok = (wc == "已连接")
    items.append({"name": "微信连接", "ok": wc_ok, "detail": wc,
                  "fix": None if wc_ok else ("start" if not running else "restart")})

    # 6 暂停状态同步
    ctrl_paused = bool(ctrl.get("paused", False))
    bot_paused = bool(bot_status.get("paused", False)) if bot_status.get("pid") else None
    if bot_paused is None:
        items.append({"name": "暂停状态同步", "ok": not running,
                      "detail": "bot 未运行，无同步对象", "fix": None})
    else:
        ok = ctrl_paused == bot_paused
        items.append({"name": "暂停状态同步", "ok": ok,
                      "detail": "控制文件=%s 心跳=%s" % (ctrl_paused, bot_paused),
                      "fix": None if ok else "sync_pause"})

    # 7 解释器（2.0 新增：1.0 硬编码路径，换电脑必崩）
    py = python_info()
    items.append({"name": "Python 解释器", "ok": py["ok"],
                  "detail": ("%s（%s）" % (py["path"], py["source"])) if py["ok"]
                  else "未找到可用解释器",
                  "fix": None if py["ok"] else "hint_python"})

    # 8 密钥来源（2.0 新增：提示明文风险）
    secrets = audit_config_secrets(cfg)
    plain = [s["name"] for s in secrets if s["plaintext_in_config"]]
    items.append({
        "name": "API 密钥",
        "ok": not plain,
        "detail": ("明文写在 config.json：%s（建议改用环境变量）" % "、".join(plain))
        if plain else "均通过环境变量或脱敏配置提供",
        "fix": None if not plain else "hint_key",
    })

    passed = sum(1 for i in items if i["ok"])
    return {"ok": passed == len(items), "passed": passed,
            "total": len(items), "items": items}


def text_report(result, status):
    """自检的文本报告（供 --self-test 命令行输出）。"""
    lines = []
    lines.append("微信 AI 陪聊控制面板 · 自检报告")
    lines.append("版本 2.0    生成时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("项目目录 %s" % base_dir())
    lines.append("")
    lines.append("[运行状态]")
    for k in ("running", "paused", "pid", "uptime", "wechat", "listen",
              "python_source", "api_key_source"):
        if k in status:
            lines.append("  %-14s %s" % (k, status[k]))
    lines.append("")
    lines.append("[检查项] %d/%d 通过" % (result["passed"], result["total"]))
    for i in result["items"]:
        mark = "OK  " if i["ok"] else "FAIL"
        lines.append("  %s %-12s %s" % (mark, i["name"], i["detail"]))
    lines.append("")
    lines.append("结论：" + ("全部通过" if result["ok"] else "存在未通过项，见上"))
    return "\n".join(lines)
