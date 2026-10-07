"""Regressions for Linux app-server discovery without daemon PID files."""
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_timer as timer


@unittest.skipUnless(sys.platform == "linux", "Linux socket ownership fallback")
class LinuxRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="timer-linux-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        target = self.home / "server.sock"
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(target))
        self.addCleanup(self.socket.close)
        self.path = self.home / "app-server-control" / "app-server-control.sock"
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        self.server = MagicMock(pid=123)
        self.server.exe.return_value = "/usr/bin/codex"
        self.server.cmdline.return_value = ["codex", "app-server", "--listen", "unix://"]
        self.server.create_time.return_value = 100.0
        self.server.is_running.return_value = True
        self.server.status.return_value = "running"
        self.server.net_connections.return_value = [SimpleNamespace(laddr=str(target))]
        self.caller = MagicMock()
        self.caller.parents.return_value = [self.server]
        self.rpc = MagicMock()
        self.rpc.__enter__.return_value.loaded.return_value = {"target"}
        for context in (
                patch.object(timer, "codex_home", return_value=self.home),
                patch("psutil.Process", side_effect=lambda pid=None: self.caller if pid is None else self.server),
                patch.object(timer, "Rpc", return_value=self.rpc)):
            context.start()
            self.addCleanup(context.stop)

    def discover(self):
        return timer.discover_daemon(self.home / "state", "target", require_self=True)

    def test_symlink_socket_and_loaded_thread_are_discovered(self):
        path, runtime = self.discover()
        self.assertEqual(timer.runtime_record(path), runtime)
        self.assertTrue(timer.owns_current_call(runtime))
        self.assertNotIn("daemon_pid_file", runtime)

    def test_unrelated_process_or_socket_is_rejected(self):
        self.server.exe.return_value = "/usr/bin/python"
        self.assertIsNone(self.discover())
        self.server.exe.return_value = "/usr/bin/codex"
        self.server.net_connections.return_value = []
        self.assertIsNone(self.discover())
        self.rpc.assert_not_called()

    def test_unloaded_thread_is_rejected(self):
        self.rpc.__enter__.return_value.loaded.return_value = {"other"}
        self.assertIsNone(self.discover())

    def test_replaced_process_or_lost_socket_is_rejected(self):
        _, runtime = self.discover()
        self.server.create_time.return_value = 101.0
        with self.assertRaises(timer.RuntimeGone):
            timer.check_daemon(runtime)
        self.server.create_time.return_value = 100.0
        self.server.net_connections.return_value = []
        with self.assertRaises(timer.RuntimeGone):
            timer.check_daemon(runtime)

    def test_retargeted_socket_is_rejected(self):
        _, runtime = self.discover()
        replacement = self.home / "replacement.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(replacement))
            self.path.unlink()
            self.path.symlink_to(replacement)
            self.server.net_connections.return_value = [SimpleNamespace(laddr=str(replacement))]
            with self.assertRaises(timer.RuntimeGone):
                timer.check_daemon(runtime)

    def test_windows_and_existing_pid_record_do_not_use_fallback(self):
        with patch.object(timer.sys, "platform", "win32"):
            self.assertIsNone(self.discover())
        pid_file = self.home / "app-server-daemon" / "daemon.pid"
        pid_file.parent.mkdir()
        pid_file.write_text(json.dumps({"pid": 123}))
        _, runtime = self.discover()
        self.assertEqual(runtime["daemon_pid_file"], str(pid_file))
        self.assertNotIn("daemon_process", runtime)


if __name__ == "__main__":
    unittest.main()
