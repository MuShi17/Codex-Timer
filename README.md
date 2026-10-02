# Codex Timer

让 Codex 为长时间异步任务安排下一次检查。适用于深度学习训练、批量推理、数据处理等需要等待较长时间的任务：先检查一次进度，估计何时值得再次查看，再向当前会话设置一条定时消息。

## Why

深度学习实验可能运行几十分钟或数小时。Codex 启动任务后，如果一直等待或频繁查询日志，会产生许多没有变化的检查，消耗调用和上下文；如果结束当前回合，又需要用户记得回来要求检查。

Codex Timer 让 Codex 根据第一次轮询获得的进度安排下一次检查。例如，结合已完成的 epoch、稳定后的每轮耗时和剩余轮数，估计实验还需要多久。设置定时消息后即可结束当前回合，到时再检查日志和结果。

这适用于任务已经在后台运行、暂时没有需要处理的问题、且下一次有意义的检查时间可以估计的场景。等待期间，用户可以继续在当前会话中做其他工作。

## What

Codex Timer 是一个命令行工具，在指定延迟后向现有 Codex CLI 或桌面会话发送一次消息。用户安装一次后，Codex 可从任意项目目录调用全局命令，无需查找源码路径。

- 会话忙碌：立即追加输入到当前任务。
- 会话空闲：发送原文，开启新一轮。
- 原 runtime 关闭或会话卸载：跳过，不另起 runtime，不恢复历史会话。
- 支持修改、取消、查询，以及 JSON 配置文件。
- 安装时自动添加用户级 `codex-timer` skill，让 Codex 学习命令用法和发送规则。

工具负责计时和发送消息；实验状态检查、时长估计和结果分析由 Codex 根据当前项目完成。它不会替你启动训练、读取训练日志或自动判断实验是否结束。当前支持一次性任务；需要持续跟进时，Codex 可在每次复查后重新估计并设置下一条一次性任务。

## How to use

### 安装

需要 Python 3.9+ 和已安装、已登录的 Codex CLI 或桌面应用。仓库的安装入口为：

```text
git clone https://github.com/MuShi17/Codex-Timer.git codex-timer
cd codex-timer
python install.py
```

安装程序一次完成：

1. 使用 uv 将工具安装到独立环境并提供全局 `codex-timer` 命令；没有 uv 时，先通过当前 Python 安装 uv。
2. 将包内的 skill 安装到 `~/.codex/skills/codex-timer/`；设置了 `CODEX_HOME` 时使用 `$CODEX_HOME/skills/codex-timer/`。
3. 必要时通过 uv 更新 shell 的 PATH 配置。

安装完成后重新打开终端。命令不依赖 clone 的目录，移动或删除源码不会影响已安装版本。这里的“全局”指当前用户的所有项目，不需要系统管理员权限。

```text
codex-timer --version
codex-timer doctor
```

普通 `pip install .` 或 `uv tool install .` 只安装 Python 包，不会自动写入 skill。要一次完成两项安装，使用 `python install.py`；手动安装包后可用 `codex-timer install-skill` 补齐 skill。

### 长时间异步任务：首次检查后安排复查

在已经安装工具的 Codex 会话中，可以这样要求：

> 启动训练实验 exp-a。首次检查确认任务正常运行后，根据进度估计剩余时长，用 $codex-timer 安排下次检查。请跟进到完成或失败，等待期间不用持续轮询或反复汇报没有变化的进度。

Codex 的工作流程：

1. 启动用户要求的后台任务，并首次检查进程状态、日志和进度。如果已经失败或完成，直接处理结果。
2. 根据实际进度估计剩余时长。例如，计划训练 100 个 epoch，已完成 10 个，稳定后的平均耗时约 50 秒/epoch，则剩余训练时间约为 `(100 - 10) × 50 = 4500` 秒，即 75 分钟。验证、保存模型和排队等耗时也应计入估计。
3. 选择下次检查的延迟。预计任务接近结束时检查，或在关键阶段检查；进度不足、耗时波动大时，先安排一次合理的阶段性检查，避免把粗略估计当作确定的完成时间。
4. 创建一次性任务，把实验标识、日志/结果路径和后续动作写入消息。确认返回 `pending` 后报告任务 ID、预计检查时间及估计依据，然后结束当前回合。
5. 消息到达后，Codex 检查实际状态。完成则汇报结果，失败则报告原因或按已授权范围处理；如果仍在运行且用户要求持续跟进，重新估计并安排下一次检查。

例如，假设本项目的实验日志和结果分别位于 `runs/exp-a/train.log` 和 `runs/exp-a/results.json`：

```text
codex-timer schedule --after 75m --message "检查实验 exp-a 的进程、runs/exp-a/train.log 和 runs/exp-a/results.json。完成则汇报结果，失败则报告原因；若仍在运行，按用户要求重新估计并设置下一次检查。无实质变化时不反复汇报进度。"
```

消息应足够具体，使到时的 Codex 能找到正确的实验并继续工作。示例中的路径应替换为实际路径；时长由当前日志推算，不必固定为 75 分钟。已有同一实验的待发送检查任务时，优先更新它，避免重复唤醒。

这里的持续跟进是一系列按最新进度决定的一次性检查，不是固定周期任务。用户只要求检查一次时，检查后即结束；明确要求停止跟进时，取消该实验尚未发送的检查任务。

### 直接指定延迟和消息

在 CLI 共享 daemon 会话或桌面会话中，可以直接说：

> 75 分钟后，向本会话发送“请检查实验结果”。

也可以显式指定 skill：

> 使用 $codex-timer，75 分钟后提醒本会话检查实验结果。

skill 指导 Codex 调用全局命令：

```text
codex-timer schedule --after 75m --message "请检查实验结果"
```

省略 `--thread` 时，工具读取当前 `CODEX_THREAD_ID`，自动绑定当前会话和原 runtime。创建成功后立即返回 JSON，包含任务 ID、发送时间和 `pending` 状态；独立后台进程计时，不占用当前模型回合等待。

skill 会指导 Codex 正确引用消息、报告创建结果，并避免误用父会话、恢复已关闭 runtime 或重复发送结果不明的消息。Codex 支持根据描述自动选择 skill，也支持显式调用；如果安装后未显示，可重启 Codex。[官方技能说明](https://learn.chatgpt.com/docs/build-skills)

## 运行环境和接入方式

### 普通 Codex CLI 会话

正常启动 Codex 即可：

```text
codex
```

对于已经启动、连接到本机共享 daemon 的 CLI 会话，无需退出或重新启动。在会话中让 Codex 调用 `codex-timer schedule`，工具会读取 `CODEX_THREAD_ID`，通过已有控制 socket 找到该会话的 runtime。

接入过程只连接现有服务，不执行 daemon start、queue 或 resume。每个任务绑定创建时的 daemon 进程和启动时间；服务退出或被替换后，旧任务跳过。

本版本在 Windows、完整 npm 安装的 Codex CLI 0.159.2 上验证了普通启动流程。`--no-daemon`、禁用共享服务、部分命令行配置覆盖或管理员启动可能没有可接入的共享服务。此时创建任务会明确失败，不会自动重启 Codex。CLI 和桌面端可能看到同一会话记录，但并不因此共享同一个运行进程。

关闭 CLI 窗口不一定关闭共享 daemon。只要原 runtime 仍在且会话仍加载，任务会继续；daemon 关闭后才按 runtime 关闭规则跳过。

底层接口来自 [Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server)。本版本依赖现有共享 daemon 的控制接口。

### 已启动的桌面会话

在 Codex 桌面应用的会话中调用相同命令，无需退出或重新启动：

```text
codex-timer schedule --after 2m --message "check"
```

工具自动识别应用提供的 `CODEX_APP_TOOLS_PIPE_PATH`，通过应用自身的 `read_thread`、`send_message_to_thread` 工具发送消息。桌面通道优先于 CLI daemon；桌面接入失败时不会把同一个历史会话恢复到 CLI。

后台任务绑定创建时的桌面进程和私有 app-server 进程的 PID、启动时间及程序路径，并验证本地通道仍属于原进程。桌面应用或原 app-server 退出、重启后跳过任务；发送前读取目标状态，未加载的会话跳过。只支持本地 Codex 会话，不发送到云端或远程主机。

桌面接入使用 Node.js，优先采用应用提供的 `CODEX_MCP_NODE_PATH`，其次使用 PATH 中的 `node`，不需要 npm 安装依赖。工具和桥接文件随 Python 包一起安装，源码目录移动后仍可使用。普通外部终端不会自动获得桌面通道，需从桌面 Codex 会话内部调用；桌面模式下 `threads` 只显示当前会话，显式 `--thread` 可指定同一实例中已加载的本地会话。

桌面通道来自应用内置插件，尚非稳定的公开接口；应用版本改变时，接口可能变化。Windows Codex 桌面版 26.928.2636.0 上已实测两分钟延迟，并确认忙碌时消息进入同一回合。空闲发送使用同一个应用消息工具，已通过模拟通道测试，尚未实测真实桌面空闲唤醒。

## 命令

```text
codex-timer schedule --after 75m --message "请检查实验结果"
codex-timer update <任务ID> --after 20m --message "检查最新实验结果"
codex-timer cancel <任务ID>
codex-timer status <任务ID>
codex-timer list
codex-timer threads
```

延迟支持 `s`、`m`、`h`、`d`，如 `30s`、`75m`、`2h`。每个任务只发送一次。`update --after 20m` 从修改时刻重新计时；仅 `pending` 任务可以修改或取消。

从其他终端针对已有会话设置任务：

```text
codex-timer threads
codex-timer schedule --thread <会话UUID> --after 75m --message "请检查实验结果"
```

## 配置文件

创建任意位置的 JSON 文件：

```json
{
  "after": "75m",
  "message": "请检查实验结果，并向我汇报。"
}
```

```text
codex-timer schedule --config <配置文件路径>
```

可选字段为 `thread_id`；省略时使用当前会话。命令行参数覆盖配置文件。修改配置只影响下次创建；已创建任务使用 `update` 修改。

## Skill 安装、升级和移除

```text
codex-timer install-skill
codex-timer uninstall-skill
```

更新仓库后再次运行 `python install.py`，会更新全局命令和 skill：

```text
git pull
python install.py
```

安装包包含 skill 文件，安装后的使用不依赖 clone 路径。`codex-timer install-skill` 使用当前已安装版本的 skill；获取新版流程时应先升级工具，再新开会话加载更新后的 skill。

已有同名 skill 若不属于本工具，默认保留并返回冲突错误；明确替换时使用 `python install.py --replace-skill` 或 `codex-timer install-skill --force`。覆盖用户修改前会备份到 `$CODEX_HOME/codex-timer/skill-backups/`。移除时保留修改过的 skill 文件。

指定其他 Codex 目录：

```text
python install.py --codex-home <目录>
codex-timer install-skill --codex-home <目录>
```

`--codex-home` 只控制安装目标；运行时也应设置相同的 `CODEX_HOME`。安装测试或自行管理 PATH 时，可加 `python install.py --no-path-update`。

完整卸载：

```text
codex-timer uninstall-skill
uv tool uninstall codex-timer
```

任务记录及 skill 修改备份保留，不自动删除。

## 数据与发送状态

默认数据目录为 `$CODEX_HOME/codex-timer/`，未设置 `CODEX_HOME` 时是 `~/.codex/codex-timer/`。任务写入 `tasks.sqlite3`，runtime 记录存于 `runtimes/`。源码目录和当前工作目录不会影响任务位置。可通过命令行开头的 `--state <目录>` 或 `CODEX_TIMER_STATE` 覆盖。

| 状态 | 含义 |
| --- | --- |
| `pending` | 等待发送 |
| `sending` | 已获得发送权，正在调用 runtime |
| `delivered` | runtime 已确认收到，不代表模型已完成工作 |
| `skipped` | 原 runtime 不可用，或会话已卸载 |
| `cancelled` | 已取消 |
| `failed` | 已知错误导致发送失败 |
| `unknown` | 无法确认是否收到，不自动重发 |

首版没有周期任务、开机自启或后台计时进程崩溃后的自动恢复。电脑休眠时不会发送；恢复后若原 runtime 仍在，任务会尽快执行一次。调度不是硬实时。计时进程被单独杀死时，任务可能停留在 `pending` 或 `sending`，可根据 `worker_pid` 排查；不要手工重发 `sending` 或 `unknown` 任务。

## 验证

需要完整安装的 Codex CLI 和 uv。运行全局安装及普通 CLI 流程测试：

```text
python tests/installed_workflow.py
```

测试隔离工具、PATH、用户目录和缓存，不修改真实用户的 skill 或 shell 配置。它验证全局安装、从其他目录调用、移动源码、skill 升级备份和冲突保护，再使用已安装命令验证普通 CLI 会话的真实工具调用、空闲唤醒、忙碌追加、修改、取消、重复发送保护和 runtime 关闭。

只验证普通 CLI：

```text
python -m pip install ".[test]"
python tests/normal_cli.py
```

Windows 测试使用 pywinpty，其他系统使用 pexpect。测试正常启动交互式 Codex，使用本地模拟 Responses 服务触发真实 `exec_command`；没有外部模型调用，也不会预先启动 timer 专用 runtime。若本机 `codex` 来自桌面应用的单文件副本，测试需设置 `CODEX_TIMER_TEST_CODEX` 指向完整安装的 CLI。直接使用 npm 包中的原生程序时，可额外设置 `CODEX_TIMER_TEST_PACKAGE_ROOT` 指向 `@openai/codex` 包目录。

桌面传输的隔离测试（不连接真实应用，不发送真实会话消息）：

```text
python tests/desktop_transport.py
```

验证本地通道分片、消息原文、空闲/忙碌分支、未加载和远程会话拦截、发送结果不明时不重发、竞争进程只发送一次，以及原进程关闭或通道被新进程复用后跳过。全局安装测试还使用已安装包中的桌面桥接资源运行这些检查。

本机结果见 `verification.json`。尚未验证无 uv 环境的自动引导、真实 shell PATH 持久化更新、Linux/macOS、真实桌面空闲唤醒，或真实模型的 75 分钟长测。
