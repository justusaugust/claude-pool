"""Load the phone page's app.js in Node with a stubbed fetch and DOM; check how a refresh loads and renders."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

APP = Path(__file__).resolve().parent / 'phone/app.js'
NODE = shutil.which('node')
# One page load: app.js runs its first refresh; each request answers after `latency` ms.
PROBE = r'''
import vm from 'node:vm'
import { readFileSync } from 'node:fs'
const [source, scenario] = [readFileSync(process.argv[2], 'utf8'), JSON.parse(process.argv[3])]
const now = Math.floor(Date.now() / 1000)
const task = 'task-fixture-0001'
const bodies = {
  '/api/state': { csrf: 'fixture-csrf', pool: null, pending: [], commands: [],
    tasks: [{ task, title: 'Fixture task', project: 'fixture', status: 'idle', seen: now, generation: 'g1' }] },
  ['/api/task/' + task]: { tasks: [{ task, events: [{ kind: 'assistant', at: now, text: 'Fixture event text' }] }] },
}
let out = 0, together = 0, settled
const done = new Promise(resolve => { settled = resolve })
const requests = []
const element = id => {
  let text = ''
  return { id, innerHTML: '', dataset: {}, classList: { add() {}, remove() {} }, setAttribute() {}, removeAttribute() {},
    querySelectorAll: () => [], addEventListener() {},
    get textContent() { return text },
    set textContent(value) { text = String(value); if (id === 'connection') settled(performance.now()) } }
}
const elements = {}
const document = { hidden: false, activeElement: null, getElementById: id => (elements[id] ??= element(id)),
  querySelectorAll: () => [], addEventListener() {} }
const context = vm.createContext({
  document, location: { hash: scenario.hash }, crypto: globalThis.crypto, setInterval: () => 0,
  window: { addEventListener() {}, scrollTo() {} },
  fetch: async path => {
    requests.push(path)
    together = Math.max(together, ++out)
    await new Promise(resolve => setTimeout(resolve, scenario.latency))
    out--
    const error = scenario.fail?.[path]
    return { ok: !error, json: async () => error ? { error } : bodies[path] }
  },
})
const started = performance.now()
vm.runInContext(source, context)
const finished = await done
await new Promise(resolve => setTimeout(resolve, 0))
console.log(JSON.stringify({
  requests, together, ms: Math.round(finished - started),
  connection: elements.connection.textContent, notice: elements.notice.textContent, html: elements.app.innerHTML,
  state: vm.runInContext('state', context), detail: vm.runInContext('detail', context),
}))
'''
TASK = 'task-fixture-0001'


@unittest.skipUnless(NODE or os.environ.get('CI'), 'The phone page test needs Node')
class PhoneRefresh(unittest.TestCase):
    def setUp(self):
        # In CI a missing Node is a broken runner, not a reason to skip.
        if not NODE: self.fail('node is not on PATH; the phone page test needs Node in CI')
        self.folder = tempfile.TemporaryDirectory()
        self.probe = Path(self.folder.name) / 'probe.mjs'
        self.probe.write_text(PROBE)

    def tearDown(self):
        self.folder.cleanup()

    def load(self, hash='', fail=None, latency=40):
        result = subprocess.run([NODE, str(self.probe), str(APP), json.dumps({'hash': hash, 'fail': fail or {}, 'latency': latency})],
                                capture_output=True, text=True, timeout=20, env=dict(os.environ, NODE_NO_WARNINGS='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_an_open_task_loads_the_state_and_its_activity_together(self):
        page = self.load('#task=' + TASK)
        self.assertEqual(sorted(page['requests']), ['/api/state', '/api/task/' + TASK])
        self.assertEqual(page['together'], 2, 'the task request waited for the state request')
        self.assertTrue(page['connection'].startswith('Live · '))
        self.assertIn('Fixture task', page['html'])
        self.assertIn('Fixture event text', page['html'])
        self.assertEqual(page['notice'], '')

    def test_the_overview_loads_only_the_state(self):
        page = self.load('')
        self.assertEqual(page['requests'], ['/api/state'])
        self.assertIsNone(page['detail'])
        self.assertIn('Fixture task', page['html'])

    def test_a_failed_task_request_keeps_the_new_state_and_shows_its_error(self):
        page = self.load('#task=' + TASK, fail={'/api/task/' + TASK: 'Task fixture unavailable'})
        self.assertEqual(page['connection'], 'Disconnected')
        self.assertEqual(page['notice'], 'Task fixture unavailable')
        self.assertEqual(page['html'], '')
        self.assertEqual(page['state']['csrf'], 'fixture-csrf')
        self.assertIsNone(page['detail'])

    def test_a_failed_state_request_shows_its_error_before_the_task_error(self):
        page = self.load('#task=' + TASK, fail={'/api/state': 'State fixture unavailable', '/api/task/' + TASK: 'Task fixture unavailable'})
        self.assertEqual(page['connection'], 'Disconnected')
        self.assertEqual(page['notice'], 'State fixture unavailable')
        self.assertEqual(page['html'], '')
        self.assertIsNone(page['state'])
        self.assertIsNone(page['detail'])


if __name__ == '__main__': unittest.main()
