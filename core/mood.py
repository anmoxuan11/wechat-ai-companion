# -*- coding: utf-8 -*-
"""B3：情绪与敏感场景分级（工程兜底，不靠 prompt 自觉）

为什么需要：prompt 里写一句「吵架时别开玩笑」，模型不一定遵守。而在这类
场景说错话的代价，比不说话大得多——所以这里在**生成之前**就给她的话打标，
命中高敏就强制切保守模式（更短、更温柔、不撩、不玩笑），并把这件事记进
事件时间线让你看见。

金钱 / 承诺类的硬拦截不在这里：那是发送前的最后一道，归 core.guard 管。
"""

import re

# 标签 -> (关键词, 级别)
#   high：立刻切保守模式，并记一条警告事件
#   mid ：切温和模式（少撩、多关心），记一条 info 事件
_RULES = {
    "争吵": (["吵架", "生气", "气死", "烦死了", "别理我", "你根本", "你总是", "你从来",
              "算了", "无所谓", "随便你", "滚", "离我远点", "不想说了", "你爱怎样怎样"], "high"),
    "难过": (["难过", "想哭", "哭了", "委屈", "心情不好", "好难受", "低落", "崩溃",
              "emo", "不开心", "好累啊", "撑不住"], "high"),
    "道歉": (["对不起", "抱歉", "我错了", "原谅我", "别生气了", "是我的问题"], "mid"),
    "生病": (["生病", "发烧", "头疼", "头痛", "不舒服", "去医院", "打针", "吃药",
              "感冒", "肚子疼", "生理期"], "mid"),
    "告别": (["分手", "别联系", "结束了", "到此为止", "我们完了"], "high"),
    "压力": (["考砸", "没考好", "压力大", "焦虑", "复习不完", "来不及", "熬夜",
              "失眠", "保研", "考研"], "mid"),
}

_CONSERVATIVE = (
    "## 当前场景提醒（系统自动判定，务必遵守）\n"
    "她现在的状态：{tags}。这一轮请立刻收敛：\n"
    "- 回复更短、更温柔，一句顶一句就够，不要连发\n"
    "- **不撩、不推拉、不玩笑、不反撩、不装高冷**\n"
    "- 不评价对错、不辩解、不翻旧账\n"
    "- 先接住她的情绪，再说别的；不确定就只陪着\n"
    "- 绝对不要在这个时候抖机灵——说错一句比不说严重得多\n"
)

_MILD = (
    "## 当前场景提醒（系统自动判定）\n"
    "她现在的状态：{tags}。这一轮请：\n"
    "- 少撩少逗，多给实际的关心和办法\n"
    "- 可以问一句具体的情况（哪里不舒服 / 怎么了 / 还难受吗）\n"
    "- 别在这个时候讲道理或者开玩笑\n"
)


def classify(text):
    """给一段话打情绪/场景标签。

    返回 (tags, level)：tags 为命中的标签列表，level 取最严重的那个
    （high > mid），没命中返回 ([], None)。
    """
    s = (text or "").strip()
    if not s:
        return [], None
    low = s.lower()
    tags, level = [], None
    for tag, (words, lv) in _RULES.items():
        for w in words:
            if w.lower() in low:
                tags.append(tag)
                if lv == "high" or level is None:
                    level = lv if (level != "high") else level
                break
    # 多个感叹号 / 反复单字，也算情绪上头
    if not tags and (len(re.findall(r"[!！]", s)) >= 2 or len(re.findall(r"[?？]", s)) >= 3):
        tags.append("情绪激动")
        level = "mid"
    return tags, level


def instruction(tags, level):
    """按级别生成追加到 system prompt 的约束段。没命中返回空串。"""
    if not tags:
        return ""
    label = "、".join(tags)
    if level == "high":
        return _CONSERVATIVE.format(tags=label)
    if level == "mid":
        return _MILD.format(tags=label)
    return ""


def severity_rank(level):
    return {"high": 2, "mid": 1}.get(level or "", 0)
