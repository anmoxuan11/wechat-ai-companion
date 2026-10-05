# AGENTS.md

给 AI 编码助手（及人）的上手说明。改代码前请先读这份，能少踩很多坑。

## 环境

```bash
python --version          # 需要 3.9+，推荐 3.11
pip install -r requirements.txt   # 只在真正连微信时才需要
pip install pytest                 # 跑测试
```

面板层（`ui_server.py` / `core/` / `web/` / `assets/`）**零第三方依赖**，只吃标准库。
不装任何东西也能把面板跑起来，只有真正收发微信消息才需要后端库。

## 提交前必跑

```bash
python -m pytest tests -q          # 单元测试，必须全绿
python ui_server.py --self-test    # 面板自检报告
```

改动涉及面板前端或 API 时，额外跑一次端到端验证（会自己拉起面板再关掉）：

```bash
python tools/e2e_check.py
```

## 目录职责（别越界放文件）

| 目录 | 放什么 |
|---|---|
| 根目录 | **只有 3 个入口** `main.py` / `ui_server.py` / `launcher.py`，加配置与打包文件 |
| `core/` | 与业务无关的内核：原子写、锁、事件、存储、计价、护栏、隐私、路径 |
| `memory/` | 记忆子系统：日摘要 / 周摘要 / 事实抽取 / 后台补跑 |
| `web/` | 面板状态聚合与扩展 API |
| `backends/` | 微信后端适配（wechatauto / wxauto / ilink） |
| `assets/` | 面板前端：dashboard.html / style.css / app.js / extra.js |
| `tools/` | 运维与开发脚本。**不是**运行时依赖，不要 import 它 |
| `tests/` | pytest 用例，命名 `test_*.py` |

新增运行时能力优先放进 `core/`，别往根目录堆文件。

## 硬性约束

1. **密钥绝不落盘**：只读环境变量 `DEEPSEEK_API_KEY` / `DEEPSEEK_VISION_KEY`，
   取值统一走 `core.security.resolve_api_key()`。别把 Key 写进 `config.json` 提交。
2. **运行时数据不进 Git**：整个 `data/` 已被 `.gitignore` 忽略，只保留 `*.example.*` 模板。
   聊天记录、记忆、人设卡、SQLite 都属隐私，**永远不要提交**。
3. **敏感信息过隐私闸门**：任何要沉淀进长期记忆的文本，先过 `core/privacy.py`
   （高危丢弃、中危脱敏）。新增抽取逻辑时别绕过它。
4. **写文件走原子写**：用 `core.atomicio`，不要裸 `open(..., "w")` 覆盖运行时文件。
5. **面板写操作要校验**：沿用现有令牌 + 同源 Origin + `confirm=true` 的模式。

## 容易踩的坑

- `paths.base_dir()` 是「`core/` 的父目录」，所以 `core/`、`web/`、`memory/` 必须留在根目录，
  挪进子目录会让所有数据路径错位
- `tools/` 里的脚本用 `os.path.dirname(os.path.dirname(__file__))` 推导项目根，
  移动它们时记得同步改
- 定时任务与配置是**双进程共享**的（面板 + bot），改写必须走 `core.lock.FileLock`
- 改动 `config.json` 结构时记得同步 `config.example.json` 和 README 的配置速查表
