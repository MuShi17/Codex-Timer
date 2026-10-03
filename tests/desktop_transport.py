"""Exercise desktop framing, guarded delivery and workers against a local fixture."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[1]
if not os.environ.get('CODEX_TIMER_TEST_INSTALLED'):
    sys.path.insert(0, str(ROOT))
import codex_timer as timer
SCRIPT = Path(timer.__file__).resolve()


def main():
    node = os.environ.get('CODEX_MCP_NODE_PATH') or shutil.which('node')
    assert node, 'Node.js is required for this isolated desktop transport test'
    with tempfile.TemporaryDirectory(prefix='timer-desktop-') as temporary:
        root = Path(temporary)
        pipe = '\\\\.\\pipe\\timer-test-' + uuid.uuid4().hex if os.name == 'nt' else str(root / 'tools.sock')
        control, log = root / 'control.json', root / 'calls.jsonl'
        state = root / 'state'
        def settings(**values):
            timer.write_json(control, {'status':'idle','turnId':'busy-turn',**values})
        def calls():
            return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []
        settings()
        server = subprocess.Popen([node, str(ROOT / 'tests' / 'desktop_server.mjs'), pipe, str(control), str(log)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            assert server.stdout.readline().strip() == 'ready'
            def record():
                value = {'id':uuid.uuid4().hex, 'transport':'desktop','alive':True,'pipe':pipe,
                         'owner_pid':server.pid,'node':node,'caller_thread':'desktop-self',
                         'processes':[timer.process_snapshot(psutil.Process(server.pid))],
                         'pipe_identity':None if os.name=='nt' else timer.socket_identity(pipe)}
                path = state / 'runtimes' / (value['id']+'.json')
                timer.write_json(path, value)
                return path, value
            path, runtime = record()
            rpc = timer.DesktopRpc(runtime, timeout=0.5)
            assert rpc.read('desktop-self')['thread']['status']['type']=='idle'
            wanted = '中文 "引号"\n第二行 $HOME `literal`'
            assert rpc.dispatch('desktop-self', wanted) == ('send_message_to_thread','new-turn')
            assert calls()[-1]['prompt'] == wanted
            settings(status='active')
            assert rpc.dispatch('desktop-self','busy') == ('send_message_to_thread','busy-turn')
            assert calls()[-1]['before']=='active'
            for status in ('notLoaded', 'systemError', 'unknown'):
                before=len(calls()); settings(status=status)
                try: rpc.dispatch('desktop-self','must not resume')
                except timer.RuntimeGone: pass
                else: raise AssertionError(status)
                assert len(calls())==before
            settings(host='remote')
            try: rpc.read('desktop-self')
            except timer.TimerError: pass
            else: raise AssertionError('Remote thread accepted')
            for mode in ('disconnect','timeout','rpcError','toolError','malformed','noResult'):
                before=len(calls()); settings(**{mode:True})
                try: rpc.dispatch('desktop-self','ambiguous-'+mode)
                except timer.DeliveryUnknown: pass
                else: raise AssertionError(mode)
                assert len(calls())==before+1
            for acknowledgement in (None, False, {}, {'delivered':False}, {'threadId':'wrong'},
                                    {'threadId':'desktop-self','turnId':False},
                                    {'threadId':'desktop-self','accepted':False}):
                before=len(calls()); settings(ack=acknowledgement)
                try: rpc.dispatch('desktop-self','unrecognized-ack')
                except timer.DeliveryUnknown: pass
                else: raise AssertionError(acknowledgement)
                assert len(calls())==before+1
            # Real app acknowledgements can omit turnId; target identity remains required.
            settings(ack={'threadId':'desktop-self'})
            assert rpc.dispatch('desktop-self','idle-no-turn-id') == ('send_message_to_thread',None)
            settings(status='active',ack={'threadId':'desktop-self'})
            assert rpc.dispatch('desktop-self','busy-no-turn-id') == ('send_message_to_thread','busy-turn')
            # A failed desktop discovery must never fall through to a CLI daemon.
            with patch.dict(os.environ, {'CODEX_APP_TOOLS_PIPE_PATH':pipe}), \
                 patch.object(timer,'discover_desktop',side_effect=timer.TimerError('desktop unavailable')), \
                 patch.object(timer,'discover_daemon') as daemon:
                try: timer.locate_runtime(state,'desktop-self',require_self=True)
                except timer.TimerError: pass
                else: raise AssertionError('Desktop failure accepted')
                daemon.assert_not_called()
            settings()
            def task(message, delay=0):
                task_id=uuid.uuid4().hex; now=time.time()
                with timer.connect_db(state) as db:
                    db.execute('INSERT INTO tasks (id,thread_id,runtime_path,runtime_id,message,due,created,status) '
                               'VALUES (?,?,?,?,?,?,?,?)', (task_id,'desktop-self',str(path),runtime['id'],message,now+delay,now,'pending'))
                return task_id
            def row(task_id):
                with timer.connect_db(state) as db:
                    return dict(db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
            duplicate=task('competing workers')
            workers=[subprocess.Popen([sys.executable,str(SCRIPT),'--state',str(state),'_worker',duplicate],
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
            for worker in workers: assert worker.wait(timeout=15)==0
            assert row(duplicate)['status']=='delivered'
            assert len([c for c in calls() if c['prompt']=='competing workers'])==1
            settings(status='notLoaded'); unloaded=task('unloaded')
            timer.worker(state,unloaded); assert row(unloaded)['status']=='skipped'
            assert not any(c['prompt']=='unloaded' for c in calls())
            settings(disconnect=True); ambiguous=task('ambiguous-worker')
            timer.worker(state,ambiguous); assert row(ambiguous)['status']=='unknown'
            timer.worker(state,ambiguous)
            assert len([c for c in calls() if c['prompt']=='ambiguous-worker'])==1
            settings(ack=None); invalid_ack=task('invalid-ack-worker')
            timer.worker(state,invalid_ack); assert row(invalid_ack)['status']=='unknown'
            timer.worker(state,invalid_ack)
            assert len([c for c in calls() if c['prompt']=='invalid-ack-worker'])==1
            settings(); closed=task('closed-runtime')
            server.terminate(); server.wait(timeout=5)
            timer.worker(state,closed); assert row(closed)['status']=='skipped'
            # Reusing the same endpoint with a new process must not revive old timers.
            if os.name!='nt':
                with contextlib.suppress(FileNotFoundError): Path(pipe).unlink()
            server=subprocess.Popen([node,str(ROOT/'tests'/'desktop_server.mjs'),pipe,str(control),str(log)],
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            assert server.stdout.readline().strip()=='ready'
            try: timer.DesktopRpc(runtime)
            except timer.RuntimeGone: pass
            else: raise AssertionError('Replacement runtime accepted')
            forged=dict(runtime,processes=[timer.process_snapshot(psutil.Process(server.pid))])
            try: timer.DesktopRpc(forged)
            except timer.RuntimeGone: pass
            else: raise AssertionError('Replacement channel owner accepted')
            print(json.dumps({'passed':['fragmented native pipe framing','exact Unicode/multiline input',
                  'idle and busy dispatch (fixture)','unloaded/remote target rejected without sending',
                  'disconnect/timeout/generic errors -> unknown, no retry','competing workers deliver once',
                  'original runtime close and endpoint replacement -> skipped']},ensure_ascii=False,indent=2))
        finally:
            if server.poll() is None:
                server.terminate(); server.wait(timeout=5)


if __name__=='__main__':
    main()
