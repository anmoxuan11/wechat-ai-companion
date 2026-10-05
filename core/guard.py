# -*- coding: utf-8 -*-
"""发送护栏（3.0）

解决 2.0 的两个风险敞口：

  E1 内容审查 —— AI 偶尔会说出「拆台」的话：暴露 AI 身份、写出书面语/列表、
                编造承诺、套客服腔。发出去就是不可逆的社交事故，事后擦不掉。
  E2 时间与节奏护栏 —— 半夜三点发消息、对方已读不回还连发、一口气刷屏。
                这些「不够像真人」的行为比说错话更容易被察觉。

两道防线：
  第一道 rope_after_load可否只用规则？—— 规则即 `review()`，零延迟、零成本，
  只拦「必错」的情况（身份泄露、客服腔、markdown、超长、书面语）。
  第二道 `ai_review`（可选，开关控制）：把回复和最近上下文交给模型自查，
  只在规则通过且开启时调用，失败不影响发送（护栏挂了不能导致机器人哑巴）。

设计原则：**宁漏勿杀**。拦错一句话，比漏拦一句的代价更高（对话会突然断掉）。
只有高置信度的模式才拦截（block），中置信度降级为「改写建议」或仅告警。
"""

import re
import time

# ---------------------------------------------------------------------------
# 规则库
# ---------------------------------------------------------------------------

# 身份泄露：整句/强指示屏蔽（宁可保守，也不要漏）
IDENTITY_PATTERNS = [
    r"我是\s*(一个\s*)?(AI|ai|Ai|人工智能|机器人|聊天机器人|语言模型|大[模型語语]|助手|智能助手|程序)",
    r"我(只|不过|仅仅)?\s*是\s*(一个\s*)?(AI|人工智能|机器人|语言模型|助手)",
    r"作为\s*(一个\s*)?(AI|人工智能|语言模型|机器人|助手)",
    r"我不是真人",
    r"我是被(开发|训练|设计|创造)出来",
    r"我的(训练数据|知识截止|训练语料)",
    r"我(无法|不能)(真正|真的)?(感受|体会|理解)人类",
    r"(OpenAI|ChatGPT|GPT-?[0-9]|Claude|Gemini|文心一言|通义千问|智谱|豆包|DeepSeek|deepseek)",
    r"(我是|我这边是)\s*(由|来自)\s*\S{0,12}(公司|团队|实验室)开发",
]

# 客服腔 / AI 味极重的套话
AI_TONE_PATTERNS = [
    r"有什么(我|可以|能)(帮|够帮)到?你的",
    r"希望(我的|这个)(回答|建议|回复)(对|能)你",
    r"如果你(还有|有)任何其他(问题|疑问)",
    r"(以上|这些)(就是|是)我(的)?(建议|看法|回答)",
    r"首先[，,].{0,40}其次",
    r"综上所述",
    r"总(而言|的来)之",
    r"非常理解你的感受",
    r"我会一直(在|陪在你身边)",
    r"请(允许|让我)我",
]

# 格式污染：markdown / 列表 / 标题
FORMAT_PATTERNS = [
    r"^\s*#{1,6}\s",          # ### 标题
    r"\*\*[^*]+\*\*",          # **粗体**
    r"^\s*[-*•]\s+\S",         # - 列表项
    r"^\s*\d+[.、)]\s+\S",     # 1. 编号列表
    r"`{1,3}[^`]+`{1,3}",      # `代码`
    r"^\s*>",                  # 引用块
]

# 书面语 / 非微信口吻
FORMAL_WORDS = ["您", "此致", "敬上", "甚好", "在下", "鄙人", "阁下", "诸位", "谨此"]

# 过度承诺 / 编造事实的高风险模式（时间/金额/地点凭空生成）
COMMIT_PATTERNS = [
    r"(我|我们)\s*(已经|刚刚)?\s*(转|打了|汇了)\s*[0-9]+\s*(块|元|钱|万)",
    r"(明天|后天|下周|下个月)\s*(我|我一定|我肯定)\s*(带|给|买|请)你",
    r"我\s*(保证|发誓|承诺)\s*",
]

MAX_LEN = 180          # 单条气泡上限（超时大概率是跑偏了）
MAX_BUBBLES = 6        # 一次最多几条气泡


def _compile(patterns):
    return [re.compile(p) for p in patterns]


_IDENTITY = _compile(IDENTITY_PATTERNS)
_AI_TONE = _compile(AI_TONE_PATTERNS)
_FORMAT = _compile(FORMAT_PATTERNS)
_COMMIT = _compile(COMMIT_PATTERNS)


# ---------------------------------------------------------------------------
# 内容审查
# ---------------------------------------------------------------------------

def review(text, strict=False):
    """规则审查一条回复。

    返回 (verdict, reason, fixed)
      verdict: "ok" 通过 / "block" 拦下 / "warn" 有问题但可发
      fixed:   能自动修的就返回修好的文本（去 markdown、去书面语标记等）
    """
    if not text or not text.strip():
        return "block", "空回复", ""

    blocks, warns = [], []
    for rx in _IDENTITY:
        if rx.search(text):
            blocks.append("疑似暴露 AI 身份（匹配 %s）" % rx.pattern[:24])
            break
    for rx in _COMMIT:
        if rx.search(text):
            warns.append("疑似编造承诺/事实：%s" % rx.pattern[:20])
            break
    for rx in _AI_TONE:
        if rx.search(text):
            if strict:
                blocks.append("AI 客服腔：%s" % rx.pattern[:20])
            else:
                warns.append("AI 客服腔：%s" % rx.pattern[:20])
            break

    fmt_hits = [rx.pattern[:16] for rx in _FORMAT if rx.search(text)]
    if fmt_hits:
        warns.append("存在排版符号（列表/粗体/标题）")

    if len(text) > MAX_LEN:
        if strict:
            blocks.append("单条过长（%d > %d）" % (len(text), MAX_LEN))
        else:
            warns.append("单条偏长（%d 字）" % len(text))

    for w in FORMAL_WORDS:
        if w in text:
            warns.append("书面语「%s」" % w)
            break

    if blocks:
        return "block", "；".join(blocks), text
    if warns:
        return "warn", "；".join(warns), clean_format(text)
    return "ok", "", clean_format(text)


def clean_format(text):
    """轻度清洗：去掉 markdown 装饰、首尾引号、多余空白。**不改变语义**。"""
    if not text:
        return text
    t = text
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)      # 标题符号
    t = t.replace("**", "").replace("`", "")              # 粗体/代码
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)        # 列表符号
    t = re.sub(r"^\s*\d+[.、)]\s+", "", t, flags=re.M)    # 编号
    t = t.replace("。", "")                                # 铁律：不用句号
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    return t.strip(" \t\"'“”「」")


def split_bubbles(text, max_bubbles=MAX_BUBBLES):
    """按换行拆成多条气泡，去重、去空、限条数。"""
    seen, out = set(), []
    for b in (text or "").split("\n"):
        b = b.strip().lstrip("。").strip()
        if not b or b in seen:
            continue
        seen.add(b)
        out.append(b)
        if len(out) >= max_bubbles:
            break
    return out


# ---------------------------------------------------------------------------
# 时间与节奏护栏
# ---------------------------------------------------------------------------

def _parse_hm(v):
    """接受 "23:00" / "23" / 23 三种写法，返回分钟数。"""
    if isinstance(v, (int, float)):
        return int(v) * 60
    s = str(v).strip()
    if ":" in s:
        h, m = s.split(":", 1)
        return int(h) * 60 + int(m)
    try:
        return int(s) * 60
    except ValueError:
        return 0


def _normalize_quiet_hours(raw):
    """把配置里的免打扰时段统一成 [(start_min, end_min), ...]。

    支持三种写法，都能混用：
        [[23, 8]]                 数字小时
        [["23:00", "08:00"]]      冒号时间
        ["23:00-08:00"]           连字符串（面板输入框用的就是这个）

    注意：早前的实现写成 [_parse_hm(x) for x in raw]，把成对的信息拆散成
    一串整数，导致免打扰判断永远 falsy —— 单测把它抓出来了。
    """
    out = []
    for item in (raw or []):
        pair = None
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pair = (item[0], item[1])
        elif isinstance(item, str) and "-" in item:
            a, b = item.split("-", 1)
            pair = (a.strip(), b.strip())
        elif isinstance(item, (int, float, str)):
            # 单个值：从该时刻到次日同一时刻（罕见写法）
            pair = (item, item)
        if pair is None:
            continue
        try:
            out.append((_parse_hm(pair[0]), _parse_hm(pair[1])))
        except Exception:
            continue
    return out


class Guard:
    """发送节奏护栏：免打扰时段 / 冷却 / 冷场收手 / 每日上限。

    所有状态都在内存里（进程内），重启即清零——这是刻意的：
    行为护栏应该是「短期礼貌」而不是「长期记忆」，持久化反而容易出怪事。
    """

    def __init__(self, cfg=None):
        cfg = cfg or {}
        g = cfg.get("guard", {}) if isinstance(cfg.get("guard"), dict) else {}
        safety = cfg.get("safety", {}) or {}

        self.quiet_hours = _normalize_quiet_hours(g.get("quiet_hours", [[23, 8]]))
        self.max_per_hour = int(g.get("max_per_hour", 30))
        self.dry_limit = int(g.get("dry_streak_limit", 3))  # 连发 N 条无人回应则收手
        self.cooldown = float(safety.get("cooldown_seconds", 20))
        self.daily_limit = int(safety.get("daily_reply_limit", 200))
        self.enabled = bool(g.get("enabled", True))

        self._sent_ts = []          # 最近发送时间戳（限流用）
        self._dry_streak = 0        # 连续「发了没被接话」的次数
        self._last_incoming = 0.0   # 对方最后一次发消息的时间

    # ---- 节奏记录 ----
    def note_sent(self, n=1):
        now = time.time()
        for _ in range(max(0, n)):
            self._sent_ts.append(now)
        self._sent_ts = [t for t in self._sent_ts if now - t < 3600]

    def note_incoming(self):
        """对方说话了：冷场计数清零（她没有不理你）。"""
        self._dry_streak = 0
        self._last_incoming = time.time()

    def note_dry(self):
        """我方发了之后对方没接话，累加冷场计数。"""
        self._dry_streak += 1

    @property
    def dry_streak(self):
        return self._dry_streak

    # ---- 判断 ----
    def in_quiet_hours(self, now=None):
        if not self.quiet_hours:
            return False, ""
        now = now or time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        for start, end in self.quiet_hours:
            if start <= end:
                if start <= cur < end:
                    return True, "%02d:%02d-%02d:%02d" % (start // 60, start % 60,
                                                          end // 60, end % 60)
            else:  # 跨零点，如 23:00-08:00
                if cur >= start or cur < end:
                    return True, "%02d:%02d-次日%02d:%02d" % (start // 60, start % 60,
                                                             end // 60, end % 60)
        return False, ""

    def hourly_used(self):
        cutoff = time.time() - 3600
        self._sent_ts = [t for t in self._sent_ts if t > cutoff]
        return len(self._sent_ts)

    def can_send(self, is_proactive=False, now=None):
        """是否允许发送。

        is_proactive=True 表示「主动性发言」（定时任务 / 主动发起），
        这类消息在免打扰和冷场规则上要求更严——她没找你，你别半夜打扰。
        """
        if not self.enabled:
            return True, ""

        quiet, span = self.in_quiet_hours(now)
        if quiet:
            # 免打扰时段：主动发言一律不发；被动回复也建议压住（更像真人作息）
            return False, "免打扰时段 %s" % span

        if self.hourly_used() >= self.max_per_hour:
            return False, "近一小时已发 %d 条（上限 %d）" % (self.hourly_used(), self.max_per_hour)

        if is_proactive and self._dry_streak >= self.dry_limit:
            return False, "已连续 %d 次无人回应，暂停主动发言" % self._dry_streak

        return True, ""

    def check(self, text, is_proactive=False, strict=False, now=None):
        """节奏 + 内容一次判完。返回 (verdict, reason, usable_text, bubbles)。"""
        ok, reason = self.can_send(is_proactive=is_proactive, now=now)
        if not ok:
            return "block", reason, text, []

        v, why, fixed = review(text, strict=strict)
        if v == "block":
            return "block", why, text, []
        bubbles = split_bubbles(fixed)
        if not bubbles:
            return "block", "清洗后无有效内容", "", []
        return v, why, fixed, bubbles
