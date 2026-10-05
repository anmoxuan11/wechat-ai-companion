# -*- coding: utf-8 -*-
"""手动补跑成长记忆（3.0）

用法：
    python tools/backfill.py              # 补跑全部缺失的日期
    python tools/backfill.py 3                # 最多补 3 天
    python tools/backfill.py --date 2026-08-24       # 只重跑某一天
    python tools/backfill.py --no-facts       # 只做日摘要，不抽事实

什么时候用它：
  - 机器人长时间没开，攒了一堆没沉淀的对话
  - 想重新生成某天的摘要（先把它从 SQLite 删掉再跑）
  - 面板「立即沉淀」按钮走的也是同一条链路，这是命令行版

它比面板按钮好在：能在终端里看到每天的摘要原文，便于判断沉淀质量。
"""

import io
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)


def bootstrap_env():
    """可能在受限环境运行 → 先把用户环境变量从注册表补进来（Windows）。"""
    try:
        from core.security import refresh_env_from_registry
        n = refresh_env_from_registry()
        if n:
            print("（已从注册表补入 %d 个环境变量）" % n)
    except Exception:
        pass


def main(argv):
    args = [a for a in argv[1:]]
    max_days = None
    only_date = None
    want_facts = True

    for a in list(args):
        if a == "--no-facts":
            want_facts = False
            args.remove(a)
        elif a.startswith("--date="):
            only_date = a.split("=", 1)[1].strip()
            args.remove(a)
    for a in args:
        if a.isdigit():
            max_days = int(a)

    bootstrap_env()

    from ai_client import AIClient
    from core import cost, store
    from memory import daily, summarizer

    with io.open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    store.init()
    ai = AIClient(cfg["ai"])
    print("密钥来源: %s" % ai.key_source)
    if not ai.api_key or ai.key_source == "未配置":
        print("未配置 API Key：请设置环境变量 DEEPSEEK_API_KEY")
        return 2

    if only_date:
        targets = [only_date]
    else:
        targets = summarizer.pending_dates()
        if max_days:
            targets = targets[:max_days]

    if not targets:
        print("没有需要沉淀的日期。")
        return 0

    print("待沉淀 %d 天：%s" % (len(targets), ", ".join(targets)))
    if max_days and len(targets) > max_days:
        targets = targets[:max_days]
        print("本次处理前 %d 天" % max_days)

    print("-" * 60)
    ok_n = fail_n = fact_n = 0
    for d in targets:
        rows = daily.read_day(d)
        if len(rows) < summarizer.DAY_MIN_LINES:
            print("[%s] 跳过：仅 %d 条对话" % (d, len(rows)))
            continue
        if only_date:
            store.save_summary("day", d, "")   # 强制重跑：清空旧摘要占位
        r = summarizer.summarize_day(ai, d, rows=rows,
                                     my_name=cfg.get("my_name", "我"),
                                     partner_name=cfg.get("partner_name", ""))
        if r.get("ok"):
            ok_n += 1
            print("[%s] 摘要（%d 条对话）" % (d, len(rows)))
            print(r["text"])
        else:
            fail_n += 1
            print("[%s] 摘要失败：%s" % (d, r.get("error")))
            continue

        if want_facts:
            fr = summarizer.extract_facts(ai, d, rows=rows)
            fs = fr.get("facts", [])
            fact_n += len(fs)
            if fs:
                print("  事实：")
                for f in fs:
                    print("    - [%s] %s：%s（%.2f）"
                          % (f["category"], f["key"], f["value"], f["confidence"]))
        print("-" * 60)
        time.sleep(1)

    total = cost.fmt_cost(store.usage_total(days=1).get("cost", 0))
    print("完成：成功 %d / 失败 %d，抽到事实 %d 条，今日花费 %s"
          % (ok_n, fail_n, fact_n, total))
    print("摘要已写入 data/state.db + data/memory.md")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
