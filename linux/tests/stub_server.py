"""Stand-in for the user's servers on 127.0.0.1: Ollama /api/chat (NDJSON stream), /api/generate (loading
the model), /api/version, /api/tags and /api/ps; an OpenAI-compatible server, /v1/chat/completions (server-sent
events, a key it can require, parameters it can refuse) and /v1/models; and LanguageTool /v2/check and
/v2/languages; and an OpenAI-style transcription server, /v1/audio/transcriptions (multipart WAV in, {"text"}
out); and an OpenID Connect provider (discovery, /token with PKCE and rotating refresh tokens, /userinfo) with the
"nginx" in front of the rest that wants one of its access tokens. Used by unit and integration tests so no text ever
leaves the machine."""
import base64
import hashlib
import json
import socket
import threading
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CLIENT_ID = 'test-client'


def stream(text, pieces=3):
    """Ollama chunks that stream `text` in `pieces` parts and finish."""
    size = max(1, -(-len(text) // pieces))
    parts = [text[i:i + size] for i in range(0, len(text), size)] or ['']
    return [{'message': {'content': p}} for p in parts[:-1]] + [{'message': {'content': parts[-1]}, 'done': True}]


def sse(text, pieces=3, finish='stop', done=True):
    """The events of an OpenAI-style stream that says `text` in `pieces` parts: data lines as strings, the last
    one the finish reason (None: the stream just ends) and then [DONE] (done=False: no [DONE])."""
    size = max(1, -(-len(text) // pieces))
    parts = [text[i:i + size] for i in range(0, len(text), size)] or ['']
    events = ['data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': p}, 'finish_reason': None}]}) for p in parts]
    if finish:
        events.append('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}))
    return events + (['data: [DONE]'] if done else [])


def parse_multipart(content_type, body):
    """({field: value}, {field: (filename, bytes)}) of a multipart/form-data body."""
    boundary = content_type.split('boundary=')[1].encode()
    fields, files = {}, {}
    for part in body.split(b'--' + boundary)[1:-1]:
        head, _, data = part[2:-2].partition(b'\r\n\r\n')
        disposition = next(line for line in head.decode().split('\r\n') if line.lower().startswith('content-disposition'))
        name = disposition.split('name="')[1].split('"')[0]
        if 'filename="' in disposition:
            files[name] = (disposition.split('filename="')[1].split('"')[0], data)
        else:
            fields[name] = data.decode()
    return fields, files


def closed_port():
    """A local port with nothing listening (connection refused), for an unreachable server."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Stub:
    def __init__(self, port=0):  # port: a fixed one (OSError when taken), else any free one
        self.chunks = stream('')  # the reply of /api/chat, or set reply() for a computed one
        self.reply = None  # optional callable(request json) -> chunks
        self.chunk_delay = 0.0  # seconds between streamed chunks
        self.chat_status = 200
        self.chat_error = None  # with chat_status: Ollama's JSON body {"error": chat_error} instead of a page
        self.lt_delay = 0.0  # seconds LanguageTool takes to answer
        self.drop_after = None  # chunked like Ollama; drop the connection inside chunk N
        self.lt_matches = []
        self.lt_body = None  # bytes to answer /v2/check with instead (a captive portal's page)
        self.models = ['bielik-test']  # /api/tags
        # /api/pull: the lines of a pull (two layers, then success); a model pulled is in /api/tags afterwards
        self.pull_lines = [{'status': 'pulling manifest'},
                           {'status': 'pulling a', 'digest': 'sha256:a', 'total': 1000, 'completed': 400},
                           {'status': 'pulling b', 'digest': 'sha256:b', 'total': 3000},
                           {'status': 'pulling a', 'digest': 'sha256:a', 'total': 1000, 'completed': 1000},
                           {'status': 'pulling b', 'digest': 'sha256:b', 'total': 3000, 'completed': 3000},
                           {'status': 'verifying sha256 digest'}, {'status': 'success'}]
        self.loaded = []  # /api/ps: models in memory
        self.get_status = {}  # path -> HTTP status to answer a GET with instead
        self.events = sse('')  # the reply of /v1/chat/completions, or set openai_reply() for a computed one
        self.openai_reply = None  # optional callable(request json) -> events
        self.require_key = None  # /v1: a Bearer token the server wants (401 without it)
        self.reject = {}  # /v1: a parameter of the request -> the 400 message that refuses it
        self.ollama_reject = {}  # /api/chat: a parameter of the request -> the 400 message that refuses it (think)
        self.openai_status = 200
        self.transcript = 'Dyktowany tekst.'  # /v1/audio/transcriptions answers this
        self.stt_status = 200
        self.stt_body = None  # bytes to answer a transcription with instead (a plain text, a page)
        # the provider: the codes it issued (code -> (challenge, client id)), the access and refresh tokens that are valid
        self.codes, self.access_tokens, self.refresh_tokens = {}, set(), {}
        self.token_ttl = 3600  # seconds an access token lives (expires_in)
        self.rotate = True  # a refresh gives a new refresh token and ends the old one
        self.token_delay = 0.0  # seconds /token takes (processes then ask it at the same time)
        self.token_status = 200  # /token answers this instead (500: the provider is down)
        self.token_error = None  # with a 400: the OAuth error ("invalid_grant")
        self.require_login = False  # the "nginx": every other path wants one of the valid access tokens
        self.token_requests = []  # the form of every /token request
        self.headers_seen = []  # (path, Authorization header or None) of every request
        # The remote-access gateway (core.GATEWAY_PATHS): a request under /ollama, /languagetool or /whisper is answered as
        # without it, and the full path is kept here.
        self.gateway_seen = []
        self.agents = []  # (path, User-Agent header or None) of every request
        self.requests = []  # (path, parsed body)
        self.disconnects = 0  # streams the client closed early
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def gateway(self):
                for prefix in ('/ollama/', '/languagetool/', '/whisper/'):
                    if self.path.startswith(prefix):
                        stub.gateway_seen.append(self.path)
                        self.path = self.path[len(prefix) - 1:]
                        return

            def do_POST(self):
                self.gateway()
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                stub.headers_seen.append((self.path, self.headers.get('Authorization')))
                stub.agents.append((self.path, self.headers.get('User-Agent')))
                if self.path == '/token':
                    return self.token(body)
                if self.login_refused():
                    return
                if self.path.endswith(('/audio/transcriptions', '/inference')):
                    self.transcribe(body)
                elif self.path.endswith('/chat/completions'):
                    self.openai_chat(json.loads(body))
                elif self.path == '/api/chat':
                    request = json.loads(body)
                    stub.requests.append((self.path, request))
                    refused = next((message for param, message in stub.ollama_reject.items() if param in request), None)
                    if refused is not None:
                        return self.reply_json(json.dumps({'error': refused}).encode(), 400)
                    if stub.chat_status != 200:
                        if stub.chat_error is None:
                            self.send_error(stub.chat_status)
                        else:
                            self.reply_json(json.dumps({'error': stub.chat_error}).encode(), stub.chat_status)
                        return
                    chunks = stub.reply(request) if stub.reply else stub.chunks
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-ndjson')
                    if stub.drop_after is not None:
                        self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    try:
                        for i, chunk in enumerate(chunks):
                            data = (json.dumps(chunk) + '\n').encode()
                            if stub.drop_after is None:
                                self.wfile.write(data)
                            elif i < stub.drop_after:
                                self.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
                            else:  # announce a chunk, send half of it, hang up
                                self.wfile.write(b'%x\r\n%s' % (len(data), data[:len(data) // 2]))
                                self.wfile.flush()
                                self.close_connection = True
                                return
                            self.wfile.flush()
                            time.sleep(stub.chunk_delay)
                        if stub.drop_after is not None:
                            self.wfile.write(b'0\r\n\r\n')
                    except (BrokenPipeError, ConnectionResetError):
                        stub.disconnects += 1
                elif self.path == '/api/pull':  # pulling a model into Ollama: its progress, one JSON object a line
                    stub.requests.append((self.path, json.loads(body)))
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-ndjson')
                    self.end_headers()
                    for line in stub.pull_lines:
                        self.wfile.write((json.dumps(line) + '\n').encode())
                        self.wfile.flush()
                        time.sleep(stub.chunk_delay)
                    if any(line.get('status') == 'success' for line in stub.pull_lines):
                        stub.models = sorted(set(stub.models) | {json.loads(body)['model']})
                elif self.path == '/api/generate':  # loading a model: no prompt, one answer
                    stub.requests.append((self.path, json.loads(body)))
                    self.reply_json(json.dumps({'model': stub.models[0], 'done': True, 'done_reason': 'load'}).encode())
                elif self.path == '/v2/check':
                    form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
                    stub.requests.append((self.path, form))
                    time.sleep(stub.lt_delay)
                    self.reply_json(stub.lt_body if stub.lt_body is not None else
                                    json.dumps({'matches': stub.lt_matches, 'language': {'code': 'pl-PL'}}).encode())
                else:
                    self.send_error(404)

            def login_refused(self):
                """The "nginx" in front of the servers: 401 without a valid access token of the provider."""
                if stub.require_login and self.headers.get('Authorization') not in {'Bearer ' + t for t in stub.access_tokens}:
                    self.reply_json(json.dumps({'error': 'unauthorized'}).encode(), 401)
                    return True
                return False

            def token(self, body):
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
                stub.token_requests.append(form)
                time.sleep(stub.token_delay)
                if stub.token_status != 200:
                    error = {'error': stub.token_error} if stub.token_error else {}
                    return self.reply_json(json.dumps(error).encode(), stub.token_status)
                grant = form.get('grant_type')
                if grant == 'authorization_code':
                    challenge, client = stub.codes.pop(form.get('code'), (None, None))
                    digest = base64.urlsafe_b64encode(hashlib.sha256(form.get('code_verifier', '').encode()).digest())
                    if challenge is None or client != form.get('client_id') or digest.rstrip(b'=').decode() != challenge \
                            or form.get('redirect_uri') != 'gramadrama://auth':
                        return self.reply_json(json.dumps({'error': 'invalid_grant'}).encode(), 400)
                elif grant == 'refresh_token':
                    client = stub.refresh_tokens.get(form.get('refresh_token'))
                    if client is None or client != form.get('client_id'):
                        return self.reply_json(json.dumps({'error': 'invalid_grant'}).encode(), 400)
                    if stub.rotate:
                        del stub.refresh_tokens[form['refresh_token']]
                else:
                    return self.reply_json(json.dumps({'error': 'unsupported_grant_type'}).encode(), 400)
                reply = {'access_token': stub.new_access_token(), 'token_type': 'Bearer', 'expires_in': stub.token_ttl,
                         'scope': 'openid profile offline_access'}
                if grant == 'authorization_code' or stub.rotate:
                    reply['refresh_token'] = secrets.token_urlsafe(24)
                    stub.refresh_tokens[reply['refresh_token']] = form['client_id']
                self.reply_json(json.dumps(reply).encode())

            def transcribe(self, body):
                fields, files = parse_multipart(self.headers['Content-Type'], body)
                stub.requests.append((self.path, dict(fields, file=files.get('file'))))
                if stub.require_key is not None and self.headers.get('Authorization') != 'Bearer ' + stub.require_key:
                    return self.reply_json(json.dumps({'error': {'message': 'Incorrect API key provided.'}}).encode(), 401)
                if stub.stt_status != 200:
                    return self.reply_json(json.dumps({'error': {'message': 'The model does not exist.'}}).encode(),
                                           stub.stt_status)
                self.reply_json(stub.stt_body if stub.stt_body is not None else json.dumps({'text': stub.transcript}).encode())

            def openai_chat(self, request):
                stub.requests.append((self.path, request))
                if stub.require_key is not None and self.headers.get('Authorization') != 'Bearer ' + stub.require_key:
                    return self.reply_json(json.dumps({'error': {'message': 'Incorrect API key provided.',
                                                                 'type': 'invalid_request_error'}}).encode(), 401)
                for name, message in stub.reject.items():
                    if name in request:
                        return self.reply_json(json.dumps({'error': {'message': message}}).encode(), 400)
                if stub.openai_status != 200:
                    return self.reply_json(json.dumps({'error': {'message': 'The model `x` does not exist.'}}).encode(),
                                           stub.openai_status)
                events = stub.openai_reply(request) if stub.openai_reply else stub.events
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    for event in events:
                        self.wfile.write((event + '\n\n').encode())
                        self.wfile.flush()
                        time.sleep(stub.chunk_delay)
                except (BrokenPipeError, ConnectionResetError):
                    stub.disconnects += 1

            def reply_json(self, data, status=200):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.gateway()
                stub.requests.append((self.path, None))
                stub.headers_seen.append((self.path, self.headers.get('Authorization')))
                stub.agents.append((self.path, self.headers.get('User-Agent')))
                if self.path == '/.well-known/openid-configuration':
                    return self.reply_json(json.dumps({
                        'issuer': stub.url, 'authorization_endpoint': stub.url + '/authorize',
                        'token_endpoint': stub.url + '/token', 'userinfo_endpoint': stub.url + '/userinfo'}).encode())
                if self.path == '/userinfo':
                    if self.headers.get('Authorization') not in {'Bearer ' + t for t in stub.access_tokens}:
                        return self.reply_json(json.dumps({'error': 'invalid_token'}).encode(), 401)
                    return self.reply_json(json.dumps({'sub': '1', 'name': 'Jan Testowy',
                                                       'preferred_username': 'jan'}).encode())
                if self.login_refused():
                    return
                if self.path.endswith(('/models', '/health')) and stub.require_key is not None and \
                        self.headers.get('Authorization') != 'Bearer ' + stub.require_key:
                    return self.reply_json(json.dumps({'error': {'message': 'Incorrect API key provided.'}}).encode(), 401)
                if self.path in stub.get_status:
                    self.send_error(stub.get_status[self.path])
                    return
                replies = {'/v1/models': lambda: {'object': 'list', 'data': [{'id': m, 'object': 'model'} for m in stub.models]},
                           '/health': lambda: {'status': 'ok'}, '/speech/health': lambda: {'status': 'ok'},
                           '/openai/v1/models': lambda: {'object': 'list', 'data': [{'id': m} for m in stub.models]},
                           '/api/version': lambda: {'version': '0.0.0-stub'},
                           '/api/tags': lambda: {'models': [{'name': m} for m in stub.models]},
                           '/api/ps': lambda: {'models': [{'name': m} for m in stub.loaded]},
                           '/v2/languages': lambda: [{'name': 'Polish', 'code': 'pl', 'longCode': 'pl-PL'}]}
                if self.path not in replies:
                    self.send_error(404)
                    return
                data = json.dumps(replies[self.path]()).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(0.02), daemon=True)

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server.server_port

    def settings(self, ollama=None, languagetool=None, model='bielik-test', api='ollama', key='', login=False):
        """settings.json contents pointing at this stub (or at the given URLs). api: the protocol the model
        server is spoken to in; ollama is the URL of the model server whatever the protocol. login: this stub is also
        the provider the user logs in to."""
        data = {'llm_url': ollama or self.url, 'llm_api': api, 'languagetool_url': languagetool or self.url,
                'model': model}
        if key:
            data['llm_key'] = key
        if login:
            data.update(auth_issuer=self.url, auth_client_id=CLIENT_ID)
        return data

    def new_access_token(self):
        token = secrets.token_urlsafe(24)
        self.access_tokens.add(token)
        return token

    def authorize(self, url):
        """What the browser does after the user logged in to the provider: the address it comes back to for the
        authorization URL the app opened (gramadrama://auth?code=…&state=…), with a code issued for its challenge."""
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        assert query['response_type'] == 'code' and query['code_challenge_method'] == 'S256', query
        assert query['redirect_uri'] == 'gramadrama://auth' and 'offline_access' in query['scope'].split(), query
        code = secrets.token_urlsafe(16)
        self.codes[code] = (query['code_challenge'], query['client_id'])
        return 'gramadrama://auth?' + urllib.parse.urlencode({'code': code, 'state': query['state']})

    def login(self, name='', ttl=None):
        """The tokens of a user who is logged in already (auth.json's content), as finish_login would have saved them."""
        refresh = secrets.token_urlsafe(24)
        self.refresh_tokens[refresh] = CLIENT_ID
        return {'access_token': self.new_access_token(), 'refresh_token': refresh,
                'expires_at': time.time() + (self.token_ttl if ttl is None else ttl), 'issuer': self.url,
                'client_id': CLIENT_ID, 'token_endpoint': self.url + '/token', 'name': name}

    def chats(self):
        return [body for path, body in self.requests if path == '/api/chat']

    def transcriptions(self):
        return [body for path, body in self.requests if path.endswith(('/audio/transcriptions', '/inference'))]

    def completions(self):
        return [body for path, body in self.requests if path.endswith('/chat/completions')]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        return False
