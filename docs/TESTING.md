# 构建与离线验收

## 1. 环境与范围

在包含完整 Git 历史的仓库根目录运行。语气保真测试读取固定上游提交
`ac5026791845b730a18eb4ff07512a3b6f2f06f5`；只有源码 ZIP 或缺少该提交的浅克隆不能完成这项检查。

需要 Git、Bash、Python 3.10+、`jq` 和 Python 的 PyYAML（YAML 解析库）。可在自己选择的虚拟环境中安装 `PyYAML>=6,<7`；测试不会自动安装依赖。以下命令不调用真实模型，不需要模型认证。

## 2. 一次完成构建与 18 组离线检查

```bash
set -euo pipefail
python3 scripts/build-model-compat.py </dev/null
for name in \
  agent-governance integrity-guard issue-regressions microsoft-flavor \
  no-telemetry platform-compat pua-loop-hook release-consistency \
  trigger-regex windows-python-hooks yaml-frontmatter; do
  bash "evals/test-${name}.sh" </dev/null
done
for name in cc0-fable codex-evidence feedback-runtime hook-runtime model-compat omp-evidence release-preparation; do
  python3 "evals/test-${name}.py" </dev/null
done
git diff --check
```

`dist/` 由构建器生成且不提交：三个 ZIP 为独立技能包，`manifest.json` 记录实际成员和文件指纹。完整 Claude Code 插件的钩子和命令在仓库的 `hooks/`、`commands/` 中，不在独立技能 ZIP 里。

正式发布由 `vMAJOR.MINOR.PATCH` 标签触发，不由主分支推送或市场刷新代替。发布流水线核对标签与插件版本，提取该版本完整更新说明，链接绑定该标签，并上传上述四个附件。`scripts/prepare-release.py` 只准备说明，不会自行打标签或发布；本地可用 `python3 scripts/prepare-release.py --tag v3.5.1 --repository tanweai/pua` 预览。

其中 Windows 检查是本机模拟兼容回归，不等于在真实 Windows 上完成全流程安装。通过条件是所有命令正常退出、源文件与包一致、两次构建结果相同、原话术不变量保留；不把离线检查数量当作真实模型样本数。

## 3. 在线评估必须另外授权

`run-trigger-test.sh`、`test-behavior.sh` 会调用真实模型，不属于上面的离线列表。
`run-cc0-fable.py`、`run-omp-pua.py`、`run-codex-pua.py` 等运行器默认只预览，显式加 `--run` 才执行；具体参数先看各自 `--help`。`cc0` 是本地包装函数，不是本项目提供的通用命令。

按 [原生加载验收方法](PUA-FABLE-EVAL-WORKFLOW.md) 准备新的隔离目录、固定提示和外置可信清单，再分别检查身份、加载、功能、语气、执行顺序与权限。工具白名单不是操作系统沙箱，独立事后检查也不能倒算成模型自己运行过。

当前结果及限制见 [模型验收矩阵](MODEL-MATRIX-20260909.md)。`compat/evidence/` 是忽略的本地私有留档，不是公开仓库的缺失依赖；离线测试不依赖该目录。发布前审查待提交文件，不要用强制添加把会话、包装函数、凭据或原始输出带入 Git。
