# Security Policy

本项目所有数据默认只存本机，不上传任何第三方服务器（AI 对话接口除外）。

## 上报漏洞

请不要直接开 public issue 贴日志/截图（日志里可能有微信昵称与聊天片段）。
建议先自行脱敏（昵称→甲/乙、删掉 `data/` 下的真实记录），再提交。

## 密钥管理

- 永远不要把真实 `config.json`、`data/ilink_token.json`、`.env` 提交到仓库。
- API Key 推荐走环境变量：
  - `DEEPSEEK_API_KEY`（对话，必填）
  - `DEEPSEEK_VISION_KEY`（视觉，可选，不设则复用对话 Key）
- `config.json` 里的 `ai.api_key` / `vision.api_key` 留空即可；面板自检会显示 Key 来源与掩码。
- 不小心提交了密钥：立刻去服务商后台轮换 Key，并用 `git filter-repo` / BFG 清理历史。

## 隐私闸门

`core/privacy.py` 会在记忆沉淀前拦截：

- 高危（银行卡号 / 身份证 / 验证码 / 密码）：直接丢弃，不进长期记忆
- 中危（手机号 / 邮箱等）：自动打码后再存

即使如此，也请不要把 `data/` 目录（含 `chat_history.json`、`state.db`、
`daily/*.jsonl`、`memory.md`、`persona.md`、`relationship.md`）推到任何公开仓库。
本仓库的 `.gitignore` 已默认忽略整个 `data/` 目录。
