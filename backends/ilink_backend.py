# -*- coding: utf-8 -*-
"""
iLink 后端：微信官方个人号 bot API（实验性，推荐但需调试）

来源：微信 iLink AI 开放接口（openclaw-weixin 生态），nanobot PR #2391 公开协议。
地址形如 https://ilinkai.weixin.qq.com，HTTP JSON API：
  GET  /ilink/bot/get_bot_qrcode     获取登录二维码
  GET  /ilink/bot/get_qrcode_status  轮询扫码状态，成功后返回 token
  POST /ilink/bot/getupdates         长轮询收取新消息（带 cursor 续传）
  POST /ilink/bot/sendmessage        发送文本消息
  POST /ilink/bot/sendtyping         "对方正在输入"状态

注意：
  1. 该接口为 2026 年新开放能力，字段名/流程可能与本实现有出入，
     首次使用请在日志里观察实际响应并按需修正 _parse_* 方法。
  2. 需要将手机微信更新到支持 iLink/openclaw 的版本。
  3. 官方接口，无封号风险；token 保存于 data/ilink_token.json。

零第三方依赖：使用 urllib 标准库。
"""

import json
import logging
import os
import time
import urllib.request
import urllib.parse
import urllib.error

from .base import BaseBackend, Message, BackendError

log = logging.getLogger("backend.ilink")


class IlinkBackend(BaseBackend):

    name = "ilink"

    def setup(self, cfg):
        self.base = cfg.get("ilink", {}).get("base_url", "https://ilinkai.weixin.qq.com").rstrip("/")
        self.poll_timeout_ms = cfg.get("ilink", {}).get("poll_timeout_ms", 30000)
        token_file = cfg.get("ilink", {}).get("token_file", "data/ilink_token.json")
        self.token_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), token_file)
        os.makedirs(os.path.dirname(self.token_file), exist_ok=True)

        self.token = self._load_token()
        if not self.token:
            self._login_flow()
        self.cursor = None
        self._client_seq = 0
        log.info("iLink 后端就绪 (token: %s...)", (self.token or "")[:8])

    # ---------- HTTP ----------
    def _http(self, method, path, payload=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.poll_timeout_ms / 1000 + 10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise BackendError(f"iLink HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
        except Exception as e:
            raise BackendError(f"iLink 请求失败: {e}")

    # ---------- 登录 ----------
    def _load_token(self):
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    return json.load(f).get("token")
            except Exception:
                return None
        return None

    def _save_token(self, token):
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump({"token": token, "saved_at": time.time()}, f, ensure_ascii=False, indent=2)

    def _login_flow(self):
        """扫码登录：控制台显示二维码链接，等待扫码确认"""
        log.info("开始 iLink 扫码登录...")
        qr = self._http("GET", "/ilink/bot/get_bot_qrcode")
        qrcode = qr.get("qrcode") or qr.get("url") or qr.get("qrcode_url")
        if not qrcode:
            raise BackendError(f"获取二维码失败，响应字段需人工核对: {json.dumps(qr, ensure_ascii=False)[:300]}")

        print("\n" + "=" * 50)
        print("请用手机微信扫描该二维码链接内容（复制到浏览器打开或用二维码工具生成）：")
        print(qrcode)
        print("=" * 50 + "\n")

        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(3)
            st = self._http("GET", "/ilink/bot/get_qrcode_status",
                            params={"qrcode": qrcode} if isinstance(qrcode, str) else None)
            # 常见状态字段：status / state == "confirmed" / "ok"
            status = str(st.get("status") or st.get("state") or "").lower()
            token = st.get("token") or st.get("access_token")
            if token:
                self.token = token
                self._save_token(token)
                log.info("iLink 登录成功，token 已保存")
                return
            if status in ("expired", "cancel", "cancelled"):
                raise BackendError("二维码已过期或被取消，请重新运行")
            log.info("等待扫码确认... (状态: %s)", status or "unknown")
        raise BackendError("扫码超时（5分钟）")

    # ---------- 收发 ----------
    def add_listener(self, chat_name):
        # iLink 按消息来源过滤，无需显式添加监听
        self._allow = set(getattr(self, "_allow", set()))
        self._allow.add(chat_name)
        log.info("iLink 白名单加入: %s", chat_name)

    def poll_messages(self):
        """长轮询拉取新消息"""
        payload = {"timeout": self.poll_timeout_ms}
        if self.cursor:
            payload["cursor"] = self.cursor
        try:
            resp = self._http("POST", "/ilink/bot/getupdates", payload)
        except BackendError as e:
            # 会话失效：errcode -14 等，重新登录
            msg = str(e)
            if "-14" in msg or "401" in msg or "token" in msg.lower():
                log.warning("token 失效，尝试重新登录")
                os.remove(self.token_file)
                raise BackendError("iLink token 失效，请重新运行程序完成扫码登录")
            log.warning("getupdates 异常: %s", e)
            return []

        self.cursor = resp.get("cursor") or resp.get("next_cursor") or self.cursor
        msgs = []
        for item in resp.get("messages", []) or resp.get("updates", []) or []:
            m = self._parse_message(item)
            if m:
                msgs.append(m)
        return msgs

    def _parse_message(self, item):
        """解析单条消息。字段名以实际响应为准，必要时人工修正。"""
        try:
            sender = item.get("from") or item.get("from_user") or item.get("sender") or ""
            if not sender or sender in ("self", "Me"):
                sender = "self"
            content = item.get("content") or item.get("text") or ""
            content = content.strip()
            if not content:
                return None
            chat = item.get("talker") or item.get("chat") or sender
            is_group = bool(item.get("is_chatroom") or "@chatroom" in str(chat))
            return Message(chat=chat, sender=sender, content=content, is_group=is_group)
        except Exception as e:
            log.warning("消息解析失败: %s -> %s", e, json.dumps(item, ensure_ascii=False)[:200])
            return None

    def send(self, chat_name, text):
        self._client_seq += 1
        payload = {
            "to": chat_name,
            "content": text,
            "client_id": f"bot-{int(time.time()*1000)}-{self._client_seq}",
        }
        try:
            resp = self._http("POST", "/ilink/bot/sendmessage", payload)
            ok = resp.get("errcode", 0) in (0, None) or resp.get("ok") or resp.get("success")
            if not ok:
                log.error("iLink 发送返回异常: %s", json.dumps(resp, ensure_ascii=False)[:200])
                return False
            return True
        except BackendError as e:
            log.error("iLink 发送失败: %s", e)
            return False

    def send_typing(self, chat_name):
        """发送"正在输入"状态（可选调用，更像真人）"""
        try:
            self._http("POST", "/ilink/bot/sendtyping", {"to": chat_name})
        except Exception:
            pass
