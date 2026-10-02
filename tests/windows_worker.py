"""Verify the installed Windows worker launcher has no console window."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import codex_timer

if os.name != "nt":
    raise SystemExit("Windows only")
with tempfile.TemporaryDirectory() as directory:
    result = Path(directory) / "console.json"
    probe = (
        "import ctypes,json; from pathlib import Path; "
        "Path(" + repr(str(result)) + ").write_text(json.dumps({"
        "'console_window':ctypes.windll.kernel32.GetConsoleWindow()}))"
    )
    process = subprocess.Popen([sys.executable, "-c", probe],
                               **codex_timer.detached_kwargs())
    assert process.wait(timeout=20) == 0
    data = json.loads(result.read_text())
    # CREATE_NO_WINDOW may attach processes to a headless console; only the
    # window handle must be absent, not the console process list.
    assert data["console_window"] == 0, data
    print(json.dumps({"passed": "installed worker has no console window"}))
