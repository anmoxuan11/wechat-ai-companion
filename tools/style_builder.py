# -*- coding: utf-8 -*-
"""
喂料工具：从你与对象的聊天记录中学习你的说话风格，生成 style_profile.json

数据来源（四选一）：
  A. 项目后端直读（推荐，wechatauto 后端可用时）：--backend --talker 对象备注名
     直接从微信本地数据库读取与对象的全部历史消息，无需安装 chatlog
  B. chatlog HTTP 服务：先运行 chatlog 并开启 HTTP 服务，
     本工具直接调 http://127.0.0.1:5030/api/v1/chatlog?talker=<对象>&format=json 拉取
  C. chatlog 导出的 JSON 文件
  D. chatlog / WeChatMsg 导出的纯文本文件（格式：每行一条，如 "2024-01-01 12:00:00 张三 你好"）

用法：
  python tools/style_builder.py --backend --talker 对象备注名
  python tools/style_builder.py --fetch --chatlog-url http://127.0.0.1:5030 --talker 对象备注名
  python tools/style_builder.py --build --input data/chat_history.json --samples 20

产出 data/style_profile.json：
  - 口头禅 / 高频词 / 语气词统计
  - 平均消息长度、表情使用习惯
  - 精选 few-shot 对话样本（对象提问 -> 你的真实回复）
"""

import argparse
import collections
import json
import os
import random
import re
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

# 常见语气词 / 萌系用语（用于口头禅检测）
TONE_WORDS = ["哈哈", "哈哈哈", "嘻嘻", "嘿嘿", "呀", "啦", "咯", "嘛", "噢", "哦", "emm",
              "额", "呜呜", "哭死", "绝了", "离谱", "无语", "好的呀", "好滴", "宝", "宝宝",
              "亲", "老婆", "老公", "笨蛋", "讨厌", "想你了", "爱你", "么么", "mua", "抱抱"]


def load_history(args):
    """加载聊天记录 -> 统一格式 [{"is_self": bool, "content": str, "time": str}]"""
    if args.backend:
        records = fetch_from_backend(args.talker, args.limit)
    elif args.fetch:
        records = fetch_from_chatlog(args.chatlog_url, args.talker)
    elif args.input.lower().endswith(".json"):
        records = load_json_file(args.input)
    else:
        records = load_text_file(args.input)
    print(f"共加载 {len(records)} 条消息（我方 {sum(1 for r in records if r['is_self'])} 条 / "
          f"对方 {sum(1 for r in records if not r['is_self'])} 条）")
    if len(records) < 50:
        print("警告：消息太少（<50），风格学习效果会差，建议多导出一些历史记录")
    return records


def fetch_from_backend(talker, limit):
    """通过项目后端（wechatauto）直接读微信本地数据库"""
    import json as _json
    sys.path.insert(0, BASE_DIR)
    from backends import create_backend
    cfg_path = os.path.join(BASE_DIR, "config.json")
    cfg = _json.load(open(cfg_path, "r", encoding="utf-8"))
    backend = create_backend(cfg)
    backend.setup(cfg)
    if not hasattr(backend, "get_history"):
        raise SystemExit(f"当前后端 {backend.name} 不支持读历史消息，请改用 --fetch 或 --input")
    raw = backend.get_history(talker, limit=limit)
    backend.close()
    return [{"is_self": s, "content": c, "time": str(t)} for s, c, t in raw]


def fetch_from_chatlog(base_url, talker, days=365):
    """从 chatlog HTTP 服务拉取与指定联系人的聊天记录"""
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    url = (f"{base_url.rstrip('/')}/api/v1/chatlog"
           f"?time={start}~{end}&talker={urllib.request.quote(talker)}&limit=50000&format=json")
    print(f"正在从 chatlog 拉取: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return normalize_records(data)


def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return normalize_records(data)


def normalize_records(data):
    """chatlog JSON -> 统一格式（兼容多种字段命名）"""
    records = []
    if isinstance(data, dict):
        data = data.get("data") or data.get("chatlog") or data.get("messages") or []
    for item in data:
        is_self = item.get("is_self", item.get("isSelf", item.get("IsSelf", False)))
        content = str(item.get("content") or item.get("message") or "").strip()
        if not content:
            continue
        records.append({
            "is_self": bool(is_self),
            "content": content,
            "time": str(item.get("time") or item.get("created_at") or ""),
        })
    return records


def load_text_file(path):
    """纯文本导出格式兜底解析：'2024-01-01 12:00 张三 内容'"""
    records = []
    my_names, partner_names = set(), set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 时间(2段) + 人名 + 内容
            m = re.match(r"^[\d\-/ :.]+\s+(\S+)\s+(.*)$", line)
            if not m:
                continue
            name, content = m.group(1), m.group(2).strip()
            if not content:
                continue
            records.append({"is_self": None, "sender_name": name, "content": content, "time": ""})

    # 文本模式无法直接知道哪边是自己，提示用户标注
    names = sorted({r["sender_name"] for r in records if r.get("sender_name")})
    if len(names) > 2:
        names = names[:2]
    print(f"\n检测到发送者: {names}")
    my_name = input("上面哪个名字是你自己？（输入名字，留空取消）: ").strip()
    for r in records:
        r["is_self"] = (r.get("sender_name") == my_name)
        r.pop("sender_name", None)
    return records


# ---------- 风格分析 ----------
def extract_fewshot_pairs(records, n=20):
    """提取 (对方消息 -> 我方紧跟回复) 对话样本，偏好短小口语化的"""
    pairs = []
    pending_partner_msgs = []
    for r in records:
        if not r["is_self"]:
            pending_partner_msgs.append(r["content"])
        else:
            if pending_partner_msgs:
                reply = r["content"]
                question = pending_partner_msgs[-1]  # 取最后一条对方消息
                if 0 < len(reply) <= 60 and 0 < len(question) <= 80:
                    pairs.append((question, reply))
            pending_partner_msgs = []
    random.seed(42)
    random.shuffle(pairs)
    return pairs[:n]


def analyze_style(records):
    """统计风格特征"""
    mine = [r["content"] for r in records if r["is_self"]]

    lens = [len(c) for c in mine if c]
    avg_len = round(sum(lens) / len(lens), 1) if lens else 0
    short_ratio = round(sum(1 for l in lens if l <= 15) / max(len(lens), 1), 2)

    counter = collections.Counter()
    for c in mine:
        for w in TONE_WORDS:
            if w in c:
                counter[w] += 1
    catchphrases = [w for w, _ in counter.most_common(8)]

    emoji_counter = collections.Counter(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "".join(mine)))
    top_emojis = [e for e, _ in emoji_counter.most_common(6)]

    punctuation = collections.Counter()
    for c in mine:
        if "？" in c or "?" in c:
            punctuation["问句"] += 1
        if "！" in c:
            punctuation["感叹号"] += 1
        if "~" in c or "～" in c:
            punctuation["波浪号"] += 1
        if "…" in c or "..." in c:
            punctuation["省略号"] += 1

    return {
        "avg_message_length": avg_len,
        "short_message_ratio": short_ratio,
        "catchphrases": catchphrases,
        "top_emojis": top_emojis,
        "punctuation_habits": dict(punctuation.most_common(5)),
        "total_messages_from_me": len(mine),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_profile(records, fewshot_n):
    profile = analyze_style(records)
    profile["fewshot_pairs"] = [
        {"partner": q, "me": a} for q, a in extract_fewshot_pairs(records, fewshot_n)
    ]
    return profile


def main():
    parser = argparse.ArgumentParser(description="聊天风格学习工具（喂料）")
    parser.add_argument("--backend", action="store_true", help="通过项目后端直读微信数据库（推荐）")
    parser.add_argument("--limit", type=int, default=5000, help="最多读取的消息条数")
    parser.add_argument("--fetch", action="store_true", help="从 chatlog HTTP 服务拉取聊天记录")
    parser.add_argument("--chatlog-url", default="http://127.0.0.1:5030", help="chatlog 服务地址")
    parser.add_argument("--talker", default="", help="对象的备注名/wxid（--backend/--fetch 时必填）")
    parser.add_argument("--input", default="", help="本地聊天记录文件路径（JSON 或文本）")
    parser.add_argument("--samples", type=int, default=20, help="few-shot 样本数量")
    args = parser.parse_args()

    if not args.backend and not args.fetch and not args.input:
        parser.error("需要 --backend / --fetch / --input <文件> 之一")
    if (args.backend or args.fetch) and not args.talker:
        parser.error("--backend/--fetch 模式必须指定 --talker 对象备注名")

    os.makedirs(DATA_DIR, exist_ok=True)
    records = load_history(args)

    # 保存原始记录供后续使用
    hist_path = os.path.join(DATA_DIR, "chat_history.json")
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
    print(f"原始聊天记录已保存: {hist_path}")

    profile = build_profile(records, args.samples)
    out_path = os.path.join(DATA_DIR, "style_profile.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    print(f"\n风格档案已生成: {out_path}")
    print(f"  口头禅: {', '.join(profile['catchphrases']) or '未检测到'}")
    print(f"  平均消息长度: {profile['avg_message_length']} 字（短句占比 {profile['short_message_ratio']:.0%}）")
    print(f"  常用表情: {''.join(profile['top_emojis']) or '无'}")
    print(f"  few-shot 样本: {len(profile['fewshot_pairs'])} 组")


if __name__ == "__main__":
    main()
