# data/ 目录说明（本目录真实内容永不进 Git）

这个仓库里 `data/` 只保留模板和占位，运行时会自动生成真实文件：

| 文件 | 说明 |
|---|---|
| `config.json`（在仓库根目录，由 `config.example.json` 复制而来） | 监听对象、密钥占位、面板端口等 |
| `data/style_profile.json` | 说话风格档案（自动蒸馏生成） |
| `data/persona.md` | 人设卡（参考 `data/persona.example.md` 手写或面板生成） |
| `data/relationship.md` | 关系背景（参考 `data/relationship.example.md`） |
| `data/schedule.json` | 定时任务（参考 `data/schedule.example.json`） |
| `data/daily/*.jsonl` | 每日原始对话日志（SQLite 的可读备份） |
| `data/state.db` | 事件 / 成本 / 记忆的 SQLite 存储 |
| `data/memory.md` | 日摘要的可读备份 |

首次启动步骤见仓库根目录 `README.md`。
