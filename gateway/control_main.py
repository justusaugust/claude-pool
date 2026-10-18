"""Run one optional control listener without restarting the inference owner."""
import fcntl
import http.client
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import webbrowser
from control import Controls, ControlServer

from settings import HERE, RUNTIME, CONTROL_PORT, private_directory
TAILSCALE = shutil.which('tailscale') or '/Applications/Tailscale.app/Contents/MacOS/Tailscale'


def config():
    return json.loads((RUNTIME / 'phone-config.json').read_text())


def alive():
    conn = http.client.HTTPConnection('127.0.0.1', CONTROL_PORT, timeout=1)
    try:
        conn.request('GET', '/health')
        response = conn.getresponse()
        return response.status == 200 and json.loads(response.read()).get('service') == 'claude-controls'
    except (OSError, ValueError, http.client.HTTPException): return False
    finally: conn.close()


def start():
    if alive(): return
    private_directory(RUNTIME)
    with (RUNTIME / 'phone-start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if alive(): return
        config()
        child = subprocess.Popen([sys.executable, str(__file__), '--serve'], cwd=HERE, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):
            if alive(): return
            if child.poll() is not None: raise RuntimeError('Phone controls could not start; active Claude tasks are unaffected.')
            time.sleep(.1)
        raise RuntimeError('Phone controls did not become ready; inspect before retrying.')


def prepare():
    from bridge import atomic_write
    if (RUNTIME / 'phone-config.json').exists(): return config()
    result = subprocess.run([TAILSCALE, 'status', '--json'], capture_output=True, text=True, timeout=15)
    status = json.loads(result.stdout)
    own = status.get('Self', {})
    login = status.get('User', {}).get(str(own.get('UserID')), {}).get('LoginName')
    dns = own.get('DNSName', '').rstrip('.')
    if status.get('BackendState') != 'Running' or not login or not dns.endswith('.ts.net'):
        raise RuntimeError('Connect Tailscale on this machine before enabling phone controls.')
    private_directory(RUNTIME)
    value = {'host': dns + ':8443', 'login': login}
    atomic_write(RUNTIME / 'phone-config.json', json.dumps(value))
    return value


def cli(arguments):
    if arguments not in [[], ['--status'], ['--chat']]: raise RuntimeError('Usage: claude phone [--status | --chat]')
    value = prepare()
    start()
    if arguments == ['--chat']:
        from launch import NATIVE_CLAUDE, configured_plugin
        from conductor import native_env
        env = native_env(os.environ)
        raise SystemExit(subprocess.call([NATIVE_CLAUDE, '--name', 'Claude pool overview', '--remote-control', 'Claude pool overview',
            '--permission-mode', 'default', '--setting-sources', '', '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--plugin-dir', str(configured_plugin('phone-hub')), '--', 'Show pool overview.'], env=env))
    url = 'https://' + value['host'] + '/'
    if not arguments: webbrowser.open(url)
    print('Private Cockpit · ' + url)
    print('New Claude sessions connect automatically. Existing tasks keep running unchanged.')


def serve():
    import launch
    value = config()
    client = launch.token('.pool-client-token')
    controls = Controls(RUNTIME / 'phone-state.json')
    inventory = {'at': 0, 'rows': []}
    def discover():
        from control import UUID
        if time.monotonic() - inventory['at'] < 15: return inventory['rows']
        result = subprocess.run([launch.NATIVE_CLAUDE, 'agents', '--json', '--all'], capture_output=True, text=True, timeout=5)
        if result.returncode: raise RuntimeError('Native inventory unavailable')
        rows = []
        for item in json.loads(result.stdout):
            session = item.get('sessionId')
            if not isinstance(session, str) or not UUID.fullmatch(session): continue
            if item.get('kind') != 'interactive' and item.get('state') not in {'running', 'blocked', 'idle', 'waiting'}: continue
            rows.append({'task': session, 'session': session, 'generation': '', 'legacy': True,
                         'title': str(item.get('name') or '')[:100], 'project': Path(str(item.get('cwd') or 'Claude')).name,
                         'status': 'needs-native' if item.get('state') == 'blocked' else 'observed', 'seen': time.time()})
        inventory.update(at=time.monotonic(), rows=rows[:64])
        return inventory['rows']
    server = ControlServer(('127.0.0.1', CONTROL_PORT), controls, client, value['host'], value['login'],
                           lambda: json.loads(launch.request(client, path='/pool/overview')), discover)
    try: server.serve_forever(poll_interval=.5)
    finally: server.server_close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']: serve()
    else: cli(sys.argv[1:])
