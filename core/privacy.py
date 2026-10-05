# -*- coding: utf-8 -*-
"""敏感信息识别与脱敏（3.0 · E4）

为什么必须要有它：
  3.0 会做「事实抽取」——把聊过的东西沉淀成长期记忆。这个功能在真实数据上
  第一次跑就把**银行卡号、手机号、验证码问题**原封不动地抽进了 facts 表，
  而这些内容会跟着 system prompt 一起被送进模型、会出现在面板和备份里。

  她随口说一句卡号是信任，不是让我们存档。

两条原则：
  1. **不入库**：金融/身份类高敏信息，事实抽取阶段直接丢弃（同Silver告警）
  2. **出口脱敏**：面板展示、导出、备份时按 138****8899 这种方式打码

只做**确定性强**的模式（正则），不做模糊的语义判断——漏一点没关系，
把正常内容误判成敏感信息反而更糟。
"""

import re

# 每条规则： (name, 编译后的正则, 打码方式)
#   打码方式 "keep_head_tail" → 138****1234 ；"mask_all" → ******
_PATTERNS = [
    ("银行卡号", re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)"), "keep_head_tail"),
    ("身份证号", re.compile(r"(?<!\d)[1-9]\d{5}(19|20)\d{2}[01]\d[0-3]\d{4}[\dXx](?!\d)"), "keep_head_tail"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "keep_head_tail"),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "keep_head_tail"),
    ("验证码", re.compile(r"(验证码|校验码|动态码|短信码)\s*(是|为|:|：)\s*\d{4,8}"), "mask_context"),
    ("密码", re.compile(r"(密码|口令|pwd|password)\s*(是|为|:|：)\s*\S{4,}"), "mask_context"),
    # 必须带分隔符 + 具体内容才算地址：否则「姐姐住址」这种类别名会被误判
    ("详细住址", re.compile(r"(住址|家庭住址|宿舍地址|收货地址)\s*(是|为|:|：)\s*.{4,40}"), "mask_context"),
]

# 触发「直接丢弃这条事实」的高危类别（金融/身份）
BLOCK_LEVEL = {"银行卡号", "身份证号", "验证码", "密码"}


def detect(text):
    """返回命中列表：[{"name":"银行卡号","match":"6214 8329 ..."}]。"""
    hits = []
    if not text:
        return hits
    s = str(text)
    for name, rx, _kind in _PATTERNS:
        for m in rx.finditer(s):
            hits.append({"name": name, "match": m.group(0)})
    return hits


def has_sensitive(text):
    return bool(detect(text))


def is_blocked(text):
    """是否命中「禁止入库」级别的高敏信息。"""
    return any(h["name"] in BLOCK_LEVEL for h in detect(text))


def _mask_number(s):
    digits = s.strip()
    n = len(digits)
    if n <= 4:
        return "*" * n
    head = digits[:3] if n > 7 else digits[:1]
    tail = digits[-4:] if n > 7 else digits[-1:]
    return "%s%s%s" % (head, "*" * max(3, n - len(head) - len(tail)), tail)


def redact(text, aggressive=False):
    """出口脱敏：把文本里的敏感片段打码。

    aggressive=True 时连「地址」这类非高危项也打码（用于导出/备份）。
    """
    if not text:
        return text
    s = str(text)
    soft = {"地址", "邮箱", "手机号"} if not aggressive else set()
    for name, rx, kind in _PATTERNS:
        if kind == "keep_head_tail":
            s = rx.sub(lambda m: _mask_number(m.group(0)), s)
        elif kind == "mask_context" and (aggressive or name not in soft):
            s = rx.sub(name + "：******", s)
    return s


def filter_fact_dict(item):
    """给一条待入库的事实做体检。

    返回 (是否放行, 原因)。命中高危的整条丢弃——宁可漏一条记忆，
    也不能把卡号写进会被送去模型的地方。
    """
    if not isinstance(item, dict):
        return False, "格式错误"
    blob = "%s %s" % (item.get("key", ""), item.get("value", ""))
    hits = detect(blob)
    if not hits:
        return True, ""
    names = sorted({h["name"] for h in hits})
    if any(n in BLOCK_LEVEL for n in names):
        return False, "含高敏信息（%s），已丢弃" % "、".join(names)
    # 非高危（如留了个邮箱）→ 放行但打码，保留记忆价值
    if isinstance(item.get("value"), str):
        item["value"] = redact(item["value"])
    return True, "已脱敏（%s）" % "、".join(names)
