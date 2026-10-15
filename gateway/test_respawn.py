"""Targeted respawn never touches the pool service or any other agent."""
import fcntl,io,json,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch
import launch

class RespawnTests(unittest.TestCase):
    def setUp(self):
        aliases = patch.object(launch, 'ACCOUNT_SHORTCUTS', {'secondary':'secondary'})
        aliases.start(); self.addCleanup(aliases.stop)

    def test_exact_target_and_one_delivery_preserve_other_tasks_and_native_permissions(self):
        session=str(uuid.uuid4());calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            if argv[1]=='agents':return SimpleNamespace(returncode=0,stdout=json.dumps([{'id':'deadbeef','kind':'background','sessionId':session},{'id':'cafebabe','kind':'background','sessionId':str(uuid.uuid4())}]))
            if argv[1]=='respawn':return SimpleNamespace(returncode=0)
            self.assertEqual(argv[argv.index('--tools')+1],'SendMessage')
            self.assertIn('--no-session-persistence',argv);self.assertIn('--strict-mcp-config',argv)
            self.assertEqual(argv[argv.index('--permission-mode')+1],'default')
            self.assertEqual(argv[argv.index('--allowedTools')+1],'SendMessage')
            env=kwargs['env']
            for key in ['ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_BASE_URL','CLAUDE_CODE_USE_BEDROCK']:self.assertNotIn(key,env)
            self.assertEqual(env['COCKPIT_RESPAWN_SESSION'],session)
            Path(env['COCKPIT_RESPAWN_RESULT']).write_text(json.dumps({'delivered':True,'session':session}))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp,patch.object(launch,'RUNTIME',Path(tmp)),patch.object(launch.subprocess,'run',side_effect=run),patch.object(launch,'ensure_pool') as pool,redirect_stdout(io.StringIO()):
            launch.respawn_agent(['deadbeef']);pool.assert_not_called()
        self.assertEqual([a[1] for a in calls],['agents','respawn','agents','-p'])
        self.assertEqual(calls[1],[launch.NATIVE_CLAUDE,'respawn','deadbeef'])

    def test_ambiguous_bulk_and_service_flags_are_refused_before_any_action(self):
        for argv in [['respawn','--all'],['respawn','--stop'],['respawn','friendly-name'],['respawn','deadbeef','cafebabe'],['secondary','respawn','deadbeef']]:
            with patch.object(launch.sys,'argv',['claude',*argv]),patch.object(launch.subprocess,'run') as native,patch.object(launch,'ensure_pool') as pool:
                with self.assertRaises(RuntimeError):launch.main()
                native.assert_not_called();pool.assert_not_called()

    def test_failed_unknown_or_changed_identity_never_sends_continuation(self):
        session=str(uuid.uuid4())
        def inventory(s=session):return SimpleNamespace(returncode=0,stdout=json.dumps([{'id':'deadbeef','kind':'background','sessionId':s}]))
        for results in [[SimpleNamespace(returncode=0,stdout='[]')],[inventory('invalid')],[inventory(),SimpleNamespace(returncode=1)],[inventory(),SimpleNamespace(returncode=0),inventory(str(uuid.uuid4()))]]:
            with tempfile.TemporaryDirectory() as tmp,patch.object(launch,'RUNTIME',Path(tmp)),patch.object(launch.subprocess,'run',side_effect=results) as native:
                with self.assertRaises(RuntimeError):launch.respawn_agent(['deadbeef'])
                self.assertTrue(all(c.args[0][1]!='-p' for c in native.call_args_list))

    def test_unconfirmed_delivery_is_not_retried(self):
        session=str(uuid.uuid4());inventory=SimpleNamespace(returncode=0,stdout=json.dumps([{'id':'deadbeef','kind':'background','sessionId':session}]))
        with tempfile.TemporaryDirectory() as tmp,patch.object(launch,'RUNTIME',Path(tmp)),patch.object(launch.subprocess,'run',side_effect=[inventory,SimpleNamespace(returncode=0),inventory,SimpleNamespace(returncode=1)]) as native:
            with self.assertRaisesRegex(RuntimeError,'unconfirmed'):launch.respawn_agent(['deadbeef'])
            self.assertEqual(native.call_count,4)

    def test_simultaneous_respawn_of_same_agent_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(launch,'RUNTIME',Path(tmp)),patch.object(launch.subprocess,'run') as native:
            with (Path(tmp)/'respawn-deadbeef.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError,'already'):launch.respawn_agent(['deadbeef'])
                native.assert_not_called()

if __name__=='__main__':unittest.main()
