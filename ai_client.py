# -*- coding: utf-8 -*-
"""
AI 客户端：调用 OpenAI 兼容接口（DeepSeek / GLM / 豆包 / OpenAI 等通用）
零第三方依赖，使用 urllib 标准库。
"""

import base64
import json
import logging
import os
import urllib.request
import urllib.error

log = logging.getLogger("ai")

# ---------------------------------------------------------------------------
# 2.0：密钥不再只信 config.json。优先级 = 环境变量 > 配置文件。
# 这样 config.json 里可以只留占位符，真实密钥放环境变量，
# 桌面副本 / 打包产物 / 截图里都不会泄露明文。
# core 不可用时（例如单独运行本文件）退化为等价的内联实现。
# ---------------------------------------------------------------------------
try:
    from core.security import (ENV_API_KEY, ENV_VISION_KEY,
                               is_placeholder, resolve_api_key)
except Exception:  # pragma: no cover - 独立运行兜底
    ENV_API_KEY = "DEEPSEEK_API_KEY"
    ENV_VISION_KEY = "DEEPSEEK_VISION_KEY"
    _PH = ("sk-xxxx", "sk-your", "YOUR_API_KEY", "xxx")

    def is_placeholder(key):
        k = (key or "").strip()
        return (not k) or any(k.startswith(p) for p in _PH)

    def resolve_api_key(cfg_key, env_name=ENV_API_KEY):
        env = os.environ.get(env_name, "").strip()
        if env and not is_placeholder(env):
            return env, "环境变量 %s" % env_name
        if cfg_key and not is_placeholder(cfg_key):
            return str(cfg_key).strip(), "config.json"
        return "", "未配置"


def _pick_key(cfg_key, env_names):
    """按 env_names 顺序取第一个可用密钥；都没有则退回配置文件原值。

    视觉 Key 常常和对话 Key 是同一个，所以视觉侧依次尝试
    DEEPSEEK_VISION_KEY -> DEEPSEEK_API_KEY，避免迁移后视觉功能静默失效。
    """
    if isinstance(env_names, str):
        env_names = (env_names,)
    for name in env_names:
        env = os.environ.get(name, "").strip()
        if env and not is_placeholder(env):
            return env
    if cfg_key and not is_placeholder(cfg_key):
        return str(cfg_key).strip()
    return cfg_key or ""


def _key_source(cfg_key, env_names):
    """给自检/日志用：说明密钥来自哪里。"""
    if isinstance(env_names, str):
        env_names = (env_names,)
    for name in env_names:
        env = os.environ.get(name, "").strip()
        if env and not is_placeholder(env):
            return "环境变量 %s" % name
    if cfg_key and not is_placeholder(cfg_key):
        return "config.json（建议改用环境变量）"
    return "未配置"


def _extract_usage(data):
    """从 OpenAI 兼容响应里取 token 用量。取不到返回空 dict，绝不抛异常。"""
    try:
        u = (data or {}).get("usage") or {}
        if not isinstance(u, dict):
            return {}
        p = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        c = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        detailed = u.get("prompt_tokens_details")
        cached = 0
        if isinstance(detailed, dict):
            cached = int(detailed.get("cached_tokens") or 0)
        if not cached:
            cached = int(u.get("cached_tokens") or 0)
        # reasoning_tokens 也算输出（DeepSeek-R1 之类），保持账面完整
        c += int(u.get("reasoning_tokens") or 0)
        return {"prompt_tokens": p, "completion_tokens": c, "cached_tokens": cached}
    except Exception:
        return {}


def _post(api_base, api_key, payload, timeout):
    """返回 {"text": str, "usage": {...}}。兼容无 usage 字段的实现。"""
    req = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        usage = _extract_usage(data)
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'", "“", "”"):
            text = text[1:-1].strip()
        return {"text": text, "usage": usage}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"AI 接口 HTTP {e.code}: {body}")
    except (KeyError, IndexError):
        raise RuntimeError("AI 接口响应格式异常")
    except Exception as e:
        raise RuntimeError(f"AI 请求失败: {e}")


class AIClient:

    def __init__(self, ai_cfg):
        self.api_base = ai_cfg["api_base"].rstrip("/")
        # 2.0：环境变量优先，config.json 兜底
        self.api_key = _pick_key(ai_cfg.get("api_key", ""), ENV_API_KEY)
        self.key_source = _key_source(ai_cfg.get("api_key", ""), ENV_API_KEY)
        self.model = ai_cfg.get("model", "deepseek-chat")
        self.temperature = ai_cfg.get("temperature", 0.8)
        self.max_tokens = ai_cfg.get("max_tokens", 200)
        self.timeout = ai_cfg.get("timeout", 60)
        # 3.0：每次调用的用量留痕，供面板成本看板读取
        self.last_usage = {}
        self.last_cost = {}

    def _record(self, usage, kind):
        """记账到 SQLite（失败静默：统计挂了不能影响聊天）。"""
        try:
            from core import cost
            r = cost.record(self.model, kind, usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                            usage.get("cached_tokens", 0))
            self.last_cost = r
        except Exception:
            self.last_cost = {}
        return self.last_cost

    def chat(self, messages):
        """messages: [{"role": ..., "content": "..."}] -> 回复文本（str 不变）"""
        if is_placeholder(self.api_key):
            raise RuntimeError(
                "未配置 API Key：请设置环境变量 %s，或填写 config.json 的 ai.api_key"
                % ENV_API_KEY)
        r = _post(self.api_base, self.api_key, {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }, self.timeout)
        self.last_usage = r.get("usage", {})
        self._record(self.last_usage, "chat")
        return r["text"]


class VisionClient:
    """多模态客户端：识别图片（OpenAI 兼容 vision 格式）"""

    def __init__(self, vision_cfg):
        self.api_base = vision_cfg.get("api_base", "").rstrip("/")
        # 视觉 Key 常与对话 Key 共用，依次回退
        self.api_key = _pick_key(vision_cfg.get("api_key", ""),
                                 (ENV_VISION_KEY, ENV_API_KEY))
        self.model = vision_cfg.get("model", "")
        # 温度 0：描述要客观准确，不要自由发挥/幻觉
        self.temperature = vision_cfg.get("temperature", 0)
        self.timeout = vision_cfg.get("timeout", 90)
        self.last_usage = {}
        self.last_cost = {}

    @property
    def available(self):
        return bool(self.api_base and self.model) and not is_placeholder(self.api_key)

    def describe_image(self, image_path, question=""):
        """识别图片内容。question 为对方随图附言（可选）"""
        if not self.available:
            return ""
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        ext = image_path.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")

        prompt = (
            "请客观、准确地描述这张图片的真实内容。\n"
            "第一步：判断图片类型——真人实拍照片 / 游戏截图 / UI界面 / 动漫二次元 / 食物 / 风景 / 物品 / 其他\n"
            "第二步：按类型如实描述主体和关键细节\n"
            "硬性规则：\n"
            "1) 只描述图中真实可见的内容，绝对不编造、不脑补\n"
            "2) 游戏截图 / UI / 动漫角色：如实描述为'游戏画面''角色''界面'等——"
            "绝对不要用'穿''戴''你的'等暗示真人穿戴或拥有的词。"
            "对方是在分享这个内容（游戏截图/动漫/UI），不是在展示自己的穿搭\n"
            "3) 真人实拍照片：可以描述穿着/外貌，但聚焦于她分享的重点\n"
            "4) 食物 / 风景 / 物品：就描述食物/风景/物品本身，不要评论人物穿着\n"
            "5) 输出一两句话，口语化自然，像朋友口述"
        )
        if question:
            prompt = f"对方发这张图时附言「{question}」。{prompt}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }]
        try:
            # vision 模型带推理（reasoning_content），max_tokens 要给推理留足空间；
            # temperature=0 保证描述客观准确、不幻觉
            r = _post(self.api_base, self.api_key, {
                "model": self.model, "messages": messages,
                "max_tokens": 2000, "temperature": self.temperature,
            }, self.timeout)
        except Exception as e:
            log.warning("图片识别失败: %s", e)
            return ""
        self.last_usage = r.get("usage", {})
        try:
            from core import cost
            self.last_cost = cost.record(self.model, "vision",
                                         self.last_usage.get("prompt_tokens", 0),
                                         self.last_usage.get("completion_tokens", 0),
                                         self.last_usage.get("cached_tokens", 0))
        except Exception:
            pass
        return r["text"]
