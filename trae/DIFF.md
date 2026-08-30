# Trae Skill vs Claude Code PUA — 差异说明

| 维度 | Claude Code PUA plugin | Trae Skill pack |
|---|---|---|
| 载体 | `plugin.json` + `skills/` + `commands/` + `hooks/` + `agents/` | `.trae/skills/*/SKILL.md` 或 `npx skills` 安装后的 Trae skill |
| 触发 | Skill 描述、slash commands、UserPromptSubmit/Stop/SubagentStop hooks | Trae 的 skill 发现/显式调用；无本仓库 hooks 自动注入 |
| commands | `/pua`, `/pua:pro`, `/pua:loop`, `/pua:off` 等 | Trae 版不注册 Claude Code commands；用自然语言或 Trae skill 调用 |
| hooks | failure detector、session restore、integrity guard、stop feedback | Trae 版没有这些机械 hooks；必须把 gate 写进 SKILL.md 工作规程 |
| agents | pua-action-executor / self-reviewer / verifier / policy-guardian | Trae 版默认单上下文；用“行动权/自我评价权/评分权/环境修改权”模板模拟权责分离 |
| 安装 | Claude Code marketplace/cache | `.trae/skills/`、`~/.trae/skills/`、`~/.trae-cn/skills/`，或 `npx skills add ... -a trae` |

## 设计结论

Trae 版不能假装拥有 Claude Code 的 hook 机械门禁，所以不能把“Stop 反馈问卷”“SubagentStop 生命周期”“PreToolUse integrity guard”写成已实现能力。正确做法是：

1. 提供标准 `SKILL.md`；
2. 在 Skill 内部写清楚治理边界；
3. 将评分权交给外部命令、CI、E2E、用户验收；
4. 用 `npx skills` 和 `.trae/skills/` 两条安装路径覆盖 Trae / Trae CN。

不一般但关键的洞察：**Trae 兼容不是复制 Claude Code 的能力名词，而是把不可移植的 hooks 降级为可执行的制度约束。**
