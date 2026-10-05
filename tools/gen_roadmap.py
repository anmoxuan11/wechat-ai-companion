# -*- coding: utf-8 -*-
"""生成 3.0 优化方案清单（Markdown + HTML 方案墙）。

数据只维护一份，同时产出两种格式，避免两边不一致。
"""
import io
import os
import html
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

P0, P1, P2, P3 = "P0", "P1", "P2", "P3"

ITEMS = [
    # ---------------- A 必修 ----------------
    dict(id="A1", cat="必修", pri=P0, title="成长记忆从未真正生成（招牌功能是死的）",
         now="data/memory.md 是 0 字节，但 data/daily 里躺着 数百条历史对话。",
         why="_summarize_yesterday() 只在 _check_daily_reset() 里被调用，而后者又只在 _flush() 里被调用 "
             "——也就是「只有在准备发一条回复、且刚好跨天」时才会沉淀记忆。机器人一暂停、"
             "或那天没触发回复，记忆就永远不更新。而且它只看「昨天」一天。",
         how="① 抽一个独立的 memory_worker 线程，启动即扫描 data/daily 下所有缺摘要的日期并按序补跑；"
             "② 补跑加节流（每次启动最多补 N 天，避免一口气烧掉一堆 token）；"
             "③ 面板加「立即沉淀记忆」按钮 + 进度显示；④ 补跑结果写进事件时间线。",
         gain="「跨天成长」这个核心卖点真正生效；AI 终于能记住她前几天在忙什么、心情如何。",
         eff="M", risk="低（AI 调用失败已有兜底，补跑失败不影响主流程）"),

    dict(id="A2", cat="必修", pri=P0, title="main.py 的 PID_PATH 未定义，bot.pid 永远写不出来",
         now="main.py 第 126 行用 PID_PATH 写 bot.pid，但全文件从未定义这个常量。",
         why="NameError 被 try/except Exception 静默吞掉，所以没有任何报错。后果：面板 stop_bot() 的"
             "「心跳文件失效时的兜底路径」永远拿不到 PID，只能靠猜。",
         how="① 补上 PID_PATH 常量；② 把这一段的 except 从「静默 pass」改成「log.warning」，"
             "避免同类问题再被吞掉；③ 顺手接 core.atomicio 原子写。",
         gain="关停/重启机器人的可靠性提升，不再出现「关不掉」或「误杀」的玄学问题。",
         eff="S", risk="极低"),

    dict(id="A3", cat="必修", pri=P0, title="schedule.json 读-改-写非原子，面板与 bot 同时写会丢任务",
         now="_run_job() 里是 load → 改 last_run → dump；面板 save_schedule() 也是整文件覆盖。",
         why="两个进程（面板 / bot）加两个线程（主循环 / 定时任务线程）都在写同一个文件，"
             "没有任何锁。经典后果：定时任务刚标记「今天已执行」，面板改个开关就被覆盖回去，"
             "于是同一条早安一天发两次。",
         how="① 统一走 core.atomicio（tmp → fsync → replace）；② 加一个跨进程文件锁（.schedule.lock）；"
             "③ 写之前重新读一次（read-modify-write 竞态窗口最小化）。",
         gain="定时任务不再重复触发/丢失，配置改动不丢。",
         eff="S", risk="低"),

    dict(id="A4", cat="必修", pri=P0, title="main.py 完全没接入 2.0 的 core 模块",
         now="控制文件、心跳、每日日志、定时任务全是裸 open 写；路径硬编码 BASE_DIR。",
         why="2.0 给面板做了原子写 + 备份 + 统一路径解析，bot 侧还是 1.0 写法。两边对同一批文件的"
             "写入策略不一致，是最容易出诡异 bug 的地方。",
         how="① main.py 引入 core.atomicio / core.paths / core.proclog；"
             "② 日志接入 rotate_if_needed；③ 路径走 _resolve_base_dir 同一套逻辑。",
         gain="bot 与面板行为一致；崩溃/断电不再写坏控制文件和配置。",
         eff="M", risk="中（改动面较大，建议配回归测试）"),

    dict(id="A5", cat="必修", pri=P0, title="历史日志没有补跑通道，数百条对话白白浪费",
         now="历史每日日志一条都没转成记忆，且现在没有任何入口能补。",
         why="见 A1：补跑能力不存在，不只是没触发。",
         how="随 A1 一起做：补跑工具独立成 CLI（python main.py --backfill-memory --days 14），"
             "面板也能点。补跑时按天倒序、每天一次 AI 调用、失败可重试。",
         gain="现有历史对话一次性变成可用的长期记忆，AI 立刻变「懂事」。",
         eff="S", risk="低"),

    # ---------------- B 让 AI 更像人 ----------------
    dict(id="B1", cat="体验", pri=P1, title="回复预演（干跑）：改完人设卡先看它怎么回",
         now="改人设卡 / 关系档案只能盲改，要验证就得等她真发消息，或者直接拿真人做实验。",
         why="这是迭代语气的最大障碍。改一个字要等下次真实对话才知道效果，试错成本极高。",
         how="面板加「预演」输入框：输入她可能说的话 → 服务端用真实 system prompt + 当前上下文生成回复，"
             "**只返回不发送**；支持一次生成 3 个候选（temperature 微调）供挑选；"
             "可指定「假装她刚连发这三条」测试 burst 聚合效果。",
         gain="调语气从「靠运气」变成「所见即所得」，半小时能调完过去一周的活。",
         eff="M", risk="低（只是多调一次 API，不发送）"),

    dict(id="B2", cat="体验", pri=P1, title="主动发起：从「自动回复」进化成「会找你说话」",
         now="机器人只能被动应答，她不说话就永远沉默。",
         why="真人恋爱里主动问候、主动分享是刚需。纯被动的机器人聊久了会觉得「他从不找我」。",
         how="① 沉默时长阈值触发（如 6 小时，可配）；② 时间窗限制（只在 9:00–23:00，且避开她可能在忙的时段）；"
             "③ 一天最多主动 1–2 次，且与定时任务互斥；④ 主动内容由 AI 结合记忆生成（「今天考试加油」而不是固定早安）；"
             "⑤ 主动失败的兜底：她没回就不追加。",
         gain="从「自动回复工具」变成「陪聊」，这是体验上最大的一档跃升。",
         eff="L", risk="中（主动发消息风险较高，必须有开关 + 次数上限 + 时间窗三重护栏）"),

    dict(id="B3", cat="体验", pri=P1, title="情绪与敏感场景分级处理",
         now="只在 prompt 里写了一句「涉及吵架/道歉/转账就拖延话术」，没有工程兜底。",
         why="prompt 里的规矩模型不一定遵守，一旦在吵架时回了个玩笑话，后果比不回严重得多。",
         how="① 发送前用一个轻量分类器（关键词 + 可选小模型）打标：争吵 / 难过 / 道歉 / 生病 / 金钱 / 承诺 / 告别；"
             "② 命中高敏标签 → 强制切换到「保守应答模式」（更短、更温柔、不撩、不玩笑），或直接转人工（通知你 + 暂停自动回复）；"
             "③ 命中「承诺/金钱」→ 直接拦截不发送，转给你。",
         gain="把「AI 说错话」的尾部风险从不可控变成可控。",
         eff="M", risk="低（只做收敛不做增强，误判代价小）"),

    dict(id="B4", cat="体验", pri=P1, title="回复质量反馈闭环（赞 / 踩）",
         now="没有任何「这条回得好不好」的记录，模型永远不知道自己哪儿不对。",
         why="没有反馈就没有优化方向，只能靠你凭感觉改 prompt。",
         how="① 面板每日记录里每条 AI 回复加赞/踩按钮；② 写入 data/feedback.jsonl（含原文、回复、时间戳、标签）；"
             "③ 面板显示近 7 天「踩率」曲线；④ 踩的样本自动进入「待复盘」列表，供参谋分析；"
             "⑤ 长期可作为 few-shot 负样本，或微调依据。",
         gain="有了可量化的质量指标，优化不再靠拍脑袋。",
         eff="M", risk="低"),

    dict(id="B5", cat="体验", pri=P1, title="语气强度滑杆（不写 md 也能调风格）",
         now="要调语气必须改 persona.md 或改 prompt 源码，门槛高。",
         why="很多微调需求其实只是「再撩一点 / 别这么油 / 话再少点」，不值得去改长文本。",
         how="面板加一组滑杆：撩的程度、话多话少、表情使用频率、玩笑比例、关心直白度。"
             "存进 config.json 的 style_tuning，PromptBuilder 按档位拼接对应指令段。",
         gain="调风格从「写作文」变成「拧旋钮」，随时可回滚。",
         eff="M", risk="低"),

    dict(id="B6", cat="体验", pri=P2, title="「对方正在输入」感知与抢答抑制",
         now="对方连发时靠 settle_seconds 攒 3 秒再回，但她边打字边发时仍可能抢答。",
         why="真人不会在你还没说完时插话。抢答是很强的「这不是真人」信号。",
         how="若后端能拿到「正在输入」状态（wxauto/wechatauto 部分版本可得），则延长 settle；"
             "拿不到就用启发式：最后一条消息 <2 秒且连续多条 → 再等一轮。",
         gain="对话节奏更像真人。",
         eff="S", risk="低（后端不支持时优雅降级）"),

    dict(id="B7", cat="体验", pri=P2, title="更多消息类型：语音 / 表情包 / 视频 / 红包 / 撤回",
         now="只处理文本和图片；语音、表情包、视频、红包、撤回一律被忽略或当成空消息。",
         why="她发个表情包或语音，机器人不接话，对话就断了。",
         how="① 表情包：取包名/包描述当文本处理（「[表情包: 熊猫头-无语]」）；② 语音：若后端能拿到转写文本则处理，"
             "拿不到则回一句自然的兜底（「在忙，晚点听」）；③ 红包/转账：只记日志不回复；"
             "④ 撤回：记为「她撤回了一条」，不追问。",
         gain="覆盖真实聊天的长尾场景，不再出现「她发了东西机器人没反应」的尴尬。",
         eff="M", risk="中（依赖后端能力，需要按后端做能力探测）"),

    dict(id="B8", cat="体验", pri=P2, title="多监听对象各自独立的配置",
         now="config.listen 是个列表，但定时任务、早安消息只发给第一个（_sched_target 直接 return 第一个）。",
         why="现在只有一个对象所以没暴露问题，一旦加第二个，定时任务就会发错人。",
         how="listen 改成对象数组：{name, 人设覆盖, 定时任务, 冷却, 开关}；面板按对象分页管理；"
             "PromptBuilder 按对象取不同的人设与关系档案。",
         gain="支持多对象而不用复制一份部署。",
         eff="L", risk="中（数据结构变更，需要迁移旧 config）"),

    # ---------------- C 记忆与成长 ----------------
    dict(id="C1", cat="记忆", pri=P1, title="三层记忆：日摘要 → 周摘要 → 长期画像",
         now="只有一层日摘要，且 keep_days=3（只带最近 3 天）。",
         why="3 天太短，「她在备考」「她怕冷」这类长期事实会随滚动窗口丢失；"
             "而只存日流水又会让 system prompt 越堆越长。",
         how="① 日摘要（每天）→ ② 周摘要（每周把 7 条日摘要压成一段）→ ③ 长期画像（结构化：喜好/禁忌/日程/约定，"
             "存 data/profile_facts.json）；system prompt 只带「周摘要 + 长期画像」，token 可控且信息密度更高。",
         gain="记得住长期事实，同时 system prompt 不膨胀。",
         eff="L", risk="中"),

    dict(id="C2", cat="记忆", pri=P1, title="从「流水账总结」改成「事实抽取」",
         now="每日摘要 prompt 是「用 3-5 句话总结要点」，产出的是叙事流水账。",
         why="流水账重复率高（每天都说「她很累」），占 token 却没增量信息；真正有用的是新事实。",
         how="① 摘要 prompt 改为抽取结构化事实（人物/事件/情绪/约定/偏好），只写「新增或发生变化」的；"
             "② 写入前与已有画像 diff，重复的丢弃、冲突的标记待你裁决；③ 结果以提案形式给你过目。",
         gain="记忆从「越攒越啰嗦」变成「越攒越精准」。",
         eff="M", risk="中"),

    dict(id="C3", cat="记忆", pri=P1, title="人设卡自动再蒸馏的闭环",
         now="tools/distill_persona.py 要手动跑，且依赖 chat_history.json（8/23 的快照），之后再没更新过。",
         why="你的说话习惯会被 AI 的回复反向影响（回声室），也会随时间变化。人设卡是一次性产物，会越来越失真。",
         how="① 每周自动用「主人手动发的消息」（is_ai=False，看 main.py 已有这个标记）重新蒸馏；"
             "② 新旧人设卡 diff 生成提案，你点头才覆盖；③ 蒸馏输入排除 AI 代发内容，防回声室污染。",
         gain="人设卡能跟上你的真实变化，且不引入 AI 自我模仿的污染。",
         eff="M", risk="低（有提案确认机制兜底）"),

    dict(id="C4", cat="记忆", pri=P1, title="提案中心减负（现在积压 23 条待处理）",
         now="proposals.json 里 38 条，其中 23 条 pending，且没有批量操作、没有过期、没有去重。",
         why="提案一多就没人看了，自进化机制等于失效。",
         how="① 批量通过/拒绝/归档；② 超过 30 天未处理自动归档并可一键清空；"
             "③ 相似提案合并（按内容指纹去重）；④ 标注来源（哪天的哪段对话触发的），可跳转回看；"
             "⑤ 按类型分栏（人设/关系/事实/提醒）。",
         gain="提案中心从「越积越焦虑的收件箱」变回有用的工具。",
         eff="M", risk="低"),

    dict(id="C5", cat="记忆", pri=P2, title="每日回顾卡片",
         now="面板的「今日聊天记录」是原始 jsonl 文本，没有可读性。",
         why="你想知道「今天她心情怎么样、聊了什么」，现在得自己读流水。",
         how="每天自动生成一张卡片：她的情绪曲线、今天的主要话题、新产生的事实、待办约定；"
             "放在面板首页，可翻历史。",
         gain="一眼掌握今天聊得怎么样。",
         eff="M", risk="低"),

    dict(id="C6", cat="记忆", pri=P2, title="约定与纪念日提醒",
         now="relationship.md 里写着「8.29 飞B城」这类约定，但纯文本，模型不一定记得住，你也没有提醒。",
         why="约定是恋爱里最不该忘的东西，漏掉一次很伤。",
         how="① 从 relationship.md / 记忆里抽取带日期的约定，存成结构化事件；"
             "② 面板「即将到来」列表 + 到期前提醒；③ 可选：让 AI 在当天自然提起（可关）。",
         gain="不再错过重要日子。",
         eff="M", risk="低"),

    # ---------------- D 可观测性与运维 ----------------
    dict(id="D1", cat="运维", pri=P1, title="Token 与成本统计",
         now="完全不记录 token 消耗，也不知道每天花了多少钱。",
         why="虽然 DeepSeek 便宜，但 burst 聚合 + 长 system prompt + 视觉识别都在吃 token，"
             "一旦配置不当（比如补跑记忆一口气跑 30 天）可能意外烧钱。",
         how="① ai_client 记录每次调用的 usage（prompt/completion tokens）；② 写入 data/usage.jsonl；"
             "③ 面板显示：今日/本月 token、估算花费、平均每次回复成本；④ 超过阈值告警。",
         gain="成本可见，心里有数。",
         eff="S", risk="低"),

    dict(id="D2", cat="运维", pri=P1, title="事件时间线（关键动作审计日志）",
         now="出了事只能翻 bot.log 和 ui_server_startup.log，两处还不同步。",
         why="「她昨天说机器人没回她」这种问题，需要的是一条按时间排好的、跨 bot 和面板的事件流。",
         how="统一写 data/events.jsonl：启动/停止/暂停/恢复/改人设/通过提案/定时任务触发/AI 失败/微信掉线/自动重连。"
             "面板加「事件」标签页，可按类型和日期过滤。",
         gain="排障时间从「翻日志猜」降到「看时间线」。",
         eff="M", risk="低"),

    dict(id="D3", cat="运维", pri=P1, title="崩溃自愈：bot 意外退出后自动拉起",
         now="bot 挂了就是挂了，面板只显示「未运行」，等你发现。",
         why="微信掉线、后端异常、内存问题都可能让它半夜挂掉，你第二天才知道。",
         how="① 面板检测心跳超时 → 按策略自动重启（可开关）；② 重启次数上限（如 10 分钟内最多 3 次），"
             "超过则停止并告警，避免重启风暴；③ 每次重启写事件时间线。",
         gain="夜里挂了也能自己爬起来，不用第二天才发现「它一整天没回她」。",
         eff="M", risk="中（必须有次数上限，否则会刷屏重启）"),

    dict(id="D4", cat="运维", pri=P1, title="微信掉线自动重连 + 明确告警",
         now="后端 poll 报错只 log.error，wechat_ready 不会置回 False，面板一直显示「已连接」。",
         why="状态显示是假的：实际上已经收不到消息了，但面板骗你说一切正常。",
         how="① 连续 N 次 poll 失败 → wechat_ready=False 并写事件；② 自动重连（退避重试，最多 M 次）；"
             "③ 失败则通过文件传输助手 + 面板红条告警；④ 面板区分「已连接 / 重连中 / 已断开」。",
         gain="状态显示终于可信。",
         eff="M", risk="低"),

    dict(id="D5", cat="运维", pri=P2, title="延迟与失败率看板",
         now="没有任何性能指标。",
         why="「她抱怨回复慢」时无法判断是 AI 慢、后端慢还是网络慢。",
         how="记录三段耗时：收到消息 → AI 返回 → 发送成功；面板画 P50/P95 折线；"
             "同时统计 AI 失败率、图片识别成功率。",
         gain="性能问题可定位。",
         eff="S", risk="低"),

    dict(id="D6", cat="运维", pri=P2, title="一键备份 / 恢复快照",
         now="人设卡、记忆、关系档案散落在 data/ 下，.bak 只有最近 5 份且是按文件分散的。",
         why="换电脑、误删、改崩了想回滚——现在没有可靠的整体快照。",
         how="① 面板「导出快照」打包：config(脱敏) + persona/relationship/memory + style_profile + proposals + daily；"
             "② 「导入快照」可整体恢复（覆盖前自动再打一份）；③ 支持定时自动快照（每日 1 份，保留 30 天）。",
         gain="误操作可回滚，换机可迁移。",
         eff="M", risk="中（导入会覆盖数据，必须二次确认 + 自动预备份）"),

    dict(id="D7", cat="运维", pri=P2, title="面板实时推送（SSE 取代 3 秒轮询）",
         now="2.0 已经把轮询降到「不可见时 30 秒」，但本质上还是轮询。",
         why="日志、状态、新提案都不是实时的，且轮询再怎么优化也在做无用请求。",
         how="服务端加 /api/stream（Server-Sent Events），状态/日志/事件变更时主动推；"
             "前端改为监听，断线自动重连；保留轮询作为降级。",
         gain="日志真·实时，请求量降到接近 0。",
         eff="M", risk="中（需要处理连接数与重连）"),

    # ---------------- E 安全与护栏 ----------------
    dict(id="E1", cat="安全", pri=P0, title="发送前内容审查（防乱承诺 / 防暴露身份）",
         now="AI 生成什么就发什么，唯一约束是 prompt 里的铁律。",
         why="模型偶尔会违规。一旦发出「明天我去找你」「我给你转 5000」或者一句「作为 AI」，"
             "代价远大于不回复。",
         how="① 发送前过规则引擎：承诺类（见面/转账/买/答应）、身份泄露（AI/机器人/助手/作为）、"
             "越界亲密、敏感信息；② 高敏命中 → 拦截不发送 + 通知你 + 记事件；"
             "③ 中敏命中 → 改写为安全话术后再发；④ 规则可配，命中记录可见。",
         gain="把最坏情况兜住。这是「敢让它跑」的前提。",
         eff="M", risk="低（只做拦截不做放行）"),

    dict(id="E2", cat="安全", pri=P1, title="护栏升级：免打扰时段 + 连续冷却 + 冷场自动收手",
         now="只有固定冷却 20 秒和每日上限 200 条。",
         why="三个真实风险没覆盖：半夜回消息、她连着回「嗯」「哦」时机器人还在热情输出、"
             "她明显不想聊了还在追着说话。",
         how="① 免打扰时段（如 23:30–07:30 只收不发，可配，紧急情况除外）；"
             "② 连续回复 N 条后冷却时间翻倍；③ 检测「冷场信号」（连续 3 条单字/无实质内容）→ 自动降频并通知你；"
             "④ 所有护栏的触发都写事件时间线。",
         gain="从「会不会骚扰到她」变成「有明确边界」。",
         eff="M", risk="低"),

    dict(id="E3", cat="安全", pri=P1, title="后端健康度评分与自动降级（iLink 优先）",
         now="backend: auto，但选了之后没有健康度跟踪，也没有失败时的切换。",
         why="README 自己写了 iLink 是官方接口无封号风险、wxauto 有理论风险，"
             "但代码里没有把「优先走安全的」这件事工程化。",
         how="① 给每个后端维护健康度（成功率、连续失败次数、最近可用时间）；"
             "② 首选 iLink，失败降级到 wechatauto/wxauto，恢复后再升回；③ 面板显示当前后端 + 风险提示 + 切换按钮。",
         gain="默认走最安全的路径，出问题自动兜底。",
         eff="L", risk="中（后端切换涉及消息通道差异，需要充分测试）"),

    dict(id="E4", cat="安全", pri=P2, title="导出内容自动脱敏",
         now="导出的日志、快照、自检报告里可能带真实姓名甚至密钥。",
         why="这些东西你可能会发给别人看，或者贴到群里求助。",
         how="导出时按配置替换真实姓名为占位符，密钥一律不导出；自检报告默认脱敏，可开关。",
         gain="分享排障信息时不再泄露隐私。",
         eff="S", risk="低"),

    # ---------------- F 面板体验 ----------------
    dict(id="F1", cat="面板", pri=P1, title="手机也能看（可选局域网访问）",
         now="只绑 127.0.0.1，只能在本机看。",
         why="你在外面时完全不知道机器人状态，也不方便随手改。",
         how="① 可选开启 0.0.0.0 监听（默认关闭）；② 必须配合访问密码 + 面板令牌 + 同源校验；"
             "③ 面板显示本机局域网地址与二维码；④ 提供一键关闭。",
         gain="躺床上也能看它有没有好好回话。",
         eff="M", risk="中（暴露到局域网，必须做鉴权；默认关闭）"),

    dict(id="F2", cat="面板", pri=P2, title="「接管中」醒目横幅",
         now="暂停状态只是控制台里一个小徽章，容易看漏。",
         why="很可能出现「我以为它在自动回，结果它暂停了一整天」。",
         how="暂停时页面顶部固定一条醒目横幅，写明「已暂停，AI 不会自动回复」+ 一键恢复；"
             "同时在标题栏/g标签页标题上体现状态。",
         gain="状态一眼可见，不再误判。",
         eff="S", risk="低"),

    dict(id="F3", cat="面板", pri=P2, title="日志彩色高亮与过滤",
         now="日志是一整块纯文本。",
         why="想从一堆日志里找「她说了什么」「AI 回了什么」「哪里报错」，现在靠肉眼扫。",
         how="按行分类着色（她的消息 / AI 回复 / 错误 / 系统），加关键字过滤 + 只看错误开关 + 自动滚动开关。",
         gain="排障和回看都快很多。",
         eff="S", risk="低"),

    dict(id="F4", cat="面板", pri=P2, title="新手指引（首次打开的分步引导）",
         now="README 写了但没人看，新开一份部署要自己摸索。",
         why="换台电脑重装一遍时，步骤（装后端 → 配密钥 → 喂风格 → 开跑）很容易漏。",
         how="首次打开时按自检结果驱动的分步卡片：缺依赖 → 缺密钥 → 没风格档案 → 没开跑，"
             "每步有按钮直达。全部完成后自动隐藏。",
         gain="换机部署不再靠记忆。",
         eff="M", risk="低"),

    dict(id="F5", cat="面板", pri=P3, title="深浅色主题 + 响应式布局",
         now="固定深色，窄屏下排版一般。",
         why="白天看深色刺眼；手机上打开（配合 F1）布局需要适配。",
         how="加主题变量与切换按钮（记住选择）；断点适配手机窄屏。",
         gain="好看一点，用得舒服一点。",
         eff="S", risk="低"),

    dict(id="F6", cat="面板", pri=P3, title="快捷键与命令面板",
         now="所有操作靠鼠标点。",
         why="常用操作（暂停/继续/刷新）点起来慢。",
         how="Ctrl+K 命令面板，支持搜索操作；常用操作绑快捷键。",
         gain="重度使用时效率提升。",
         eff="S", risk="低"),

    # ---------------- G 工程架构 ----------------
    dict(id="G1", cat="工程", pri=P1, title="main.py 接入 core（与 A4 同源，单列便于排期）",
         now="bot 侧仍是 1.0 写法。",
         why="两边不一致是长期维护的最大隐患。",
         how="见 A4。建议与 A1–A3 一起做成一个「bot 侧 2.0 化」的改动包，配回归测试。",
         gain="一套代码风格，一处修 bug 两边都受益。",
         eff="M", risk="中"),

    dict(id="G2", cat="工程", pri=P2, title="配置热重载（改完不用重启）",
         now="改 config.json 大多要重启机器人；切监听对象现在是「改配置 + 重启」。",
         why="重启意味着微信监听要重建，有几秒到十几秒的空窗，可能漏消息。",
         how="① 区分可热更新字段（监听列表、冷却、上限、语气滑杆、人设/关系档案——这三个已经是热读的）"
             "与必须重启的字段（后端、AI 供应商）；② 面板改配置时提示是否需要重启；"
             "③ 监听列表变更改为原地 add/remove listener。",
         gain="大部分调整不再有空窗期。",
         eff="M", risk="中"),

    dict(id="G3", cat="工程", pri=P2, title="AI 供应商抽象（DeepSeek / GLM / 豆包 / 本地）",
         now="ai_client 写死 OpenAI 兼容格式，配置里只有 api_base 能改。",
         why="换供应商时有些细节（视觉、reasoning_content、max_tokens 语义）不一样，现在靠硬编码兼容。",
         how="抽一层 provider：各自实现 chat / vision / usage 解析；配置里选 provider；"
             "保留「自定义 OpenAI 兼容」兜底。",
         gain="换模型不用改代码；也方便主备切换（一家挂了换另一家）。",
         eff="L", risk="中"),

    dict(id="C4b", cat="工程", pri=P2, title="daily 日志迁移到 SQLite",
         now="每天一个 jsonl，统计要全量读。",
         why="现在数据量还小；跑一年就是几万条，补跑记忆、画情绪曲线、做去重都要全量扫描。",
         how="① 新增 data/state.db（messages / events / usage / feedback / proposals 表）；"
             "② jsonl 保留为可读备份；③ 提供一次性迁移脚本。",
         gain="统计与查询变快，为 C 组和 D 组的分析功能打底。",
         eff="L", risk="中（数据迁移需谨慎，保留 jsonl 双写过渡）"),

    dict(id="G5", cat="工程", pri=P3, title="CI：提交即跑测试 + 自动打包",
         now="测试靠手动跑，打包靠手动执行。",
         why="改动多了以后，回归风险只能靠人记。",
         how="GitHub Actions：push → pytest → pyinstaller → 上传产物。",
         gain="每次改动都有自动验证。",
         eff="S", risk="低"),

    dict(id="G6", cat="工程", pri=P3, title="类型标注与静态检查",
         now="无类型标注，无 mypy。",
         why="代码量上来后，重构（比如 G1、C1）容易漏改。",
         how="给 core/ 和 web/ 先加类型标注并接入 mypy（宽松模式），逐步覆盖。",
         gain="重构更安全。",
         eff="M", risk="低"),
]

CAT_ORDER = ["必修", "体验", "记忆", "运维", "安全", "面板", "工程"]
PRI_ORDER = [P0, P1, P2, P3]


def eff_label(e):
    return {"S": "小（<1h）", "M": "中（1–4h）", "L": "大（>4h）"}.get(e, e)


def build_md():
    out = []
    out.append("# 微信 AI 陪聊 · 3.0 优化方案清单\n")
    out.append("> 基于 2.0 交付后的二次深挖。先修「实际上是坏的」，再谈「能不能更强」。\n")

    cnt = Counter(i["pri"] for i in ITEMS)
    out.append("**共 %d 项**：P0 必修 %d · P1 高价值 %d · P2 增强 %d · P3 可选 %d\n"
               % (len(ITEMS), cnt[P0], cnt[P1], cnt[P2], cnt[P3]))
    out.append("| 优先级 | 含义 |")
    out.append("|---|---|")
    out.append("| P0 | 功能现在就是坏的 / 有实质风险，建议最先处理 |")
    out.append("| P1 | 能力或体验的明显跃升 |")
    out.append("| P2 | 有价值的增强 |")
    out.append("| P3 | 锦上添花，不急 |\n")
    out.append("工作量：S < 1 小时 · M 1–4 小时 · L > 4 小时\n")
    out.append("---\n")

    for cat in CAT_ORDER:
        items = [i for i in ITEMS if i["cat"] == cat]
        if not items:
            continue
        out.append("## %s（%d 项）\n" % (cat, len(items)))
        for it in sorted(items, key=lambda x: PRI_ORDER.index(x["pri"])):
            out.append("### %s · [%s] %s\n" % (it["id"], it["pri"], it["title"]))
            out.append("- **现状**：%s" % it["now"])
            out.append("- **为什么是问题**：%s" % it["why"])
            out.append("- **怎么做**：%s" % it["how"])
            out.append("- **预期效果**：%s" % it["gain"])
            out.append("- **工作量**：%s ｜ **风险**：%s\n" % (eff_label(it["eff"]), it["risk"]))
        out.append("---\n")

    out.append("## 建议的落地顺序\n")
    out.append("1. **第一波（先把坏的修好）**：A1 + A5（记忆补跑）→ A2 → A3 → A4/G1 → E1（内容审查）")
    out.append("2. **第二波（体验跃升）**：B1（预演）→ B4（反馈）→ B5（滑杆）→ B2（主动发起）→ D2（事件时间线）→ D1（成本）")
    out.append("3. **第三波（记忆体系）**：C1 + C2（三层记忆 + 事实抽取）→ C4（提案减负）→ C3（人设再蒸馏）")
    out.append("4. **第四波（运维与护栏）**：D3（自愈）→ D4（重连告警）→ E2（护栏）→ D6（快照）→ D7（SSE）")
    out.append("5. **其余**按喜好挑\n")
    out.append("第一波做完，最大的变化是：**成长记忆终于开始工作**（现在它是 0 字节），"
               "并且 AI 说出格的话会被拦下来。")
    return "\n".join(out)


def esc(s):
    return html.escape(s)


def build_html():
    cnt = Counter(i["pri"] for i in ITEMS)
    cards = []
    for it in ITEMS:
        cards.append(
            '<article class="card p-%s" data-cat="%s" data-pri="%s" data-id="%s">'
            '<div class="chead"><span class="pri">%s</span><span class="cat">%s</span>'
            '<span class="eid">%s</span></div>'
            '<h3>%s</h3>'
            '<div class="sec"><b>现状</b>%s</div>'
            '<div class="sec"><b>为什么是问题</b>%s</div>'
            '<div class="sec"><b>怎么做</b>%s</div>'
            '<div class="sec gain"><b>预期效果</b>%s</div>'
            '<div class="foot"><span class="eff">工作量 %s</span><span class="risk">风险 %s</span></div>'
            '</article>'
            % (it["pri"].lower(), esc(it["cat"]), it["pri"], esc(it["id"]),
               it["pri"], esc(it["cat"]), esc(it["id"]),
               esc(it["title"]), esc(it["now"]), esc(it["why"]),
               esc(it["how"]), esc(it["gain"]),
               eff_label(it["eff"]), esc(it["risk"])))

    chips = "".join('<button class="chip" data-filter="%s">%s</button>' % (c, c)
                    for c in CAT_ORDER)
    prichips = "".join('<button class="chip pc" data-prif="%s">%s (%d)</button>'
                       % (p, p, cnt[p]) for p in PRI_ORDER)

    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>微信 AI 陪聊 · 3.0 优化方案墙</title>
<style>
:root{
  --bg:#0f1419; --panel:#1a212b; --panel2:#212b38; --line:#2c3744;
  --txt:#e6edf3; --sub:#8b98a5; --accent:#4f9cff;
  --green:#3fb950; --red:#f85149; --yellow:#d29922; --purple:#a371f7;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--txt);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.7;}
.wrap{max-width:1180px;margin:0 auto;padding:32px 22px 80px;}
h1{font-size:24px;margin:0 0 6px;}
.sub{color:var(--sub);font-size:13px;margin-bottom:20px;}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:10px 16px;min-width:96px;}
.stat b{display:block;font-size:20px;line-height:1.2;}
.stat span{color:var(--sub);font-size:12px;}
.bars{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px;}
.chip{background:var(--panel);border:1px solid var(--line);color:var(--sub);
  padding:6px 14px;border-radius:20px;cursor:pointer;font-size:13px;font-family:inherit;}
.chip:hover{color:var(--txt);}
.chip.on{background:rgba(79,156,255,.14);color:var(--accent);border-color:var(--accent);}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px;}
.card{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);
  border-radius:12px;padding:14px 16px;}
.card.p-p0{border-left-color:var(--red);}
.card.p-p1{border-left-color:var(--yellow);}
.card.p-p2{border-left-color:var(--accent);}
.card.p-p3{border-left-color:var(--sub);}
.chead{display:flex;align-items:center;gap:8px;margin-bottom:6px;}
.pri{font-size:11px;font-weight:800;padding:2px 8px;border-radius:6px;letter-spacing:.5px;}
.p-p0 .pri{background:rgba(248,81,73,.16);color:var(--red);}
.p-p1 .pri{background:rgba(210,153,34,.16);color:var(--yellow);}
.p-p2 .pri{background:rgba(79,156,255,.16);color:var(--accent);}
.p-p3 .pri{background:rgba(139,152,165,.16);color:var(--sub);}
.cat{font-size:11px;color:var(--sub);background:var(--panel2);
  padding:2px 8px;border-radius:6px;}
.eid{margin-left:auto;font-size:11px;color:var(--sub);font-family:ui-monospace,Consolas,monospace;}
.card h3{margin:0 0 10px;font-size:15px;line-height:1.5;}
.sec{margin:6px 0;font-size:13px;color:#c9d4e0;}
.sec b{display:inline-block;color:var(--sub);font-weight:600;margin-right:6px;}
.sec.gain{color:#b9e0c4;}
.foot{display:flex;gap:12px;margin-top:10px;padding-top:8px;border-top:1px solid var(--line);
  font-size:11px;color:var(--sub);}
.plan{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin-top:26px;}
.plan h2{margin:0 0 10px;font-size:16px;}
.plan ol{margin:0;padding-left:20px;color:#c9d4e0;font-size:13px;}
.plan li{margin:5px 0;}
.note{margin-top:12px;font-size:13px;color:var(--sub);}
</style>
</head>
<body>
<div class="wrap">
<h1>微信 AI 陪聊 · 3.0 优化方案墙</h1>
<div class="sub">共 __TOTAL__ 项 · 点上方标签筛选 · 基于 2.0 交付后的二次深挖</div>
<div class="stats">
  <div class="stat"><b style="color:var(--red)">__P0__</b><span>P0 必修</span></div>
  <div class="stat"><b style="color:var(--yellow)">__P1__</b><span>P1 高价值</span></div>
  <div class="stat"><b style="color:var(--accent)">__P2__</b><span>P2 增强</span></div>
  <div class="stat"><b style="color:var(--sub)">__P3__</b><span>P3 可选</span></div>
</div>
<div class="bars">
  <button class="chip on" data-filter="ALL">全部</button>__CHIPS__
  <span style="width:14px"></span>__PRICHIPS__
</div>
<div class="grid">__CARDS__</div>
<div class="plan">
<h2>建议落地顺序</h2>
<ol>
<li><b>第一波 · 先把坏的修好</b>：A1 + A5（成长记忆补跑）→ A2 → A3 → A4/G1（bot 侧 2.0 化）→ E1（发送前审查）</li>
<li><b>第二波 · 体验跃升</b>：B1（回复预演）→ B4（赞踩反馈）→ B5（语气滑杆）→ B2（主动发起）→ D2（事件时间线）→ D1（成本可见）</li>
<li><b>第三波 · 记忆体系</b>：C1 + C2（三层记忆 + 事实抽取）→ C4（提案减负）→ C3（人设自动再蒸馏）</li>
<li><b>第四波 · 运维与护栏</b>：D3（崩溃自愈）→ D4（掉线重连告警）→ E2（护栏升级）→ D6（快照）→ D7（实时推送）</li>
<li><b>其余</b>按喜好挑（F 面板体验 / G 长期工程）</li>
</ol>
<div class="note">第一波做完，最直观的变化有两个：<b>成长记忆终于开始工作</b>（现在是 0 字节，数百条对话白白浪费），
以及 <b>AI 说出格的话会被拦下来</b>，敢让它长时间跑着。</div>
</div>
</div>
<script>
(function(){
  var cat='ALL', pri='ALL';
  function apply(){
    document.querySelectorAll('.card').forEach(function(c){
      var ok = (cat==='ALL'||c.dataset.cat===cat) && (pri==='ALL'||c.dataset.pri===pri);
      c.style.display = ok ? '' : 'none';
    });
  }
  document.querySelectorAll('[data-filter]').forEach(function(b){
    b.onclick=function(){
      cat=b.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach(function(x){x.classList.toggle('on',x===b);});
      apply();
    };
  });
  document.querySelectorAll('[data-prif]').forEach(function(b){
    b.onclick=function(){
      pri = (pri===b.dataset.prif) ? 'ALL' : b.dataset.prif;
      document.querySelectorAll('[data-prif]').forEach(function(x){x.classList.toggle('on',x.dataset.prif===pri);});
      apply();
    };
  });
})();
</script>
</body>
</html>""".replace("__CARDS__", "".join(cards)) \
         .replace("__CHIPS__", chips) \
         .replace("__PRICHIPS__", prichips) \
         .replace("__TOTAL__", str(len(ITEMS))) \
         .replace("__P0__", str(cnt[P0])) \
         .replace("__P1__", str(cnt[P1])) \
         .replace("__P2__", str(cnt[P2])) \
         .replace("__P3__", str(cnt[P3]))


if __name__ == "__main__":
    md = build_md()
    io.open(os.path.join(BASE, "docs", "roadmap-3.0.md"), "w",
            encoding="utf-8", newline="").write(md)
    h = build_html()
    io.open(os.path.join(BASE, "docs", "roadmap-board.html"), "w",
            encoding="utf-8", newline="").write(h)
    print("方案 %d 项" % len(ITEMS))
    print("  md   %.1f KB" % (len(md.encode("utf-8")) / 1024.0))
    print("  html %.1f KB" % (len(h.encode("utf-8")) / 1024.0))
