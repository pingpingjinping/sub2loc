import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from sub2loc.connection import test_translation as probe
from test_identified import config


class ProbeFormatTests(unittest.TestCase):
    def test_real_http_probe_all_adapters_and_export_choices_without_mutating_settings(self):
        calls=[]
        class Server(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));calls.append(data)
                if self.path.endswith('/completions') and not self.path.endswith('/chat/completions'):
                    choice=dict(text='좋은 날씨네요.',finish_reason='stop')
                else:
                    payload=json.loads(data['messages'][-1]['content'])
                    rows=[dict(id=r['id'],text='좋은 날씨네요.') for r in payload['translate']]
                    text='\n'.join(json.dumps(r,ensure_ascii=False) for r in rows) if isinstance(rows[0]['id'],str) else json.dumps(dict(translations=rows,context='메모'))
                    choice=dict(message=dict(content=text),finish_reason='stop')
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(dict(choices=[choice])).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Server)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            for mode in ('identified','chat','ja-ko-vn'):
                for fmt in ('ass','srt','smi'):
                    with self.subTest(mode=mode,format=fmt):
                        cfg=config(model='mock',api_mode=mode,url=f'http://127.0.0.1:{server.server_port}/v1',stream=False,output_format=fmt)
                        before=copy.deepcopy(cfg)
                        self.assertEqual(probe(cfg)['translation'],'좋은 날씨네요.')
                        self.assertEqual(cfg,before)
            self.assertEqual(len(calls),9)
        finally:server.shutdown();server.server_close()
