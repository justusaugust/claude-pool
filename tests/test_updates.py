"""Real versioned installs and worker continuity, without production credentials."""
import contextlib
import fcntl
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
import plistlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('update_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
sys.path.insert(0, str(ROOT / 'gateway'))
import update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        shutil.copytree(ROOT, self.source, ignore=shutil.ignore_patterns('.git', '__pycache__', 'bin', 'versions'))
        binary = self.source / 'gateway/bin/shunt'; binary.parent.mkdir()
        binary.write_text('#!/bin/sh\necho "fixture-backend"\n'); binary.chmod(0o755)
        self.app, self.bin, self.config = self.root/'app', self.root/'bin', self.root/'config.json'
        self.native = self.root/'native'
        self.native.write_text('#!/bin/sh\necho "2.1.300 (Claude Code)"\n'); self.native.chmod(0o755)
        self.patch_source = patch.object(installer, 'SOURCE', self.source); self.patch_source.start(); self.addCleanup(self.patch_source.stop)
        self.patch_platform = patch.object(installer.sys, 'platform', 'darwin'); self.patch_platform.start(); self.addCleanup(self.patch_platform.stop)
        with contextlib.redirect_stdout(io.StringIO()): installer.install(self.app,self.bin,self.config,self.native)
        self.env = dict(os.environ, HOME=str(self.root), CLAUDE_POOL_CONFIG=str(self.config), PYTHONDONTWRITEBYTECODE='1')

    def change_source(self):
        path = self.source/'gateway/launch.py'
        path.write_text(path.read_text().replace('claude-pool [account-alias]', 'claude-pool new-release [account-alias]'))

    def apply(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.install(self.app,self.bin,self.config,self.native,update=True)
        return output.getvalue()

    def current(self): return (self.app/'current').resolve() if (self.app/'current').exists() else self.app

    def command(self,*args):
        return subprocess.run([str(self.bin/'claude-pool'),*args],env=self.env,capture_output=True,text=True,check=True)

    def test_new_commands_change_and_busy_old_worker_keeps_its_files(self):
        resource=self.app/'gateway/worker-data';resource.write_text('old')
        worker=self.app/'gateway/fixture-worker.py'
        worker.write_text('import sys,time\nfrom pathlib import Path\np=Path(__file__).with_name("worker-data")\nprint(p.read_text(),flush=True)\nsys.stdin.readline()\nprint(p.read_text(),flush=True)\n')
        process=subprocess.Popen([sys.executable,str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'old')
            old_bytes=(self.app/'gateway/launch.py').read_bytes(); config=self.config.read_bytes()
            self.change_source();self.apply()
            self.assertIsNone(process.poll())
            self.assertEqual((self.app/'gateway/launch.py').read_bytes(),old_bytes)
            self.assertEqual(self.config.read_bytes(),config)
            self.assertIn('new-release',self.command('help').stdout)
            process.stdin.write('continue\n');process.stdin.flush()
            out,err=process.communicate(timeout=5)
            self.assertEqual(process.returncode,0,err);self.assertEqual(out.strip(),'old')
        finally:
            if process.poll() is None: process.terminate();process.wait(timeout=5)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def test_update_is_repeatable_and_does_not_call_service_controls(self):
        self.change_source();self.apply();first=self.current()
        with patch.object(installer.os,'kill',side_effect=AssertionError('No process signals allowed')):
            self.assertIn('already up to date',self.apply())
        self.assertEqual(first,self.current())
        self.assertEqual(len(list((self.app/'versions').glob('[!.]*'))),1)

    def test_stable_service_entry_survives_release_switch(self):
        def definition(folder):
            code='import json,sys;sys.path.insert(0,sys.argv[1]);import launch;print(json.dumps(launch.service_definition()))'
            return json.loads(subprocess.check_output([sys.executable,'-c',code,str(folder/'gateway')],env=self.env,text=True))
        before=definition(self.app);self.change_source();self.apply();after=definition(self.current())
        self.assertEqual(before,after)
        self.assertEqual(before['ProgramArguments'][-2:], [str(self.app/'run.py'),'--daemon'])
        self.assertEqual(before['WorkingDirectory'],str(self.app))

    def test_changed_mod_gets_new_path_and_keeps_previous_loaded_files(self):
        value=json.loads(self.config.read_text());value['state_dir']=str(self.root/'state');self.config.write_text(json.dumps(value))
        def plugin(folder):
            code='import sys;sys.path.insert(0,sys.argv[1]);import launch;print(launch.configured_plugin("cockpit"))'
            return Path(subprocess.check_output([sys.executable,'-c',code,str(folder/'gateway')],env=self.env,text=True).strip())
        old=plugin(self.app);before=(old/'hooks/register.tsx').read_bytes()
        source=self.source/'gateway/cockpit/hooks/register.tsx';source.write_text(source.read_text()+'\n// fixture update\n')
        self.apply();new=plugin(self.current())
        self.assertNotEqual(old,new);self.assertEqual((old/'hooks/register.tsx').read_bytes(),before)
        self.assertIn('fixture update',(new/'hooks/register.tsx').read_text())

    def test_failed_copy_and_failed_validation_leave_current_unchanged(self):
        self.change_source();self.apply();before=self.current();launcher=(self.bin/'claude-pool').read_bytes()
        with patch.object(installer.shutil,'copytree',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):self.apply()
        (self.source/'gateway/launch.py').write_text('invalid python !')
        with self.assertRaises(subprocess.CalledProcessError):self.apply()
        self.assertEqual(before,self.current());self.assertEqual(launcher,(self.bin/'claude-pool').read_bytes())
        self.assertEqual(list((self.app/'versions').glob('.staging-*')),[])

    def test_modified_launcher_is_never_replaced(self):
        launcher=self.bin/'claude-pool';launcher.write_text('user edit')
        with self.assertRaisesRegex(RuntimeError,'Launcher changed'):self.apply()
        self.assertEqual(launcher.read_text(),'user edit');self.assertFalse((self.app/'current').exists())

    def test_verified_same_backend_is_reused_without_build(self):
        binary=self.source/'gateway/bin/shunt';expected=binary.read_bytes();binary.unlink()
        with patch.object(installer.subprocess,'run',side_effect=AssertionError('No build needed')):
            installer.prepare_backend(self.app)
        self.assertEqual(binary.read_bytes(),expected)

    def test_corrupt_backend_or_changed_pin_never_reuses_binary(self):
        (self.source/'gateway/bin/shunt').unlink()
        (self.app/'gateway/bin/shunt').write_text('corrupted')
        with patch.object(installer.shutil,'which',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'build tools'):installer.prepare_backend(self.app)
        self.assertFalse((self.source/'gateway/bin/shunt').exists())
        (self.source/'backend/shunt.patch').write_text('unreviewed patch')
        with self.assertRaisesRegex(RuntimeError,'patch digest'):installer.prepare_backend(self.app)

    def test_cli_update_download_failure_leaves_install_unchanged(self):
        before=(self.bin/'claude-pool').read_bytes()
        with patch.object(update.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'git')):
            with self.assertRaisesRegex(RuntimeError,'Download failed'): update.cli([],self.app/'gateway')
        self.assertEqual(before,(self.bin/'claude-pool').read_bytes());self.assertFalse((self.app/'current').exists())

    def test_installer_lock_rejects_concurrent_update_before_build(self):
        lock=self.app.parent/('.'+self.app.name+'.install.lock')
        with lock.open('a') as file:
            fcntl.flock(file,fcntl.LOCK_EX)
            result=subprocess.run([sys.executable,str(self.source/'install.py'),'--bootstrap','--prefix',str(self.app)],env=self.env,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('already running',result.stderr)
        self.assertFalse((self.app/'current').exists())

    def test_retained_release_tampering_blocks_reactivation(self):
        self.change_source();self.apply();first=self.current()
        launch=self.source/'gateway/launch.py';launch.write_text(launch.read_text()+'\n# second update\n')
        self.apply();second=self.current()
        (first/'gateway/launch.py').write_text('tampered')
        launch.write_text(launch.read_text().replace('\n# second update\n',''))
        with self.assertRaisesRegex(RuntimeError,'Retained release changed'):self.apply()
        self.assertEqual(second,self.current())

    def test_one_command_bootstrap_then_installed_update_cli(self):
        tools=self.root/'tools';tools.mkdir()
        git=tools/'git'
        git.write_text('#!'+sys.executable+'\nimport os,shutil,sys\nshutil.copytree(os.environ["POOL_FIXTURE_SOURCE"],sys.argv[-1])\n')
        git.chmod(0o755)
        app=self.root/'fresh app';binaries=self.root/'fresh bin';config=self.root/'fresh config.json'
        env=self.env|{'POOL_FIXTURE_SOURCE':str(self.source),'PATH':str(tools)+os.pathsep+os.environ['PATH']}
        command=['sh',str(self.source/'install.sh'),'--prefix',str(app),'--bin-dir',str(binaries),'--config',str(config),'--native-claude',str(self.native)]
        first=subprocess.run(command,env=env,capture_output=True,text=True)
        self.assertEqual(first.returncode,0,first.stderr);self.assertIn('Installed to',first.stdout)
        self.change_source()
        second=subprocess.run([str(binaries/'claude-pool'),'update'],env=env,capture_output=True,text=True)
        self.assertEqual(second.returncode,0,second.stderr);self.assertIn('Updated Claude Pool',second.stdout)
        result=subprocess.run([str(binaries/'claude-pool'),'help'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('new-release',result.stdout)

    def test_update_command_is_reserved_before_any_login(self):
        import account_metadata
        self.assertIn('update',account_metadata.RESERVED)

    def test_legacy_install_adopts_runner_and_retains_old_gateway(self):
        receipt=json.loads((self.app/'installation.json').read_text())
        receipt.pop('root');receipt.pop('native')
        launcher=self.bin/'claude-pool'
        launcher.write_text(launcher.read_text().replace(str(self.app/'run.py'),str(self.app/'gateway/launch.py')))
        receipt['launchers'][str(launcher)]=hashlib.sha256(launcher.read_bytes()).hexdigest()
        (self.app/'installation.json').write_text(json.dumps(receipt));(self.app/'run.py').unlink()
        old=(self.app/'gateway/launch.py').read_bytes()
        self.change_source();self.apply()
        self.assertEqual(old,(self.app/'gateway/launch.py').read_bytes())
        self.assertIn('new-release',self.command('help').stdout)
        self.assertEqual(update.installed_root(self.current()/'gateway'),self.app)

    def test_legacy_service_file_is_prepared_without_reload_or_signals(self):
        service=self.root/'Library/LaunchAgents/io.github.claude-pool.plist';service.parent.mkdir(parents=True)
        service.write_bytes(plistlib.dumps({'Label':'io.github.claude-pool','ProgramArguments':[sys.executable,str(self.app/'gateway/launch.py'),'--daemon']}))
        self.change_source()
        with patch.object(installer.Path,'home',return_value=self.root),patch.object(installer.os,'kill',side_effect=AssertionError('No signals')):
            self.apply()
        self.assertEqual(plistlib.loads(service.read_bytes())['ProgramArguments'][-2:],[str(self.app/'run.py'),'--daemon'])

    def test_unrelated_service_file_is_not_overwritten(self):
        service=self.root/'Library/LaunchAgents/io.github.claude-pool.plist';service.parent.mkdir(parents=True)
        value=plistlib.dumps({'Label':'io.github.claude-pool','ProgramArguments':['python','/other/app/launch.py']});service.write_bytes(value)
        with patch.object(installer.Path,'home',return_value=self.root):
            with self.assertRaisesRegex(RuntimeError,'another installation'):self.apply()
        self.assertEqual(service.read_bytes(),value);self.assertFalse((self.app/'current').exists())

    def test_owned_development_service_keeps_label_without_reload(self):
        receipt=json.loads((self.app/'installation.json').read_text())
        old_entry=self.root/'development/gateway/launch.py'
        receipt.update(serviceLabel='example.dev.pool',legacyEntry=str(old_entry))
        (self.app/'installation.json').write_text(json.dumps(receipt))
        service=self.root/'Library/LaunchAgents/example.dev.pool.plist';service.parent.mkdir(parents=True)
        service.write_bytes(plistlib.dumps({'Label':'example.dev.pool','ProgramArguments':[sys.executable,str(old_entry),'--daemon']}))
        self.change_source()
        with patch.object(installer.Path,'home',return_value=self.root):self.apply()
        definition=plistlib.loads(service.read_bytes())
        self.assertEqual(definition['Label'],'example.dev.pool')
        self.assertEqual(definition['ProgramArguments'][-2:],[str(self.app/'run.py'),'--daemon'])
        self.assertEqual(definition['EnvironmentVariables']['CLAUDE_POOL_CONFIG'],str(self.config))
        code='import sys;sys.path.insert(0,sys.argv[1]);import launch;print(launch.SERVICE_LABEL)'
        label=subprocess.check_output([sys.executable,'-c',code,str(self.current()/'gateway')],env=self.env,text=True)
        self.assertEqual(label.strip(),'example.dev.pool')

    def test_invalid_service_label_cannot_escape_launchagent_folder(self):
        with self.assertRaisesRegex(RuntimeError,'service label'):installer.service_path({'serviceLabel':'../other'})

    def test_stream_in_progress_finishes_during_update(self):
        started,release=threading.Event(),threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length','12');self.end_headers()
                self.wfile.write(b'before');self.wfile.flush();started.set()
                if release.wait(10): self.wfile.write(b'after!');self.wfile.flush()
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
        try:
            connection.request('GET','/');response=connection.getresponse()
            self.assertEqual(response.read(6),b'before');self.assertTrue(started.is_set())
            self.change_source();self.apply();self.assertTrue(thread.is_alive())
            release.set();self.assertEqual(response.read(),b'after!')
            self.assertIn('new-release',self.command('help').stdout)
        finally:
            release.set();connection.close();server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
