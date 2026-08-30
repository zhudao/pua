# PUA FAQ / Issue Playbook

## 需不需要总是开启 PUA？

不建议无脑 always-on。推荐按风险分层：

| 场景 | 建议 |
|---|---|
| 普通首轮问答/简单代码 | 不必 always-on，避免噪音 |
| Debug、失败 2 次以上、用户明显不满 | 开启 PUA 或手动触发 |
| 高风险交付、测试/评分/CI/memory 相关 | 开启 PUA + harness governance，按四权分离执行 |
| 项目初期探索 | 使用温和味道或仅用诊断先行/验证闭环 |

核心不是“压力越大越好”，而是把**行动、诊断、评分、环境修改**分开，并用证据交付。压力只负责防摆烂，不能替代 verifier。

## Claude 说这是 prompt injection，怎么办？

从 v3.3.0 起，UserPromptSubmit hook 已做两件事：

1. hook 脚本内部过滤关键词；普通首轮请求不再注入。
2. 注入文案改为“用户安装的 productivity context”，不再使用强制式 `MUST invoke Skill` 文案。

如果仍遇到拒绝：

- 先确认 Claude Code 版本足够新；
- 使用 `/pua:off` 关闭自动注入，只在需要时手动 `/pua`；
- 对调试任务使用诊断先行格式：`[PUA-DIAGNOSIS] 问题是... 证据是... 下一步...`；
- 如果模型仍拒绝，提供完整 session JSONL，便于复现。

## 封闭网络 / 内网环境怎么用？

使用 `/pua:offline` 或手动设置：

```json
{
  "offline": true,
  "feedback_frequency": 0
}
```

离线模式会关闭任务结束时的反馈问卷；PUA 的本地验证、压力升级、诊断先行仍可使用。

注意：PUA 已移除全部联网上报功能，**默认就不发送任何数据**，无需靠离线模式来阻止上传。这个开关现在只控制问卷是否弹出。

## Codex CLI 子命令怎么对应 Claude Code？

Codex 没有 Claude Code 的 `/pua:xxx` slash command 命名空间时，可以用 `$pua-xxx` alias：

| Claude Code | Codex CLI |
|---|---|
| `/pua:on` | `$pua-on` |
| `/pua:off` | `$pua-off` |
| `/pua:p7` | `$pua-p7` |
| `/pua:p9` | `$pua-p9` |
| `/pua:p10` | `$pua-p10` |
| `/pua:pro` | `$pua-pro` |
| `/pua:pua-loop` | `$pua-loop` |

## Pi / Trae 支持状态

- `pi/pua/`：官方轻量 pi extension，提供 `/pua-on`、`/pua-off`、`/pua-status`、`/pua-reset` 和会话注入。
- `pi/package/`：pi.dev package 版本，包含 extension + `skills/pua/SKILL.md`，可用 `pi install ./pi/package` 本地安装。
- `.trae/skills/`：Trae 标准 `SKILL.md` 包；`trae/` 保留 Prompt/Rule 复制版和差异说明。
- Trae / Pi 都不继承 Claude Code hooks；四权分离 gate 必须通过 Skill 工作规程、外部验证和用户确认落地。

## PUA 会不会上传我的数据？

不会。全部数据收集功能已移除，客户端不发、服务端也不再接收。具体删掉了五条通道：

| 已移除 | 曾经发送的内容 |
|--------|----------------|
| session 语料上传 | 脱敏后的对话 `.jsonl` 全文 |
| 评分反馈上报 | 评分、PUA 计数、味道、任务摘要 |
| 静默心跳 telemetry | 随机安装 ID、插件版本、平台、味道 |
| PUA 排行榜 | 邮箱、手机号、PUA 计数、L3+ 计数 |
| pua-api 平台 | 手机号 + 短信验证码注册、session_start/pua_triggered/command_used 事件上报，以及依赖它的远端指令模板拉取和支付流程 |

对应的客户端 hook、服务端 Pages Functions、D1 迁移和 Cloudflare 绑定都已删除。

任务结束时的反馈问卷保留，但只 append 一行到本机 `~/.pua/feedback.jsonl`。

回归防护：`evals/test-no-telemetry.sh` 对全仓做反向断言——扫描已知采集域名、endpoint 路径、行首的 `curl`/`wget` 调用，以及已删文件的重新出现。任何一条被加回来，测试就会失败。


## Integrity Guard 为什么不再使用 `permissionDecision: "ask"`？

从 v3.4.6 起，PUA Integrity Guard 将敏感但合法的操作降级为 advisory-only：只注入 `additionalContext`，不再输出 `permissionDecision: "ask"`。

原因是 Claude Code 会把 hook 返回的 `ask` 当成硬权限请求处理，它的优先级高于 `bypassPermissions`，会导致用户明明开启 bypass 仍频繁弹窗。

新的分层是：

- memory、`CLAUDE.md`、`settings.json`、tests/evals/CI 等敏感操作：advisory-only，提醒模型谨慎并解释治理边界；
- hidden tests、hidden solution、gold patch、benchmark answers：`permissionDecision: "deny"`，硬阻断，避免答案污染和评测作弊；
- 普通源码读写：静默放行。

核心原则：提醒走上下文通道，阻断才走权限裁决通道。

## “下场”这个词为什么改了？

“下场”同时可能表示“亲自动手介入”和“停止工作/退场”，容易让 agent lifecycle 语义混乱。现在统一为：

- start/intervene → “亲自动手” / “亲自介入”；
- stop/release → “释放” / “退场”。

## 静默 heartbeat 还在吗？

不在了。SessionStart 的 heartbeat hook 已删除，`~/.pua/install_id` 不再被使用也不再被创建。老版本留下的这个文件可以直接删掉。

## `openpua.ai/contribute.html` 现在是什么？

一个说明页。原来的上传表单已移除，页面只说明数据收集已停止。GitHub 登录、上传历史、管理统计面板一并下线，因为它们唯一的用途就是给上传归属和看采集量。

## 我想手动脱敏一份 session 自己用，还有工具吗？

有。`hooks/sanitize-session.sh` 保留下来了，它是纯本地工具，不联网：

```bash
bash hooks/sanitize-session.sh <输入.jsonl> <输出.jsonl>
```

三层脱敏：已知格式黑名单（路径、各家云厂商密钥、JWT、PEM 私钥、数据库连接串、邮箱/IP/手机号）→ `key=value` 上下文识别 → Shannon 熵兜底（32 字符以上的高熵串，纯 hex 用更高阈值以免误伤 git hash）。

它现在没有任何调用方，删掉它也不影响 PUA 运行。
