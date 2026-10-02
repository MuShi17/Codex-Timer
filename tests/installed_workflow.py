"""Validate a non-editable global install in isolated user/tool directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    scratch = Path(os.environ.get('CODEX_TIMER_TEST_WORK', tempfile.gettempdir()))
    scratch.mkdir(parents=True, exist_ok=True)
    if not shutil.which('uv'):
        raise RuntimeError('This isolated installation test needs uv already installed.')
    with tempfile.TemporaryDirectory(dir=scratch, prefix='timer-install-') as temporary:
        root = Path(temporary)
        assert root.resolve().parent == scratch.resolve()
        clone = root / 'clone'
        shutil.copytree(ROOT, clone, ignore=shutil.ignore_patterns(
            '.git', '.state', '.test-work', '__pycache__', '*.egg-info', 'build', 'dist'))
        home = root / 'codex-home'
        bin_dir = root / 'bin'
        project = root / 'unrelated-project'
        project.mkdir()
        env = os.environ.copy()
        env.update({'UV_TOOL_DIR': str(root / 'tools'), 'UV_TOOL_BIN_DIR': str(bin_dir),
                    'UV_CACHE_DIR': str(root / 'cache'), 'CODEX_HOME': str(home),
                    'CODEX_TIMER_TEST_WORK': str(root / 'scratch'),
                    'PYTHONUTF8': '1', 'PATH': str(bin_dir) + os.pathsep + env.get('PATH', '')})
        env.pop('CODEX_TIMER_STATE', None)
        env.pop('CODEX_TIMER_RUNTIME', None)
        # Windows resolves the executable using the parent PATH before applying
        # the child environment. Keep the test process's PATH isolated too.
        os.environ['PATH'] = env['PATH']
        def run(argv, expected=0, **kwargs):
            result = subprocess.run(argv, env=env, cwd=project, capture_output=True,
                                    text=True, encoding='utf-8', timeout=180, **kwargs)
            if result.returncode != expected:
                raise AssertionError((argv, result.returncode, result.stdout, result.stderr))
            return result
        def command(*args, expected=0):
            return run(['codex-timer', *args], expected=expected)
        installed = run([sys.executable, str(clone / 'install.py'), '--no-path-update'])
        print(installed.stdout, flush=True)
        assert (home / 'skills' / 'codex-timer' / 'SKILL.md').is_file()
        assert command('--version').stdout.strip() == 'codex-timer 0.2.0'
        doctor = json.loads(command('doctor').stdout)
        assert Path(doctor['state']) == home / 'codex-timer', doctor
        assert doctor['skill_installed'] and doctor['command'], doctor
        # Source moved after installation: both code and bundled skill must live in the wheel.
        moved = root / 'moved-clone'
        assert clone.resolve().parent == root.resolve() and moved.resolve().parent == root.resolve()
        clone.rename(moved)
        assert command('--version').stdout.strip() == 'codex-timer 0.2.0'
        assert json.loads(command('install-skill').stdout)['status'] == 'installed'
        skill_path = home / 'skills' / 'codex-timer' / 'SKILL.md'
        original = skill_path.read_bytes()
        custom = original + b'\nUser custom instruction\n'
        skill_path.write_bytes(custom)
        upgraded = json.loads(command('install-skill').stdout)
        assert skill_path.read_bytes() == original
        assert (Path(upgraded['backup']) / 'SKILL.md').read_bytes() == custom
        skill_path.write_bytes(custom)
        removed = json.loads(command('uninstall-skill').stdout)
        assert 'SKILL.md' in removed['preserved_modified_files'] and skill_path.read_bytes() == custom
        # Unmanaged same-name skill is preserved unless replacement is requested explicitly.
        conflict_home = root / 'conflicting-home'
        conflict = conflict_home / 'skills' / 'codex-timer' / 'SKILL.md'
        conflict.parent.mkdir(parents=True)
        conflict.write_text('User-owned skill', encoding='utf-8')
        command('install-skill', '--codex-home', str(conflict_home), expected=1)
        assert conflict.read_text(encoding='utf-8') == 'User-owned skill'
        forced = json.loads(command('install-skill', '--codex-home', str(conflict_home), '--force').stdout)
        assert (Path(forced['backup']) / 'SKILL.md').read_text(encoding='utf-8') == 'User-owned skill'
        command('uninstall-skill', '--codex-home', str(conflict_home))
        assert not conflict.exists()
        # Restore this test user's managed skill before the runtime integration pass.
        command('install-skill', '--force')
        env['CODEX_TIMER_TEST_COMMAND'] = str(bin_dir / ('codex-timer.exe' if os.name == 'nt' else 'codex-timer'))
        runtime = run([doctor['python'], str(ROOT / 'tests' / 'integration_timer.py')])
        print(runtime.stdout, flush=True)
        verification = json.loads((ROOT / 'verification.json').read_text(encoding='utf-8'))
        verification['global_install'] = {
            'version': '0.2.0', 'isolated_tool_and_codex_home': True,
            'passed': [
                'one installation command installs the global CLI and skill',
                'bare codex-timer command works from an unrelated project directory',
                'installed CLI and skill resources work after moving the cloned source',
                'default state belongs to the user, independent of source and working directory',
                'managed upgrade backs up user edits',
                'unmanaged same-name skill is preserved unless explicitly replaced',
                'uninstall preserves edited skill files',
                'installed CLI is used for scheduling, updates, cancellation and runtime self-invocation',
            ],
            'not_tested': ['automatic uv bootstrap on a machine without uv', 'persistent shell PATH update',
                           'Linux/macOS execution', 'external real-model or 75-minute soak test'],
        }
        (ROOT / 'verification.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding='utf-8')
        subprocess.run([shutil.which('uv'), 'tool', 'uninstall', 'codex-timer'], env=env,
                       check=True, capture_output=True, timeout=30)
        print('Global installation and installed runtime workflow passed.', flush=True)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
