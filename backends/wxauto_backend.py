# -*- coding: utf-8 -*-
"""
wxauto 后端：Windows UI 自动化，自动适配新版/旧版微信

自动探测安装的库（优先级从高到低）：
  1. wxauto4  —— 免费版，支持微信 4.0/4.1（适配到 4.1.8.107）
                 pip install wxauto4   (仅支持 Python 3.9-3.12)
  2. wxautox4 —— Plus 收费版，支持微信 4.x，需激活码
  3. wxauto   —— 老版免费库，仅支持微信 3.9.x

消息获取两种模式自动切换：
  A. 监听模式（AddListenChat/GetListenMessage，wxauto 老接口支持）
  B. 单窗口轮询模式（ChatWith + GetAllMessage 增量检测，wxauto4 兼容）
     一次只盯一个聊天窗口 —— 对"只回复对象一个人"的场景刚好够用

风险：非官方 UI 自动化，违反微信条款（低频白名单使用风险低，勿群发勿加人）。
"""

import logging

from .base import (BaseBackend, Message, BackendError, KIND_CARD, KIND_FILE,
                   KIND_LINK, KIND_LOCATION, KIND_OTHER, KIND_RECALL,
                   KIND_REDPACKET, KIND_STICKER, KIND_TRANSFER, KIND_VIDEO,
                   KIND_VOICE)

# B7：wxauto / wxauto4 的消息类型是英文，这里归一化成统一 kind
_WX_KIND_MAP = {
    "image": KIND_OTHER,
    "picture": KIND_OTHER,
    "voice": KIND_VOICE,
    "audio": KIND_VOICE,
    "video": KIND_VIDEO,
    "sticker": KIND_STICKER,
    "emoji": KIND_STICKER,
    "animation": KIND_STICKER,
    "file": KIND_FILE,
    "location": KIND_LOCATION,
    "card": KIND_CARD,
    "share": KIND_LINK,
    "link": KIND_LINK,
    "redpacket": KIND_REDPACKET,
    "transfer": KIND_TRANSFER,
    "recall": KIND_RECALL,
    "revoke": KIND_RECALL,
}

_WX_PLACEHOLDER = {
    KIND_VOICE: "[语音]",
    KIND_VIDEO: "[视频]",
    KIND_STICKER: "[表情包]",
    KIND_FILE: "[文件]",
    KIND_LOCATION: "[位置]",
    KIND_CARD: "[分享名片]",
    KIND_LINK: "[链接]",
    KIND_REDPACKET: "[红包]",
    KIND_TRANSFER: "[转账]",
    KIND_RECALL: "[对方撤回了一条消息]",
}

log = logging.getLogger("backend.wxauto")


def _import_wechat():
    """按优先级导入 wxauto 系列库，返回 (WeChat类, 库名)

    探测顺序：
      1. wxauto4     免费版，微信 4.x（适配到 4.1.8.107，更高版本不兼容）
                     pip install wxauto4          (Python 3.9-3.12)
      2. wxautox4    Plus 收费版，微信 4.x，需激活码
      3. wechatauto  微信 4.1.12+ 的复刻版（读消息走本地数据库，对版本不敏感）
                     pip install git+https://github.com/fanyuantaier/wechatauto-replica.git
      4. wxauto      老版免费库，仅微信 3.9.x
    """
    for mod in ("wxauto4", "wxautox4", "wechatauto", "wxauto"):
        try:
            m = __import__(mod)
            # wechatauto-replica 的入口可能是小写模块，兼容常见导出名
            for attr in ("WeChat", "Wechat"):
                if hasattr(m, attr):
                    return getattr(m, attr), mod
        except ImportError:
            continue
    raise BackendError(
        "未安装 wxauto 系列库。微信 4.1.12+ 推荐: "
        "pip install git+https://github.com/fanyuantaier/wechatauto-replica.git；"
        "微信 4.0-4.1.8: pip install wxauto4；微信 3.9.x: pip install wxauto"
    )


class WxautoBackend(BaseBackend):

    name = "wxauto"

    def setup(self, cfg):
        WeChat, lib_name = _import_wechat()
        self.lib_name = lib_name
        self.mode = None            # "listen" 或 "poll"
        self._listen_map = {}
        self._poll_chats = []       # 单窗口轮询模式盯的会话
        self._seen = {}             # poll 模式去重: chat -> [已见消息key]

        try:
            self.wx = WeChat()
        except Exception as e:
            raise BackendError(
                f"连接微信失败: {e}\n请确认微信 PC 客户端已登录且窗口未最小化到托盘"
            )
        log.info("已连接微信（库: %s）", lib_name)

    def add_listener(self, chat_name):
        # 优先尝试监听模式（wxauto 3.9.x 接口）
        try:
            win = self.wx.AddListenChat(who=chat_name)
            self._listen_map[win] = chat_name
            self.mode = self.mode or "listen"
            log.info("监听模式: 已添加 [%s]", chat_name)
            return
        except Exception as e:
            log.info("AddListenChat 不可用(%s)，切换到单窗口轮询模式", e)

        # 回退：单窗口轮询模式
        self.mode = "poll"
        self._poll_chats.append(chat_name)
        try:
            self.wx.ChatWith(chat_name)
            # 初始化已见消息集合，跳过历史消息
            self._seen[chat_name] = self._current_keys(chat_name)
            log.info("轮询模式: 已打开 [%s]（忽略历史消息，只处理新消息）", chat_name)
        except Exception as e:
            raise BackendError(f"打开会话失败 [{chat_name}]: {e}")

    # ---------- 监听模式 ----------
    def _poll_listen(self):
        msgs = []
        try:
            raw = self.wx.GetListenMessage()
        except Exception as e:
            log.warning("获取消息异常: %s", e)
            return []

        if not raw:
            return []

        for chat_win, msg_list in raw.items():
            chat_name = self._listen_map.get(chat_win, getattr(chat_win, "name", str(chat_win)))
            is_group = bool(getattr(chat_win, "is_group", False))
            for m in msg_list:
                msg = self._to_message(m, chat_name, is_group)
                if msg:
                    msgs.append(msg)
        return msgs

    # ---------- 单窗口轮询模式 ----------
    def _current_keys(self, chat_name):
        """当前窗口消息指纹集合（用于增量检测）"""
        keys = []
        try:
            for m in (self.wx.GetAllMessage() or []):
                keys.append(self._msg_key(m))
        except Exception as e:
            log.warning("GetAllMessage 异常 [%s]: %s", chat_name, e)
        return keys

    def _msg_key(self, m):
        sender = getattr(m, "sender", "") or ""
        content = getattr(m, "content", "") or ""
        return f"{sender}|{content[:80]}"

    def _poll_window(self):
        msgs = []
        for chat_name in self._poll_chats:
            try:
                current = self._current_keys(chat_name)
            except Exception:
                continue
            seen = self._seen.setdefault(chat_name, [])
            if len(current) <= len(seen):
                # 无新消息（或消息被撤回/窗口切换）
                self._seen[chat_name] = current
                continue

            # 出现新消息：取出新增部分
            new_keys = current[len(seen):]
            # 重新拉取完整消息对象以取得 sender
            try:
                all_msgs = self.wx.GetAllMessage() or []
                new_objs = [m for m in all_msgs if self._msg_key(m) in new_keys]
            except Exception:
                new_objs = []
            for m in new_objs:
                msg = self._to_message(m, chat_name, False)
                if msg:
                    msgs.append(msg)
            self._seen[chat_name] = current
        return msgs

    # ---------- 公共 ----------
    def _to_message(self, m, chat_name, is_group):
        sender = getattr(m, "sender", "")
        if sender in ("self", "Self", "Me", ""):
            # sender 为空且 content 与自己刚发的一致时按自己处理
            if getattr(m, "is_self", False):
                sender = "self"
            elif sender == "":
                return None  # 无法判断发送者，丢弃
        if sender in ("self", "Self", "Me"):
            sender = "self"
        # B7：wxauto 的类型是英文，同样归一化成 kind，别再一律丢弃
        raw_type = str(getattr(m, "type", "text") or "text").lower()
        kind = _WX_KIND_MAP.get(raw_type, KIND_TEXT if raw_type == "text" else KIND_OTHER)
        content = getattr(m, "content", "").strip()
        if kind == KIND_STICKER and content:
            content = "[表情包: %s]" % content
        if not content:
            content = _WX_PLACEHOLDER.get(kind, "")
        if not content:
            return None
        return Message(chat=chat_name, sender=sender, content=content,
                       is_group=is_group, kind=kind)

    def poll_messages(self):
        if self.mode == "listen":
            return self._poll_listen()
        if self.mode == "poll":
            return self._poll_window()
        return []

    def send(self, chat_name, text):
        try:
            self.wx.SendMsg(text, who=chat_name)
            return True
        except Exception as e:
            log.error("发送失败 [%s]: %s", chat_name, e)
            return False
