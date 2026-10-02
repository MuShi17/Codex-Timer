import http.server
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'codex_timer.py'
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
                if os.name == 'nt':
                    def psquote(value): return "'"+str(value).replace("'","''")+"'"
                    cmd="codex-timer" if TIMER_COMMAND else '& '+psquote(sys.executable)+' '+psquote(SCRIPT)
                    shell='powershell'
                else:
                    cmd="codex-timer" if TIMER_COMMAND else shlex.quote(sys.executable)+' '+shlex.quote(str(SCRIPT))
                    shell='/bin/bash'
                cmd += " schedule --after 2s --message '来自会话自身的定时消息'"
                call={'id':'fc_timer','type':'function_call','name':'exec_command',
                      'call_id':'call_timer_self','arguments':json.dumps({'cmd':cmd,'shell':shell,'login':False,'max_output_tokens':1500})}
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
