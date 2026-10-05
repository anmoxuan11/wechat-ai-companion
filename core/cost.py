# -*- coding: utf-8 -*-
"""Token 用量与成本统计（3.0）

解决的问题（2.0 的盲区）：
  AI 每句话到底花了多少钱、今天调了多少次、是不是缓存命中了——完全看不到。
  跑一天不知道花了几毛还是几十块，出问题也不知道是请求量上去了还是 prompt 变长了。

做法：
  - AIClient 每次调用把 usage 交回 core.usage-cost 计算
  - 结果落到 SQLite（core.store.usage），面板按天/按模型看曲线
  - 价格表可在 config.json 的 cost.prices 里覆盖，**单位统一为「元 / 百万 tokens」**

 DeepSeek 官方定价（2025-09 起，人民币 / 百万 tokens）：
   deepseek-chat     输入 2（缓存未命中）/ 0.5（命中），输出 8
   deepseek-reasoner 输入 4（缓存未命中）/ 1（命中），输出 16
 这里给的是「够用的默认估计」，拿不准的模型走默认档并标记 estimated。
"""

from .paths import base_dir

# 默认价格表：元 / 百万 tokens
DEFAULT_PRICES = {
    "deepseek-chat":      {"in": 2.0, "cache": 0.5, "out": 8.0},
    "deepseek-reasoner":  {"in": 4.0, "cache": 1.0, "out": 16.0},
    "deepseek-v4-flash-vision-exp": {"in": 2.0, "cache": 0.5, "out": 8.0},
    "glm-4-flash":        {"in": 0.1, "cache": 0.1, "out": 0.1},
    "glm-4-plus":         {"in": 5.0, "cache": 5.0, "out": 5.0},
    "doubao-pro-32k":     {"in": 0.8, "cache": 0.8, "out": 2.0},
    "gpt-4o-mini":        {"in": 1.1, "cache": 0.55, "out": 4.4},
}
FALLBACK = {"in": 2.0, "cache": 0.5, "out": 8.0}

_PRICES = dict(DEFAULT_PRICES)
_LOADED = False


def load_prices(cfg=None):
    """从 config.json 的 cost.prices 加载自定义价格（单位：元/百万 tokens）。"""
    global _PRICES, _LOADED
    if _LOADED:
        return _PRICES
    _LOADED = True
    try:
        if cfg is None:
            import json
            import os
            with open(os.path.join(base_dir(), "config.json"), "r", encoding="utf-8") as f:
                cfg = json.load(f)
        custom = (cfg or {}).get("cost", {}).get("prices", {})
        if isinstance(custom, dict):
            for model, p in custom.items():
                if isinstance(p, dict):
                    _PRICES[str(model)] = {
                        "in": float(p.get("in", FALLBACK["in"])),
                        "cache": float(p.get("cache", FALLBACK["cache"])),
                        "out": float(p.get("out", FALLBACK["out"])),
                    }
    except Exception:
        pass
    return _PRICES


def price_of(model):
    """返回 (价格字典, 是否精确匹配)。未知模型走 fallback 并标 estimated。"""
    load_prices()
    p = _PRICES.get(str(model or ""), None)
    if p:
        return p, True
    # 前缀兜底：如 deepseek-chat-xxxx 归到 deepseek-chat
    name = str(model or "")
    for key, val in _PRICES.items():
        if name.startswith(key) or key.startswith(name):
            return val, False
    return FALLBACK, False


def estimate(model, prompt_tokens=0, completion_tokens=0, cached_tokens=0):
    """估算一次调用的成本（元）。返回 dict。"""
    p, exact = price_of(model)
    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    ca = max(0, int(cached_tokens or 0))
    ca = min(ca, pt)  # 缓存 token 是 prompt 的子集
    hot = pt - ca
    cost = (hot * p["in"] + ca * p["cache"] + ct * p["out"]) / 1_000_000.0
    return {
        "cost": round(cost, 6),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cached_tokens": ca,
        "estimated": not exact,
    }


def fmt_cost(cost):
    """人类可读：低于 1 分显示 4 位小数，否则 2 位。"""
    c = float(cost or 0.0)
    if c >= 1:
        return "%.2f 元" % c
    if c >= 0.01:
        return "%.4f 元" % c
    return "%.6f 元" % c


def record(model, kind, prompt_tokens=0, completion_tokens=0, cached_tokens=0):
    """估算 + 入库。返回 estimate() 的结果（外加 in_db 标记）。"""
    est = estimate(model, prompt_tokens, completion_tokens, cached_tokens)
    est["model"] = model
    est["kind"] = kind
    est["in_db"] = False
    try:
        from . import store
        est["in_db"] = store.add_usage(
            kind, model, est["prompt_tokens"], est["completion_tokens"],
            est["cached_tokens"], est["cost"])
    except Exception:
        pass
    return est
