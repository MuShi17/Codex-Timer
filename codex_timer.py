"""One-shot messages to an existing, live Codex app-server runtime."""
import argparse
import contextlib
import datetime as dt
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

BASE = Path(__file__).resolve().parent
VERSION = "0.2.0"


def codex_home(explicit=None):
    return Path(explicit or os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()


def default_state():
    return Path(os.environ.get("CODEX_TIMER_STATE") or (codex_home() / "codex-timer"))


class TimerError(Exception):
    pass


class RpcError(TimerError):
    def __init__(self, error):
        self.error = error
        super().__init__(str(error.get("message", error)))


class RuntimeGone(TimerError):
    pass


class DeliveryUnknown(TimerError):
    pass


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        temporary.chmod(0o600)
    os.replace(str(temporary), str(path))


def skill_payload():
    folder = resources.files("codex_timer_assets").joinpath("skill")
    return {"SKILL.md": folder.joinpath("SKILL.md").read_text(encoding="utf-8"),
            "agents/openai.yaml": folder.joinpath("agents").joinpath("openai.yaml").read_text(encoding="utf-8")}


def check_skill_destination(home=None, force=False):
    root = codex_home(home) / "skills"
    target = root / "codex-timer"
    if target.resolve().parent != root.resolve() or target.is_symlink():
        raise TimerError("skill 目标是指向其他目录的链接，拒绝覆盖：" + str(target))
    manifest = target / ".codex-timer-managed.json"
    previous = None
    if manifest.exists():
        previous = read_json(manifest)
    if target.exists() and any(target.iterdir()) and not (previous and previous.get("owner") == "codex-timer") and not force:
        raise TimerError("已存在非本工具管理的 codex-timer skill，已保留。若确认替换，请运行 codex-timer install-skill --force；旧文件会备份。")
    return target, manifest, previous


def install_skill(home=None, force=False):
    target, manifest, previous = check_skill_destination(home, force)
    payload = skill_payload()
    backup = None
    # Preserve user edits before a managed upgrade or explicit replacement.
    for relative, content in payload.items():
        dest = target / relative
        if dest.resolve() != (target.resolve() / relative):
            raise TimerError("skill 文件指向其他位置，拒绝覆盖：" + str(dest))
        if dest.exists():
            current = dest.read_bytes()
            known = (previous or {}).get("files", {}).get(relative)
            changed = hashlib.sha256(current).hexdigest() != known
            if current != content.encode("utf-8") and changed:
                if backup is None:
                    backup = codex_home(home) / "codex-timer" / "skill-backups" / uuid.uuid4().hex
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(current)
    hashes = {}
    for relative, content in payload.items():
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(str(temp), str(dest))
        hashes[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    write_json(manifest, {"owner": "codex-timer", "version": VERSION, "files": hashes})
    return {"status": "installed", "skill": str(target / "SKILL.md"), "version": VERSION,
            "backup": str(backup) if backup else None, "next": "新开 Codex 会话以加载 skill"}


def uninstall_skill(home=None):
    root = codex_home(home) / "skills"
    target = root / "codex-timer"
    if target.resolve().parent != root.resolve() or target.is_symlink():
        raise TimerError("skill 目标是指向其他目录的链接，拒绝删除")
    manifest = target / ".codex-timer-managed.json"
    if not manifest.exists():
        raise TimerError("该 skill 没有本工具的管理记录，已保留")
    record = read_json(manifest)
    if record.get("owner") != "codex-timer":
        raise TimerError("该 skill 不属于本工具，已保留")
    retained = []
    for relative, digest in record.get("files", {}).items():
        path = target / relative
        resolved = path.resolve()
        if target.resolve() not in resolved.parents:
            raise TimerError("skill 管理记录包含目标目录之外的路径")
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            path.unlink()
        elif path.exists():
            retained.append(relative)
    manifest.unlink()
    for directory in (target / "agents", target):
        with contextlib.suppress(OSError):
            directory.rmdir()
    return {"status": "removed", "preserved_modified_files": retained}


def doctor(state):
    executable = os.environ.get("CODEX_CLI_PATH") or shutil.which("codex")
    skill = codex_home() / "skills" / "codex-timer" / "SKILL.md"
    return {"version": VERSION, "command": shutil.which("codex-timer"),
            "python": sys.executable, "codex": executable,
            "state": str(state), "skill": str(skill), "skill_installed": skill.is_file(),
            "thread_id": os.environ.get("CODEX_THREAD_ID"),
            "runtime": os.environ.get("CODEX_TIMER_RUNTIME")}


def duration(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|m|h|d)", str(value).strip())
    if not match:
        raise TimerError("时间格式应为 30s、75m、2h 或 1d")
    seconds = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    if not math.isfinite(seconds) or seconds <= 0:
        raise TimerError("延迟必须是大于零的有限值")
    return seconds


def iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class Rpc:
    def __init__(self, runtime, timeout=10):
        try:
            import websocket
        except ImportError:
            raise TimerError("缺少 websocket-client，请运行 python -m pip install -r requirements.txt")
        self.timeout = timeout
        self.counter = 0
        self.events = []
        try:
            self.ws = websocket.create_connection(
                runtime["endpoint"], timeout=timeout,
                header=["Authorization: Bearer " + runtime["token"]],
                suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"],
            )
        except Exception as exc:
            raise RuntimeGone("无法连接原 runtime；不会启动或恢复其他 runtime") from exc
        try:
            self.call("initialize", {
                "clientInfo": {"name": "codex_timer", "title": "Codex Timer", "version": VERSION},
                "capabilities": {"experimentalApi": True},
            })
            self.ws.send(json.dumps({"method": "initialized"}))
        except Exception:
            self.close()
            raise

    def call(self, method, params=None, mutation=False):
        self.counter += 1
        request_id = self.counter
        request = {"id": request_id, "method": method, "params": params or {}}
        deadline = time.monotonic() + self.timeout
        try:
            self.ws.send(json.dumps(request, ensure_ascii=False))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(method)
                self.ws.settimeout(remaining)
                raw = self.ws.recv()
                if not raw:
                    raise ConnectionError("runtime disconnected")
                response = json.loads(raw)
                if response.get("id") == request_id and "method" not in response:
                    if "error" in response:
                        raise RpcError(response["error"])
                    return response["result"]
                if "id" in response and "method" in response:
                    # This client must never silently grant approvals or answer for the user.
                    self.ws.send(json.dumps({"id": response["id"], "error": {
                        "code": -32601, "message": "Use the interactive Codex client for this request"
                    }}))
                else:
                    self.events.append(response)
                    self.events = self.events[-200:]
        except RpcError:
            raise
        except Exception as exc:
            if mutation:
                raise DeliveryUnknown("发送后未收到确认；可能已送达，不会自动重发") from exc
            raise RuntimeGone("读取 runtime 时连接中断") from exc

    def loaded(self):
        ids, cursor = set(), None
        while True:
            result = self.call("thread/loaded/list", {"cursor": cursor, "limit": 100})
            ids.update(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                return ids

    def close(self):
        with contextlib.suppress(Exception):
            self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def runtime_record(path, expected_id=None):
    try:
        record = read_json(path)
    except (OSError, ValueError) as exc:
        raise RuntimeGone("原 runtime 记录已不可用") from exc
    if not record.get("alive") or (expected_id and record.get("id") != expected_id):
        raise RuntimeGone("原 runtime 已关闭")
    return record


def locate_runtime(state, thread_id, explicit=None):
    configured = explicit or os.environ.get("CODEX_TIMER_RUNTIME")
    candidates = [Path(configured)] if configured else list((state / "runtimes").glob("*.json"))
    matches = []
    for path in candidates:
        try:
            runtime = runtime_record(path)
            with Rpc(runtime) as rpc:
                if thread_id in rpc.loaded():
                    matches.append((path.resolve(), runtime))
        except RuntimeGone:
            continue
    if len(matches) != 1:
        raise TimerError("未找到唯一的、已加载当前会话的 runtime。请通过 launch 启动 Codex，或指定 --runtime；不会恢复历史会话。")
    return matches[0]


def connect_db(state):
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state / "tasks.sqlite3"), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, runtime_path TEXT NOT NULL,
        runtime_id TEXT NOT NULL, message TEXT NOT NULL, due REAL NOT NULL,
        created REAL NOT NULL, status TEXT NOT NULL, detail TEXT,
        method TEXT, turn_id TEXT, finished REAL, worker_pid INTEGER
    )""")
    conn.commit()
    return conn


def task_view(row):
    result = dict(row)
    for key in ("due", "created", "finished"):
        if result.get(key) is not None:
            result[key] = iso(result[key])
    return result


def active_turn(rpc, thread_id):
    try:
        turns = rpc.call("thread/turns/list", {
            "threadId": thread_id, "limit": 5, "sortDirection": "desc", "itemsView": "notLoaded"
        }).get("data", [])
    except RpcError:
        turns = []
    found = next((t["id"] for t in turns if t.get("status") == "inProgress"), None)
    if found:
        return found
    # Legacy threads and transient turns may expose their live turn on a full read.
    try:
        thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
    except RpcError:
        return None
    return next((t["id"] for t in reversed(thread.get("turns", [])) if t.get("status") == "inProgress"), None)


def dispatch(rpc, thread_id, message, task_id):
    for _ in range(3):
        if thread_id not in rpc.loaded():
            raise RuntimeGone("目标会话已经从原 runtime 卸载")
        thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
        status = thread["status"]["type"]
        params = {"threadId": thread_id, "input": [{"type": "text", "text": message}],
                  "clientUserMessageId": task_id}
        if status == "active":
            turn_id = active_turn(rpc, thread_id)
            if not turn_id:
                time.sleep(0.1)
                continue
            params["expectedTurnId"] = turn_id
            try:
                result = rpc.call("turn/steer", params, mutation=True)
                return "turn/steer", result["turnId"]
            except RpcError as exc:
                # Only a known rejected precondition is safe to re-read/retry.
                text = str(exc).lower()
                if any(s in text for s in ("no active turn", "turn id mismatch", "does not match", "expected turn")):
                    continue
                raise
        if status == "idle":
            result = rpc.call("turn/start", params, mutation=True)
            return "turn/start", result["turn"]["id"]
        if status == "notLoaded":
            raise RuntimeGone("目标会话已卸载")
        raise TimerError("目标会话 runtime 状态异常：" + status)
    raise TimerError("会话状态持续变化，无法确认当前任务；本次未发送")


def detached_kwargs():
    result = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
    if os.name == "nt":
        result["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        result["start_new_session"] = True
    return result


def schedule(args, state):
    config = read_json(args.config) if args.config else {}
    thread_id = args.thread or config.get("thread_id") or os.environ.get("CODEX_THREAD_ID")
    after = args.after if args.after is not None else config.get("after")
    message = args.message if args.message is not None else config.get("message")
    if not thread_id:
        raise TimerError("无法识别当前会话。请在 Codex 内调用，或指定 --thread UUID")
    if not isinstance(message, str) or not message.strip():
        raise TimerError("消息必须是非空字符串")
    delay = duration(after)
    path, runtime = locate_runtime(state, thread_id, args.runtime or config.get("runtime"))
    task_id = str(uuid.uuid4())
    now = time.time()
    with connect_db(state) as conn:
        conn.execute("""INSERT INTO tasks
            (id, thread_id, runtime_path, runtime_id, message, due, created, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (task_id, thread_id, str(path), runtime["id"], message, now + delay, now))
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--state", str(state),
                                 "_worker", task_id], **detached_kwargs())
    except OSError:
        with connect_db(state) as conn:
            conn.execute("UPDATE tasks SET status='failed', detail='无法启动后台计时进程' WHERE id=?", (task_id,))
        raise
    with connect_db(state) as conn:
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (proc.pid, task_id))
        emit(task_view(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()))


def finish(state, task_id, status, detail=None, method=None, turn_id=None):
    with connect_db(state) as conn:
        conn.execute("""UPDATE tasks SET status=?, detail=?, method=?, turn_id=?, finished=?
            WHERE id=? AND status IN ('pending','sending')""",
            (status, detail, method, turn_id, time.time(), task_id))


def worker(state, task_id):
    while True:
        with connect_db(state) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["status"] != "pending":
            return
        try:
            runtime_record(row["runtime_path"], row["runtime_id"])
        except RuntimeGone as exc:
            finish(state, task_id, "skipped", str(exc))
            return
        remaining = row["due"] - time.time()
        if remaining > 0:
            time.sleep(min(remaining, 1.0))
            continue
        try:
            runtime = runtime_record(row["runtime_path"], row["runtime_id"])
            with Rpc(runtime) as rpc:
                # Atomic claim also checks due to handle an edit racing the worker.
                with connect_db(state) as conn:
                    changed = conn.execute("""UPDATE tasks SET status='sending'
                        WHERE id=? AND status='pending' AND due<=?""", (task_id, time.time())).rowcount
                    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if not changed:
                    continue
                runtime_record(row["runtime_path"], row["runtime_id"])
                method, turn_id = dispatch(rpc, row["thread_id"], row["message"], task_id)
                finish(state, task_id, "delivered", "runtime 已确认接收；不代表模型已完成执行", method, turn_id)
        except RuntimeGone as exc:
            finish(state, task_id, "skipped", str(exc))
        except DeliveryUnknown as exc:
            finish(state, task_id, "unknown", str(exc))
        except Exception as exc:
            finish(state, task_id, "failed", str(exc))
        return


def edit_task(args, state):
    fields, values = [], []
    if args.after is not None:
        fields.append("due=?")
        values.append(time.time() + duration(args.after))
    if args.message is not None:
        if not args.message.strip():
            raise TimerError("消息不能为空")
        fields.append("message=?")
        values.append(args.message)
    if not fields:
        raise TimerError("请指定 --after 或 --message")
    with connect_db(state) as conn:
        count = conn.execute("UPDATE tasks SET " + ", ".join(fields) + " WHERE id=? AND status='pending'",
                             values + [args.id]).rowcount
        if not count:
            raise TimerError("任务不存在或已开始发送，无法调整")
        emit(task_view(conn.execute("SELECT * FROM tasks WHERE id=?", (args.id,)).fetchone()))


def start_runtime(state, codex=None, extra_server_args=None, env_overrides=None):
    executable = codex or os.environ.get("CODEX_CLI_PATH") or shutil.which("codex")
    if not executable:
        raise TimerError("未找到 codex 可执行文件")
    runtime_id = uuid.uuid4().hex
    directory = state / "runtimes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (runtime_id + ".json")
    token = secrets.token_urlsafe(32)
    token_path = directory / (runtime_id + ".token")
    token_path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        token_path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = "ws://127.0.0.1:" + str(port)
    env = os.environ.copy()
    env.update(env_overrides or {})
    env["CODEX_TIMER_RUNTIME"] = str(path.resolve())
    env["CODEX_TIMER_STATE"] = str(state.resolve())
    env["CODEX_TIMER_SCRIPT"] = str(Path(__file__).resolve())
    # Preserve command discovery even when the launching shell has a custom PATH.
    timer_command = shutil.which("codex-timer")
    if timer_command:
        env["PATH"] = str(Path(timer_command).parent) + os.pathsep + env.get("PATH", "")
    env["CODEX_TIMER_AUTH_TOKEN"] = token
    log_path = directory / (runtime_id + ".log")
    with log_path.open("ab") as log:
        proc = subprocess.Popen([executable, *(extra_server_args or []), "app-server", "--listen", endpoint,
                                 "--ws-auth", "capability-token", "--ws-token-file", str(token_path.resolve())],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    runtime = {"id": runtime_id, "endpoint": endpoint, "token": token, "alive": True,
               "pid": proc.pid, "started": time.time()}
    write_json(path, runtime)
    ready = False
    try:
        for _ in range(100):
            if proc.poll() is not None:
                break
            try:
                with Rpc(runtime, timeout=1):
                    ready = True
                    break
            except RuntimeGone:
                time.sleep(0.1)
    except BaseException:
        stop_runtime(proc, path)
        raise
    if not ready:
        stop_runtime(proc, path)
        raise TimerError("runtime 启动失败，查看 " + str(log_path))
    return proc, path, runtime, env, executable


def stop_runtime(proc, path):
    try:
        runtime = read_json(path)
        runtime["alive"] = False
        runtime["stopped"] = time.time()
        write_json(path, runtime)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        with contextlib.suppress(OSError):
            Path(path).with_suffix(".token").unlink()
        # Do not keep a usable local control token after shutdown.
        with contextlib.suppress(OSError, ValueError):
            runtime = read_json(path)
            runtime.pop("token", None)
            write_json(path, runtime)


def launch(args, state):
    extra = args.codex_args
    if extra and extra[0] == "--":
        extra = extra[1:]
    if any(v in extra for v in ("--remote", "--remote-auth-token-env", "--no-daemon")):
        raise TimerError("launch 已管理 runtime 连接，请勿传入 --remote、--remote-auth-token-env 或 --no-daemon")
    # Explicit CLI config overrides must also reach the shared runtime.
    configs = []
    for i, value in enumerate(extra):
        if value in ("-c", "--config") and i + 1 < len(extra):
            configs.extend(["-c", extra[i + 1]])
    proc, path, runtime, env, executable = start_runtime(state, extra_server_args=configs)
    try:
        cli = subprocess.Popen([executable, "--remote", runtime["endpoint"],
                                "--remote-auth-token-env", "CODEX_TIMER_AUTH_TOKEN", *extra], env=env)
        try:
            return cli.wait()
        except KeyboardInterrupt:
            with contextlib.suppress(subprocess.TimeoutExpired):
                cli.wait(timeout=3)
            if cli.poll() is None:
                cli.terminate()
                cli.wait(timeout=5)
            return 130
    finally:
        stop_runtime(proc, path)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Codex 会话的一次性延迟消息工具")
    parser.add_argument("--version", action="version", version="codex-timer " + VERSION)
    parser.add_argument("--state", default=str(default_state()), help="任务和 runtime 记录目录，默认 $CODEX_HOME/codex-timer")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("launch", help="启动可接收定时消息的 Codex CLI")
    p.add_argument("codex_args", nargs=argparse.REMAINDER)
    p = commands.add_parser("schedule", help="创建一次性任务，后台等待后立即返回")
    p.add_argument("--after", help="例如 75m、30s、2h")
    p.add_argument("--message")
    p.add_argument("--thread", help="默认使用当前 CODEX_THREAD_ID")
    p.add_argument("--runtime", help="默认自动识别原 runtime")
    p.add_argument("--config", help="JSON 配置文件")
    p = commands.add_parser("update", help="调整尚未触发任务的时间或内容")
    p.add_argument("id")
    p.add_argument("--after", help="从此次修改开始重新计时")
    p.add_argument("--message")
    p = commands.add_parser("cancel", help="取消尚未触发的任务")
    p.add_argument("id")
    p = commands.add_parser("list", help="列出任务及发送状态")
    p.add_argument("--thread")
    p = commands.add_parser("status", help="查询一个任务")
    p.add_argument("id")
    p = commands.add_parser("threads", help="列出当前 runtime 已加载的会话")
    p.add_argument("--runtime", default=os.environ.get("CODEX_TIMER_RUNTIME"))
    p = commands.add_parser("install-skill", help="安装或更新全局 codex-timer skill")
    p.add_argument("--codex-home", help="默认 CODEX_HOME 或 ~/.codex")
    p.add_argument("--force", action="store_true", help="替换同名未管理 skill，备份被覆盖的文件")
    p = commands.add_parser("uninstall-skill", help="移除未修改的 skill 文件，保留用户修改")
    p.add_argument("--codex-home")
    commands.add_parser("doctor", help="检查全局命令、skill 和 runtime 环境")
    p = commands.add_parser("_worker", help=argparse.SUPPRESS)
    p.add_argument("id")
    args = parser.parse_args()
    state = Path(args.state).resolve()
    try:
        if args.command == "launch":
            return launch(args, state)
        if args.command == "install-skill":
            emit(install_skill(args.codex_home, args.force))
        elif args.command == "uninstall-skill":
            emit(uninstall_skill(args.codex_home))
        elif args.command == "doctor":
            emit(doctor(state))
        elif args.command == "schedule":
            schedule(args, state)
        elif args.command == "update":
            edit_task(args, state)
        elif args.command == "_worker":
            worker(state, args.id)
        elif args.command == "threads":
            candidates = [Path(args.runtime)] if args.runtime else list((state / "runtimes").glob("*.json"))
            found = []
            for path in candidates:
                try:
                    with Rpc(runtime_record(path)) as rpc:
                        for thread_id in sorted(rpc.loaded()):
                            thread = rpc.call("thread/read", {"threadId": thread_id})["thread"]
                            found.append({"id": thread_id, "name": thread.get("name"),
                                          "status": thread["status"], "runtime": str(path)})
                except RuntimeGone:
                    continue
            emit(found)
        else:
            with connect_db(state) as conn:
                if args.command == "cancel":
                    count = conn.execute("UPDATE tasks SET status='cancelled', finished=? WHERE id=? AND status='pending'",
                                         (time.time(), args.id)).rowcount
                    if not count:
                        raise TimerError("任务不存在或已开始发送，无法取消")
                    emit({"id": args.id, "status": "cancelled"})
                elif args.command == "status":
                    row = conn.execute("SELECT * FROM tasks WHERE id=?", (args.id,)).fetchone()
                    if not row:
                        raise TimerError("任务不存在")
                    emit(task_view(row))
                else:
                    rows = conn.execute("SELECT * FROM tasks" + (" WHERE thread_id=?" if args.thread else "") +
                                        " ORDER BY created DESC", (args.thread,) if args.thread else ()).fetchall()
                    emit([task_view(row) for row in rows])
        return 0
    except (TimerError, OSError, ValueError) as exc:
        emit({"error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
