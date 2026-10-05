# -*- coding: utf-8 -*-
"""后端工厂：按 config 的 backend 字段创建消息后端

backend 可选值：
  auto        自动选择（推荐）：微信 4.1.9+ 用 wechatauto，其余回退 wxauto 系
  wechatauto  wechatauto-replica 原生后端（微信 4.1.12+ 最佳，收消息走数据库）
  wxauto      wxauto/wxauto4 系后端（微信 3.9.x / 4.0-4.1.8）
  ilink       微信官方 bot API（会显示 bot 身份，仅作技术储备）
"""

import logging

from .base import BaseBackend, Message, BackendError

log = logging.getLogger("backends")


def create_backend(cfg):
    name = cfg.get("backend", "auto")

    if name == "auto":
        # 优先 wechatauto（微信 4.x，收消息走本地数据库最稳），失败回退 wxauto 系
        try:
            import wechatauto  # noqa: F401
            from .wechatauto_backend import WechatautoBackend
            log.info("自动选择: wechatauto 后端")
            return WechatautoBackend()
        except ImportError:
            pass
        from .wxauto_backend import WxautoBackend
        log.info("自动选择: wxauto 系后端")
        return WxautoBackend()

    if name == "wechatauto":
        from .wechatauto_backend import WechatautoBackend
        return WechatautoBackend()
    if name == "wxauto":
        from .wxauto_backend import WxautoBackend
        return WxautoBackend()
    if name == "ilink":
        from .ilink_backend import IlinkBackend
        return IlinkBackend()
    raise BackendError(f"未知后端: {name}（可选: auto / wechatauto / wxauto / ilink）")
