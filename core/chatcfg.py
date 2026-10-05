# -*- coding: utf-8 -*-
"""B8：多监听对象各自独立的配置

为什么需要：config.listen 现在是个字符串列表，所有对象共用一套配置；
更糟的是定时任务 / 早安消息只发给**第一个**（`_sched_target` 直接 next(iter(...))）。
现在只有一个对象所以没出事，但这是迟早会咬人的那类 bug——加了第二个对象，
早安就会发错人。

怎么做：listen 的每一项可以是字符串（老格式，继续支持），也可以是对象：
    {"name": "伴侣示例", "enabled": true, "cooldown": 20,
     "persona_file": "data/persona_example.md", "proactive": true, "note": "..."}

没有单独配置的字段一律回落到全局配置，所以**老 config 不用迁移**——
这也是为什么没有选「强制改成对象数组」那条路：数据结构大改最容易在半夜出问题。

另外补一个顶层 `chat_overrides: {名字: {...}}`，方便面板改配置时只写差异，
不必把 listen 里的每一项都展开成对象。
"""

import copy
import os

from core import paths

# 允许在 listen 对象 / chat_overrides 里单独设置的字段
PER_CHAT_KEYS = ("enabled", "cooldown", "daily_limit", "persona_file",
                 "relationship_file", "proactive", "schedule", "note")


class ChatConfig:
    """一个监听对象的有效配置（已把回落值算好）"""

    def __init__(self, name, raw=None, defaults=None):
        raw = raw or {}
        d = defaults or {}
        self.name = name
        self.enabled = bool(raw.get("enabled", True))
        self.cooldown = raw.get("cooldown", d.get("cooldown"))
        self.daily_limit = raw.get("daily_limit", d.get("daily_limit"))
        self.persona_file = raw.get("persona_file") or ""
        self.relationship_file = raw.get("relationship_file") or ""
        self.proactive = bool(raw.get("proactive", d.get("proactive", True)))
        self.schedule = bool(raw.get("schedule", True))
        self.note = raw.get("note", "") or ""
        self.raw = dict(raw)

    def to_dict(self):
        return {
            "name": self.name, "enabled": self.enabled,
            "cooldown": self.cooldown, "daily_limit": self.daily_limit,
            "persona_file": self.persona_file,
            "relationship_file": self.relationship_file,
            "proactive": self.proactive, "schedule": self.schedule,
            "note": self.note, "customized": bool(self.raw),
        }


def _defaults(cfg):
    safety = cfg.get("safety", {}) or {}
    return {
        "cooldown": safety.get("cooldown_seconds", 20),
        "daily_limit": safety.get("daily_reply_limit", 200),
        "proactive": bool((cfg.get("proactive", {}) or {}).get("enabled", False)),
    }


def _entry_name(item):
    if isinstance(item, dict):
        return (item.get("name") or "").strip()
    return str(item).strip()


def normalize(cfg):
    """把 config 里的 listen（+ chat_overrides）整理成 {名字: ChatConfig}"""
    defaults = _defaults(cfg)
    overrides = cfg.get("chat_overrides") or {}
    out = {}
    for item in (cfg.get("listen") or []):
        name = _entry_name(item)
        if not name:
            continue
        raw = {}
        if isinstance(item, dict):
            raw.update({k: v for k, v in item.items()
                        if k in PER_CHAT_KEYS and k != "name"})
        ov = overrides.get(name)
        if isinstance(ov, dict):
            raw.update({k: v for k, v in ov.items() if k in PER_CHAT_KEYS})
        out[name] = ChatConfig(name, raw, defaults)
    return out


def names(cfg):
    """配置里出现的所有监听对象名字（不管启用与否）"""
    return list(normalize(cfg).keys())


def enabled_names(cfg):
    return [n for n, c in normalize(cfg).items() if c.enabled]


def resolve_path(raw_path):
    """把配置里的相对路径补成绝对路径；空值返回空串"""
    if not raw_path:
        return ""
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.join(paths.base_dir(), raw_path)


def update(cfg, name, patch):
    """改某个对象的独立配置，写回 cfg（**原地修改**）。

    只把被改的字段写进 listen 的对应项，没改过的项保持字符串原样——
    这样老 config 不会在一次面板操作后全被展开成对象。
    """
    if not name:
        return False, "缺少对象名"
    patch = {k: v for k, v in (patch or {}).items() if k in PER_CHAT_KEYS}
    if not patch:
        return False, "没有可修改的字段"

    listen = cfg.setdefault("listen", [])
    idx = None
    for i, item in enumerate(listen):
        if _entry_name(item) == name:
            idx = i
            break
    if idx is None:
        return False, "监听列表里没有 [%s]" % name

    cur = listen[idx]
    if isinstance(cur, dict):
        new_entry = copy.deepcopy(cur)
        new_entry.pop("_说明", None)
    else:
        new_entry = {"name": name}
    new_entry.update(patch)
    listen[idx] = new_entry
    # 顶层 overrides 里若还有同名字段，清掉，避免两处冲突
    ov = cfg.get("chat_overrides")
    if isinstance(ov, dict) and name in ov:
        ov.pop(name, None)
    return True, "已更新 [%s]" % name


def remove(cfg, name):
    """从监听列表里移除一个对象"""
    listen = cfg.get("listen") or []
    left = [it for it in listen if _entry_name(it) != name]
    if len(left) == len(listen):
        return False, "监听列表里没有 [%s]" % name
    cfg["listen"] = left
    return True, "已移除 [%s]" % name


def add(cfg, name):
    """往监听列表里加一个对象（已存在则不动）"""
    name = (name or "").strip()
    if not name:
        return False, "对象名不能为空"
    listen = cfg.setdefault("listen", [])
    for it in listen:
        if _entry_name(it) == name:
            return False, "[%s] 已经在监听列表里" % name
    listen.append(name)
    return True, "已添加 [%s]" % name
