"""Plain Codex CLI -> self-scheduling through its existing shared daemon.

Requires a complete packaged Codex CLI, plus pywinpty (Windows) or pexpect.
Inference uses the local fake Responses provider from fake_provider.py.
"""
import contextlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from fake_provider import Provider, hold, requests_seen, timer, wait_for, ROOT, SCRIPT


def main():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    executable = os.environ.get('CODEX_TIMER_TEST_CODEX') or shutil.which('codex')
    if not executable:
        raise RuntimeError('A packaged Codex CLI must be installed.')
    cli = client = None
    # The Windows control socket path must fit sockaddr_un.sun_path (108 bytes).
    with tempfile.TemporaryDirectory(prefix='ct-') as temporary:
        root = Path(temporary)
        home = root / 'h'; home.mkdir()
        state = home / 'codex-timer'
        env = os.environ.copy()
        for key in ('CODEX_TIMER_RUNTIME', 'CODEX_TIMER_STATE', 'CODEX_THREAD_ID',
                    'CODEX_SESSION_ID', 'CODEX_EXEC_SERVER_URL', 'CODEX_CLI_PATH'):
            env.pop(key, None)
        env.update(CODEX_HOME=str(home), TERM='xterm-256color', PYTHONUTF8='1')
        package = os.environ.get('CODEX_TIMER_TEST_PACKAGE_ROOT')
        if package:
            env.update(CODEX_MANAGED_PACKAGE_ROOT=package, CODEX_MANAGED_BY_NPM='1')
        config = ('model_provider = "timer_test"\nmodel = "timer-test"\n'
                  'sandbox_mode = "danger-full-access"\napproval_policy = "never"\n'
                  '[features]\ncode_mode = false\n'
                  '[model_providers.timer_test]\nname = "Timer Test"\n'
                  f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                  'wire_api = "responses"\nrequires_openai_auth = false\n'
                  'supports_websockets = false\n'
                  f'[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n')
        (home / 'config.toml').write_text(config, encoding='utf-8')
        prefix = [os.environ['CODEX_TIMER_TEST_COMMAND']] if os.environ.get('CODEX_TIMER_TEST_COMMAND') else [sys.executable, str(SCRIPT)]
        subprocess.run([*prefix, 'install-skill', '--codex-home', str(home)], env=env,
                       capture_output=True, check=True, timeout=20)
        output = []
        argv = [executable, '--no-alt-screen', '--dangerously-bypass-approvals-and-sandbox',
                'SELF_SCHEDULE_TIMER']
        if os.name == 'nt':
            from winpty import PtyProcess
            cli = PtyProcess.spawn(subprocess.list2cmdline(argv), cwd=str(root), env=env,
                                   dimensions=(40, 120))
        else:
            import pexpect
            cli = pexpect.spawn(executable, argv[1:], cwd=str(root), env=env, encoding='utf-8')
        def drain():
            try:
                while cli.isalive():
                    chunk = cli.read(4096)
                    if chunk:
                        output.append(chunk)
            except (EOFError, OSError):
                pass
        threading.Thread(target=drain, daemon=True).start()
        def command(*args, expected=0):
            if args and args[0] == 'schedule' and '--thread' not in args:
                # This runs in the external driver; CLI self-scheduling uses
                # its real exec_command above, without an explicit thread ID.
                args = (args[0], '--thread', env['CODEX_THREAD_ID'], *args[1:])
            result = subprocess.run([*prefix, *args], env=env, cwd=root, capture_output=True,
                                    text=True, encoding='utf-8', timeout=20)
            assert result.returncode == expected, (args, result.stdout, result.stderr)
            return json.loads(result.stdout)
        def first_task():
            if not (state / 'tasks.sqlite3').exists():
                return None
            with timer.connect_db(state) as db:
                row = db.execute('SELECT * FROM tasks ORDER BY created LIMIT 1').fetchone()
                return dict(row) if row else None
        def finished(task_id):
            return wait_for(lambda: (lambda r:r if r['status'] not in ('pending', 'sending') else None)(command('status', task_id)))
        try:
            try:
                task = wait_for(first_task, seconds=45)
            except AssertionError:
                raise AssertionError('Plain CLI did not self-schedule. Terminal output:\n' + ''.join(output)[-12000:])
            result = finished(task['id'])
            assert result['status'] == 'delivered', result
            record = timer.runtime_record(task['runtime_path'])
            assert record['transport'] == 'daemon', record
            client = timer.Rpc(record)
            tid = task['thread_id']; env['CODEX_THREAD_ID'] = tid
            skills = client.call('skills/list', {'cwds':[str(root)], 'forceReload':True})['data']
            assert any(s.get('name') == 'codex-timer' for group in skills for s in group.get('skills', [])), skills
            wait_for(lambda: client.call('thread/read', {'threadId':tid})['thread']['status']['type'] == 'idle')
            assert not env.get('CODEX_TIMER_RUNTIME') and not env.get('CODEX_TIMER_STATE')
            foreign = subprocess.run([*prefix, 'schedule', '--after', '1s', '--message', '不应投递'], env=env,
                                     cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=20)
            assert foreign.returncode == 1, foreign.stdout
            listed = command('threads')
            assert len([t for t in listed if t['id'] == tid]) == 1, listed
            # Existing CLI is idle: discover automatically, do not start another runtime.
            wanted = '普通 CLI 空闲后唤醒：中文 "引号"\n第二行 $HOME `literal`'
            config_file = root / 'task.json'
            config_file.write_text(json.dumps({'after':'1s', 'message':wanted}, ensure_ascii=False), encoding='utf-8')
            idle = command('schedule', '--config', str(config_file))
            idle_result = finished(idle['id'])
            assert idle_result['status'] == 'delivered' and idle_result['method'] == 'turn/start', idle_result
            wait_for(lambda: wanted in json.dumps(requests_seen, ensure_ascii=False).replace('\\n','\n').replace('\\"','"'))
            wait_for(lambda: client.call('thread/read', {'threadId':tid})['thread']['status']['type'] == 'idle')
            hold.clear()
            turn = client.call('turn/start', {'threadId':tid, 'input':[{'type':'text','text':'保持忙碌'}]})['turn']['id']
            wait_for(lambda: client.call('thread/read', {'threadId':tid})['thread']['status']['type'] == 'active')
            busy = command('schedule', '--after', '1s', '--message', '普通 CLI 忙碌时追加')
            result = finished(busy['id'])
            assert result['method'] == 'turn/steer' and result['turn_id'] == turn, result
            raced = client.call('turn/start', {'threadId':tid, 'input':[{'type':'text','text':'空闲检查后变忙的竞争'}]})
            assert raced['turn']['id'] == turn, raced
            hold.set()
            wait_for(lambda: client.call('thread/read', {'threadId':tid})['thread']['status']['type'] == 'idle')
            changed = command('schedule', '--after', '30s', '--message', '旧内容')
            command('update', changed['id'], '--after', '1s', '--message', '修改后的内容')
            assert finished(changed['id'])['status'] == 'delivered'
            wait_for(lambda: '修改后的内容' in json.dumps(requests_seen, ensure_ascii=False))
            wait_for(lambda: client.call('thread/read', {'threadId':tid})['thread']['status']['type'] == 'idle')
            cancelled = command('schedule', '--after', '1s', '--message', '取消后不发送')
            command('cancel', cancelled['id']); time.sleep(1.2)
            assert command('status', cancelled['id'])['status'] == 'cancelled'
            duplicate = command('schedule', '--after', '2s', '--message', '重复 worker 只发一次')
            other_worker = subprocess.Popen([*prefix, '_worker', duplicate['id']], env=env,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert finished(duplicate['id'])['status'] == 'delivered'
            other_worker.wait(timeout=8)
            wait_for(lambda: client.call('thread/read', {'threadId':tid})['thread']['status']['type'] == 'idle')
            turns = client.call('thread/turns/list', {'threadId':tid, 'limit':30, 'itemsView':'full'})['data']
            matches = [i for t in turns for i in t.get('items', []) if i.get('type') == 'userMessage' and
                       any(c.get('text') == '重复 worker 只发一次' for c in i.get('content', []))]
            assert len(matches) == 1, matches
            gone = client.call('thread/start', {'cwd':str(root), 'model':'timer-test', 'approvalPolicy':'never',
                                               'sandbox':'read-only'})['thread']['id']
            archived = command('schedule', '--thread', gone, '--after', '1s', '--message', '会话卸载后不发送')
            client.call('thread/archive', {'threadId':gone})
            assert finished(archived['id'])['status'] == 'skipped'
            assert timer.duration('75m') == 4500
            old = command('schedule', '--after', '30s', '--message', '后台服务关闭后不能发送')
            client.close(); client = None
            cli.write('/exit\r'); time.sleep(.3); cli.write('\r')
            wait_for(lambda: not cli.isalive(), seconds=10)
            # The TUI may detach while its shared runtime continues. Explicitly stop it.
            stopped = subprocess.run([executable, 'app-server', 'daemon', 'stop'], env=env,
                                     capture_output=True, text=True, encoding='utf-8', timeout=30)
            assert stopped.returncode == 0, (stopped.stdout, stopped.stderr)
            assert finished(old['id'])['status'] == 'skipped'
            # A replacement server must not receive timers bound to the old process.
            started = subprocess.run([executable, 'app-server', 'daemon', 'start'], env=env,
                                     capture_output=True, text=True, encoding='utf-8', timeout=30)
            assert started.returncode == 0, (started.stdout, started.stderr)
            with contextlib.suppress(timer.RuntimeGone):
                timer.runtime_record(task['runtime_path'])
                raise AssertionError('old runtime identity survived replacement')
            assert command('status', old['id'])['status'] == 'skipped'
            report = {'codex_version': subprocess.check_output([executable, '--version'], env=env, text=True).strip(),
                      'provider': 'local fake Responses endpoint; no external model calls',
                      'passed': ['plain CLI startup with no timer-specific runtime environment variables',
                                 'installed skill is discovered by the real shared runtime',
                                 'real CLI exec_command schedules its own CODEX_THREAD_ID',
                                 'existing shared daemon discovered from another command invocation',
                                 'inherited thread ID cannot bind self-scheduling to a different runtime process',
                                 'idle -> new turn and active -> steer on the same turn ID',
                                 'exact Unicode, quotes and multiline input from JSON configuration',
                                 'idle-to-active race keeps the existing turn',
                                 'pending task update and cancellation',
                                 'two competing workers deliver exactly one user message',
                                 'unloaded session is skipped without resume',
                                 'shared runtime closed -> skipped; replacement cannot receive old timers']}
            path = ROOT / 'verification.json'
            verification = {'version':timer.VERSION, 'normal_cli':report}
            path.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        finally:
            hold.set()
            if client:
                client.close()
            if cli and cli.isalive():
                cli.terminate(force=True)
            if cli:
                cli.close(force=True)
            subprocess.run([executable, 'app-server', 'daemon', 'stop'], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            # Codex stop leaves its update loop alive. Only reap test-owned
            # package processes under this verified temporary CODEX_HOME.
            import psutil
            owned = []
            for process in psutil.process_iter(['exe']):
                with contextlib.suppress(psutil.Error, OSError, ValueError):
                    if not process.info['exe']:
                        continue
                    candidate = Path(process.info['exe']).resolve()
                    if home.resolve() in candidate.parents:
                        process.terminate()
                        owned.append(process)
            _, alive = psutil.wait_procs(owned, timeout=5)
            for process in alive:
                process.kill()
            psutil.wait_procs(alive, timeout=5)
            time.sleep(1.2)
    server.shutdown()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
