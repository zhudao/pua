# PUA 精确模型与原生加载验收

## 原则：把五个问题分开

1. **模型身份**：请求参数和初始化目录不是最终模型证据。核对 assistant.model（主回复模型）、modelUsage（模型用量）及显式 fallback（回退），包括 CLI 的 `model_refusal_fallback` 事件；辅助用量单列，不冒充主模型。若请求 Fable、实际回复 Opus，即使答案正确也不能计为 Fable 通过。
2. **原生加载**：可发现技能 ≠ 调用了技能。使用独立插件命名空间，绑定成功 Skill（技能）调用、该插件的实际路径、对应 SKILL.md 的成功读取和前后 SHA-256（文件指纹）。不要从系统提示、工具结果中的 PUA 字样推断行为。额外 Read 门是严格来源复核；缺失它不等于原生调用一定没加载。
3. **实际效果**：目录外可信清单保护验收资产；独立检查器验证输出。语气保真、修改前诊断、失败级别映射、诚实说明未执行、验收后停止，分别评分。代码正确不掩盖协议错误，标记齐全也不代表代码正确。
4. **执行边界**：cc0 是用户 zsh 函数，不是 PATH 中的独立二进制；它会固定工作目录、选择配置源和权限模式。逐次明确模型、插件路径与绝对业务路径；会话级关闭自动记忆/外部钩子，不修改全局默认。工具名单与进程组超时不是操作系统沙箱。
5. **失败保留**：每次调用使用新目录，保留非零退出、超时、模型替换、缺证据和行为失败；不能删除首轮或把同提示重试冒充首轮成功。发布包必须绑定实际受测源文件指纹，并明列通过、失败和未测范围，不能把“已测”改写为“全通过”。公开发布前重新检查凭据、个人路径与会话记录；`compat/evidence/` 只作本地私有留档，公开报告不链接不存在的本地评测页。

## 本仓库工具

- `evals/run-cc0-fable.py`：默认仅打印计划，`--run` 才调用现有账号。必须由用户授权模型测试；不创建账号、密钥、付费资源或全局安装。
- `evals/prepare-fable-evals.py`：准备旧版/新版配对夹具与外置可信文件清单，不调用模型、不覆盖已有目录。
- `evals/check-fable-artifacts.py`：事后独立功能检查。它运行的是已审查的测试输出，不是安全沙箱；检查器的运行不能算作“无执行工具”模型自己运行过。
- `evals/test-cc0-fable.py`：离线模拟发现≠加载、跨结果标记污染、模型替换、辅助模型、非零 CLI（命令行工具）退出和超时。
- `evals/test-hook-runtime.py`：官方事件字段、结构化上下文、去重、会话/目录隔离、条件化压力和检查点。
- `evals/test-feedback-runtime.py`：非阻断提醒、只看可见回复、跳过不记录、递归/子代理/离线抑制。
- `evals/render-fable-review.py`：调用实际 creator 的汇总与展示脚本，只装载已评分的可见产物。保留官方原始报告，按真实记录修正占位模型名/固定样本数，注明差值方向，不修改断言或评分，不纳入原始模型流。

可复现的调用形式（路径替换为本次实测值）：

```bash
python3 evals/run-cc0-fable.py \
  --cc0-definition /absolute/path/to/cc0-function.zsh \
  --prompt-file /absolute/path/to/prompt.txt \
  --run-dir /absolute/path/to/new-attempt \
  --plugin-dir /absolute/path/to/isolated-plugin \
  --model claude-fable-5 \
  --require-skill pua-check:pua \
  --require-read-file /absolute/path/to/isolated-plugin/skills/pua/SKILL.md \
  --expected-cwd /absolute/path/to/wrapper-selected-workspace \
  --require-runtime-marker --run
```

`cc0-function.zsh` 应来自用户实际定义，仅在本地留存，不能通过整份 shell 初始化脚本顺带执行无关初始化。关闭 stdin（标准输入），禁止把驱动脚本误送进模型。

## 使用实际 Anthropic skill-creator

临时 `--plugin-dir` 装载用户 cc0 安装中的官方 creator；原生 `Skill("skill-creator:skill-creator")` 成功后读取它的实际文件并核对指纹。让 creator 做有边界的审查和编辑，主线程独立验收其建议——模型审查不是事实终审。

按实际 creator 的流程保存旧版、2–3 个真实任务、配对执行、`grading.json` 与 `timing.json`；用它自带的 `scripts/aggregate_benchmark.py` 和 `eval-viewer/generate_review.py --static` 生成评测页，不自造“成功率”。新增的协议错误须作为失败保留并触发下一轮，而非仅凭代码通过结束。

当前实际 creator 汇总脚本会把样本数写成固定的 3；还会按配置遍历顺序计算差值，`old_skill` 排在前时就是“旧版减新版”。用真实运行列表校准报告元信息，并保留未改动的官方原始报告。不要把这些格式问题包装成模型效果差异。辅助说明文件也不要命名成与评测目录同样的 `eval-*` 前缀；当前上游脚本会把匹配的普通文件误当目录。本仓库展示包装器只暂存已评分的目录，绕开该输入布局问题，不改官方插件。

## 钩子为什么必须分层

官方 [Hooks reference（钩子文档）](https://code.claude.com/docs/en/hooks) 规定了各事件的输入输出：PostToolUse 读取 `tool_response`；失败事件单独处理；模型补充上下文用 `additionalContext`。PreCompact 不支持原先的 prompt（提示词）实现，保存状态要用真实命令。Stop 普通 stdout 并不能让模型再发问，非阻断提醒应使用客户端可见的 `systemMessage`。

工具错误只是一条观察，不是业务验收失败。钩子不能从退出码推断当前子目标，也不该读用户私有思考来补齐它：保存最小的 session+cwd（会话与工作目录）范围数值，提供候选模板，由执行者对当前验收核对后才升压。配置中的用户风味与任务检查点是两条独立恢复路径，不能混为一谈。

显式且有效的风味配置才是锁定；没有配置或 `auto` 只是默认起点。`SessionStart.source == "clear"` 只清除本插件当前会话与目录的数值状态，不能恢复前一任务的 L4，也不删除相邻作用域或业务文件。运行中的模型行为契约仍需独立验收，不能由离线钩子通过代替。

## 真实宿主钩子探针

默认模型评估仍关闭钩子。只有准备好隔离插件与状态目录后，才对该次 `run-cc0-fable.py` 增加 `--enable-plugin-hooks-for-test`：它要求显式插件路径，会话内开启钩子并追加 `--setting-sources ''`，不修改全局文件。个人、项目和本地设置源不参与本次测试；组织托管策略仍适用。不要用 `hooks: {}` 假装清空已有钩子，列表会合并；省略设置源也可能改变模型服务相关配置，因此必须重新核验实际模型，不能改认证来掩盖失败。[设置优先级](https://code.claude.com/docs/en/settings)、[命令行参数](https://code.claude.com/docs/en/cli-reference)。

1. 复制当前技能和钩子脚本，绑定指纹；将注册命令接到 instrumentation shim（观测包装器）。包装器原样传递宿主 JSON（结构化数据），为钩子子进程隔离 `HOME`、当前目录、`PUA_CONFIG` 和 `PUA_STATE_DIR`，防止旧版相对循环状态路径碰到真实工作区。本次还固定 `PYTHON` 为观测进程的 Python 3.14 解释器，并使用 4 秒子进程超时、捕获后转发标准输出/错误；这些测试差异必须随回执披露。这是定向隔离，不是安全沙箱或未改变执行环境的生产复现。
2. 保存每个真实事件的最小回执：事件名、源脚本指纹、工具标识哈希、返回字段名、退出状态、是否输出上下文。不要保存用户提示、错误全文、隐藏思考或凭据；每次回执独立落盘，避免并发覆盖。`UserPromptSubmit` 的普通标准输出与其他事件的 `additionalContext`（补充上下文）要分开识别。
3. 用两个预期非零退出和一个成功命令验证链路：宿主真实失败事件到达、首次静默、第二次候选提醒、成功不清零观察数、模型不把预期错误升级为业务失败。未实际触发的压缩/恢复/清空/代理事件不得算入实测通过。观测包装器通过也不能冒充未修改生产插件或原 E1 行为验收通过。

本次实测 Claude Code `2.1.258` 将两次非零退出交给 `PostToolUseFailure`，成功命令则通过 `PostToolUse.tool_response` 返回，后者没有退出码字段；不能为证明计数而从成功输出里编造退出码。回执在本地证据目录的 `native-hook-probe-1` 中，未来版本需重验。
