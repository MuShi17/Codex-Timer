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
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

VERSION = "0.4.2"


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


def read_skill_manifest(path, force=False):
    if not path.exists():
        return None
    try:
        record = read_json(path)
        if not isinstance(record, dict):
            raise ValueError("管理记录必须是对象")
        if record.get("owner") != "codex-timer":
            return None
        files = record.get("files")
        if not isinstance(files, dict) or not all(
                isinstance(name, str) and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest)
                for name, digest in files.items()):
            raise ValueError("管理记录的 files 字段无效")
        return record
    except ValueError as exc:
        if force:
            return None
        raise TimerError("skill 管理记录损坏，已保留；可用 --force 备份并替换") from exc


def check_skill_destination(home=None, force=False):
    root = codex_home(home) / "skills"
    target = root / "codex-timer"
    if target.resolve().parent != root.resolve() or target.is_symlink():
        raise TimerError("skill 目标是指向其他目录的链接，拒绝覆盖：" + str(target))
    manifest = target / ".codex-timer-managed.json"
    previous = read_skill_manifest(manifest, force)
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
    record = read_skill_manifest(manifest)
    if record is None:
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
            "desktop_tools_available": bool(os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")),
            "desktop_node_available": bool(os.environ.get("CODEX_MCP_NODE_PATH") or shutil.which("node")),
            "shared_control_socket_exists": (codex_home() / "app-server-control" / "app-server-control.sock").exists()}


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


class ProxySocket:
    """Socket interface over Codex's transparent stdio-to-control-socket proxy.

    The proxy carries HTTP/WebSocket bytes, not JSONL RPC messages. It only
    connects to a running server and cannot start or resume a server.
    """
    def __init__(self, executable, socket_path, timeout):
        self.timeout = timeout
        self.buffer = b""
        self.incoming = queue.Queue()
        self.proc = subprocess.Popen(
            [executable, "app-server", "proxy", "--sock", socket_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                self.incoming.put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.incoming.put(b"")

    def send(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        return len(data)

    def recv(self, size):
        if not self.buffer:
            try:
                self.buffer = self.incoming.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError("Codex control socket proxy timed out")
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def shutdown(self, how):
        self.close()

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        for stream in (self.proc.stdin, self.proc.stdout):
            with contextlib.suppress(OSError):
                stream.close()


class Rpc:
    def __init__(self, runtime, timeout=10):
        try:
            import websocket
        except ImportError:
            raise TimerError("缺少 websocket-client，请运行 python -m pip install -r requirements.txt")
        self.timeout = timeout
        self.counter = 0
        self.events = []
        self.proxy = None
        try:
            check_daemon(runtime)
            self.proxy = ProxySocket(runtime["codex"], runtime["socket_path"], timeout)
            self.ws = websocket.create_connection(
                "ws://localhost/", timeout=timeout, socket=self.proxy,
                suppress_origin=True, http_no_proxy=["localhost"],
            )
            check_daemon(runtime)
        except Exception as exc:
            if self.proxy:
                self.proxy.close()
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
        if self.proxy:
            self.proxy.close()

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
    check_runtime(record)
    return record


def process_snapshot(process):
    return {"pid": process.pid, "created": process.create_time(), "executable": process.exe()}


def check_process(snapshot):
    import psutil
    process = psutil.Process(snapshot["pid"])
    if (process_snapshot(process) != snapshot or not process.is_running()
            or process.status() == psutil.STATUS_ZOMBIE):
        raise RuntimeGone("原桌面 runtime 已关闭或被替换")


def pipe_owner(pipe):
    """Obtain the actual Windows pipe owner, without issuing an app tool call."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise RuntimeGone("桌面消息通道不可用")
    try:
        pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            raise RuntimeGone("无法确认桌面消息通道的原进程")
        return pid.value
    finally:
        kernel.CloseHandle(handle)


def check_runtime(runtime, channel=False):
    if runtime.get("transport") != "desktop":
        return check_daemon(runtime)
    import psutil
    try:
        for snapshot in runtime["processes"]:
            check_process(snapshot)
        if channel:
            if os.name == "nt":
                if pipe_owner(runtime["pipe"]) != runtime["owner_pid"]:
                    raise RuntimeGone("桌面消息通道已被替换")
            elif socket_identity(runtime["pipe"]) != runtime["pipe_identity"]:
                raise RuntimeGone("桌面消息通道已被替换")
    except RuntimeGone:
        raise
    except (OSError, ValueError, KeyError, psutil.Error) as exc:
        raise RuntimeGone("原桌面 runtime 已不可用") from exc


class DesktopRpc:
    """Use the app's own tools; never resume a thread through another server."""
    def __init__(self, runtime, timeout=10):
        self.runtime, self.timeout = runtime, timeout
        check_runtime(runtime, channel=True)

    def native(self, method, params, mutation=False):
        check_runtime(self.runtime, channel=True)
        request = {"pipe": self.runtime["pipe"], "timeoutMs": int(self.timeout * 1000),
                   "rpc": {"id": uuid.uuid4().hex, "jsonrpc": "2.0", "method": method, "params": params}}
        encoded = json.dumps(request, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 8 * 1024 * 1024:
            raise TimerError("桌面消息请求超过 8 MiB 通道限制")
        helper = resources.files("codex_timer_assets").joinpath("desktop_bridge.mjs")
        try:
            with resources.as_file(helper) as path:
                result = subprocess.run([self.runtime["node"], str(path)],
                                        input=encoded,
                                        capture_output=True, text=True, encoding="utf-8",
                                        timeout=self.timeout + 3,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or not isinstance(response.get("response", {}), dict):
                raise ValueError("Invalid desktop RPC envelope")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if mutation:
                raise DeliveryUnknown("桌面发送结果无法确认，不会自动重发") from exc
            raise RuntimeGone("无法读取原桌面 runtime") from exc
        error = response.get("error") or response.get("response", {}).get("error")
        if error:
            # The app's generic error can also occur after accepting a message.
            if mutation and response.get("sent"):
                raise DeliveryUnknown("桌面发送后未收到成功确认，不会自动重发：" + str(error))
            raise RuntimeGone("桌面消息通道不可用：" + str(error))
        envelope = response.get("response", {})
        if "result" not in envelope:
            if mutation and response.get("sent"):
                raise DeliveryUnknown("桌面发送确认缺少结果，不会自动重发")
            raise RuntimeGone("桌面通道返回无效结果")
        return envelope["result"]

    def tool(self, name, arguments, mutation=False):
        result = self.native("tools/call", {
            "namespace": "codex_app", "tool": name, "callerSource": "codex",
            "threadId": self.runtime["caller_thread"],
            "turnId": "mcp-turn-" + uuid.uuid4().hex,
            "callId": "mcp-call-" + uuid.uuid4().hex,
            "arguments": arguments,
        }, mutation=mutation)
        if not isinstance(result, dict):
            if mutation:
                raise DeliveryUnknown("桌面发送确认格式无法识别，不会自动重发")
            raise TimerError("桌面工具返回格式无法识别")
        items = result.get("contentItems")
        if not isinstance(items, list):
            if mutation:
                raise DeliveryUnknown("桌面发送确认内容无法识别，不会自动重发")
            raise TimerError("桌面工具返回内容无法识别")
        text = "\n".join(item["text"] for item in items
                         if isinstance(item, dict) and item.get("type") == "inputText" and isinstance(item.get("text"), str))
        if result.get("success") is not True:
            if mutation:
                raise DeliveryUnknown("桌面工具未确认送达，不会自动重发：" + text[:500])
            raise TimerError("桌面工具读取失败：" + text[:500])
        try:
            return json.loads(text)
        except ValueError as exc:
            if mutation:
                raise DeliveryUnknown("桌面发送确认格式无法识别，不会自动重发") from exc
            raise TimerError("桌面工具返回格式无法识别") from exc

    def read(self, thread_id):
        data = self.tool("read_thread", {"threadId": thread_id, "hostId": "local", "turnLimit": 1,
                                         "includeOutputs": False, "maxOutputCharsPerItem": 0})
        if not isinstance(data, dict) or not isinstance(data.get("thread"), dict):
            raise TimerError("桌面会话读取返回格式无法识别")
        thread = data.get("thread", {})
        if (not isinstance(thread.get("status"), dict) or not isinstance(data.get("turns", []), list)
                or not all(isinstance(turn, dict) for turn in data.get("turns", []))):
            raise TimerError("桌面会话状态或回合列表格式无法识别")
        if thread.get("id") != thread_id or thread.get("hostId") != "local" or thread.get("kind") != "codex":
            raise TimerError("只能接入当前桌面实例中的本地 Codex 会话")
        if thread.get("status", {}).get("type") not in ("active", "idle"):
            raise RuntimeGone("目标会话未在原桌面 runtime 中加载")
        return data

    def dispatch(self, thread_id, message):
        before = self.read(thread_id)
        check_runtime(self.runtime, channel=True)
        result = self.tool("send_message_to_thread", {"threadId": thread_id, "hostId": "local", "prompt": message},
                           mutation=True)
        if (not isinstance(result, dict) or result.get("threadId") != thread_id
                or any(result.get(key) is False for key in ("success", "accepted", "delivered"))):
            raise DeliveryUnknown("桌面发送确认未标明目标会话或成功接收，不会自动重发")
        turn_id = result.get("turnId")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise DeliveryUnknown("桌面发送确认的回合 ID 无效，不会自动重发")
        if not turn_id and before["thread"]["status"]["type"] == "active":
            # Some app acknowledgements omit the turn id. Only record it if a
            # post-acceptance read confirms the original turn is still active.
            prior = next((t.get("id") for t in before.get("turns", []) if t.get("status") == "inProgress"), None)
            with contextlib.suppress(TimerError):
                after = self.read(thread_id)
                if prior and any(t.get("id") == prior and t.get("status") == "inProgress" for t in after.get("turns", [])):
                    turn_id = prior
        return "send_message_to_thread", turn_id

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def connect_runtime(runtime, timeout=10):
    return DesktopRpc(runtime, timeout) if runtime.get("transport") == "desktop" else Rpc(runtime, timeout)


def discover_desktop(state, thread_id=None):
    import psutil
    pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    caller = os.environ.get("CODEX_THREAD_ID")
    if not pipe or not caller:
        raise TimerError("桌面接入需要在 Codex 桌面会话内调用")
    node = os.environ.get("CODEX_MCP_NODE_PATH") or shutil.which("node")
    if not node or not Path(node).is_file():
        raise TimerError("桌面接入找不到 Node.js 运行程序")
    try:
        parents = psutil.Process().parents()
        server = next((p for p in parents if "app-server" in p.cmdline() and Path(p.exe()).stem.lower() == "codex"), None)
    except psutil.Error as exc:
        raise RuntimeGone("无法确认当前桌面 runtime 的原进程") from exc
    if not server:
        raise TimerError("当前调用不属于正在运行的桌面 Codex runtime")
    if os.name == "nt":
        owner_pid = pipe_owner(pipe)
        owner = next((p for p in parents if p.pid == owner_pid), None)
        if not owner:
            raise TimerError("桌面通道不属于当前进程的原桌面实例")
        pipe_identity = None
    else:
        owner = server.parent()
        owner_pid, pipe_identity = owner.pid, socket_identity(pipe)
    try:
        processes = [process_snapshot(server), process_snapshot(owner)]
    except psutil.Error as exc:
        raise RuntimeGone("原桌面 runtime 已不可用") from exc
    runtime = {"transport": "desktop", "alive": True, "pipe": pipe, "owner_pid": owner_pid,
               "pipe_identity": pipe_identity, "processes": processes,
               "node": str(Path(node).resolve()), "caller_thread": caller}
    runtime["id"] = hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest()
    with DesktopRpc(runtime, timeout=5) as rpc:
        catalog = rpc.native("tools/list", {"threadStartKind": "all"})
        names = {t["name"] for t in catalog.get("tools", []) if t.get("namespace") == "codex_app"}
        if not {"read_thread", "send_message_to_thread"} <= names:
            raise TimerError("当前桌面版本没有所需的会话消息工具")
        rpc.read(thread_id or caller)
    path = state / "runtimes" / (runtime["id"] + ".json")
    write_json(path, runtime)
    return path.resolve(), runtime


def daemon_identity(record):
    return {key: record.get(key) for key in ("pid", "processStartTime", "processIdentity", "linuxProcessIdentity", "executableIdentity")}


def socket_identity(path):
    path = Path(path)
    stat = path.stat()
    # Windows AF_UNIX entries are reparse points that GetFinalPathNameByHandle
    # cannot resolve. Their file identity still changes when the socket is replaced.
    return [str(path.absolute()), stat.st_ino, stat.st_mtime_ns]


def owns_current_call(runtime):
    """A persisted thread ID alone cannot identify the caller's live runtime."""
    import psutil
    try:
        return any(parent.pid == runtime["daemon_pid"] and parent.create_time() == runtime["daemon_created"]
                   for parent in psutil.Process().parents())
    except psutil.Error:
        return False


def owns_linux_socket(process, socket_path):
    resolved = str(Path(socket_path).resolve())
    return any(connection.laddr == resolved
               for connection in process.net_connections(kind="unix"))


def check_daemon(runtime):
    try:
        import psutil
        process = psutil.Process(runtime["daemon_pid"])
        if runtime.get("daemon_process"):
            identity_changed = (sys.platform != "linux"
                                or process_snapshot(process) != runtime["daemon_process"]
                                or not owns_linux_socket(process, runtime["socket_path"]))
        else:
            current = read_json(runtime["daemon_pid_file"])
            identity_changed = daemon_identity(current) != runtime["daemon_identity"]
        if (identity_changed
                or process.create_time() != runtime["daemon_created"]
                or socket_identity(runtime["socket_path"]) != runtime["socket_identity"]
                or not process.is_running() or process.status() == psutil.STATUS_ZOMBIE):
            raise RuntimeGone("原共享 runtime 已关闭或被替换")
    except RuntimeGone:
        raise
    except (OSError, ValueError, KeyError, psutil.Error) as exc:
        raise RuntimeGone("原共享 runtime 已不可用") from exc


def discover_linux_server(state, socket_path, thread_id):
    """Bind a PID-file-free Linux server to the caller's ancestry and socket."""
    import psutil
    try:
        for process in psutil.Process().parents():
            if Path(process.exe()).name != "codex" or "app-server" not in process.cmdline():
                continue
            if not owns_linux_socket(process, socket_path):
                continue
            snapshot = process_snapshot(process)
            socket_snapshot = socket_identity(socket_path)
            runtime_id = hashlib.sha256(json.dumps([snapshot, socket_snapshot], sort_keys=True).encode()).hexdigest()
            runtime = {"id": runtime_id, "transport": "daemon", "alive": True,
                       "endpoint": "ws://localhost/", "socket_path": str(socket_path),
                       "codex": snapshot["executable"], "daemon_process": snapshot,
                       "daemon_pid": snapshot["pid"], "daemon_created": snapshot["created"],
                       "socket_identity": socket_snapshot}
            with Rpc(runtime, timeout=3) as rpc:
                if thread_id is not None and thread_id not in rpc.loaded():
                    continue
            check_daemon(runtime)
            path = state / "runtimes" / (runtime_id + ".json")
            write_json(path, runtime)
            return path.resolve(), runtime
    except (OSError, ValueError, KeyError, psutil.Error, RuntimeGone):
        pass
    return None


def discover_daemon(state, thread_id=None, require_self=False):
    """Passively discover the existing daemon; never invoke daemon start/queue."""
    import psutil
    home = codex_home()
    socket_path = home / "app-server-control" / "app-server-control.sock"
    if not socket_path.exists():
        return None
    for name in ("daemon.pid", "app-server.pid"):
        pid_file = home / "app-server-daemon" / name
        try:
            identity = daemon_identity(read_json(pid_file))
            process = psutil.Process(identity["pid"])
            executable = process.exe()
            created = process.create_time()
            socket_snapshot = socket_identity(socket_path)
            runtime_id = hashlib.sha256(json.dumps([str(pid_file), identity, created, socket_snapshot], sort_keys=True).encode()).hexdigest()
            runtime = {"id": runtime_id, "transport": "daemon", "alive": True,
                       "endpoint": "ws://localhost/", "socket_path": str(socket_path),
                       "codex": str(executable), "daemon_pid_file": str(pid_file),
                       "daemon_pid": process.pid, "daemon_created": created,
                       "daemon_identity": identity, "socket_identity": socket_snapshot}
            if require_self and not owns_current_call(runtime):
                continue
            with Rpc(runtime, timeout=3) as rpc:
                if thread_id is not None and thread_id not in rpc.loaded():
                    continue
            check_daemon(runtime)
            # Immutable discovery records bind each task to its original process.
            path = state / "runtimes" / (runtime["id"] + ".json")
            write_json(path, runtime)
            return path.resolve(), runtime
        except (OSError, ValueError, KeyError, psutil.Error, RuntimeGone):
            continue
    if sys.platform == "linux" and not any(
            (home / "app-server-daemon" / name).exists() for name in ("daemon.pid", "app-server.pid")):
        return discover_linux_server(state, socket_path, thread_id)
    return None


def locate_runtime(state, thread_id, require_self=False):
    if os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"):
        return discover_desktop(state, thread_id)
    found = discover_daemon(state, thread_id, require_self)
    if not found:
        raise TimerError("未找到已加载目标会话的 runtime。CLI 需要共享 daemon；桌面端需在应用会话内调用。不会启动或恢复目标会话。")
    return found


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
    if isinstance(rpc, DesktopRpc):
        return rpc.dispatch(thread_id, message)
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
                return "turn/steer", confirmed_turn(result, "turn/steer")
            except RpcError as exc:
                # Only a known rejected precondition is safe to re-read/retry.
                text = str(exc).lower()
                if any(s in text for s in ("no active turn", "turn id mismatch", "does not match", "expected turn")):
                    continue
                raise
        if status == "idle":
            result = rpc.call("turn/start", params, mutation=True)
            return "turn/start", confirmed_turn(result, "turn/start")
        if status == "notLoaded":
            raise RuntimeGone("目标会话已卸载")
        raise TimerError("目标会话 runtime 状态异常：" + status)
    raise TimerError("会话状态持续变化，无法确认当前任务；本次未发送")


def confirmed_turn(result, method):
    try:
        turn_id = result["turnId"] if method == "turn/steer" else result["turn"]["id"]
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("Invalid turn id")
        return turn_id
    except (KeyError, TypeError, ValueError) as exc:
        raise DeliveryUnknown("CLI 发送确认格式无法识别，不会自动重发") from exc


def detached_kwargs():
    result = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
    if os.name == "nt":
        # DETACHED_PROCESS disables CREATE_NO_WINDOW; a venv Python launcher
        # can then create a console when it starts the underlying interpreter.
        result["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        result["start_new_session"] = True
    return result


def schedule(args, state):
    config = read_json(args.config) if args.config else {}
    if not isinstance(config, dict) or set(config) - {"after", "message", "thread_id"}:
        raise TimerError("配置文件必须是 JSON 对象，仅支持 after、message 和 thread_id")
    thread_id = args.thread or config.get("thread_id") or os.environ.get("CODEX_THREAD_ID")
    after = args.after if args.after is not None else config.get("after")
    message = args.message if args.message is not None else config.get("message")
    if not thread_id:
        raise TimerError("无法识别当前会话。请在 Codex 内调用，或指定 --thread UUID")
    if not isinstance(message, str) or not message.strip():
        raise TimerError("消息必须是非空字符串")
    delay = duration(after)
    path, runtime = locate_runtime(state, thread_id,
                                   require_self=not (args.thread or config.get("thread_id")))
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


def finish(state, task_id, status, detail=None, method=None, turn_id=None, *, claimed=False):
    # An observer may finish only pending; only the successful claimant owns sending.
    with connect_db(state) as conn:
        conn.execute("""UPDATE tasks SET status=?, detail=?, method=?, turn_id=?, finished=?
            WHERE id=? AND status=?""",
            (status, detail, method, turn_id, time.time(), task_id, "sending" if claimed else "pending"))


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
        claimed = False
        try:
            runtime = runtime_record(row["runtime_path"], row["runtime_id"])
            with connect_runtime(runtime) as rpc:
                # Atomic claim also checks due to handle an edit racing the worker.
                with connect_db(state) as conn:
                    changed = conn.execute("""UPDATE tasks SET status='sending'
                        WHERE id=? AND status='pending' AND due<=?""", (task_id, time.time())).rowcount
                    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if not changed:
                    continue
                claimed = True
                runtime_record(row["runtime_path"], row["runtime_id"])
                method, turn_id = dispatch(rpc, row["thread_id"], row["message"], task_id)
                finish(state, task_id, "delivered", "runtime 已确认接收；不代表模型已完成执行", method, turn_id,
                       claimed=claimed)
        except RuntimeGone as exc:
            finish(state, task_id, "skipped", str(exc), claimed=claimed)
        except DeliveryUnknown as exc:
            finish(state, task_id, "unknown", str(exc), claimed=claimed)
        except Exception as exc:
            finish(state, task_id, "failed", str(exc), claimed=claimed)
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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Codex 会话的一次性延迟消息工具")
    parser.add_argument("--version", action="version", version="codex-timer " + VERSION)
    parser.add_argument("--state", default=str(default_state()), help="任务和 runtime 记录目录，默认 $CODEX_HOME/codex-timer")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("schedule", help="创建一次性任务，后台等待后立即返回")
    p.add_argument("--after", help="例如 75m、30s、2h")
    p.add_argument("--message")
    p.add_argument("--thread", help="默认使用当前 CODEX_THREAD_ID")
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
            if os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"):
                path, runtime = discover_desktop(state)
                with DesktopRpc(runtime) as rpc:
                    emit([{**rpc.read(runtime["caller_thread"])["thread"], "runtime": str(path)}])
                return 0
            discovered = discover_daemon(state)
            candidates = [discovered[0]] if discovered else []
            found = []
            seen = set()
            for path in candidates:
                try:
                    with Rpc(runtime_record(path)) as rpc:
                        for thread_id in sorted(rpc.loaded()):
                            if thread_id in seen:
                                continue
                            seen.add(thread_id)
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
