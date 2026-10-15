"""Private human controls beside the live inference bridge; never a model endpoint."""
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from bridge import atomic_write

UUID = re.compile(r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
STATIC = Path(__file__).with_name('phone')


class Conflict(ValueError):
    pass


class Controls:
    def __init__(self, path, now=time.time):
        self.path, self.now, self.lock = Path(path), now, threading.RLock()
        self.tasks, self.commands, self.requests, self.prepared = {}, {}, {}, {}
        self.storage_error = False
        try:
            if self.path.exists() and self.path.stat().st_size > 2 * 1024 * 1024: raise ValueError('Control metadata too large')
            data = json.loads(self.path.read_text())
            self.tasks = data['tasks']
            for task in self.tasks.values():
                task.update(events=[], status='offline', seen=0)
            self.commands = {k: {**v, 'status': 'unknown' if v['status'] in {'queued', 'delivering'} else v['status']} for k, v in data.get('commands', {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise RuntimeError('Control metadata needs repair; original file preserved') from error

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {'tasks': {k: {a: b for a, b in v.items() if a != 'events'} for k, v in self.tasks.items()},
                 'commands': {k: {a: b for a, b in v.items() if a != 'text'} for k, v in self.commands.items()}}
        try:
            atomic_write(self.path, json.dumps(value))
            os.chmod(self.path, 0o600)
            self.storage_error = False
        except OSError:
            self.storage_error = True
            raise

    def current(self, task, generation):
        row = self.tasks.get(task)
        if not row or row['generation'] != generation or row['status'] == 'retired':
            raise Conflict('Task changed or disconnected. Refresh before acting.')
        return row

    def register(self, body):
        task, session = body.get('task'), body.get('session')
        if not isinstance(task, str) or not UUID.fullmatch(task) or not isinstance(session, str) or not UUID.fullmatch(session):
            raise ValueError('Invalid task identity')
        pid = body.get('pid')
        if type(pid) != int or pid <= 1:
            raise ValueError('Invalid worker identity')
        instance = body.get('instance')
        if not isinstance(instance, str) or not UUID.fullmatch(instance): raise ValueError('Invalid worker instance')
        with self.lock:
            # Native respawn may omit inherited environment; its saved session still owns the logical task.
            owner = next((row for row in self.tasks.values() if row['session'] == session), None)
            if owner: task = owner['task']
            previous = self.tasks.get(task)
            if previous and instance in previous.get('retired', []): raise Conflict('Worker was replaced')
            if previous and previous['session'] == session and previous.get('instance') == instance:
                previous['seen'] = self.now()
                previous['status'] = 'idle'
                return {k: previous[k] for k in ['task', 'session', 'generation']}
            if not previous and len(self.tasks) >= 64:
                old = min(self.tasks, key=lambda k: self.tasks[k]['seen'])
                if self.now() - self.tasks[old]['seen'] < 90:
                    raise Conflict('Task capacity reached')
                del self.tasks[old]
            generation = str(uuid.uuid4())
            row = {'task': task, 'session': session, 'generation': generation, 'pid': pid, 'instance': instance,
                   'project': str(body.get('project', 'Claude'))[:80], 'title': str(body.get('title', ''))[:100],
                   'seen': self.now(), 'status': 'idle', 'events': previous.get('events', []) if previous else [],
                   'retired': (previous.get('retired', []) + [previous.get('instance', previous['session'] + ':' + str(previous['pid']))])[-32:] if previous else []}
            self.tasks[task] = row
            for request in self.requests.values():
                if request['task'] == task and request['status'] == 'pending': request['status'] = 'stale'
            for command in self.commands.values():
                if command['task'] == task and command['status'] in {'queued', 'delivering'}: command['status'] = 'unknown'
            self.save()
            return {k: row[k] for k in ['task', 'session', 'generation']}

    def worker(self, body):
        with self.lock:
            row = self.current(body.get('task'), body.get('generation'))
            row['seen'] = self.now()
            operation = body.get('operation')
            if operation == 'prepare':
                if not isinstance(body.get('tool'), str) or not isinstance(body.get('input'), dict): raise ValueError('Invalid tool preparation')
                raw = json.dumps(body['input'], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
                if len(raw.encode()) > 65536: raise ValueError('Tool input too large')
                self.prepared[(row['task'], row['generation'], body['tool'])] = (hashlib.sha256(raw.encode()).hexdigest(), self.now())
                if len(self.prepared) > 256: del self.prepared[min(self.prepared, key=lambda k: self.prepared[k][1])]
                return {'ok': True}
            if operation == 'poll':
                if row['status'] == 'offline': row['status'] = 'idle'
                command = next((v for v in self.commands.values() if v['task'] == row['task'] and v['generation'] == row['generation'] and v['status'] == 'queued'), None)
                if command:
                    command['status'] = 'delivering'
                    self.save()  # Record uncertainty before handing bytes to Native; never replay a lease.
                    return {'command': dict(command)}
                return {'command': None}
            if operation == 'receipt':
                command = self.commands.get(body.get('id'))
                if not command or command['task'] != row['task'] or command['generation'] != row['generation']:
                    raise Conflict('Instruction no longer belongs to this worker')
                if command['status'] == 'delivering':
                    command['status'] = 'accepted' if body.get('accepted') is True else 'unknown'
                    command.pop('text', None)
                    self.save()
                return {'status': command['status']}
            if operation == 'event':
                kind = body.get('kind')
                if kind not in {'user', 'assistant', 'tool', 'notice'}: raise ValueError('Invalid activity')
                text = body.get('text')
                if not isinstance(text, str) or len(text) > 4096: raise ValueError('Activity too large')
                event_id = str(body.get('id', ''))[:100]
                if text and not any(e['id'] == event_id for e in row['events']):
                    row['events'] = (row['events'] + [{'id': event_id, 'kind': kind, 'text': text, 'at': self.now()}])[-48:]
                    if kind == 'user' and not row['title']: row['title'] = text.splitlines()[0][:100]
                return {'ok': True}
            if operation == 'status':
                if body.get('status') not in {'idle', 'working', 'offline', 'needs-native'}: raise ValueError('Invalid status')
                row['status'] = body['status']
                if body['status'] in {'idle', 'offline'}:
                    for r in self.requests.values():
                        if r['task'] == row['task'] and r['generation'] == row['generation'] and r['status'] == 'pending': r['status'] = 'stale'
                return {'ok': True}
            raise ValueError('Unknown worker operation')

    def message(self, body):
        with self.lock:
            row = self.current(body.get('task'), body.get('generation'))
            if row['status'] == 'offline' or self.now() - row['seen'] > 20: raise Conflict('Task is disconnected. No instruction sent.')
            key, text = body.get('id'), body.get('text')
            if not isinstance(key, str) or not UUID.fullmatch(key) or not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
                raise ValueError('Enter an instruction of 1–8000 characters')
            digest = hashlib.sha256(text.encode()).hexdigest()
            if key in self.commands:
                old = self.commands[key]
                if (old['task'], old['generation'], old['digest']) != (row['task'], row['generation'], digest):
                    raise Conflict('Instruction identifier was already used')
                return {'id': key, 'status': old['status']}
            if len(self.commands) >= 128:
                done = [k for k, v in self.commands.items() if v['status'] not in {'queued', 'delivering'}]
                if not done: raise Conflict('Instruction queue is full')
                del self.commands[done[0]]
            self.commands[key] = {'id': key, 'task': row['task'], 'generation': row['generation'], 'digest': digest,
                                  'text': text, 'status': 'queued', 'at': self.now()}
            self.save()
            return {'id': key, 'status': 'queued'}

    def challenge(self, body):
        with self.lock:
            row = self.current(body.get('task'), body.get('generation'))
            tool, data, kind = body.get('tool'), body.get('input'), body.get('kind')
            if not isinstance(tool, str) or not 1 <= len(tool) <= 200 or not isinstance(data, dict) or kind not in {'permission', 'question'}:
                raise ValueError('Invalid permission request')
            raw = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            if len(raw.encode()) > 65536: raise ValueError('Request too large; answer in Native')
            digest = hashlib.sha256(raw.encode()).hexdigest()
            prepared = self.prepared.get((row['task'], row['generation'], tool))
            if kind == 'permission' and (not prepared or prepared[0] != digest or self.now() - prepared[1] > 30):
                raise Conflict('Tool input is not confirmed by PreToolUse. Answer in Native.')
            pending = [v for v in self.requests.values() if v['status'] == 'pending' and v['expires'] > self.now()]
            if len(pending) >= 16: raise Conflict('Decision capacity reached; answer in Native')
            if kind == 'question':
                questions = data.get('questions')
                if not isinstance(questions, list) or not 1 <= len(questions) <= 4 or any(not isinstance(q, dict) or not isinstance(q.get('question'), str) for q in questions):
                    raise ValueError('Unsupported question; answer in Native')
            key = str(uuid.uuid4())
            request = {'id': key, 'task': row['task'], 'generation': row['generation'], 'kind': kind, 'tool': tool,
                       'input': data, 'digest': digest, 'status': 'pending',
                       'expires': self.now() + 120, 'at': self.now()}
            self.requests = {k: v for k, v in self.requests.items() if v['expires'] > self.now() - 300}
            while len(self.requests) >= 64:
                completed = [k for k, v in self.requests.items() if v['status'] != 'pending' or v['expires'] <= self.now()]
                if not completed: raise Conflict('Decision capacity reached; answer in Native')
                del self.requests[completed[0]]
            self.requests[key] = request
            return {'id': key, 'digest': request['digest'], 'expires': request['expires']}

    def decision(self, body):
        with self.lock:
            request = self.requests.get(body.get('id'))
            if not request or request['task'] != body.get('task') or request['generation'] != body.get('generation') or request['digest'] != body.get('digest'):
                raise Conflict('Decision does not match the pending action')
            worker = self.current(request['task'], request['generation'])
            if worker['status'] == 'offline' or self.now() - worker['seen'] > 20: raise Conflict('Worker disconnected. Answer in Native.')
            action = body.get('action')
            if action not in {'allow', 'deny', 'local'}: raise ValueError('Choose approve once, deny or answer in Native')
            if request['status'] in {'allow', 'deny', 'local'}:
                if request['status'] != action or request.get('answers') != body.get('answers'):
                    raise Conflict('This request already has a different decision')
                return {'status': request['status'], 'duplicate': True}
            if request['status'] != 'pending' or request['expires'] <= self.now(): raise Conflict('Request expired. Refresh the task.')
            if action == 'allow' and request['kind'] == 'question':
                answers = body.get('answers')
                keys = {q['question'] for q in request['input']['questions']}
                if not isinstance(answers, dict) or set(answers) != keys or any(not isinstance(v, str) or not 1 <= len(v) <= 4000 for v in answers.values()):
                    raise ValueError('Answer each question before submitting')
                request['answers'] = answers
            request['status'] = action
            return {'status': action, 'duplicate': False}

    def result(self, body):
        with self.lock:
            request = self.requests.get(body.get('id'))
            if not request or (request['task'], request['generation']) != (body.get('task'), body.get('generation')): raise Conflict('Unknown request')
            self.current(request['task'], request['generation'])
            self.tasks[request['task']]['seen'] = self.now()
            if body.get('cancel') is True and request['status'] == 'pending': request['status'] = 'local'
            if request['expires'] <= self.now() and request['status'] == 'pending': request['status'] = 'expired'
            if request.get('consumed'): return {'status': 'consumed'}
            if request['status'] in {'allow', 'deny'}: request['consumed'] = True
            return {k: request[k] for k in ['status', 'answers'] if k in request}

    def view(self, task=None):
        with self.lock:
            rows = []
            for key, row in self.tasks.items():
                if task and key != task: continue
                state = {k: v for k, v in row.items() if k not in {'pid', 'instance', 'events', 'retired'}}
                if self.now() - row['seen'] > 20: state['status'] = 'offline'
                if task: state['events'] = list(row['events'])
                rows.append(state)
            pending = [dict(v) for v in self.requests.values() if (not task or v['task'] == task) and v['status'] == 'pending' and v['expires'] > self.now()
                       and self.tasks.get(v['task'], {}).get('generation') == v['generation']
                       and self.tasks[v['task']]['status'] != 'offline' and self.now() - self.tasks[v['task']]['seen'] <= 20]
            commands = [{k: v for k, v in c.items() if k not in {'text', 'digest'}} for c in self.commands.values() if not task or c['task'] == task]
            return {'tasks': rows, 'pending': pending, 'commands': commands[-30:], 'storageError': self.storage_error, 'updatedAt': self.now()}


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, controls, token, host, login, overview, discover=lambda: []):
        super().__init__(address, ControlHandler)
        self.controls, self.token, self.host, self.login, self.overview = controls, token, host, login, overview
        self.csrf = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.clients = threading.BoundedSemaphore(32)
        self.discover = discover

    def view(self):
        value = self.controls.view()
        known = {t['session'] for t in value['tasks']}
        try: inventory = self.discover()
        except (OSError, ValueError, RuntimeError): inventory = []
        for task in value['tasks']:
            native = next((r for r in inventory if r['session'] == task['session']), None)
            if native and native.get('title'): task['title'] = native['title']
        observed = [t for t in inventory if t['session'] not in known]
        value['tasks'].extend(observed)
        return value

    def process_request(self, request, client_address):
        if not self.clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try: super().process_request(request, client_address)
        except BaseException:
            self.clients.release()
            raise

    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.clients.release()


class ControlHandler(BaseHTTPRequestHandler):
    timeout = 5
    def log_message(self, *args):
        pass

    def reply(self, status, data, mime='application/json', cookie=False):
        raw = json.dumps(data, ensure_ascii=False).encode() if mime == 'application/json' else data
        self.send_response(status)
        for k, v in {'Content-Type': mime, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                     'Content-Length': str(len(raw)), 'Connection': 'close', 'Referrer-Policy': 'no-referrer',
                     'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items(): self.send_header(k, v)
        if cookie: self.send_header('Set-Cookie', '__Host-cockpit=' + self.server.cookie + '; Secure; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        try: self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError): pass

    def human(self):
        # Only Serve's private HTTPS front door is trusted; worker bearer auth never reaches this branch.
        return (self.headers.get('Host') == self.server.host and self.headers.get('Tailscale-User-Login') == self.server.login
                and len(self.headers.get_all('Tailscale-User-Login', [])) == 1)

    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()

    def handle_request(self):
        self.connection.settimeout(5)
        path = urlsplit(self.path).path
        internal = path.startswith('/internal/')
        if path == '/health' and self.command == 'GET': return self.reply(200, {'service': 'claude-controls', 'version': 1})
        if internal:
            auth = self.headers.get('Authorization', '')
            if len(self.headers.get_all('Authorization', [])) != 1 or not hmac.compare_digest(auth.encode(), ('Bearer ' + self.server.token).encode()):
                return self.reply(401, {'error': 'Worker authentication required'})
        elif not self.human():
            return self.reply(403, {'error': 'Open Cockpit through your private Tailscale address.'})
        if self.command == 'POST' and not internal:
            cookie = SimpleCookie()
            try: cookie.load(self.headers.get('Cookie', ''))
            except Exception: return self.reply(403, {'error': 'Refresh Cockpit before acting.'})
            if (self.headers.get('Origin') != 'https://' + self.server.host or self.headers.get('X-Cockpit-CSRF') != self.server.csrf
                    or not cookie.get('__Host-cockpit') or not hmac.compare_digest(cookie['__Host-cockpit'].value, self.server.cookie)):
                return self.reply(403, {'error': 'Refresh Cockpit before acting.'})
        try:
            body = {}
            if self.command == 'POST':
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 < length <= 75000 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Invalid control request')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict): raise ValueError('Expected an object')
            if internal and self.command == 'GET' and path == '/internal/usage':
                try: value = self.server.overview()
                except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                    return self.reply(503, {'error': 'Quota telemetry unavailable'})
                return self.reply(200, {k: value[k] for k in ['accounts', 'updatedAt']})
            if internal and self.command == 'POST':
                if path == '/internal/overview':
                    value = self.server.view()
                    value['pending'] = [{'task': r['task'], 'tool': r['tool'], 'kind': r['kind']} for r in value['pending']]
                    try: value['pool'] = self.server.overview()
                    except (OSError, ValueError, RuntimeError, http.client.HTTPException): value['pool'] = None
                    return self.reply(200, value)
                call = {'/internal/register': self.server.controls.register, '/internal/worker': self.server.controls.worker,
                        '/internal/challenge': self.server.controls.challenge, '/internal/result': self.server.controls.result,
                        '/internal/message': self.server.controls.message}.get(path)
                if call: return self.reply(200, call(body))
            if not internal and self.command == 'GET':
                if path in {'/', '/index.html', '/app.js', '/style.css'}:
                    name = 'index.html' if path == '/' else path[1:]
                    mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
                    return self.reply(200, (STATIC / name).read_bytes(), mime, cookie=True)
                if path == '/api/state':
                    value = self.server.view()
                    try: value['pool'] = self.server.overview()
                    except (OSError, ValueError, RuntimeError, http.client.HTTPException): value['pool'] = None
                    value['csrf'] = self.server.csrf
                    return self.reply(200, value, cookie=True)
                if path.startswith('/api/task/'):
                    task = path.removeprefix('/api/task/')
                    if not UUID.fullmatch(task): raise ValueError('Invalid task')
                    return self.reply(200, self.server.controls.view(task))
            if not internal and self.command == 'POST':
                call = {'/api/message': self.server.controls.message, '/api/decision': self.server.controls.decision}.get(path)
                if call: return self.reply(200, call(body))
            self.reply(404, {'error': 'Unknown control'})
        except Conflict as error: self.reply(409, {'error': str(error)})
        except (ValueError, TypeError, KeyError) as error: self.reply(400, {'error': str(error) if isinstance(error, ValueError) else 'Invalid control request'})
        except (OSError, RuntimeError): self.reply(503, {'error': 'Controls unavailable. Check the task before retrying.'})
