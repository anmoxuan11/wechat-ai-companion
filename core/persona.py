# -*- coding: utf-8 -*-
"""C3：人设卡自动再蒸馏（闭环，但绝不自动覆盖）

为什么需要它：
  persona.md 是一次性蒸馏的产物，而人的说话习惯会变——更糟的是它会被 AI
  自己的回复反向影响（回声室）：AI 用它生成回复，回复进日志，下次蒸馏再把
  AI 的话学回来，人设卡越来越像 AI 而不是你。

所以这里做两件事：
  1. 蒸馏输入**只取 is_ai=0 的消息**，AI 代发的一条都不进样本；
  2. 生成的是**草案**，写进 data/persona_proposal.md，主人点头才覆盖。
     覆盖前自动备份旧卡到 data/persona.md.bak。
"""

import json
import os
import time

from core import atomicio, paths, store
from core.security import refresh_env_from_registry

MIN_SAMPLES = 40          # 少于这个数不值得蒸馏（AI 会开始编）
DEFAULT_SAMPLE_LIMIT = 400


def _paths():
    base = paths.base_dir()
    return (
        os.path.join(base, "data", "persona.md"),
        os.path.join(base, "data", "persona_proposal.md"),
        os.path.join(base, "data", "persona.md.bak"),
    )


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def build_samples(limit=DEFAULT_SAMPLE_LIMIT, chat=None):
    """取最近的真人对话片段（排除 AI 代发），拼成 `谁: 内容` 形式。"""
    rows = store.messages(chat=chat, include_ai=False, limit=limit)
    out = []
    for r in rows:
        who = (r.get("who") or "").strip()
        content = (r.get("content") or "").strip()
        if not content or content in ("[图片]", "[语音]", "[视频]"):
            continue
        out.append("%s: %s" % (who or "?", content))
    return out


def _load_cfg():
    refresh_env_from_registry()
    return atomicio.read_json(os.path.join(paths.base_dir(), "config.json"), {}) or {}


def propose(limit=DEFAULT_SAMPLE_LIMIT, chat=None):
    """生成新人设卡草案。不覆盖现有卡片，只落盘草案并等你点头。"""
    samples = build_samples(limit=limit, chat=chat)
    if len(samples) < MIN_SAMPLES:
        return {"ok": False,
                "msg": "可用样本只有 %d 条（至少需要 %d 条），先多聊几天再来"
                       % (len(samples), MIN_SAMPLES)}

    cfg = _load_cfg()
    my_name = cfg.get("my_name", "我")
    partner = cfg.get("partner_name", "对象")
    cur_path, prop_path, _ = _paths()
    current = _read(cur_path)

    # 只喂真人发言：把 AI 代发的行剔掉已经在 build_samples 里做了（is_ai=0）
    dialog = "\n".join(samples[-limit:])
    prompt = (
        "下面是「%s」和「%s」的真实微信对话片段。**其中所有署名 %s 的消息"
        "都是他本人手打的**，没有一句是 AI 生成的。\n\n"
        "对话片段：\n%s\n\n"
        "请你以第三人称，为「%s」重写一份说话风格人设卡（200-350 字），供 AI 模仿他时使用。要求：\n"
        "1. 只描述他的语气、用词、标点、表情习惯、对对象的称呼；\n"
        "2. 指出他的回复节奏（秒回 / 简短 / 爱连发几条）；\n"
        "3. 如果他近期风格有变化，以近期为准；\n"
        "4. 只输出人设卡正文，不要标题、不要解释。\n"
        "[现有卡片（供参考，可以推翻）：\n%s]"
        % (my_name, partner, my_name, dialog[-6000:], my_name, current[:1500])
    )

    from ai_client import AIClient
    ai = AIClient(cfg["ai"])
    text = (ai.chat([{"role": "user", "content": prompt}]) or "").strip()
    if not text:
        return {"ok": False, "msg": "AI 没返回内容，可能密钥或网络有问题"}

    ok, err = atomicio.atomic_write_text(prop_path, text, backup=False)
    if not ok:
        return {"ok": False, "msg": "草案写入失败: %s" % err}
    return {"ok": True, "proposal": text, "current": current,
            "samples": len(samples), "ts": time.time()}


def apply_proposal():
    """采纳草案：先备份旧卡，再覆盖。没有草案就报错。"""
    cur_path, prop_path, bak_path = _paths()
    proposal = _read(prop_path)
    if not proposal:
        return {"ok": False, "msg": "还没有待采纳的草案，先点「重新蒸馏」"}
    current = _read(cur_path)
    if current:
        atomicio.atomic_write_text(bak_path, current, backup=False)
    ok, err = atomicio.atomic_write_text(cur_path, proposal, backup=True)
    if not ok:
        return {"ok": False, "msg": "写入失败: %s" % err}
    try:
        os.remove(prop_path)
    except Exception:
        pass
    return {"ok": True, "msg": "人设卡已更新（旧卡已备份到 persona.md.bak），下次回复立即生效",
            "persona": proposal}


def status():
    cur_path, prop_path, _ = _paths()
    proposal = _read(prop_path)
    samples = len(build_samples())
    out = {"ok": True, "has_proposal": bool(proposal), "proposal": proposal,
           "current": _read(cur_path), "samples": samples,
           "min_samples": MIN_SAMPLES}
    try:
        out["generated_at"] = os.path.getmtime(prop_path) if proposal else 0
    except Exception:
        out["generated_at"] = 0
    return out
