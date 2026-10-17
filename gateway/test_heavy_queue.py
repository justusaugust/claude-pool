"""Legacy calls run concurrently; supported suites get local worker defaults."""
import json,os,shlex,shutil,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from heavy_queue import prepare,PREFIX
HERE=Path(__file__).parent;RUNNER=HERE/'heavy_queue.py'

class SuiteLimitsTests(unittest.TestCase):
 def test_supported_direct_flags_and_single_worker_are_preserved(self):
  cases={'vitest run':['--maxWorkers=2'],'npx playwright test':['--workers=2'],'npx --yes jest':['--maxWorkers=2'],'pnpm exec jest':['--maxWorkers=2'],'cargo +1.96.1 test -- --test-threads 8':['--jobs=2','--test-threads 2'],'cargo build -j8':['-j2'],'go test ./...':['-p=2','-parallel=2'],'go test ./... -parallel=8':['-p=2','-parallel=2'],'pytest -n auto':['-n 2']}
  for command,expected in cases.items():
   with self.subTest(command=command):
    capped=prepare(command).split('\n',1)[1]
    for part in expected:self.assertIn(part,capped)
  self.assertIn('--maxWorkers=1',prepare('vitest run --maxWorkers=1'))
  self.assertIn('VITEST_MAX_WORKERS=1',prepare('vitest run --maxWorkers=1'))
  self.assertIn('--workers 1',prepare('playwright test --workers 1'))
 def test_inherited_single_worker_is_not_raised(self):
  from unittest.mock import patch
  with patch.dict(os.environ,{'CARGO_BUILD_JOBS':'1','VITEST_MAX_WORKERS':'1','GOMAXPROCS':'1'}):
   self.assertIn('CARGO_BUILD_JOBS=1',prepare('cd fixture && cargo test'))
   self.assertIn('--jobs=1',prepare('cargo build'))
   self.assertIn('--maxWorkers=1',prepare('vitest run'))
   self.assertIn('-parallel=1',prepare('go test ./...'))
 def test_shell_scripts_quotes_help_and_opaque_commands_remain_intact(self):
  for command in ['cd fixture && npm test','echo "vitest run"','npm run check','pytest --help','vitest --version','cargo test -- --nocapture','printf "%s" "$VALUE"; false']:
   capped=prepare(command)
   self.assertTrue(capped.startswith(PREFIX))
   if not command.startswith('cargo test'):self.assertTrue(capped.endswith('\n'+command))
   self.assertEqual(prepare(capped),capped)
 def test_legacy_classify_and_gate_cannot_wait_for_a_lock(self):
  result=subprocess.run([sys.executable,str(RUNNER),'--classify'],input='npm test; cargo test',capture_output=True,text=True,timeout=3)
  self.assertEqual(result.stdout.strip(),'light')
  script=shlex.join([sys.executable,str(RUNNER.resolve()),'--gate'])+' "$$" && printf PASSED'
  result=subprocess.run([shutil.which('zsh') or '/bin/sh','-c',script],capture_output=True,text=True,timeout=3)
  self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,'PASSED')
 def test_independent_legacy_jobs_start_concurrently_and_preserve_exit_and_env(self):
  with tempfile.TemporaryDirectory() as folder:
   folder=Path(folder);release=folder/'release'
   jobs=[]
   for name in ['a','b']:
    marker=folder/name
    code='from pathlib import Path;import os,time;Path('+repr(str(marker))+').write_text(os.environ["CARGO_BUILD_JOBS"]);p=Path('+repr(str(release))+');\nwhile not p.exists():time.sleep(.02)'
    jobs.append(subprocess.Popen([sys.executable,str(RUNNER.resolve()),'/bin/sh',shlex.join([sys.executable,'-c',code])],cwd=folder))
   try:
    deadline=time.monotonic()+4
    while not all((folder/n).exists() for n in ['a','b']) and time.monotonic()<deadline:time.sleep(.02)
    self.assertTrue(all((folder/n).exists() for n in ['a','b']),'Independent suites were serialized')
    self.assertEqual((folder/'a').read_text(),'2')
   finally:
    release.touch()
    for job in jobs:self.assertEqual(job.wait(timeout=3),0)
   result=subprocess.run([sys.executable,str(RUNNER.resolve()),'/bin/sh','exit 7'],cwd=folder,timeout=3)
   self.assertEqual(result.returncode,7)
