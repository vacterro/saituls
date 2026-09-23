"""Local OpenAI-compatible provider for real OpenCode scheduler acceptance.

No tools, credentials, remote requests or user configuration changes. Each
completion lasts --delay seconds; OpenCode still owns the complete agent loop.
"""
import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=48771)
    parser.add_argument('--delay', type=float, default=8)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *values):
            print(fmt % values, flush=True)

        def do_GET(self):
            body = json.dumps({'object': 'list', 'data': [
                {'id': 'queue-fixture', 'object': 'model', 'owned_by': 'local'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != '/v1/chat/completions':
                self.send_error(404)
                return
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            users = [m for m in data.get('messages', []) if m.get('role') == 'user']
            text = json.dumps(users[-1].get('content', '')) if users else ''
            marker = re.search(r'\bcc[1-5]\b', text)
            answer = (marker.group() if marker else 'fixture') + ' finished'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if data.get('stream') else 'application/json')
            self.end_headers()
            try:
                if data.get('stream'):
                    def chunk(delta, finish=None):
                        body = {'id': 'chatcmpl-local', 'object': 'chat.completion.chunk',
                                'created': int(time.time()), 'model': 'queue-fixture',
                                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                        self.wfile.write(('data: ' + json.dumps(body) + '\n\n').encode())
                        self.wfile.flush()
                    # A non-empty first delta makes OpenCode publish step.started
                    # before the delay; an empty delta is buffered by the runner.
                    chunk({'role': 'assistant', 'content': answer.split(' ')[0] + ' '})
                    time.sleep(args.delay)
                    chunk({'content': 'finished'})
                    chunk({}, 'stop')
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    time.sleep(args.delay)
                    self.wfile.write(json.dumps({'id': 'chatcmpl-local', 'object': 'chat.completion',
                        'created': int(time.time()), 'model': 'queue-fixture',
                        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer},
                                     'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                print('CLIENT_INTERRUPTED', flush=True)

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'LOCAL_PROVIDER_READY http://127.0.0.1:{args.port}/v1', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
