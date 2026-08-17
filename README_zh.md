# Goal Plus (GP)

[English](README.md) | 简体中文

Goal Plus 是面向长时间 agent 任务的宿主中立运行时。`/goal-plus` 可以直接处理
普通目标；遇到可度量的优化任务时，它会升级到 Search Mode：冻结评价合同、在隔离
候选中探索，并提升经过 verifier 验证的最佳结果。

Pi 和 Codex 是当前支持的宿主路径。

## 快速开始

从 Git 或现有检出安装：

```bash
python -m pip install --user "git+https://github.com/ck0123/goal-plus.git"
# 或
python -m pip install -e ".[dev]"
# 让 HTML 报告额外包含自包含的 Plotly 搜索轨迹图
python -m pip install -e ".[dev,report]"
```

所有宿主都启动同一个 stdio MCP 服务：

```text
goal-plus --root .gp
```

然后在宿主中启动目标：

```text
/goal-plus 修复这个 bug 并验证测试套件。
/goal-plus 在不改变正确性的前提下，用两小时优化 p95 延迟。
/goal-plus mode=probe 先确认向量化是否可行。
/goal-plus mode=autonomous 深度优化这个 kernel。
```

Codex 和 Pi 还提供：

```text
/goal-plus edit <完整的新目标>
/goal-plus resume
/goal-plus-with-final-check <目标>
```

一次请求会启动自主运行。agent 自己判断 Goal Mode 是否足够，或冻结 verifier 后
并行 Search 是否更有价值；进入 Search 不需要额外确认。`mode=autonomous`（默认）
允许给每个初始 candidate worker 较长且可续的探索 lease；`mode=probe` 先做短时可行性
探测。该探索模式只会作为规范化说明写入 `raw_goal` 最后一行，不是新的调度状态。

## 宿主

| 宿主 | 项目资产 | 入口 | Search worker 路径 |
|---|---|---|---|
| Pi | `.pi/` | `/goal-plus` 或 `pi -p "/goal-plus ..."` | 持久化 Pi RPC pool；参阅 [Pi](docs/pi.md) |
| Codex | `.codex/` | `goal-plus` skill 或 `/goal-plus` 提示 | 固定 parallel loops 与原生同 worker continuation；参阅 [Codex](docs/codex.md) |

Codex 需要将 `.codex/config.example.toml` 复制为被忽略的本地文件
`.codex/config.toml`。宿主差异和策略覆盖见
[Agent Host Adapters](docs/agent-host-adapters.md)。

## 心智模型

- 一条 **Goal Plus 记录**对应完整的用户任务。
- 一个 **search task** 是在一份 frozen spec 上运行的一个 `run_id`；一个目标可关联
  多个 search task。
- 初始 **SearchPlan** 只分配固定 candidate lane，不是逐轮提交 plan 的协议。
- 一个 **candidate** 是带 verifier 历史的长期并行 loop 工作区。
- 一个 **worker session** 是宿主上下文/来源句柄。worker 生命周期属于宿主，
  不属于 Search 运行时。
- **共享平面**保存冻结合同、verifier 精确 commit、Global Evidence、异步客观 View
  和 selection 状态，不暴露其他 candidate 的推理或 workspace。
- 一个 **verifier concern** 只是 worker 的建议；只有 main agent 能确认。确认后先封锁
  当前 run，再停止全部宿主 worker，并创建新的 spec/run。

Search 使用固定 parallel loops：初始 candidate 只创建一次；任意 worker 完成后，
main 只校验结果、刷新 verifier-backed best，并在全局停止条件未满足时续跑同一个
candidate。main 不选择后续技术方向，也不会按分数替换 candidate。完整流程见
[Shared Plane](docs/shared-plane.md)。

同一份有效评价/编辑合同应保持一个 run。必须新建 successor 时，
`source_run_id` 只继承有界 frontier、feature 和带作用域 pitfalls 作为研究上下文；
旧分数不能复用，必须重新验证。

运行时状态保存在 `.gp/`。`search_tasks` 只追加；`linked_search` 只是当前任务的
兼容视图。

配置 `promotion_verifiers` 后，Promotion 会执行独立检查，而不是复用缓存结果。
Runtime 会检出选中的 verifier-backed revision，以
`GOAL_PLUS_VERIFIER_PHASE=promotion` 重新运行每个 Promotion Gate，将证据绑定到
Selected Git Head 和 Artifact Hash，之后才生成可被 Git 应用的 Patch。Promotion
失败时保持 `ready_to_promote` 以便重试，并且不会生成 Patch。

## 文档

| 需求 | 文档 |
|---|---|
| 架构、共享 Evidence、回滚和端到端流程 | [Shared Plane](docs/shared-plane.md) |
| 当前 MCP 与 Pi 本地工具 | [API](docs/api.md) |
| 宿主能力对比 | [Agent Host Adapters](docs/agent-host-adapters.md) |
| 运行时与宿主日志 | [Debugging](docs/debugging-runtime.md) |
| shared-dir 效果验证 | [Shared-dir 实验计划](docs/shared-dir-effectiveness-plan.md) |
| spec 与可运行示例 | [Examples](examples/README.md) |
| 测试与真实宿主证据 | [Tests](tests/README.md) |

Benchmark 专用任务、evaluator、campaign 和对比证据统一放在
[bench-goal-plus](https://github.com/ck0123/bench-goal-plus)，不再由本
runtime 仓重复维护。

## 开发

```bash
python -m pytest -q
git diff --check
```

Pi 和 Codex 当前维护的策略集合是 `agent_guided`
（`agent`/`default`）和 `random`（`random_mode`）。
