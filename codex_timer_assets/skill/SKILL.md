---
name: codex-timer
description: 用全局 codex-timer 命令创建、修改、取消或查询向当前 Codex 会话发送消息的一次性延迟任务。适用于“75 分钟后向本会话发送某段内容”或“过一会检查实验”等请求。
---

# Codex Timer

用 shell 调用全局命令 `codex-timer`。任务创建后立即返回，后台进程负责等待。

## 创建任务

用户提供延迟和消息时直接运行；缺少延迟或消息时，只询问缺少的内容。例如：

```text
codex-timer schedule --after 75m --message "请检查实验结果"
```

`--after` 支持 `s`、`m`、`h`、`d`，如 `30s`、`75m`、`2h`。只支持一次性任务。为当前会话设置时省略 `--thread`，工具会读取 `CODEX_THREAD_ID`；不要使用父会话 ID、最近会话或从历史列表猜测目标。

按当前 shell 正确引用消息，使原文不被 shell 展开。复杂的多行内容可以写入临时 JSON 文件，再执行 `codex-timer schedule --config <文件>`，配置字段为 `after` 和 `message`。显式针对另一会话时才使用 `--thread <UUID>`。

确认命令返回成功和 `pending` 状态，再向用户报告任务 ID、发送时间和消息原文。不要通过长时间 sleep 等待，不要把“已创建”说成“已送达”。

## 修改和取消

```text
codex-timer update <任务ID> --after 20m --message "检查最新实验结果"
codex-timer cancel <任务ID>
codex-timer status <任务ID>
codex-timer list
```

只能修改或取消 `pending` 任务；修改延迟会从修改时刻重新计时。用户没有给出任务 ID 时，用 `codex-timer list --thread <当前 CODEX_THREAD_ID>` 查询当前会话的任务并结合请求识别，多个候选且无法判断时再询问。

## runtime 行为及故障

- 目标忙碌：消息立即追加到当前任务；目标空闲：开启新一轮。
- 原 runtime 已关闭或会话已卸载：任务跳过。不要为了发送消息另起 runtime 或恢复历史会话。
- `delivered` 表示 runtime 已接收，不代表消息中的工作已经完成。`unknown` 表示无法确认是否收到，避免自动重发。
- 找不到命令或 runtime 时，用 `codex-timer doctor` 检查。命令安装后新开的终端才能获得更新后的 PATH。
- 普通 CLI 若使用共享 daemon，工具会自动接入已有服务，当前会话无需退出或重新启动。直接调用 `schedule` 即可。
- 桌面应用中也可直接调用 `schedule`，工具通过应用提供的本地消息通道接入当前实例。无需重新启动会话。桌面通道优先于 CLI daemon，接入失败时不会转发到另一服务。
- 桌面端需要当前会话提供的 `CODEX_APP_TOOLS_PIPE_PATH` 和 Node.js（优先使用应用提供的 `CODEX_MCP_NODE_PATH`）；不要从别的会话或历史任务复制通道地址。`threads` 在桌面端只显示当前会话。
- CLI 的 `--no-daemon` 等独立模式无法接入；创建失败时如实说明。不要替换正在运行的 CLI 或为了接入启动其他 runtime。
- CLI 窗口退出后共享 daemon 可能继续运行；只要原 runtime 和会话仍在，任务仍会执行。绑定的 daemon 关闭或替换后跳过，不向新服务转移任务。

若用户要求周期任务，说明当前版本只支持一次性，并确认是否改为发送一次。
