"""Native hook transport. Stdout contains only Native's decision, never credentials."""
import http.client
import json
import os
from pathlib import Path
import sys
import time
import uuid

from settings import STORES, CONTROL_PORT
TOKEN = STORES / '.pool-client-token'


def rpc(path, body):
    conn = http.client.HTTPConnection('127.0.0.1', CONTROL_PORT, timeout=3)
    try:
        conn.request('POST', '/internal/' + path, json.dumps(body), {'Authorization': 'Bearer ' + TOKEN.read_text().strip(), 'Content-Type': 'application/json'})
        response = conn.getresponse()
        if response.status != 200: raise RuntimeError('Phone control unavailable')
        return json.loads(response.read(150000))
    finally:
        conn.close()


def handle(event, call=rpc, wait=time.sleep, now=time.monotonic):
    task, generation = os.environ.get('COCKPIT_CONTROL_TASK'), os.environ.get('COCKPIT_CONTROL_GENERATION')
    if not task or not generation or event.get('session_id') != os.environ.get('COCKPIT_CONTROL_SESSION'): return None
    base = {'task': task, 'generation': generation}
    kind = event.get('hook_event_name')
    if kind == 'Notification' and event.get('notification_type') == 'permission_prompt':
        call('worker', {**base, 'operation': 'event', 'id': 'native-prompt-' + str(time.time_ns()), 'kind': 'notice',
                        'text': 'Claude needs a Native response. Open the task in its terminal; this prompt cannot be answered here.'})
        call('worker', {**base, 'operation': 'status', 'status': 'needs-native'})
        return None
    question = kind == 'PreToolUse' and event.get('tool_name') == 'AskUserQuestion'
    if kind == 'PreToolUse' and not question:
        call('worker', {**base, 'operation': 'prepare', 'tool': event.get('tool_name'), 'input': event.get('tool_input')})
        return None
    if kind != 'PermissionRequest' and not question: return None
    data = event.get('tool_input')
    if not isinstance(data, dict): return None
    created = call('challenge', {**base, 'kind': 'question' if question else 'permission', 'tool': event.get('tool_name'), 'input': data})
    request = {**base, 'id': created['id']}
    try:
        end = now() + 120
        while now() < end:
            result = call('result', request)
            status = result.get('status')
            if status == 'allow':
                if question:
                    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                            'updatedInput': {**data, 'answers': result['answers']}}}
                return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}
            if status == 'deny':
                if question: return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': 'You denied this question in Cockpit.'}}
                return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'deny', 'message': 'You denied this action in Cockpit.'}}}
            if status != 'pending': return None
            wait(.7)
        return None
    finally:
        try: call('result', {**request, 'cancel': True})
        except (OSError, ValueError, RuntimeError, http.client.HTTPException): pass


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'hook'
    raw = sys.stdin.read(75001)
    if len(raw) > 75000: return
    event = json.loads(raw)
    if mode == 'register':
        event['pid'] = os.getppid()
        event['instance'] = str(uuid.uuid4())
        result = rpc('register', event)
    else:
        result = handle(event)
    if result is not None: print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, http.client.HTTPException): pass
