# -*- coding: utf-8 -*-
"""G2：配置热重载的「判定」部分

为什么需要：改完 config.json 要重启机器人，而重启意味着微信监听要重建，
中间有几秒到十几秒的空窗，正好可能漏掉她发来的消息。大部分配置（冷却时间、
上限、语气滑杆、非文本策略、监听对象）其实根本不需要重建连接就能生效。

这个模块只回答两个问题：
  1. 两份配置之间到底哪些字段变了（精确到点号路径）；
  2. 这些变更里，哪些能热生效、哪些必须重启。

真正的「应用」在 main.py 的 Bot._apply_hot 里——判定和应用分开，
是因为判定可以单测，应用牵扯到活着的进程状态。

必须重启的三块：backend（微信后端）、ai（模型与密钥）、vision（视觉模型）。
它们都在启动时建立了长连接 / 客户端实例，换掉就得重建。
"""

# 顶层 key -> 中文名（用在事件时间线和面板提示里）
TOP_LABEL = {
    "backend": "微信后端", "ai": "AI 模型与密钥", "vision": "视觉模型",
    "safety": "安全与频率", "guard": "内容护栏", "media": "非文本消息策略",
    "style": "说话风格", "proactive": "主动发起", "listen": "监听对象",
    "control_chat": "控制会话", "cost": "成本上限", "schedule": "定时任务",
    "memory": "记忆", "panel": "面板",
}

COLD_TOP = ("backend", "ai", "vision")


def flatten(d, prefix=""):
    """把嵌套 dict 拍平成 {点号路径: 值}。列表按整体比较。"""
    out = {}
    if not isinstance(d, dict):
        return {prefix: d} if prefix else {}
    for k, v in d.items():
        path = "%s.%s" % (prefix, k) if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(flatten(v, path))
        else:
            out[path] = v
    return out


def diff(old, new):
    """比较两份配置，返回 [(点号路径, 旧值, 新值)]"""
    a, b = flatten(old or {}), flatten(new or {})
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            out.append((k, a.get(k), b.get(k)))
    return out


def classify(old, new):
    """把差异分成热更新 / 需重启两组。

    返回 {"hot": [...], "cold": [...], "hot_paths": [...], "cold_paths": [...]}
    （前两项是给人和日志看的中文标签，后两项是原始路径）
    """
    changes = diff(old, new)
    hot, cold, hot_p, cold_p = [], [], [], []
    for path, _o, _n in changes:
        top = path.split(".")[0]
        label = TOP_LABEL.get(top, top)
        if top in COLD_TOP:
            if label not in cold:
                cold.append(label)
            cold_p.append(path)
        else:
            if label not in hot:
                hot.append(label)
            hot_p.append(path)
    return {"hot": hot, "cold": cold, "hot_paths": hot_p, "cold_paths": cold_p,
            "changed": len(changes)}


def needs_restart(old, new):
    return bool(classify(old, new)["cold"])


def describe(result):
    """把 classify 的结果写成一句人话"""
    if not result["changed"]:
        return "配置没有变化"
    parts = []
    if result["hot"]:
        parts.append("已热更新：" + "、".join(result["hot"]))
    if result["cold"]:
        parts.append("需要重启才生效：" + "、".join(result["cold"]))
    return "；".join(parts)
