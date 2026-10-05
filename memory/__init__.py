# -*- coding: utf-8 -*-
"""记忆子系统（3.0）

把「成长记忆」从主流程的副作用，升级成一套独立能力：

    daily        每日对话日志（jsonl 底线备份 + SQLite 查询主力）
    summarizer   日摘要 / 周摘要 / 结构化事实抽取
    worker       后台补跑线程：缺哪天补哪天，有节流、有进度、可以手动触发

三层的分工（C1）：

    day   当天发生了什么      —— 给 system prompt 当短期记忆
    week  这一周的主线        —— 给周记 / 长期画像用
    facts 她是谁、在忙什么    —— 结构化、可检索、可手改的最小知识单元

典型用法（main.py 里）：

    from memory import MemoryWorker
    self.memory_worker = MemoryWorker(self.ai, cfg)
    self.memory_worker.start()
"""

from . import daily, summarizer
from .worker import MemoryWorker, read_progress, write_progress, state_path

__all__ = ["daily", "summarizer", "MemoryWorker",
           "read_progress", "write_progress", "state_path"]
