# -*- coding: utf-8 -*-
"""
风格蒸馏：把 style_profile.json（统计数据 + 25 组样本）一次性蒸馏成
~300 字的"人设卡"（data/persona.md），之后每次 AI 调用只带这张卡 +
少量示例，system prompt 从 ~2500 token 压到 ~600 token。

蒸馏本身只调用一次 AI（输入 ~3k token，成本 < ¥0.001）。
"""

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from ai_client import AIClient


def distill(cfg_path=None):
    cfg_path = cfg_path or os.path.join(BASE_DIR, "config.json")
    cfg = json.load(open(cfg_path, "r", encoding="utf-8"))
    profile_path = os.path.join(BASE_DIR, cfg.get("style", {}).get("style_profile_path", "data/style_profile.json"))
    profile = json.load(open(profile_path, "r", encoding="utf-8"))

    my_name = cfg.get("my_name", "我")
    partner = cfg.get("partner_name", "对象")
    pairs = profile.get("fewshot_pairs", [])

    examples = "\n".join(f"{partner}: {p['partner']}\n{my_name}: {p['me']}" for p in pairs[:25])

    prompt = f"""以下是「{my_name}」和对象「{partner}」的真实微信聊天记录片段，以及统计特征。

统计特征：
- 平均每条消息 {profile.get('avg_message_length')} 字，短句占比 {profile.get('short_message_ratio', 0):.0%}
- 口头禅: {'、'.join(profile.get('catchphrases', []))}
- 常用表情: {' '.join(profile.get('top_emojis', []))}

聊天片段：
{examples}

请你以第三人称，为「{my_name}」写一份说话风格人设卡（200-350 字），供 AI 模仿此人时使用。要求：
1. 描述他的语气、用词习惯、标点习惯、表情使用习惯、对对象的称呼
2. 指出他的回复节奏（秒回/简短/连发几条等）
3. 只输出人设卡正文，不要标题、不要解释"""

    ai = AIClient(cfg["ai"])
    persona = ai.chat([{"role": "user", "content": prompt}])

    out = os.path.join(BASE_DIR, "data", "persona.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(persona)
    print(f"人设卡已生成: {out} ({len(persona)} 字)")
    print("---")
    print(persona)
    return persona


if __name__ == "__main__":
    distill()
