# -*- coding: utf-8 -*-
"""消息后端统一接口。所有后端实现同一套接口，主程序无感知切换。"""

from dataclasses import dataclass, field


# B7：非文本消息的类型标识。后端把微信原生类型归一化成这里的一组，
# main.py 只认这些 kind，不再关心各后端的类型字符串差异。
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_STICKER = "sticker"      # 表情包
KIND_VOICE = "voice"          # 语音
KIND_VIDEO = "video"          # 视频 / 小视频
KIND_REDPACKET = "redpacket"  # 红包
KIND_TRANSFER = "transfer"    # 转账
KIND_RECALL = "recall"        # 撤回
KIND_FILE = "file"
KIND_LOCATION = "location"
KIND_CARD = "card"            # 名片 / 分享卡片
KIND_LINK = "link"            # 链接 / 公众号文章
KIND_OTHER = "other"          # 后端也不认识的类型


@dataclass
class Message:
    """统一消息结构"""
    chat: str          # 会话名（联系人备注名或群名）
    sender: str        # 发送者（'self' 表示自己）
    content: str       # 文本内容
    is_group: bool = field(default=False)
    image_path: str = field(default="")   # 图片消息的本地路径（多模态用）
    is_manual: bool = field(default=False)  # True=主人手动发的（非 AI 代发）
    kind: str = field(default=KIND_TEXT)   # B7：消息类型，见上面的 KIND_* 常量


class BackendError(Exception):
    """后端异常"""


class BaseBackend:
    """后端基类：子类实现以下方法"""

    name = "base"

    def setup(self, cfg):
        """初始化（连接微信等），cfg 为完整配置 dict"""
        raise NotImplementedError

    def add_listener(self, chat_name):
        """添加监听会话"""
        raise NotImplementedError

    def poll_messages(self):
        """拉取一批新消息，返回 [Message]，无消息返回 []"""
        raise NotImplementedError

    def send(self, chat_name, text):
        """向指定会话发送文本"""
        raise NotImplementedError

    def close(self):
        """清理资源"""
        pass
