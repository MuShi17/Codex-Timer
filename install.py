"""Install the global command and its user-scoped Codex skill in one operation."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

from codex_timer import TimerError, check_skill_destination, codex_home


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def find_or_install_uv():
    found = shutil.which("uv")
    if found:
        return found
    in_venv = sys.prefix != sys.base_prefix
    command = [sys.executable, "-m", "pip", "install"]
    if not in_venv:
        command.append("--user")
    run([*command, "uv>=0.6"])
    scheme = "nt_user" if os.name == "nt" else "posix_user"
    directories = [Path(sysconfig.get_path("scripts")), Path(sysconfig.get_path("scripts", scheme=scheme))]
    for directory in directories:
        executable = directory / ("uv.exe" if os.name == "nt" else "uv")
        if executable.is_file():
            return str(executable)
    found = shutil.which("uv")
    if found:
        return found
    raise TimerError("uv 已安装但无法定位命令，请重新打开终端后重试")


def install(args):
    home = codex_home(args.codex_home)
    # Detect skill ownership conflicts before changing the installed global command.
    check_skill_destination(home, args.replace_skill)
    uv = find_or_install_uv()
    source = Path(__file__).resolve().parent
    run([uv, "tool", "install", "--force", "--python", sys.executable,
         "--no-python-downloads", str(source)])
    bin_dir = Path(run([uv, "tool", "dir", "--bin"], capture_output=True, text=True,
                       encoding="utf-8").stdout.strip())
    executable = bin_dir / ("codex-timer.exe" if os.name == "nt" else "codex-timer")
    if not executable.is_file():
        raise TimerError("全局命令未生成：" + str(executable))
    cmd = [str(executable), "install-skill", "--codex-home", str(home)]
    if args.replace_skill:
        cmd.append("--force")
    skill = json.loads(run(cmd, capture_output=True, text=True, encoding="utf-8").stdout)
    on_path = any(Path(entry).expanduser().resolve() == bin_dir.resolve()
                  for entry in os.environ.get("PATH", "").split(os.pathsep) if entry)
    if not on_path and not args.no_path_update:
        run([uv, "tool", "update-shell"])
    print(json.dumps({"status": "installed", "command": str(executable), "skill": skill,
                      "source_required_after_install": False,
                      "next": "在任意项目目录运行 codex-timer launch" if on_path else
                              "重新打开终端后，在任意项目目录运行 codex-timer launch"},
                     ensure_ascii=False, indent=2))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="全局安装 Codex Timer，并自动安装 codex-timer skill")
    parser.add_argument("--codex-home", help="默认 CODEX_HOME 或 ~/.codex")
    parser.add_argument("--replace-skill", action="store_true", help="替换同名未管理 skill，备份旧文件")
    parser.add_argument("--no-path-update", action="store_true", help="不修改 shell 的 PATH 配置")
    args = parser.parse_args()
    try:
        install(args)
        return 0
    except (TimerError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
