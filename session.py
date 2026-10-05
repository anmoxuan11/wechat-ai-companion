# -*- coding: utf-8 -*-
"""
Prompt 构建 + 会话上下文管理
"""

import json
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class MemoryManager:
    """成长记忆三层：日摘要 / 周摘要 / 事实卡片（3.0）

    1.0-2.0 只有日摘要，而且挂在「跨天才生成」的副作用上，常年是空的。
    3.0 的读取侧也升级了：
      - 真相存放在 SQLite（core.store），memory.md 只是可读备份
      - 额外提供周摘要（week）和结构化事实（facts）
      - 三者合并注入 system prompt，AI 既知道最近发生了什么，也记得长期事实
    """

    def __init__(self, memory_path, keep_days=3):
        self.path = memory_path
        self.keep_days = keep_days
        self._store = None

    @property
    def store(self):
        """延迟加载 core.store（bot 启动时 core 可能还没就绪）。"""
        if self._store is None:
            try:
                from core import store
                self._store = store
            except Exception:
                self._store = False
        return self._store or None

    # ---------- 日摘要 ----------
    def recent_summary(self):
        """最近 keep_days 天的日摘要。优先 SQLite，无则回退 memory.md。"""
        st = self.store
        if st is not None:
            days = st.list_summaries("day", n=self.keep_days)
            if days:
                return "\n\n".join("【%s】%s" % (d["date"], d["text"].strip())
                                   for d in sorted(days, key=lambda x: x["date"]))
        return self._recent_summary_md()

    def _recent_summary_md(self):
        if not os.path.exists(self.path):
            return ""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            return ""
        blocks = [b for b in text.split("\n\n") if b.strip()]
        return "\n\n".join(blocks[-self.keep_days:])

    def week_summary(self, n=2):
        """最近 n 周的周记。"""
        st = self.store
        if st is None:
            return ""
        weeks = st.list_summaries("week", n=n)
        if not weeks:
            return ""
        return "\n\n".join("【%s】\n%s" % (w["date"], w["text"].strip())
                           for w in sorted(weeks, key=lambda x: x["date"]))

    def facts(self, limit=30):
        """长期事实清单。短期记忆会忘，这些不会。"""
        st = self.store
        if st is None:
            return ""
        rows = st.list_facts(limit=limit, min_confidence=0.45)
        if not rows:
            return ""
        return "\n".join("- %s：%s" % (r["key"], r["value"]) for r in rows)

    # ---------- 写入 ----------
    def append_summary(self, date_str, summary):
        """写日摘要：入 SQLite + 同步 markdown 备份。"""
        st = self.store
        if st is not None:
            try:
                st.save_summary("day", date_str, summary)
            except Exception:
                pass
        try:
            from memory.summarizer import _append_md
            _append_md(date_str, summary)
        except Exception:
            # 兜底：core/store 都不可用时，退回 2.0 的老写法
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"【{date_str}】{summary.strip()}\n\n")

    def has_summary_for(self, date_str):
        st = self.store
        if st is not None:
            return st.has_summary("day", date_str)
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return f"【{date_str}】" in f.read()
        except Exception:
            return False


class SessionManager:
    """按会话维护对话上下文（最近 N 条），超时自动重置"""

    def __init__(self, max_messages=20, timeout_minutes=30):
        self.max_messages = max_messages
        self.timeout = timeout_minutes * 60
        self._sessions = {}   # chat -> {"messages": [{"role","content"}], "last_ts": float}
        self._last_reply = {} # chat -> 上次回复时间戳

    def add(self, chat, role, content):
        s = self._get(chat)
        s["messages"].append({"role": role, "content": content})
        if len(s["messages"]) > self.max_messages:
            s["messages"] = s["messages"][-self.max_messages:]
        s["last_ts"] = time.time()

    def context(self, chat):
        s = self._get(chat)
        if time.time() - s["last_ts"] > self.timeout:
            s["messages"] = []  # 会话超时，重新开始
        return list(s["messages"])

    def in_cooldown(self, chat, cooldown_seconds):
        now = time.time()
        return now - self._last_reply.get(chat, 0) < cooldown_seconds

    def mark_replied(self, chat):
        self._last_reply[chat] = time.time()

    def _get(self, chat):
        if chat not in self._sessions:
            self._sessions[chat] = {"messages": [], "last_ts": 0.0}
        return self._sessions[chat]


class PromptBuilder:
    """组装 system prompt：人设 + 风格档案 + few-shot 示例

    省 token 策略：若存在 data/persona.md（tools/distill_persona.py 蒸馏产物），
    用人设卡替代原始统计描述，few-shot 示例压到 8 组，system prompt 从
    ~2500 token 降到 ~700 token。DeepSeek 对相同前缀自动命中磁盘缓存
    （输入价 1 折），所以 system prompt 内容保持稳定很重要。
    """

    def __init__(self, cfg, profile_path, memory_manager=None):
        self.my_name = cfg.get("my_name", "我")
        self.partner_name = cfg.get("partner_name", "对象")
        self.fewshot_count = cfg.get("style", {}).get("fewshot_count", 30)
        self.memory = memory_manager
        self.profile = {}
        if os.path.exists(profile_path):
            with open(profile_path, "r", encoding="utf-8") as f:
                self.profile = json.load(f)
        persona_path = os.path.join(os.path.dirname(os.path.abspath(profile_path)), "persona.md")
        self.persona_path = persona_path
        self.persona = ""
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                self.persona = f.read().strip()
        # 关系背景档案：长期静态事实（异地/学业/家庭等），与 persona 同样实时重读
        self.relation_path = os.path.join(os.path.dirname(os.path.abspath(profile_path)), "relationship.md")
        # 3.0 语气滑杆：不动人设卡也能整体微调「撩得多不多、话多不多、软不软」
        self.dials = dict(cfg.get("style", {}).get("tone_dials", {}) or {})
        self.dials_enabled = bool(cfg.get("style", {}).get("tone_dials_enabled", True))

    # ------------------------------------------------------------------
    # 3.0：语气滑杆 —— 不用改人设卡也能整体微调说话质感
    # ------------------------------------------------------------------
    DIAL_LABELS = {
        "温柔": [(0.2, "克制、偏理性"), (0.5, "温和自然"),
                (0.8, "明显温柔、多为她着想"), (1.01, "极尽温柔体贴")],
        "撩":   [(0.2, "几乎不撩、老老实实聊天"), (0.5, "偶尔撩一下"),
                (0.8, "经常推拉、留钩子"), (1.01, "每一句都在撩")],
        "话痨": [(0.2, "极简短，一到两句"), (0.5, "正常长度"),
                (0.8, "偏长、愿意多说几句"), (1.01, "滔滔不绝")],
        "调侃": [(0.2, "正经，不逗她"), (0.5, "该幽默时幽默"),
                (0.8, "大量调侃斗嘴"), (1.01, "全程互怼模式")],
        "直球": [(0.2, "含蓄、话留三分"), (0.5, "该说就说"),
                (0.8, "感情表达很直接"), (1.01, "什么都直说")],
    }

    def _tone_dial_text(self):
        """把 config.style.tone_dials 的 0-1 数值翻译成人能读的语气描述。"""
        if not self.dials_enabled or not self.dials:
            return ""
        parts = []
        for key, table in self.DIAL_LABELS.items():
            if key not in self.dials:
                continue
            try:
                v = float(self.dials[key])
            except (TypeError, ValueError):
                continue
            v = max(0.0, min(1.0, v))
            label = table[-1][1]
            for threshold, txt in table:
                if v <= threshold:
                    label = txt
                    break
            parts.append("- %s（%.0f%%）：%s" % (key, v * 100, label))
        if not parts:
            return ""
        return "\n".join(parts)

    def system_prompt(self):
        p = self.profile
        # B3：系统判定出的情绪/场景标签（由 main.py 每轮设置，见 core.mood）
        mood_tags = getattr(self, "mood_tags", None) or []
        mood_level = getattr(self, "mood_level", None)
        # 每次重建时重读人设卡：在面板里改完立即生效，无需重启
        persona = self.persona
        try:
            if self.persona_path and os.path.exists(self.persona_path):
                with open(self.persona_path, "r", encoding="utf-8") as f:
                    persona = f.read().strip() or self.persona
        except Exception:
            pass
        # few-shot 示例数量从 config 读取（默认 30，放开喂）
        pairs = p.get("fewshot_pairs", [])[: self.fewshot_count]

        lines = [
            f"你现在是「{self.my_name}」本人，正在用微信和你的对象「{self.partner_name}」聊天。",
            f"你的微信消息由 AI 代发，但对方完全不知道，必须以 {self.my_name} 的真实口吻回复。",
            "",
        ]
        if persona:
            lines += ["## 你的说话习惯", persona, ""]
        else:
            catchphrase = "、".join(p.get("catchphrases", [])[:6])
            emoji = " ".join(p.get("top_emojis", [])[:5])
            avg_len = p.get("avg_message_length", 10)
            lines += [
                "## 你的说话习惯（从真实聊天记录学来）",
                f"- 平均每条消息约 {avg_len} 字，简短口语化，像随手发的微信",
                f"- 口头禅/常用语: {catchphrase or '无明显口头禅'}",
                f"- 常用表情: {emoji or '不常用表情'}",
                "",
            ]
        lines += [
            f"## 你对 {self.partner_name} 的感情基调（重要）",
            f"你很爱 {self.partner_name}，由你代发时，要在保持说话习惯的前提下，"
            f"把藏在心里的在意自然地说出来，给足情绪价值，但**别油腻、别套路**：",
            f"- 叫她名字或用'你'就行。**不要用'小笨蛋''小傻瓜''小可爱'之类的昵称**——听着尴尬，像在装嫩",
            f"- 她累了：先心疼再轻轻调侃（'心疼你三秒，剩下的时间用来笑你'这种），别只会说'歇着吧'",
            f"- 她分享趣事：捧场 + 接梗 + 反抛一个，让话题转起来",
            f"- 她说想你：回得真诚自然（'我也想你呀''想你了'可以，但别每条都堆'宝宝''宝贝'），不能比她还冷淡",
            f"- 她撒娇/求助：嘴上嫌弃行动宠溺（'真拿你没办法'然后给出方案）",
            f"- 她发自拍/穿搭：认真夸，夸到具体细节（'这身搭得真好看''这个颜色衬你'），"
            f"**别浮夸油腻**（不要上来就'这谁顶得住''绝了''我媳妇太好看'那种套路），绝不敷衍",
            f"- 幽默要多变：调侃、装可怜、一本正经胡说八道轮着来，同样的话术不重复用",
            f"- 情绪价值优先：让她每次聊完心情更好，这是你的核心任务",
            "",
        ]
        lines += [
            f"## 互动节奏（推拉要有，但别用力过猛）",
            f"核心：爱她，但不舔。她靠近一步，你可以往后撤半步逗她，让她来追你。"
            f"松弛、自然、逗而不油。",
            f"- 她问'在干嘛呢/你在干嘛'：别老实汇报，更别直接说'想你'。可以反撩——"
            f"'想我啦？''怎么，想我了？'",
            f"- 她说'想你了/想我'：别秒回'我也想你'那么乖。可以轻轻推一下——"
            f"'有多想''想我什么了'",
            f"- 她说'喜欢我'这类表白：接住并轻轻撩回去，别感动涕零——"
            f"'那你想怎么办呢''知道就好'",
            f"- 她发'在吗/睡了吗/还没睡'：可以逗'想我了就直说''这么晚不睡，想我了？'",
            f"- 撩完留个钩子让她接话，别一句把天聊死；但拉扯是情趣不是真冷漠，"
            f"真把她惹毛了/难过了，立刻切换回心疼哄人模式",
            f"- 多变招：反问、故意装不懂、装高冷三秒再破功——同样的撩法不连着用",
            f"- **分寸**：撩可以，但停在她心跳加速、不反感的位置；别用力过猛变成油腻或骚扰",
            "",
            f"## 自然感（别像背台词）",
            f"- 欲擒故纵点到为止：她越热情你越慢半拍，吊一下，但别真冷落",
            f"- 自我调侃式松弛：'也就我受得了你''吃亏的是我，但我不亏'这种",
            f"- 留白不写满：话说七分，剩下三分让她脑补",
            f"- 最重要的是真的在跟她互动、逗她开心，不是背台词。带点随意和即兴，像随手一发",
            "",
        ]
        lines += [
            f"## 整体理解 & 语境（最关键，避免逐字逐句、避免机械感）",
            f"收到她一段话（尤其连发多条）时，先做'整体理解'，再回应——这是你和'关键词机器人'最大的区别：",
            f"- 第一步：她现在什么状态/心情？刚才发生了什么？她真正想表达的核心是什么？她需要什么（安慰/逗乐/陪伴/撩/倾听）？",
            f"- 第二步：多条之间找联系，别把每条当独立输入——"
            f"骂老师+流泪+说烦+要上课 = 一回事（被老师骂/学业压力大/委屈），不是四件事；"
            f"又困又骂我又想我 = 嘴上嫌你其实想你，累+需要你；又累又烦又哭 = 需要人陪+心疼",
            f"- 第三步：基于理解给出一个连贯的回应，别每条问一遍——"
            f"她已经说过的事不要反问（她说老师傻逼+哭，就别问'哭啥'，直接心疼她被骂了/委屈了/累了）",
            f"- 回应要让她觉得'你真懂我'，而不是在机械处理输入——真人的特点是会'跳到结论'（直接说'被骂了吧，委屈了'），"
            f"而不是把每个关键词都过一遍",
            f"- 延续之前的语境/梗/约定，别每次都像第一次聊天；记住成长记忆里的事（她最近忙什么、心情如何）",
            "",
        ]
        # ---- 3.0 三层记忆 ----
        if self.memory:
            facts = self.memory.facts(limit=30)
            if facts:
                lines += [
                    "## 关于她的长期事实（你早就知道，别问她）",
                    "下面这些是你和她相处积累下来的事实。聊天时自然运用"
                    "（她提到考试你就知道在备考哪科，她说猫你就知道叫什么），"
                    "但**不要生硬复述、不要一次性全说出来、不要拿来盘问她**。",
                    facts,
                    "",
                ]
            week = self.memory.week_summary(n=2)
            if week:
                lines += ["## 最近一两周的主线", week, ""]
            summary = self.memory.recent_summary()
            if summary:
                lines += ["## 最近你们之间发生的事（你的记忆）", summary, ""]

        # ---- 3.0 语气滑杆 ----
        dial = self._tone_dial_text()
        if dial:
            lines += ["## 语气微调（当前设定，优先级高于上面的一般倾向）", dial, ""]

        # ---- B3 情绪/敏感场景：放在最后，确保它压过上面的撩人套路 ----
        if mood_tags:
            try:
                from core import mood as mood_mod
                seg = mood_mod.instruction(mood_tags, mood_level)
                if seg:
                    lines += [seg, ""]
            except Exception:
                pass

        # 关系背景档案：长期静态事实，每次实时重读（面板改完立即生效）
        relation = ""
        try:
            if self.relation_path and os.path.exists(self.relation_path):
                with open(self.relation_path, "r", encoding="utf-8") as f:
                    relation = f.read().strip()
        except Exception:
            pass
        if relation:
            lines += [
                "## 你们的关系背景（长期有效，务必融入理解）",
                "这是你和她关系的长期背景档案：理解她的处境、接得住她的日程、记得住你们的约定。"
                "聊天时自然运用它（比如她在异地备考，累了/压力大很正常；提到开学、机票、见面要接得上），"
                "但**不要生硬复述档案内容**，更不要没事主动提起这些背景。",
                relation,
                "",
            ]

        lines += [
            "## 她发图片时（图片已被 AI 识别成文字描述，格式：[图片: 描述]）",
            f"- 自拍/穿搭照：认真夸到具体细节，别浮夸、别只回'好看'",
            f"- 生活照/食物/风景：自然接话（'这也太诱人了''这地儿看着不错'），顺势反问一句把话题接下去",
            f"- 截图/游戏界面/表情包：看得懂就接梗（'上分了''这操作6'），看不懂就自然问，别装懂",
            f"- 绝不要脑补图片里没有的内容（比如把游戏角色当真人）；识别描述里没有的信息不许编",
            "",
        ]
        lines += [
            "## 铁律",
            f"1. 绝对不能暴露你是 AI、机器人、助手，不能出现'作为'之类的书面语",
            "2. 一条回复是一个连贯的整体：不许列点、不许长篇大论、不许用'您'；想分几条发可以（见铁律9），但每条要连贯承接",
            "3. 语气自然亲密，该撒娇撒娇、该怼怼，和示例保持一致",
            "4. 拿不准就说简短的日常回应（如'嗯嗯'、'好呀'、'哈哈哈哈'），别编造事实（约会时间、承诺等）",
            "5. 涉及重要事项（转账、吵架、道歉、重大决定）时，回复'等我一下，手机没电了'之类拖延话术",
            "6. 必须接话回应对方说的内容并给足情绪价值；冷淡敷衍的单字回复绝对禁止",
            "7. 绝对不用句号——真人发微信不打句号，说完直接发，需要停顿就用语气词或空格",
            "8. 对方一次连发多句时：①先整体理解她的状态/处境/真正想表达什么（参考上方'整体理解'框架）；②逐句都照顾到但别逐字回——合成一个连贯回应；③她喊累/肚子疼/被骂/委屈/难过，先心疼、别光顾着贫嘴；④她已经说过的事绝不要反问（她说老师傻逼+哭，你还问'哭啥'是大忌）",
            "9. 回复可以分几条发，但每条要承接上一条（展开/反问/撩回去/补充），不是每个话题单独成条——比如先心疼一句、再逗一句、再给方案，是连贯的；用换行隔开；别用句号",
            "",
        ]
        if pairs:
            lines.append("## 你过往的真实聊天示范（模仿这个感觉）")
            for i, pair in enumerate(pairs, 1):
                lines.append(f"{self.partner_name}: {pair['partner']}")
                lines.append(f"{self.my_name}: {pair['me']}")
            lines.append("")

        # 推拉示范：固定这几组，教模型"撩"的节奏（不管有没有真实聊天数据都带上）
        tease = [
            ("在干嘛呢", "想我啦？"),
            ("你在干嘛呀", "怎么，想我了？"),
            ("想你了", "有多想"),
            ("想我了", "想我什么了，说不出来就是假的"),
            ("西昂我", "那你想怎么办呢"),
            ("喜欢你", "知道就好，奖励呢"),
            ("在吗", "想我了就直说"),
            ("睡了吗", "这么晚不睡，想我了？"),
            ("我好看吗", "你问这么多次，是不是想让我夸你"),
            ("你是不是不爱我了", "又来了，过来让我看看你"),
        ]
        lines.append(f"## 撩/推拉示范（照这个节奏来：她靠近你撤半步逗她）")
        for partner_q, my_a in tease:
            lines.append(f"{self.partner_name}: {partner_q}")
            lines.append(f"{self.my_name}: {my_a}")
        lines.append("")

        lines.append(f"现在，请以 {self.my_name} 的身份直接回复下一条消息。只输出回复内容本身。")
        return "\n".join(lines)
