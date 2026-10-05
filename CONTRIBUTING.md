# 贡献指南

感谢关注。这个项目是个人自用工具出身，欢迎提 issue 和 PR，但有几条请先看。

## 提 issue 前

**请先脱敏。** 本项目处理的是真实聊天记录，最容易泄漏的就是 issue 正文。贴日志前：

- 昵称 → 换成「A / B」或 `伴侣示例`
- 删掉 `data/` 下的真实内容（`chat_history.json` / `daily/*.jsonl` / `memory.md` / `persona.md` / `relationship.md` / `state.db`）
- 手机号、地址、学校、门牌号、卡号一律替换掉

如果你不确定某段信息是否敏感，默认认为它是敏感的。

## 提 PR

1. fork 后开分支，命名随意（`fix/guard-quiet-hours` 这种）
2. 提交前跑通：

   ```bash
   python -m pytest tests -q
   python ui_server.py --self-test
   ```

3. 改动面板前端或 API 时，再跑 `python tools/e2e_check.py`
4. PR 描述里说清**改了什么、为什么**，能附上问题现象最好

代码风格跟着周围写：中文注释解释「为什么」而不是「做了什么」，4 空格缩进，
不要引入新的第三方依赖到面板层。

## 边界

- **别提交运行时数据**：`data/` 已在 `.gitignore` 里，但请主动确认 `git status` 干净后再 push
- **别提交密钥**：只走环境变量。如果你不小心提交了 Key，请立刻在服务商后台轮换它
- 涉及微信风控的改动，请在 PR 里说明测试方式（用什么号、什么微信版本）

## 开发环境

详见 `AGENTS.md`，包含目录职责、提交前检查和几个容易踩的坑。

## License

贡献的代码以 MIT 许可发布，见 `LICENSE`。
