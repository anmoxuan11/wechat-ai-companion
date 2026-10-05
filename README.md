# 微信 AI 陪聊机器人 · 3.0

用你的语气，自动回复特定对象的微信消息。3.0 的重点是：**先把确实是坏的东西修好，再谈变得更强。**

所有聊天数据默认只存本机，不上传任何服务器（AI 对话接口除外）。

> ⚠️ 仅供个人学习与自用。请使用低频白名单回复，不要群发、不要加人、不要用于打扰他人。
> `wxauto` 系为 UI 自动化，存在理论上的风控风险，建议先用测试号稳定跑一段时间。

---

## 快速开始

### 1. 环境要求

- Windows + 微信（版本决定装哪个后端）
- Python 3.9+（推荐 3.11；`wxauto4` 需要 3.9–3.12）

| 微信版本 | 装哪个后端 |
|---|---|
| 4.1.9+ | `pip install git+https://github.com/fanyuantaier/wechatauto-replica.git` |
| 4.0–4.1.8 | `pip install wxauto4` |
| 3.9.x | `pip install wxauto` |

面板本身（`ui_server.py` / `core` / `web`）零第三方依赖，只吃标准库。

### 2. 配置

```bash
# 1) 复制配置模板（真实 config.json 永不进 Git）
copy config.example.json config.json

# 2) 按需改 config.json：把 listen / partner_name / my_name 改成你们的称呼
# 3) 设置 API Key（推荐环境变量，不要写进文件）
setx DEEPSEEK_API_KEY "sk-你的对话Key"
setx DEEPSEEK_VISION_KEY "sk-你的视觉Key（可选，不设则复用对话Key）"
```

也可用 `scripts/setup-api-key.ps1` 把已在 `config.json` 里的旧 Key 一键迁移到用户环境变量并清空文件。

最小可用配置示例（`config.example.json` 已包含完整注释）：

```json
{
  "backend": "auto",
  "partner_name": "伴侣示例",
  "my_name": "我",
  "listen": ["伴侣示例"],
  "control_chat": "文件传输助手",
  "ai": { "api_base": "https://api.deepseek.com/v1", "api_key": "", "model": "deepseek-chat" }
}
```

### 3. 运行

```bash
# 方式一：面板（推荐，先自检再开跑）
python ui_server.py
# 打开 http://127.0.0.1:8765，按「四步卡片」走完：依赖 → 密钥 → 风格档案 → 开跑

# 方式二：一键启动（exe / launcher / 面板三级兜底）
scripts\start.bat

# 机器人本体（一般由面板拉起，也可单独跑）
python main.py

# 命令行自检 / 单元测试
python ui_server.py --self-test
python -m pytest tests -q
```

机器人运行时可通过「文件传输助手」发命令：`接管 / 暂停 / 恢复 / 状态 / 沉淀 / 成本`。

---

## 功能一览

### 记忆：从「流水账」升级成三层金字塔

- **day** 当天摘要 → **week** 周记 → **facts** 长期事实卡片
- 事实是结构化的（例：`她养了一只叫元宝的橘猫`），可单独检索、纠正、删除
- 补跑有节流（默认每次启动最多补 3 天），不会一口气烧 token
- 数据落 SQLite（`data/state.db`），`data/daily/*.jsonl` 保留为可读备份，可随时重建

```bash
python tools/backfill.py                # 补跑所有缺摘要的历史日期
python tools/backfill.py 3              # 最多补 3 天
python tools/backfill.py --date 2026-08-24   # 只重跑某一天
python tools/backfill.py --no-facts     # 只做日摘要，不抽事实
```

### 洞察：跑了一晚上发生了什么，终于看得见

- **事件时间线**：启动 / 停止 / 回复 / 拦截 / AI 故障 / 掉线 / 重连 / 沉淀 / 定时 / 备份，结构化、可筛选
- **成本看板**：每次调用记 prompt / completion / cached token，按单价折算，近 7 天柱状图
- **健康度评分**：把 AI 失败率、掉线次数、拦截率、花费超线压成一个 0–100 分
- **崩溃自愈**：连续后端失败自动重建连接；掉线会立刻把 `wechat_ready` 复位

### 调教：改完先看看，别拿真人做实验

- **回复预演（干跑）**：改完人设卡先生成看看，不发给对方，顺带过一遍护栏
- **赞 / 踩反馈**：把「这条回复好不好」变成可积累的数据
- **语气滑杆**：温柔 / 撩 / 话痨 / 调侃 / 直球五根，不用写 markdown 也能调质感
- **发送护栏**：身份泄露、客服腔、markdown、书面语、过度承诺 → 拦截或告警；
  免打扰时段、每小时限流、冷场自动收手

### 主动发起（可选）

对方长时间没找你时，基于记忆主动开口。默认开启但**每天最多 1 次**、间隔 6 小时以上，
且同样过护栏和免打扰。想彻底关掉：把 `config.proactive.enabled` 改成 `false`。

### 面板体验

- 接管中醒目横幅 + 标签页标题变 `[已暂停]`，横幅上一键恢复
- 日志着色与过滤；深浅色主题；`Ctrl+K` 命令面板（`P` 暂停 / `R` 恢复）
- 抢答抑制（对方连发判定为还在打字时多等一会儿）
- 非文本消息分类（表情包 / 语音 / 视频 / 红包 / 转账 / 撤回），`config.media` 逐类配 `reply` 或 `log`
- 情绪与场景分级（争吵 / 难过 / 告别时自动切保守约束，10 分钟有效）
- 约定与纪念日提醒、每日回顾卡片、配置热重载、消息通道健康度与手动切换

---

## 配置速查：3.0 新增的段

| 段 | 作用 | 关键项 |
|---|---|---|
| `memory` | 沉淀策略 | `enabled` / `daily_at`（默认 03:30）/ `max_days_per_run` / `extract_facts` / `week_summary` |
| `guard` | 发送护栏 | `enabled` / `strict` / `quiet_hours`（默认 23:00–08:00）/ `max_per_hour` / `dry_streak_limit` |
| `proactive` | 主动发起 | `enabled` / `idle_hours`（默认 6h）/ `max_per_day`（默认 1 次） |
| `cost` | 计价与预警 | `daily_alert_yuan`（默认 5 元）/ `prices`（自定义模型单价，元/百万 token） |
| `style.tone_dials` | 语气滑杆 | 温柔 / 撩 / 话痨 / 调侃 / 直球，各 0–1 |
| `media` | 非文本处置 | `sticker` `voice` `video` `file` `location` `link` `redpacket` `transfer` `recall` `card` `other`，值 `reply` / `log` |
| `backend_failover` | 通道降级 | `enabled` / `fail_threshold`（默认 3）/ `order`（默认 `["wechatauto","wxauto","ilink"]`） |
| `panel` | 面板访问 | `allow_lan`（默认 false）/ `host` / `port` / `password`（开局域网必填） |
| `safety` | 节奏控制 | `typing_gap_seconds`（默认 2.0）/ `typing_extra_wait_seconds`（默认 3.0） |

> 局域网访问默认关闭。`panel.allow_lan=true` 时必须先设 `panel.password`，否则拒绝并回落到 127.0.0.1。

---

## 目录结构

根目录只留 3 个入口 + 配置，其余按职责分目录。

```
.
├── main.py                 入口①机器人主程序
├── ui_server.py            入口②本地面板后端（零第三方依赖）
├── launcher.py             入口③exe 启动器
├── config.example.json     配置模板（复制为 config.json 后使用）
│
├── core/                   公共内核
│   ├── paths.py            路径与解释器解析
│   ├── atomicio.py         原子写 + 自动备份
│   ├── instance.py         单实例锁
│   ├── security.py         令牌、凭据、注册表环境兜底
│   ├── events.py           事件时间线
│   ├── store.py            SQLite 结构化存储
│   ├── cost.py             token 计价
│   ├── guard.py            发送护栏
│   ├── privacy.py          敏感信息识别与脱敏
│   └── lock.py             跨进程文件锁
├── session.py              Prompt 构建 + 会话上下文 + MemoryManager
├── ai_client.py            AI / 视觉客户端（回传 token 用量）
├── memory/                 记忆子系统
│   ├── daily.py            每日对话日志（jsonl ⇄ SQLite）
│   ├── summarizer.py       日摘要 / 周摘要 / 事实抽取
│   └── worker.py           后台补跑线程
├── web/                    面板状态聚合与 3.0 新增 API
├── backends/               微信后端抽象（wechatauto / wxauto / ilink）
│
├── assets/                 面板前端（dashboard.html / style.css / app.js / extra.js）
├── tools/                  运维与开发脚本
│   ├── backfill.py         补跑记忆
│   ├── e2e_check.py        面板端到端验证
│   ├── exe_check.py        打包产物端到端验证
│   ├── distill_persona.py  人设卡蒸馏
│   ├── style_builder.py    说话风格采样与统计
│   └── gen_roadmap.py      生成 docs/ 下的路线图（仅开发用）
├── scripts/                Windows 一键脚本（install / start / 密钥迁移 / 快捷方式）
├── tests/                  单元测试（pytest）
├── docs/                   路线图与方案墙（开发过程文档）
├── data/                   运行时数据目录（真实内容永不进 Git，只有模板）
│   ├── README.md
│   ├── persona.example.md
│   ├── relationship.example.md
│   └── schedule.example.json
├── build_exe.spec          PyInstaller 打包配置
├── pyproject.toml          包元数据
└── requirements.txt        依赖清单
```

---

## 安全与隐私

- **API Key 不落盘**：优先读环境变量 `DEEPSEEK_API_KEY` / `DEEPSEEK_VISION_KEY`。
  Windows 上若“明明设了却读不到”，`core.security.refresh_env_from_registry()` 会从注册表补进当前进程。
- **隐私闸门**：银行卡号 / 身份证 / 验证码 / 密码 → 直接丢弃；手机号、邮箱等中危 → 自动打码。
- **面板写操作双重校验**：随机令牌 + 同源 Origin；删除类操作额外要求 `confirm=true`。
- **所有数据都在本地**，不向本项目之外的任何服务发送（AI 对话接口除外）。
- 详见 `SECURITY.md`。提 issue 前请先脱敏，不要贴真实昵称与聊天原文。

## 从旧版本升级

1. 直接覆盖代码（数据不需要迁移）
2. 首次启动会自动把 `data/daily/*.jsonl` 灌进 `data/state.db`
3. 建议跑一次 `python tools/backfill.py`，把历史对话沉淀成真正的记忆
4. 面板 →「记忆库」页确认摘要已经出现

## License

MIT，见 `LICENSE`。
