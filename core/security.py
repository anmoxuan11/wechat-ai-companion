# -*- coding: utf-8 -*-
"""安全层（2.0）

解决 1.0 的 P0-2 / P0-3：

  P0-2  API Key 明文写在 config.json，且随桌面副本和打包产物扩散
        → 改为优先读环境变量，config.json 只留占位符

  P0-3  13 个写端点零防护。虽然只绑 127.0.0.1，但：
        - 本机任意程序都能 POST /api/clear_memory 删掉人设卡和记忆
        - 浏览器不区分「本地」和「可信」，一个 CSRF 表单就能触发
        → 加一次性随机 token + Origin 校验
"""

import hmac
import os
import secrets
import time

# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------

ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_VISION_KEY = "DEEPSEEK_VISION_KEY"

# 1.0 里被当成「未配置」的占位符
PLACEHOLDERS = ("sk-xxxx", "sk-your", "YOUR_API_KEY", "", "xxx")


def refresh_env_from_registry(names=(ENV_API_KEY, ENV_VISION_KEY)):
    """Windows 专属补丁：把「用户环境变量」从注册表补进当前进程。

    为什么需要它：Windows 的用户环境变量写入 HKCU\\Environment 后，**只有重新
    登录**（或 Explorer 重启后新开的进程）才会继承。用 SetEnvironmentVariable
    之类的 API 设过也一样——很多场景（计划任务、从 IDE/调试器里起的进程、
    长时间没注销的会话）拿到的是**旧的**环境块，于是密钥看起来「明明设了却读不到」。

    这里只读注册表并补刀当前进程的内存环境，**绝不落盘、不打印**。
    非 Windows 平台直接返回 0。
    """
    if os.name != "nt":
        return 0
    n = 0
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in names:
                if os.environ.get(name, "").strip():
                    continue        # 已经有了就别动
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if val and str(val).strip():
                    os.environ[name] = str(val).strip()
                    n += 1
    except Exception:
        return 0
    return n


def is_placeholder(key):
    if not key:
        return True
    k = str(key).strip()
    if not k:
        return True
    return any(k.startswith(p) for p in PLACEHOLDERS if p)


def resolve_api_key(cfg_key, env_name=ENV_API_KEY):
    """解析 API Key：环境变量优先，其次配置文件，占位符视为未配置。

    env_name 可以传字符串，也可以传按顺序回退的元组（如视觉 Key 共用对话 Key）。

    返回 (key, source)；未配置时 key 为 ""。
    """
    names = (env_name,) if isinstance(env_name, str) else tuple(env_name)
    for n in names:
        env = os.environ.get(n, "").strip()
        if env and not is_placeholder(env):
            return env, "环境变量 %s" % n
    if cfg_key and not is_placeholder(cfg_key):
        return str(cfg_key).strip(), "config.json"
    return "", "未配置"


def mask_key(key):
    """脱敏展示：只留前 6 后 4。"""
    if not key:
        return "(未配置)"
    k = str(key)
    if len(k) <= 10:
        return "*" * len(k)
    return "%s...%s" % (k[:6], k[-4:])


def audit_config_secrets(cfg):
    """体检用：报告密钥来源与是否明文落盘。

    返回 [{name, source, masked, plaintext_in_config}]
    """
    out = []
    ai = (cfg.get("ai") or {})
    vs = (cfg.get("vision") or {})
    # 视觉 Key 常与对话 Key 是同一个，因此视觉侧允许回退到通用变量
    for label, section, env in (
        ("对话 Key", ai, (ENV_API_KEY,)),
        ("视觉 Key", vs, (ENV_VISION_KEY, ENV_API_KEY)),
    ):
        key, src = resolve_api_key(section.get("api_key", ""), env)
        out.append({
            "name": label,
            "source": src,
            "masked": mask_key(key),
            "plaintext_in_config": bool(section.get("api_key"))
            and not is_placeholder(section.get("api_key")),
        })
    return out


# ---------------------------------------------------------------------------
# 面板访问令牌（防 CSRF / 本机其他程序误操作）
# ---------------------------------------------------------------------------

TOKEN_HEADER = "X-Panel-Token"
TOKEN_BYTES = 32


class TokenGuard:
    """每次启动生成一个随机 token，注入 HTML，前端请求必须带回。"""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.token = secrets.token_hex(TOKEN_BYTES) if enabled else ""
        self.issued = time.time()

    def check(self, headers, origin_host=None):
        """校验请求。返回 (ok, reason)。

        - token 不匹配 → 拒绝
        - Origin 存在且不是本站 → 拒绝（防 CSRF）
        """
        if not self.enabled:
            return True, ""
        supplied = ""
        try:
            supplied = (headers.get(TOKEN_HEADER) or "").strip()
        except Exception:
            supplied = ""
        if not supplied or not hmac.compare_digest(supplied, self.token):
            return False, "缺少或错误的面板令牌"

        if origin_host:
            # Origin 形如 http://127.0.0.1:8765
            allowed = {"127.0.0.1", "localhost"}
            host = origin_host
            if "://" in host:
                host = host.split("://", 1)[1]
            host = host.split("/")[0]
            host = host.split(":")[0]
            if host not in allowed:
                return False, "跨站请求被拒绝"
        return True, ""

    def meta_tag(self):
        """注入 HTML 的 <meta>，前端据此取 token。"""
        if not self.enabled:
            return ""
        return '<meta name="panel-token" content="%s">' % self.token


# 需要保护的写操作（读操作如 /api/status 不强制）
PROTECTED_POST = frozenset({
    "/api/start", "/api/stop", "/api/restart", "/api/shutdown",
    "/api/fix", "/api/pause", "/api/resume", "/api/set_listen",
    "/api/clear_memory", "/api/save_persona", "/api/save_relationship",
    "/api/save_memory", "/api/advisor_chat", "/api/self_check",
    "/api/proposal/approve", "/api/proposal/reject", "/api/proposal/resolve",
    "/api/schedule/add", "/api/schedule/toggle", "/api/schedule/delete",
})

# 破坏性操作：额外要求显式确认参数，避免误触/CSRF 一键清空
DESTRUCTIVE = frozenset({"/api/clear_memory", "/api/stop", "/api/shutdown"})

CONFIRM_FIELD = "confirm"


def require_confirm(path, payload):
    """破坏性操作必须带 confirm=true。"""
    if path in DESTRUCTIVE:
        return bool(payload.get(CONFIRM_FIELD))
    return True
