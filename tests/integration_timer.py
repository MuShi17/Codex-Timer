import contextlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'codex_timer.py'
SCRATCH = Path(os.environ.get('CODEX_TIMER_TEST_WORK', tempfile.gettempdir()))
SCRATCH.mkdir(exist_ok=True)
TIMER_COMMAND = os.environ.get('CODEX_TIMER_TEST_COMMAND')
if TIMER_COMMAND:
    import codex_timer as timer
else:
    spec = importlib.util.spec_from_file_location('timer', SCRIPT)
    timer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(timer)
hold = threading.Event()
hold.set()
requests_seen = []

class Provider(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        requests_seen.append(payload)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        def event(value):
            self.wfile.write(('data: '+json.dumps(value)+'\n\n').encode()); self.wfile.flush()
        response = {'id':'resp_timer_test', 'object':'response', 'created_at':int(time.time()),
                    'model':'timer-test', 'status':'in_progress', 'output':[]}
        try:
            event({'type':'response.created','response':response})
            hold.wait(timeout=30)
            if 'SELF_SCHEDULE_TIMER' in json.dumps(payload.get('input',[])) and not any(
                    i.get('type')=='function_call_output' for i in payload.get('input',[])):
                def psquote(value): return "'"+str(value).replace("'","''")+"'"
                cmd=("codex-timer" if TIMER_COMMAND else '& '+psquote(sys.executable)+' '+psquote(SCRIPT))+" schedule --after 2s --message '来自会话自身的定时消息'"
                call={'id':'fc_timer','type':'function_call','name':'exec_command',
                      'call_id':'call_timer_self','arguments':json.dumps({'cmd':cmd,'shell':'powershell','login':False,'max_output_tokens':1500})}
                event({'type':'response.output_item.added','output_index':0,'item':dict(call,arguments='')})
                event({'type':'response.function_call_arguments.delta','item_id':call['id'],'output_index':0,'delta':call['arguments']})
                event({'type':'response.output_item.done','output_index':0,'item':call})
                response.update(status='completed',output=[call],usage={'input_tokens':1,'output_tokens':1,'total_tokens':2})
                event({'type':'response.completed','response':response})
                return
            item = {'id':'msg_test','type':'message','role':'assistant','status':'completed',
                    'content':[{'type':'output_text','text':'本地测试完成','annotations':[]}]}
            event({'type':'response.output_item.added','output_index':0,'item':dict(item,status='in_progress',content=[])})
            event({'type':'response.content_part.added','item_id':item['id'],'output_index':0,'content_index':0,
                   'part':{'type':'output_text','text':'','annotations':[]}})
            event({'type':'response.output_text.delta','item_id':item['id'],'output_index':0,'content_index':0,'delta':'本地测试完成'})
            event({'type':'response.output_item.done','output_index':0,'item':item})
            response.update(status='completed', output=[item], usage={'input_tokens':1,'output_tokens':1,'total_tokens':2})
            event({'type':'response.completed','response':response})
        except (ConnectionError, OSError): pass

def wait_for(check, seconds=15):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=check()
        if result: return result
        time.sleep(.1)
    raise AssertionError('timed out')

def main():
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    report=[]
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='timer-integration-') as tmp:
        root=Path(tmp); state=root/'state'; home=root/'codex-home'; home.mkdir()
        opts=['-c','model_provider="timer_test"', '-c','model="timer-test"',
              '-c','model_providers.timer_test.name="Timer Test"',
              '-c',f'model_providers.timer_test.base_url="http://127.0.0.1:{server.server_port}/v1"',
              '-c','model_providers.timer_test.wire_api="responses"',
              '-c','model_providers.timer_test.requires_openai_auth=false',
              '-c','model_providers.timer_test.supports_websockets=false',
              '-c','features.code_mode=false']
        proc,path,runtime,env,exe=timer.start_runtime(state,extra_server_args=opts,env_overrides={'CODEX_HOME':str(home)})
        client=timer.Rpc(runtime)
        try:
            tid=client.call('thread/start',{'cwd':str(root),'model':'timer-test','approvalPolicy':'never',
                          'sandbox':'read-only'})['thread']['id']
            env['CODEX_THREAD_ID']=tid
            def command(*args):
                prefix=[TIMER_COMMAND] if TIMER_COMMAND else [sys.executable,str(SCRIPT)]
                result=subprocess.run([*prefix,'--state',str(state),*args],env=env,
                                      capture_output=True,text=True,encoding='utf-8',timeout=20)
                data=json.loads(result.stdout)
                assert result.returncode==0, (args,data,result.stderr)
                return data
            command('install-skill','--codex-home',str(home))
            skills=client.call('skills/list',{'cwds':[str(root)],'forceReload':True})['data']
            assert any(s.get('name')=='codex-timer' for group in skills for s in group.get('skills',[])),skills
            report.append('installed skill in CODEX_HOME/skills is discovered by the real Codex runtime')
            def status(task_id): return command('status',task_id)
            def finished(task_id):
                return wait_for(lambda: (lambda r:r if r['status'] not in ('pending','sending') else None)(status(task_id)))
            config_file=root/'task.json'
            config_file.write_text(json.dumps({'after':'1s','message':'空闲时发送：中文 "引号"\n第二行 $HOME `literal`'},ensure_ascii=False),encoding='utf-8')
            first=command('schedule','--config',str(config_file))
            result=finished(first['id']); print('IDLE',result,flush=True)
            assert result['status']=='delivered' and result['method']=='turn/start',result
            wait_for(lambda:client.call('thread/read',{'threadId':tid})['thread']['status']['type']=='idle')
            wanted='空闲时发送：中文 "引号"\n第二行 $HOME `literal`'
            assert wanted in json.dumps(requests_seen,ensure_ascii=False).replace('\\n','\n').replace('\\"','"'),requests_seen
            report.append('idle -> turn/start, detached worker, automatic thread ID, exact Unicode message')
            hold.clear()
            turn=client.call('turn/start',{'threadId':tid,'input':[{'type':'text','text':'保持忙碌供测试'}]})['turn']['id']
            busy=command('schedule','--after','1s','--message','忙碌时追加')
            result=finished(busy['id']); print('BUSY',result,flush=True)
            assert result['status']=='delivered' and result['method']=='turn/steer' and result['turn_id']==turn,result
            report.append('active -> turn/steer on the same turn ID')
            raced=client.call('turn/start',{'threadId':tid,'input':[{'type':'text','text':'模拟空闲检查后立刻变忙'}]})
            assert raced['turn']['id']==turn,raced
            report.append('idle-to-active race: turn/start steers the existing turn without interrupting it')
            hold.set()
            wait_for(lambda:client.call('thread/read',{'threadId':tid})['thread']['status']['type']=='idle')
            changed=command('schedule','--after','30s','--message','旧内容')
            command('update',changed['id'],'--after','1s','--message','新内容')
            assert finished(changed['id'])['status']=='delivered'
            report.append('pending task edits affect the original detached worker')
            cancel=command('schedule','--after','1s','--message','不应发送')
            command('cancel',cancel['id']); time.sleep(1.2)
            assert status(cancel['id'])['status']=='cancelled'
            report.append('cancellation prevents delivery')
            wait_for(lambda:client.call('thread/read',{'threadId':tid})['thread']['status']['type']=='idle')
            duplicates=command('schedule','--after','1s','--message','只发送一次')
            prefix=[TIMER_COMMAND] if TIMER_COMMAND else [sys.executable,str(SCRIPT)]
            duplicate_worker=subprocess.Popen([*prefix,'--state',str(state),'_worker',duplicates['id']],
                                              env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            assert finished(duplicates['id'])['status']=='delivered'
            duplicate_worker.wait(timeout=5)
            wait_for(lambda:client.call('thread/read',{'threadId':tid})['thread']['status']['type']=='idle')
            turns=client.call('thread/turns/list',{'threadId':tid,'limit':20,'itemsView':'full'})['data']
            matches=[i for t in turns for i in t.get('items',[]) if i.get('type')=='userMessage' and
                     any(c.get('text')=='只发送一次' for c in i.get('content',[]))]
            assert len(matches)==1,matches
            report.append('two competing workers deliver only one user message')
            self_thread=client.call('thread/start',{'cwd':str(root),'model':'timer-test','approvalPolicy':'never',
                                      'sandbox':'danger-full-access'})['thread']['id']
            client.call('turn/start',{'threadId':self_thread,'input':[{'type':'text','text':'SELF_SCHEDULE_TIMER'}]})
            def self_created():
                with timer.connect_db(state) as db:
                    return db.execute('SELECT * FROM tasks WHERE thread_id=?',(self_thread,)).fetchone()
            self_task=wait_for(self_created)
            self_result=finished(self_task['id']); print('SELF',self_result,flush=True)
            assert self_result['status']=='delivered',self_result
            report.append('runtime exec_command schedules itself: CODEX_THREAD_ID and runtime lookup work; worker survives tool completion')
            missing=command('schedule','--after','1s','--message','卸载后跳过')
            client.call('thread/archive',{'threadId':tid})
            result=finished(missing['id']); print('UNLOADED',result,flush=True)
            assert result['status']=='skipped',result
            report.append('unloaded thread is skipped, never resumed')
            other=client.call('thread/start',{'cwd':str(root),'model':'timer-test','approvalPolicy':'never','sandbox':'read-only'})['thread']['id']
            env['CODEX_THREAD_ID']=other
            stopped=command('schedule','--after','1s','--message','runtime 关闭后跳过')
            timer.stop_runtime(proc,path)
            assert finished(stopped['id'])['status']=='skipped'
            report.append('runtime shutdown -> skipped, no new runtime')
            # A record falsely left alive by a crash must not cause a new process to start.
            runtime['alive']=True; timer.write_json(path,runtime)
            try:
                timer.Rpc(runtime,timeout=1)
                raise AssertionError('dead endpoint should fail')
            except timer.RuntimeGone: pass
            report.append('abrupt endpoint loss is detected without starting a replacement runtime')
            assert timer.duration('75m')==4500
            output={'codex_version':subprocess.check_output([exe,'--version'],text=True).strip(),
                    'provider':'local fake Responses endpoint; no external model calls', 'passed':report,
                    'provider_requests':len(requests_seen)}
            (ROOT/'verification.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(output,ensure_ascii=False,indent=2),flush=True)
        finally:
            hold.set(); client.close()
            if proc.poll() is None: timer.stop_runtime(proc,path)
            # Give detached workers time to observe cancellation/shutdown before temp cleanup.
            time.sleep(1.2)
    server.shutdown()

if __name__=='__main__':main()
