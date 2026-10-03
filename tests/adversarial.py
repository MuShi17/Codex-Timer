"""Isolated regressions for ownership, uncertain acknowledgements and skill recovery."""
import contextlib
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

if not os.environ.get('CODEX_TIMER_TEST_INSTALLED'):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_timer as timer


class Regressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='timer-regression-')
        self.state = Path(self.temporary.name)

    def tearDown(self):
        gc.collect()
        self.temporary.cleanup()

    def task(self):
        with contextlib.closing(timer.connect_db(self.state)) as db, db:
            db.execute('INSERT INTO tasks (id,thread_id,runtime_path,runtime_id,message,due,created,status) '
                       'VALUES (?,?,?,?,?,?,?,?)',
                       ('task','target','unused','runtime','exact',time.time()-1,time.time(),'pending'))

    def row(self):
        with contextlib.closing(timer.connect_db(self.state)) as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', ('task',)).fetchone())

    def test_failure_without_claim_cannot_overwrite_sender(self):
        for failure in ('initial-runtime-check', 'connect'):
            with self.subTest(failure=failure):
                with contextlib.closing(timer.connect_db(self.state)) as db, db:
                    db.execute('DELETE FROM tasks')
                self.task()
                both_read = threading.Barrier(2)
                sending, loser_done = threading.Event(), threading.Event()
                counts, accepted, errors = {}, [], []

                def runtime(*args):
                    name = threading.current_thread().name
                    counts[name] = counts.get(name, 0) + 1
                    if counts[name] == 1:
                        both_read.wait(timeout=5)
                        if name == 'loser' and failure == 'initial-runtime-check':
                            assert sending.wait(5)
                            raise timer.RuntimeGone('closed while another sender awaits ack')
                    return {}

                @contextlib.contextmanager
                def connect(*args):
                    if threading.current_thread().name == 'loser':
                        assert sending.wait(5)
                        raise timer.TimerError('connection failed before claim')
                    yield object()

                def dispatch(*args):
                    accepted.append('exact')
                    sending.set()
                    assert loser_done.wait(5)
                    return 'fake-send', 'turn'

                def run():
                    try:
                        timer.worker(self.state, 'task')
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        if threading.current_thread().name == 'loser':
                            loser_done.set()

                with patch.object(timer, 'runtime_record', side_effect=runtime), \
                     patch.object(timer, 'connect_runtime', side_effect=connect), \
                     patch.object(timer, 'dispatch', side_effect=dispatch):
                    threads = [threading.Thread(target=run, name=name) for name in ('owner','loser')]
                    for thread in threads: thread.start()
                    for thread in threads: thread.join(10)
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(accepted, ['exact'])
                self.assertEqual(self.row()['status'], 'delivered')

    def test_cli_malformed_ack_is_unknown_and_never_retried(self):
        for status in ('idle', 'active'):
            for ack in ({}, None, False, {'turn':{}}, {'turn':{'id':False}}, {'turnId':0}):
                with self.subTest(status=status, ack=ack):
                    with contextlib.closing(timer.connect_db(self.state)) as db, db:
                        db.execute('DELETE FROM tasks')
                    self.task()
                    accepted = []

                    class Rpc:
                        def __enter__(self): return self
                        def __exit__(self, *args): pass
                        def loaded(self): return {'target'}
                        def call(self, method, params=None, mutation=False):
                            if method == 'thread/read': return {'thread':{'status':{'type':status}}}
                            if method == 'thread/turns/list': return {'data':[{'id':'turn','status':'inProgress'}]}
                            accepted.append(params['input'][0]['text'])
                            return ack

                    with patch.object(timer, 'runtime_record', return_value={}), \
                         patch.object(timer, 'connect_runtime', return_value=Rpc()):
                        timer.worker(self.state,'task')
                        timer.worker(self.state,'task')
                    self.assertEqual(accepted, ['exact'])
                    self.assertEqual(self.row()['status'], 'unknown')

    def test_desktop_malformed_content_is_unknown(self):
        rpc = timer.DesktopRpc.__new__(timer.DesktopRpc)
        rpc.runtime = {'caller_thread':'target'}
        for content in (None, {}, False, 'text'):
            with self.subTest(content=content), patch.object(rpc, 'native', return_value={'success':True,'contentItems':content}):
                with self.assertRaises(timer.DeliveryUnknown):
                    rpc.tool('send_message_to_thread', {}, mutation=True)

    def test_corrupt_manifest_requires_force_and_preserves_edits(self):
        for broken in ('[1]', '{', '{"owner":"codex-timer","files":null}',
                       '{"owner":"codex-timer","files":{"SKILL.md":12}}'):
            with self.subTest(broken=broken):
                home = self.state / 'home'
                target = home / 'skills' / 'codex-timer'
                target.mkdir(parents=True, exist_ok=True)
                skill = target / 'SKILL.md'
                skill.write_text('user edits', encoding='utf-8')
                (target / '.codex-timer-managed.json').write_text(broken, encoding='utf-8')
                with self.assertRaises(timer.TimerError): timer.install_skill(home)
                self.assertEqual(skill.read_text(encoding='utf-8'), 'user edits')
                result = timer.install_skill(home, force=True)
                self.assertEqual((Path(result['backup']) / 'SKILL.md').read_text(encoding='utf-8'), 'user edits')
                self.assertEqual(timer.read_skill_manifest(target / '.codex-timer-managed.json')['owner'], 'codex-timer')

    def test_optional_read_failure_does_not_invalidate_confirmed_send(self):
        rpc = timer.DesktopRpc.__new__(timer.DesktopRpc)
        rpc.runtime = {'caller_thread':'target'}
        before = {'thread':{'id':'target','hostId':'local','kind':'codex','status':{'type':'active'}},
                  'turns':[{'id':'turn','status':'inProgress'}]}
        after = {'thread':{'id':'target','hostId':'local','kind':'codex','status':None}}
        responses = [{'success':True,'contentItems':[{'type':'inputText','text':json.dumps(value)}]}
                     for value in (before, {'threadId':'target'}, after)]
        with patch.object(timer, 'check_runtime'), patch.object(rpc, 'native', side_effect=responses):
            self.assertEqual(rpc.dispatch('target','exact'), ('send_message_to_thread',None))


if __name__ == '__main__':
    unittest.main()
