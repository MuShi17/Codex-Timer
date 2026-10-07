---
name: codex-timer
description: 在启动或跟进长时间异步任务（如训练、批量推理、子代理任务）前使用，安排定时复查；也用于创建、修改、取消和查询向当前 Codex 会话发送的一次性延迟消息。
---

# Codex Timer

通过全局命令 `codex-timer` 设置一次性消息，创建后立即返回，由后台进程计时。本 skill 是安装后的操作入口，不依赖 README 或源码目录。

## 设置消息

```text
codex-timer schedule --after 75m --message "请检查实验结果"
```

`--after` 支持 `s`、`m`、`h`、`d`。当前会话省略 `--thread`，工具读取 `CODEX_THREAD_ID`；不要猜测会话 ID。只有用户指定其他会话时才使用 `--thread <UUID>`。

用户给出时间和消息时按原文执行；普通提醒缺少必要信息时再询问。按当前 shell 正确引用消息；复杂内容可用 JSON 文件：`{"after":"75m","message":"消息原文"}`，然后运行 `codex-timer schedule --config <文件>`。

确认返回成功且状态为 `pending`，报告任务 ID、发送时间和消息；自主估算时间时简要说明依据。结束当前回合，等待期间不持续 sleep、轮询或反复汇报无变化的状态。

## 异步任务复查

用户要求稍后检查或持续跟进时：

1. 首次检查任务状态和进度，估计下一次有意义的检查时间。训练可用 `(总 epoch - 已完成 epoch) × 稳定后的平均每 epoch 耗时`，计入验证、保存等额外耗时；避开预热样本。信息不足时安排阶段性检查，不把估计当作确定的完成时间。子代理任务按复杂度估计，自主安排的复查间隔至少 5 分钟；用户指定的时间优先。
2. 将任务标识、检查入口（日志、结果路径或子代理 ID）及后续动作写入消息。同一任务已有 `pending` 复查时更新它，避免重复调度。
3. 到时检查实际结果。完成或失败则汇报并停止跟进；仍在运行且用户要求持续跟进时，重新估计并安排下一条一次性消息。单次检查不自动续约。

已完成、失败或用户要求停止的任务不再调度，并取消其多余的 `pending` 复查，保留无关任务。定时检查不扩大用户授权的后续操作范围。

例：计划 100 个 epoch，已完成 10 个，稳定后约 50 秒/epoch，剩余训练时间约 75 分钟。确认实际任务和路径后，可设置：

```text
codex-timer schedule --after 75m --message "检查实验 exp-a 的进程、runs/exp-a/train.log 和 runs/exp-a/results.json。完成或失败则汇报；若仍在运行且用户要求持续跟进，重新估计并安排下一次检查。无实质变化时不反复汇报进度。"
```

路径和间隔应按实际任务替换；子代理复查消息使用对应代理 ID 和要检查的产出。用户提供的原文消息仍按原文发送。

## 管理任务

```text
codex-timer list --thread <当前 CODEX_THREAD_ID>
codex-timer status <任务ID>
codex-timer update <任务ID> --after 20m --message "检查最新实验结果"
codex-timer cancel <任务ID>
```

仅 `pending` 任务可修改或取消；`update --after` 从修改时刻重新计时。任务不明确时先查询，多个候选无法区分时再询问。

## 发送行为与故障

- 目标忙碌时追加到当前任务，空闲时开启新一轮；原 runtime 关闭或会话卸载时跳过，不另起 runtime 或恢复历史会话。
- `delivered` 仅表示已接收；`sending` 或 `unknown` 任务不要自动重发。
- 工具没有固定周期调度；持续复查需每次按最新状态安排一次性任务。

## 接入与排障

- CLI 需连接已有共享 daemon；`--no-daemon` 独立模式无法接入。关闭 CLI 窗口不一定关闭 daemon，只要原 runtime 和会话仍在，任务仍可发送。
- 桌面端从应用会话内部调用，需当前环境提供 `CODEX_APP_TOOLS_PIPE_PATH`；Node.js 优先使用 `CODEX_MCP_NODE_PATH`，否则使用 PATH 中的 `node`。不复制其他会话的通道地址。工具自动选择桌面通道，失败时不转移到 CLI。
- `codex-timer threads` 可查看可接入的会话；桌面模式只显示当前会话。为自己设置消息仍省略 `--thread`，不用列表猜测当前会话。
- 找不到命令时检查 PATH。使用 uv 安装的工具可通过 `uv tool dir --bin` 定位全局可执行文件，再用绝对路径调用；不需要找 clone 目录。
- 命令可用但创建失败时运行 `codex-timer doctor`，结合错误排查会话 ID、运行实例和桌面 Node.js 环境。如实报告失败，不替换实例或恢复历史会话。
- `status` 中的 `detail` 提供失败原因；`skipped` 表示原 runtime 不可用或会话已卸载，不表示消息已发送。
- 当前只有 Windows 的实测记录，已覆盖桌面忙碌追加和空闲唤醒；macOS/Linux 未实测。
