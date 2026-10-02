# Codex Timer

在指定延迟后，向当前仍在运行的 Codex 会话发送一次消息。用户安装一次后，Codex 可从任意项目目录调用全局命令，无需查找源码路径。

- 会话忙碌：立即追加输入到当前任务。
- 会话空闲：发送原文，开启新一轮。
- 原 runtime 关闭或会话卸载：跳过，不另起 runtime，不恢复历史会话。
- 支持修改、取消、查询，以及 JSON 配置文件。
- 安装时自动添加用户级 `codex-timer` skill，让 Codex 学习命令用法和发送规则。

## Clone 后全局安装

需要 Python 3.9+ 和已安装、已登录的 Codex CLI。仓库的安装入口为：

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

## 在任意项目目录启动 Codex

```text
codex-timer launch
```

恢复已有会话：

```text
codex-timer launch -- resume <会话UUID>
```

传入其他 CLI 参数：

```text
codex-timer launch -- -C <项目目录>
```

启动入口让交互式 CLI 和定时工具连接到同一个 runtime。退出该 CLI 会关闭本次 runtime，待发送任务会跳过。runtime 仅监听本机并需要随机令牌；不会修改 Codex 的登录信息或默认执行权限。

**仍需通过 `codex-timer launch` 启动或恢复 CLI。**全局安装和 skill 解决命令发现与使用指导，但普通 CLI/桌面端若没有共享控制入口，无法直接向其当前任务追加输入。工具不会把接入失败伪装成任务创建成功。

底层接口来自 [Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server)。WebSocket 传输仍属实验性；本版本在 Windows、Codex CLI 0.159.2 上验证。

## 让 Codex 自己设置定时消息

在上述会话中可以直接说：

> 75 分钟后，向本会话发送“请检查实验结果”。

也可以显式指定 skill：

> 使用 $codex-timer，75 分钟后提醒本会话检查实验结果。

skill 指导 Codex 调用全局命令：

```text
codex-timer schedule --after 75m --message "请检查实验结果"
```

省略 `--thread` 时，工具读取当前 `CODEX_THREAD_ID`，自动绑定当前会话和原 runtime。创建成功后立即返回 JSON，包含任务 ID、发送时间和 `pending` 状态；独立后台进程计时，不占用当前模型回合等待。

skill 会指导 Codex 正确引用消息、报告创建结果，并避免误用父会话、恢复已关闭 runtime 或重复发送结果不明的消息。Codex 支持根据描述自动选择 skill，也支持显式调用；如果安装后未显示，可重启 Codex。[官方技能说明](https://learn.chatgpt.com/docs/build-skills)

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

可选字段为 `thread_id` 和 `runtime`；省略时使用当前会话和 runtime。命令行参数覆盖配置文件。修改配置只影响下次创建；已创建任务使用 `update` 修改。

## Skill 安装、升级和移除

```text
codex-timer install-skill
codex-timer uninstall-skill
```

再次运行仓库的 `python install.py` 会更新全局命令和 skill。安装包包含 skill 文件，升级时不依赖旧 clone 路径。

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

默认数据目录为 `$CODEX_HOME/codex-timer/`，未设置 `CODEX_HOME` 时是 `~/.codex/codex-timer/`。任务写入 `tasks.sqlite3`，runtime 记录存于 `runtimes/`。源码目录和当前工作目录不会影响任务位置。可通过命令行开头的 `--state <目录>` 或 `CODEX_TIMER_STATE` 覆盖；启动的 runtime 会沿用该目录。

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

```text
python tests/installed_workflow.py
```

全局安装测试需要 uv 已可用。测试将安装、PATH、Codex 用户目录及缓存隔离到临时目录，不修改真实用户的技能或 shell PATH。它会验证安装后从其他目录执行、移动源码、skill 升级备份、冲突保护和移除，再通过已安装命令进行真实 runtime 集成测试。

仅验证 runtime：

```text
python tests/integration_timer.py
```

测试使用真实 Codex app-server 和本地模拟 Responses 服务，不调用外部模型。它验证 runtime 能发现安装的 skill，Codex 的真实 `exec_command` 能通过全局命令给自身设定任务，以及忙碌、空闲、修改、取消、重复进程竞争、卸载和关闭场景。

本机结果见 `verification.json`。尚未验证无 uv 环境的自动引导、真实 shell PATH 持久化更新、Linux/macOS，或真实模型的 75 分钟长测。
