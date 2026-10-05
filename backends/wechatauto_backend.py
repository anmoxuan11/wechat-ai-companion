# -*- coding: utf-8 -*-
"""
wechatauto 后端（微信 4.x 推荐，尤其 4.1.12+）

基于 wechatauto-replica 库：
  - 收消息：本地数据库增量监听（Listener，只读，对微信版本不敏感，稳）
  - 发消息：WeChatGUI 坐标+OCR/UIA 混合驱动（需桌面可见，不能锁屏）
  - 安装：pip install git+https://github.com/fanyuantaier/wechatauto-replica.git

架构说明：Listener 是回调线程模型，本后端用线程安全 queue 桥接到
主程序的轮询模型（poll_messages）。
"""

import collections
import json
import logging
import os
import queue
import time

from .base import (BaseBackend, Message, BackendError, KIND_CARD, KIND_FILE,
                   KIND_IMAGE, KIND_LINK, KIND_LOCATION, KIND_OTHER,
                   KIND_RECALL, KIND_REDPACKET, KIND_STICKER, KIND_TEXT,
                   KIND_TRANSFER, KIND_VIDEO, KIND_VOICE)

log = logging.getLogger("backend.wechatauto")

# 消息类型：实测为中文（"文本"/"图片"），暂只处理文本
TEXT_TYPE = "文本"

# B7：微信原生类型 → 统一 kind，以及交给 AI 时的占位文本。
# 以前除文本和图片外一律 return，她发个表情包机器人就当没看见，对话直接断掉。
_KIND_MAP = {
    "文本": KIND_TEXT,
    "图片": KIND_IMAGE,
    "动画表情": KIND_STICKER,
    "表情": KIND_STICKER,
    "贴图": KIND_STICKER,
    "语音": KIND_VOICE,
    "视频": KIND_VIDEO,
    "小视频": KIND_VIDEO,
    "红包": KIND_REDPACKET,
    "转账": KIND_TRANSFER,
    "撤回": KIND_RECALL,
    "撤回消息": KIND_RECALL,
    "文件": KIND_FILE,
    "位置": KIND_LOCATION,
    "名片": KIND_CARD,
    "链接": KIND_LINK,
    "公众号文章": KIND_LINK,
    "分享": KIND_LINK,
}

_PLACEHOLDER = {
    KIND_STICKER: "[表情包]",
    KIND_VOICE: "[语音]",
    KIND_VIDEO: "[视频]",
    KIND_REDPACKET: "[红包]",
    KIND_TRANSFER: "[转账]",
    KIND_RECALL: "[对方撤回了一条消息]",
    KIND_FILE: "[文件]",
    KIND_LOCATION: "[位置]",
    KIND_CARD: "[分享名片]",
    KIND_LINK: "[链接]",
    KIND_OTHER: "[其他消息]",
}


def _classify(mtype, content):
    """把微信原生类型归一化成 (kind, 给 AI 看的文本)"""
    kind = _KIND_MAP.get(mtype)
    if kind is None:
        # 没见过的类型：不丢，标成 other，让 main.py 按策略决定是否接话
        kind = KIND_OTHER
        log.info("未识别的消息类型: %r", mtype)
    text = (content or "").strip()
    if kind == KIND_STICKER:
        # 部分版本能拿到表情包标题，比干巴巴的 [表情包] 更有信息量；
        # 但 "[动画表情]" 这类本身就是占位符，包进去只会变成 [表情包: [动画表情]]
        junk = {"", "[动画表情]", "[表情]", "[贴图]", "动画表情", "表情"}
        return kind, ("[表情包: %s]" % text) if text not in junk else "[表情包]"
    if not text:
        text = _PLACEHOLDER.get(kind, "")
    return kind, text


class WechatautoBackend(BaseBackend):

    name = "wechatauto"

    def setup(self, cfg):
        try:
            from wechatauto.db import WeChatDB, Listener   # 数据库版 Listener（带水位线）
            from wechatauto.guia import WeChatGUI
        except ImportError:
            raise BackendError(
                "未安装 wechatauto，请执行: "
                "pip install git+https://github.com/fanyuantaier/wechatauto-replica.git"
            )

        self._WeChatDB, self._WeChatGUI, self._Listener = WeChatDB, WeChatGUI, Listener
        self._msg_queue = queue.Queue()
        self._listener = None
        self._name_map = {}  # 昵称/备注 -> username

        try:
            self.db = WeChatDB()
            info = self.db.get_self_info()
            self.self_username = info.get("username") or ""
            # 自己的全部标识（wxid / 昵称），用于 sender 判定
            self._self_idents = {x for x in (self.self_username, info.get("nick_name")) if x}
            self._sender_index = None  # 延迟加载: real_sender_id -> username/alias
            self._self_ids = None      # 延迟加载: 映射到自己的 sender_id 集合
            self._recent_sent = collections.deque(maxlen=50)  # (chat, content) 最近发送
            # 发送记录持久化到磁盘：跨重启也能识别"哪些是 AI 代发的"，避免
            # 把机器人自己发的消息误判成主人手动发的
            self._sent_log_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "bot_sent.json")
            self._sent_persist = self._load_sent()
            log.info("已连接微信数据库（账号: %s）", info.get("nick_name") or self.self_username)
        except Exception as e:
            raise BackendError(
                f"连接微信数据库失败: {e}\n请确认微信 PC 客户端已登录且至少收发过一条消息"
            )

        try:
            self.gui = WeChatGUI()
            # desktop_available() 存在误报（实测 False 时发送仍正常），改为软提示
            if not self.gui.desktop_available():
                log.warning("desktop_available 检测为 False（可能误报），继续尝试运行")
        except Exception as e:
            raise BackendError(f"初始化微信 GUI 失败: {e}")

        log.info("wechatauto 后端就绪（DB 读消息 + GUI 发消息）")

    def _resolve(self, chat_name):
        """昵称/备注 -> 会话 username（wxid / xxx@chatroom）"""
        if chat_name in self._name_map:
            return self._name_map[chat_name]
        username = chat_name
        try:
            hits = self.db.search_contact(chat_name)
            # 优先个人号（wxid_）/ 群聊（@chatroom），跳过公众号（gh_）等
            picked = None
            for h in hits:
                u = h.get("username") or ""
                if u and not u.startswith("gh_"):
                    picked = u
                    break
            if picked:
                username = picked
                log.info("会话映射: 「%s」-> %s", chat_name, username)
            elif hits:
                log.warning("「%s」仅匹配到公众号，按原名处理", chat_name)
        except Exception as e:
            log.warning("search_contact(%s) 失败: %s，按原名处理", chat_name, e)
        self._name_map[chat_name] = username
        return username

    def add_listener(self, chat_name):
        username = self._resolve(chat_name)
        if self._listener is None:
            self._listener = self._Listener(self.db, interval=1.0)

        is_group = "@chatroom" in username

        def on_msg(msg, lst):
            try:
                mtype = msg.get("type")
                content = (msg.get("content") or "").strip()
                image_path = ""

                if mtype == "图片":
                    # 下载图片供多模态识别。图片刚到时 .dat 可能还没落盘
                    # （WeChat 异步把原图下到本地缓存），重试几次。
                    # 仍失败则**静默 return**（不返回 Message），由 main.py
                    # 的 _handle 决定不回复——避免之前"没看清"安全网让模型
                    # 凭 prompt 里"发自拍用力夸"的偏见继续幻觉成"好看/顶不住"。
                    try:
                        from wechatauto.media import MediaDownloader
                        dl = MediaDownloader(self.db)
                        lid = msg.get("local_id")
                        for _retry in range(4):
                            path = dl.download_image(username, lid)
                            if path:
                                image_path = str(path)
                                break
                            time.sleep(1.5)
                        if not image_path:
                            log.warning("图片下载失败（4 次重试），静默跳过: local_id=%s", lid)
                            return
                    except Exception as e:
                        log.warning("图片下载异常，静默跳过: %s", e)
                        return
                    content = content or "[图片]"
                    kind = KIND_IMAGE
                else:
                    # B7：表情包 / 语音 / 视频 / 红包 / 撤回等不再一律丢弃
                    kind, content = _classify(mtype, content)
                    if mtype != TEXT_TYPE and kind != KIND_OTHER:
                        log.info("非文本消息 [%s] 类型=%s → %s", chat_name, mtype, kind)

                if not content and not image_path:
                    return
                sender = self._sender_name(msg.get("sender_id"), chat_name, content)
                # 自己账号发的消息：区分 AI 代发 vs 主人手动发
                # （AI 代发的内容在发送记录里且 10 分钟内 → 非手动；否则是主人手动发）
                is_manual = (sender == "self" and not self._is_ai_sent(chat_name, content))
                self._msg_queue.put(
                    Message(chat=chat_name, sender=sender, content=content,
                            is_group=is_group, image_path=image_path,
                            is_manual=is_manual, kind=kind)
                )
            except Exception as e:
                log.error("消息回调异常: %s", e)

        self._listener.add_listener(username, on_msg)
        log.info("已监听: %s (%s)", chat_name, username)

    def _load_sender_index(self):
        if self._sender_index is None:
            try:
                self._sender_index = self.db._sender_id_index()
                # 反查：所有映射到自己标识的 sender_id 集合
                self._self_ids = {k for k, v in self._sender_index.items()
                                  if v in self._self_idents}
            except Exception as e:
                log.warning("sender 索引加载失败: %s", e)
                self._sender_index = {}
                self._self_ids = set()

    def _sender_name(self, sender_id, chat_name, content):
        """判定发送者。返回 'self' 或对方标识。

        四层判定（实测校准：本账号"我"的消息 sender_id 恒为 1 且不在索引中；
        对方 id 可经索引解析为其微信号 alias）：
          1. 内容与机器人最近发送消息匹配 -> self（覆盖 AI 代发）
          2. sender_id 在 self 反查集合中 -> self
          3. 索引正向解析: 是自己标识 -> self，否则 -> 对方
          4. 解析失败（如 1）-> self（安全兜底：宁可漏回，不可把主人手动发的消息
             误判为对方消息导致机器人插话穿帮）
        """
        if (chat_name, content) in self._recent_sent:
            return "self"
        self._load_sender_index()
        try:
            sid = int(sender_id)
        except (TypeError, ValueError):
            return str(sender_id or "")
        if sid in self._self_ids:
            return "self"
        u = self._sender_index.get(sid)
        if u:
            return "self" if u in self._self_idents else u
        return "self"  # 兜底见 docstring 第 4 条

    def poll_messages(self):
        if self._listener is not None and not self._listener._thread:
            self._listener.start()
            log.info("消息监听线程已启动")
        msgs = []
        while True:
            try:
                msgs.append(self._msg_queue.get_nowait())
            except queue.Empty:
                break
        return msgs

    def send(self, chat_name, text):
        try:
            resp = self.gui.send_msg(text, chat_name)
            ok = bool(getattr(resp, "is_success", False))
            if ok:
                self._recent_sent.append((chat_name, text))
                self._sent_persist.append({"chat": chat_name, "text": text, "ts": time.time()})
                self._sent_persist = self._sent_persist[-300:]
                self._save_sent()
            else:
                log.error("发送失败 [%s]: %s", chat_name, getattr(resp, "message", resp))
            return ok
        except Exception as e:
            log.error("发送异常 [%s]: %s", chat_name, e)
            return False

    def _load_sent(self):
        try:
            with open(self._sent_log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_sent(self):
        try:
            os.makedirs(os.path.dirname(self._sent_log_path), exist_ok=True)
            with open(self._sent_log_path, "w", encoding="utf-8") as f:
                json.dump(self._sent_persist, f, ensure_ascii=False)
        except Exception:
            pass

    def _is_ai_sent(self, chat, text):
        """判断这条"自己发的"消息是不是机器人代发的（含重启前旧进程发的）"""
        now = time.time()
        for r in self._sent_persist:
            if r.get("chat") == chat and r.get("text") == text and now - r.get("ts", 0) <= 600:
                return True
        return False

    def close(self):
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass

    # ---------- 附加能力：直接读历史消息（喂料用） ----------
    def get_history(self, chat_name, limit=10000):
        """读取与某人的历史消息，返回 [(is_self, content, timestamp)]"""
        username = self._resolve(chat_name)
        msgs = self.db.get_messages(username, limit=limit)
        out = []
        for m in msgs:
            content = (m.get("content") or "").strip()
            if not content or m.get("type") != TEXT_TYPE:
                continue
            out.append((self._sender_name(m.get("sender_id"), chat_name, content) == "self",
                        content, m.get("create_time", 0)))
        return out
