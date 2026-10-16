"""Token storage checks use a temporary private directory."""
import os
from pathlib import Path
import tempfile
import unittest
import json
import io
import uuid
from contextlib import redirect_stdout
from unittest.mock import patch

import launch

def routing_args(argv):
    assert argv[1] == '--plugin-dir'
    assert json.loads((Path(argv[2]) / '.claude-plugin/plugin.json').read_text())['name'] == 'cockpit'
    return [argv[0], *argv[3:]]


class LauncherTests(unittest.TestCase):
    def test_claude_adapter_preserves_native_commands_without_pool_access(self):
        from account_metadata import NATIVE_COMMANDS
        for command in sorted(NATIVE_COMMANDS):
            with self.subTest(command=command), patch.object(launch.sys,'argv',['claude-pool','exec',command,'--help']), \
                 patch.object(launch.subprocess,'call',return_value=7) as native, patch.object(launch,'token') as token, \
                 patch.object(launch,'ensure_pool') as pool, patch('update.cli') as updater:
                with self.assertRaises(SystemExit) as result:launch.main()
                self.assertEqual(result.exception.code,7)
                native.assert_called_once_with([launch.NATIVE_CLAUDE,command,'--help'])
                token.assert_not_called();pool.assert_not_called();updater.assert_not_called()

    def test_pool_update_is_explicit_and_never_runs_native_updater(self):
        for arguments in [['update'],['pool','update'],['exec','pool','update']]:
            with self.subTest(arguments=arguments), patch.object(launch.sys,'argv',['claude-pool',*arguments]), \
                 patch('update.cli',return_value=0) as updater,patch.object(launch.subprocess,'call') as native,patch.object(launch,'token') as token:
                with self.assertRaises(SystemExit) as result:launch.main()
                self.assertEqual(result.exception.code,0)
                updater.assert_called_once_with([],launch.HERE);native.assert_not_called();token.assert_not_called()

    def test_native_help_explains_both_updaters_without_changing_version_output(self):
        for flag in ['--help','-h','--version']:
            with patch.object(launch.sys,'argv',['claude-pool','exec',flag]),patch.object(launch.subprocess,'call',return_value=0) as native,redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit):launch.main()
                native.assert_called_once_with([launch.NATIVE_CLAUDE,flag])
                if flag=='--version':self.assertEqual(output.getvalue(),'')
                else:self.assertIn('claude pool update',output.getvalue());self.assertIn('claude update',output.getvalue())

    def test_update_in_a_prompt_is_not_an_updater_command(self):
        for arguments in [['-p','update'],['--','update'],['--model','claude-opus-5-5','--effort','high','-p','update']]:
            with tempfile.TemporaryDirectory() as folder,patch.object(launch,'RUNTIME',Path(folder)), \
                 patch.object(launch.sys,'argv',['claude-pool','exec',*arguments]),patch.object(launch,'token',return_value='fixture'), \
                 patch.object(launch,'ensure_pool') as pool,patch('update.cli') as updater,patch.object(launch.subprocess,'call',return_value=0) as native:
                with self.assertRaises(SystemExit):launch.main()
                pool.assert_called_once();updater.assert_not_called()
                self.assertEqual(native.call_args.args[0][-len(arguments):],arguments)
                self.assertEqual(native.call_args.kwargs['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'],'claude-opus-5-5')

    def test_native_help_and_version_work_without_starting_the_pool(self):
        for flag in ['--help', '-h', '--version']:
            with patch.object(launch.sys, 'argv', ['claude-pool', 'exec', flag]), patch.object(launch.subprocess, 'call', return_value=7) as native, patch.object(launch, 'token') as token, patch.object(launch, 'ensure_pool') as start:
                with self.assertRaises(SystemExit) as result: launch.main()
                self.assertEqual(result.exception.code, 7)
                native.assert_called_once_with([launch.NATIVE_CLAUDE, flag]); token.assert_not_called(); start.assert_not_called()

    def test_pool_help_lists_its_own_commands_without_native_or_pool_access(self):
        for arguments in [['--help'],['help'],['exec','pool','--help']]:
            with patch.object(launch.sys,'argv',['claude-pool',*arguments]),patch.object(launch.subprocess,'call') as native,patch.object(launch,'token') as token,redirect_stdout(io.StringIO()) as output:
                launch.main()
                self.assertIn('claude-pool update',output.getvalue())
                self.assertIn('claude update = Claude Code',output.getvalue())
                native.assert_not_called();token.assert_not_called()

    def setUp(self):
        self.aliases = patch.object(launch, "ACCOUNT_SHORTCUTS", {"personal":"personal", "work":"work", "studio":"studio", "secondary":"secondary", "backup":"backup"})
        self.aliases.start(); self.addCleanup(self.aliases.stop)
    def test_cockpit_and_bootstrap_share_the_reviewed_suite_limits_hook(self):
        self.assertEqual((launch.HERE/'background-bootstrap/hooks/limits.ts').read_bytes(), (launch.HERE/'cockpit/hooks/limits.ts').read_bytes())
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)):
            plugin=launch.background_plugin(None)
            self.assertEqual((plugin/'hooks/limits.ts').read_bytes(), (launch.HERE/'background-bootstrap/hooks/limits.ts').read_bytes())

    def test_session_opus_default_survives_dispatch_without_changing_global_settings(self):
        cases=[([],{},'claude-opus-5-5',None),(['--model','claude-opus-5-5'],{'ANTHROPIC_DEFAULT_OPUS_MODEL':'claude-opus-4-8'},'claude-opus-5-5','claude-opus-5-5'),
               (['--model=claude-opus-4-8'],{},'claude-opus-4-8','claude-opus-4-8'),(['--model','sonnet'],{'ANTHROPIC_DEFAULT_OPUS_MODEL':'custom-opus'},'custom-opus','sonnet'),
               (['--model','claude-opus-4-8','--model=claude-opus-5-5'],{},'claude-opus-5-5','claude-opus-5-5'),(['--','--model','claude-opus-4-8'],{},'claude-opus-5-5',None)]
        for alias in [None,*launch.ACCOUNT_SHORTCUTS]:
            for background in [False,True]:
                for options,env,expected,chosen in cases:
                    with tempfile.TemporaryDirectory() as folder,patch.object(launch,'RUNTIME',Path(folder)),patch.dict(launch.os.environ,env,clear=True), \
                         patch.object(launch,'token',return_value='fixture'),patch.object(launch,'ensure_pool'),patch.object(launch,'request',return_value=json.dumps({'mode':'pinned','pin':launch.ACCOUNT_SHORTCUTS.get(alias)}).encode()), \
                         patch.object(launch.subprocess,'call',return_value=0) as native,patch.object(launch.sys,'argv',['claude',*([alias] if alias else []),*(['--bg'] if background else []),*options]):
                        with self.assertRaises(SystemExit):launch.main()
                        self.assertEqual(native.call_args.kwargs['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'],expected)
                        self.assertEqual(native.call_args.kwargs['env'].get('ANTHROPIC_MODEL'),chosen)
                        self.assertEqual(dict(launch.os.environ),env)

    def test_native_tool_continuity_defaults_reach_resume_and_background_without_overriding_users(self):
        defaults={'ENABLE_TOOL_SEARCH':'force','CLAUDE_CODE_INLINE_TOOLS':'1','CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM':'1'}
        for options in [[],['--resume','saved-session'],['--continue'],['--bg']]:
            for overrides in [{},{key:'0' for key in defaults},{'ENABLE_TOOL_SEARCH':'auto'}]:
                with self.subTest(options=options,overrides=overrides), tempfile.TemporaryDirectory() as folder, \
                     patch.object(launch,'RUNTIME',Path(folder)),patch.dict(launch.os.environ,overrides,clear=True), \
                     patch.object(launch,'token',return_value='fixture'),patch.object(launch,'ensure_pool'), \
                     patch.object(launch.subprocess,'call',return_value=0) as native,patch.object(launch.sys,'argv',['claude',*options]):
                    with self.assertRaises(SystemExit):launch.main()
                    env=native.call_args.kwargs['env']
                    self.assertEqual({key:env[key] for key in defaults},{**defaults,**overrides})
                    self.assertEqual(dict(launch.os.environ),overrides)

    def test_foreground_origins_are_unique_and_headless_calls_keep_their_arguments(self):
        paths=[]
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
             patch.object(launch,'token',return_value='fixture'), patch.object(launch,'ensure_pool'), \
             patch.object(launch,'request',return_value=b'{"mode":"pinned","pin":"secondary"}'), patch.object(launch.subprocess,'call',return_value=0) as native:
            for alias in [None,'secondary',None,'secondary']:
                with patch.object(launch.sys,'argv',['claude',*([alias] if alias else []),'--settings','user.json','--plugin-dir','user-plugin']):
                    with self.assertRaises(SystemExit):launch.main()
                    argv=routing_args(native.call_args.args[0]);path=next(Path(argv[i+1]) for i, value in enumerate(argv[:-1]) if value == '--plugin-dir' and (Path(argv[i+1])/'hooks/account.ts').exists());paths.append(path)
                    text=(path/'hooks/account.ts').read_text()
                    origin=json.loads(text.split('export const origin: string | null = ')[1])
                    uuid.UUID(origin)
                    self.assertIn('--settings',argv);self.assertIn('user-plugin',argv)
            self.assertEqual(len(set(paths)),4)
            for flag in ['-p','--print']:
                with patch.object(launch.sys,'argv',['claude',flag,'hello']):
                    with self.assertRaises(SystemExit):launch.main()
                    self.assertEqual(routing_args(native.call_args.args[0])[3:],[flag,'hello'])
                    self.assertEqual(routing_args(native.call_args.args[0])[1], '--plugin-dir')

    def test_background_shortcuts_bootstrap_the_actual_worker_without_a_guessed_uuid(self):
        for shortcut in [None, *launch.ACCOUNT_SHORTCUTS]:
            for flag in ['--bg', '--background']:
                with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
                     patch.object(launch,'token',return_value='fixture'), patch.object(launch,'ensure_pool'), \
                     patch.object(launch,'request') as request, patch.object(launch.subprocess,'call',return_value=0) as native, \
                     patch.object(launch.sys,'argv',['claude',*([shortcut] if shortcut else []),flag,'--settings','user.json','--plugin-dir','user-plugin','--','public prompt']):
                    with self.assertRaises(SystemExit):launch.main()
                    argv = routing_args(native.call_args.args[0])
                    self.assertNotIn('--session-id',argv); request.assert_not_called()
                    self.assertEqual(argv[3:],[flag,'--settings','user.json','--plugin-dir','user-plugin','--','public prompt'])
                    plugin = Path(argv[2])
                    expected = launch.ACCOUNT_SHORTCUTS.get(shortcut)
                    self.assertEqual((plugin/'hooks/account.ts').read_text(),'export const account: string | null = '+json.dumps(expected)+'\nexport const origin: string | null = null\n')
                    self.assertTrue((plugin/'hooks/register.ts').exists())
                    self.assertEqual(launch.background_plugin(expected),plugin)
                    (plugin/'hooks/account.ts').write_text('different')
                    with self.assertRaisesRegex(RuntimeError,'version'):launch.background_plugin(expected)

    def test_background_modes_that_disable_the_bootstrap_are_refused_before_start(self):
        for flag in ['--safe-mode','--bare']:
            with patch.object(launch.sys,'argv',['claude','secondary','--bg',flag]), patch.object(launch,'token') as token:
                with self.assertRaisesRegex(SystemExit,'routing bootstrap'):launch.main()
                token.assert_not_called()

    def test_resumed_background_shortcuts_keep_native_forking_for_every_account(self):
        for shortcut in launch.ACCOUNT_SHORTCUTS:
            for options in [['--continue'], ['--resume','source-id'], ['--resume','source-id','--fork-session']]:
                with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
                     patch.object(launch,'token',return_value='fixture'), patch.object(launch,'ensure_pool'), \
                     patch.object(launch,'request') as request, patch.object(launch.subprocess,'call',return_value=0) as native, \
                     patch.object(launch.sys,'argv',['claude',shortcut,'--bg',*options]):
                    with self.assertRaises(SystemExit):launch.main()
                    argv=routing_args(native.call_args.args[0])
                    self.assertEqual(argv.count('--fork-session'),1)
                    self.assertNotIn('--session-id',argv);request.assert_not_called()

    def test_account_shortcuts_pin_unique_sessions_before_start_without_global_changes(self):
        states = {'existing-session':{'mode':'pinned','pin':'gmail'}}
        launches = []
        def request(client, method='GET', path='/health', body=None):
            if method == 'POST':
                self.assertEqual(body['action'],'pin')
                key = path.removeprefix('/cockpit/')
                uuid.UUID(key)
                self.assertNotIn(key,states)
                states[key] = {'mode':'pinned','pin':body['account']}
                return json.dumps(states[key]).encode()
            return b'{}'
        def native(argv, env):
            argv = routing_args(argv)
            key = argv[argv.index('--session-id')+1]
            self.assertEqual(states[key]['mode'],'pinned')
            self.assertEqual(argv[3],'--plugin-dir')
            self.assertEqual(argv[5:],['--model','opus','--','hello'])
            self.assertNotIn('ANTHROPIC_AUTH_TOKEN',env)
            launches.append(key)
            return 0
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
             patch.object(launch,'token',return_value='fixture'), patch.object(launch,'request',side_effect=request), \
             patch.object(launch.subprocess,'call',side_effect=native):
            for shortcut, account in launch.ACCOUNT_SHORTCUTS.items():
                with patch.object(launch.sys,'argv',['claude',shortcut,'--model','opus','--','hello']):
                    with self.assertRaises(SystemExit) as result:launch.main()
                    self.assertEqual(result.exception.code,0)
        self.assertEqual(len(set(launches)),len(launch.ACCOUNT_SHORTCUTS))
        self.assertEqual([states[key]['pin'] for key in launches],list(launch.ACCOUNT_SHORTCUTS.values()))
        self.assertEqual(states['existing-session'],{'mode':'pinned','pin':'gmail'})

    def test_resumed_shortcuts_fork_and_pin_only_their_new_session_for_all_configured_accounts(self):
        variants = [['--continue'],['-c'],['--resume','saved-session'],['-r','saved-session'],['-rsaved-session'],
                    ['--resume=saved-session'],['--resume'],['-r'],['--continue','--fork-session']]
        for shortcut, account in launch.ACCOUNT_SHORTCUTS.items():
            for options in variants:
                source = {'mode':'pinned','pin':'gmail'}
                states = {'saved-session':dict(source)}
                def request(client, method='GET', path='/health', body=None):
                    if method == 'POST':
                        key = path.removeprefix('/cockpit/')
                        self.assertNotEqual(key,'saved-session')
                        states[key] = {'mode':'pinned','pin':body['account']}
                        return json.dumps(states[key]).encode()
                    return b'{}'
                with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
                     patch.object(launch,'token',return_value='fixture'), patch.object(launch,'request',side_effect=request), \
                     patch.object(launch.subprocess,'call',return_value=0) as native, \
                     patch.object(launch.sys,'argv',['claude',shortcut,*options,'--','continue the task']):
                    with self.assertRaises(SystemExit):launch.main()
                    argv = routing_args(native.call_args.args[0])
                    self.assertEqual(argv.count('--fork-session'),1)
                    key = argv[argv.index('--session-id')+1]
                    uuid.UUID(key)
                    self.assertEqual(states[key],{'mode':'pinned','pin':account})
                    self.assertEqual(states['saved-session'],source)
                    self.assertEqual(argv[3],'--plugin-dir')
                    remaining = [a for a in argv[5:] if a != '--fork-session']
                    self.assertEqual(remaining,[a for a in options if a != '--fork-session']+['--','continue the task'])

    def test_shortcuts_refuse_explicit_session_ids_before_any_state_change(self):
        for option in ['--session-id','--session-id=existing']:
            with patch.object(launch.sys,'argv',['claude','secondary',option]), patch.object(launch,'token') as token, patch.object(launch.subprocess,'call') as native:
                with self.assertRaisesRegex(SystemExit,'own session ID'):launch.main()
                token.assert_not_called();native.assert_not_called()

    def test_resume_words_in_literal_prompts_do_not_fork(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
             patch.object(launch,'token',return_value='fixture'), patch.object(launch,'request',return_value=b'{"mode":"pinned","pin":"secondary"}'), \
             patch.object(launch.subprocess,'call',return_value=0) as native, patch.object(launch.sys,'argv',['claude','secondary','--','--resume']):
            with self.assertRaises(SystemExit):launch.main()
            self.assertNotIn('--fork-session',routing_args(native.call_args.args[0]))

    def test_account_shortcuts_cannot_stop_or_restart_other_sessions_backend(self):
        for option in ['--stop','--start','--daemon']:
            with patch.object(launch.sys,'argv',['claude','secondary',option]), patch.object(launch,'token') as token:
                with self.assertRaisesRegex(SystemExit,'cannot control'):launch.main()
                token.assert_not_called()

    def test_shortcut_pin_failure_does_not_launch_an_unpinned_task(self):
        for response in [b'{"mode":"auto"}',b'[]',b'not json',b'{"mode":"pinned","pin":"secondary"}']:
            with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
                 patch.object(launch,'token',return_value='fixture'), patch.object(launch,'ensure_pool'), \
                 patch.object(launch,'request',return_value=response), \
                 patch.object(launch.sys,'argv',['claude','backup']), patch.object(launch.subprocess,'call') as native:
                with self.assertRaisesRegex(RuntimeError,'pin'):launch.main()
                native.assert_not_called()

    def test_literal_account_prompt_after_separator_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
             patch.object(launch,'token',return_value='fixture'), patch.object(launch,'ensure_pool'), \
             patch.object(launch,'request') as request, patch.object(launch.sys,'argv',['claude','--','secondary']), \
             patch.object(launch.subprocess,'call',return_value=0) as native:
            with self.assertRaises(SystemExit):launch.main()
            self.assertEqual(routing_args(native.call_args.args[0])[3:],['--','secondary'])
            request.assert_not_called()

    def test_default_wrapper_cannot_recurse_and_preserves_arguments_and_exit_code(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(launch, 'RUNTIME', Path(folder)), \
             patch.object(launch, 'token', return_value='fixture-client'), \
             patch.object(launch, 'request', return_value=b'{}'), \
             patch.object(launch.sys, 'argv', ['claude', '--model', 'opus', '--continue']), \
             patch.object(launch.subprocess, 'call', return_value=17) as call:
            with self.assertRaises(SystemExit) as result:
                launch.main()
            self.assertEqual(result.exception.code, 17)
            self.assertEqual(routing_args(call.call_args.args[0])[0],launch.NATIVE_CLAUDE)
            self.assertEqual(routing_args(call.call_args.args[0])[1],'--plugin-dir')
            self.assertEqual(routing_args(call.call_args.args[0])[3:],['--model','opus','--continue'])
            self.assertEqual(call.call_args.kwargs['env']['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:18341')
            self.assertNotIn('ANTHROPIC_AUTH_TOKEN',call.call_args.kwargs['env'])
            self.assertNotIn('ANTHROPIC_API_KEY',call.call_args.kwargs['env'])
            self.assertEqual(call.call_args.kwargs['env']['COCKPIT_TOKEN_FILE'],str(launch.STORES/'.pool-client-token'))

class CommandTests(unittest.TestCase):
    def test_forecast_check_uses_validation_endpoint_without_native_claude(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
             patch.object(launch,'token',return_value='fixture'), patch.object(launch,'ensure_pool'), \
             patch.object(launch,'request',return_value=b'{}') as request, \
             patch.object(launch,'validation_view',return_value='validation result'), \
             patch.object(launch.sys,'argv',['claude','forecast-check']), patch.object(launch.subprocess,'call') as native:
            with redirect_stdout(io.StringIO()) as out: launch.main()
            self.assertEqual(out.getvalue(), 'validation result\n'); native.assert_not_called()
            request.assert_called_once_with('fixture',path='/pool/validation')
    def test_usage_and_history_never_launch_native_claude(self):
        data={'ready':0,'accounts':[],'updatedAt':0,'forecast':{'seconds':None,'eligibleAccounts':0,'estimatedAccounts':0,'partial':False,'nextReset':None},'history':[]}
        for command in [['usage'],['usage','--once'],['history']]:
            with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
                 patch.object(launch,'token',return_value='fixture'), patch.object(launch,'request',return_value=json.dumps(data).encode()), \
                 patch.object(launch.sys,'argv',['claude',*command]), patch.object(launch.subprocess,'call') as native:
                with redirect_stdout(io.StringIO()) as out:launch.main()
                self.assertIn('Claude pool',out.getvalue());native.assert_not_called()
    def test_usage_color_respects_terminal_no_color_and_redirects(self):
        for tty, env, expected in [(True, {'TERM':'xterm-256color'}, True), (True, {'TERM':'xterm-256color','NO_COLOR':''}, False), (True, {'TERM':'dumb'}, False), (False, {'TERM':'xterm-256color'}, False)]:
            with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), \
                 patch.object(launch,'token',return_value='fixture'), patch.object(launch,'request',return_value=b'{}'), \
                 patch.object(launch.sys,'argv',['claude','usage']), patch.dict(os.environ,env,clear=True), \
                 patch.object(launch.sys.stdout,'isatty',return_value=tty), patch.object(launch,'usage',return_value='view') as render, patch.object(launch.time,'sleep',side_effect=KeyboardInterrupt):
                launch.main()
                self.assertEqual(render.call_args.kwargs['color'],expected)

    def test_orphan_cleanup_refuses_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)):
            (Path(folder)/'owner.json').write_text(json.dumps({'daemon':9999,'backend':8888}))
            with patch.object(launch,'process_command',return_value='/unrelated/user/process'),patch.object(launch.os,'kill') as kill:
                self.assertFalse(launch.recover_orphan());kill.assert_not_called()

class TokenTests(unittest.TestCase):
    def test_private_token_is_stable_and_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch, 'STORES', Path(folder)):
            value = launch.token('client')
            self.assertEqual(launch.token('client'), value)
            self.assertEqual((Path(folder)/'client').stat().st_mode & 0o777, 0o600)
            (Path(folder)/'alias').symlink_to('client')
            with self.assertRaises(OSError): launch.token('alias')
    def test_unsafe_or_incomplete_existing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch, 'STORES', Path(folder)):
            path = Path(folder)/'client'
            path.write_text('short')
            path.chmod(0o600)
            with self.assertRaises(RuntimeError): launch.token('client')
            path.write_text('x'*48)
            path.chmod(0o644)
            with self.assertRaises(RuntimeError): launch.token('client')

if __name__ == '__main__': unittest.main()
