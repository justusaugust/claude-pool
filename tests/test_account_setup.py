"""Replay account setup failures offline, including the real child-PTY handoff."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import account_login
import account_metadata
import accounts_cli as setup


class AccountSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root/'credentials'; self.stores.mkdir(mode=0o700)
        self.runtime = self.root/'runtime'; self.runtime.mkdir()
        self.config = self.root/'config.json'; self.config.write_text('{"accounts": []}')
        self.backend = self.runtime/'shunt.toml'
        self.backend.write_text('[providers.anthropic]\nauth="claude_oauth"\naccounts=[]\n')
        self.binary = self.root/'fake-shunt'
        self.binary.write_text('#!'+sys.executable+'''\nimport os,sys,json,getpass
from pathlib import Path
if 'check' in sys.argv: sys.exit(0)
assert '--manual' in sys.argv, 'The historical loopback redirect would fail'
name=sys.argv[sys.argv.index('--name')+1]
print('https://claude.com/cai/oauth/authorize?state=fixture-state&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback',flush=True)
value=getpass.getpass('Paste authorization code (input hidden): ')
assert value=='fixture-code#fixture-state'
print('DO-NOT-LOG-FIXTURE-TOKEN '+value,flush=True)
p=Path(os.environ['SHUNT_CLAUDE_ACCOUNTS_DIR'])/(name+'.json')
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:json.dump({'claudeAiOauth':{'accessToken':'fixture-access','refreshToken':'fixture-refresh'},'shuntAccountUuid':'user-a','shuntOrganizationUuid':'team-a'},f)
''')
        self.binary.chmod(0o700)

    def stored(self, name='operations', user='user-a', org='team-a'):
        p=self.stores/(name+'.json')
        p.write_text(json.dumps({'claudeAiOauth':{'accessToken':'fixture-access','refreshToken':'fixture-refresh'},
                                'shuntAccountUuid':user,'shuntOrganizationUuid':org}))
        p.chmod(0o600)
        return p

    def info(self, email='operations@example.test', org='team-a', organization='Example Team'):
        return {'email':email,'organization':organization,'plan':'Team Premium','userId':'user-a','organizationId':org}

    def run_cli(self, *extra, overview=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result=setup.cli(['add','operations@example.test','--name','operations',*extra],config=self.config,
                stores=self.stores,binary=self.binary,runtime=self.runtime,overview=overview)
        return result,out.getvalue()

    def test_real_pty_manual_flow_hides_code_and_raw_child_output(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            account_login.login(self.binary,self.stores,'operations','Operations',
                                code_reader=lambda label:'fixture-code#fixture-state',timeout=5)
        self.assertTrue((self.stores/'operations.json').is_file())
        self.assertNotIn('fixture-code',out.getvalue())
        self.assertNotIn('DO-NOT-LOG',out.getvalue())

    def test_ssh_login_on_linux_never_hands_the_url_to_an_invisible_display(self):
        probe=self.root/'env-probe'
        self.binary.write_text('#!'+sys.executable+'''\nimport os,sys,json
from pathlib import Path
Path(sys.argv[-1]).write_text(json.dumps({k:os.environ.get(k) for k in ['DISPLAY','WAYLAND_DISPLAY','BROWSER','SSH_CONNECTION']}))
name=sys.argv[sys.argv.index('--name')+1]
print('https://claude.com/cai/oauth/authorize?state=fixture-state',flush=True)
print('Paste authorization code (input hidden): ',flush=True); sys.stdin.readline()
p=Path(os.environ['SHUNT_CLAUDE_ACCOUNTS_DIR'])/(name+'.json')
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:json.dump({'claudeAiOauth':{'accessToken':'a','refreshToken':'r'}},f)
''')
        for platform,ssh,expect_display in [('linux','203.0.113.1 1 203.0.113.2 22',None),('linux',None,':1'),('darwin','203.0.113.1 1 203.0.113.2 22',':1')]:
            with self.subTest(platform=platform,ssh=ssh):
                (self.stores/'operations.json').unlink(missing_ok=True)
                env={'DISPLAY':':1','WAYLAND_DISPLAY':'wayland-0'}
                if ssh: env['SSH_CONNECTION']=ssh
                with patch.dict(os.environ,env,clear=False),patch('account_login.sys.platform',platform),contextlib.redirect_stdout(io.StringIO()):
                    if not ssh: os.environ.pop('SSH_CONNECTION',None); os.environ.pop('SSH_TTY',None)
                    account_login.login(self.binary,self.stores,'operations','Operations',code_reader=lambda _:'x#fixture-state',timeout=5,extra_args=[str(probe)])
                seen=json.loads(probe.read_text())
                self.assertEqual(seen['DISPLAY'],expect_display)
                self.assertEqual(seen['WAYLAND_DISPLAY'],None if expect_display is None else 'wayland-0')

    def test_wrong_state_reprompt_does_not_replace_login(self):
        codes=iter(['wrong#another-state','fixture-code#fixture-state'])
        with contextlib.redirect_stdout(io.StringIO()):
            account_login.login(self.binary,self.stores,'operations','Operations',code_reader=lambda _:next(codes),timeout=5)
        self.assertTrue((self.stores/'operations.json').exists())

    def test_cancel_reaps_only_its_owned_oauth_child(self):
        with patch('account_login.os.kill',wraps=os.kill) as kill, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'cancel'):
                account_login.login(self.binary,self.stores,'operations','Operations',
                    code_reader=lambda _:(_ for _ in ()).throw(RuntimeError('cancel')),timeout=5)
        pid,sig=kill.call_args.args
        self.assertEqual(sig,signal.SIGTERM)
        with self.assertRaises(ChildProcessError): os.waitpid(pid,os.WNOHANG)
        self.assertFalse((self.stores/'operations.json').exists())

    def test_existing_credential_is_never_overwritten(self):
        p=self.stored();before=p.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'already exists'):
            account_login.login(self.binary,self.stores,'operations','Operations')
        self.assertEqual(before,p.read_bytes())

    def test_private_dialog_has_hidden_input(self):
        class Result: returncode=0;stdout='fixture-code#fixture-state\n'
        with patch('account_login.sys.platform','darwin'),patch('account_login.shutil.which',return_value='/usr/bin/osascript'),patch('account_login.subprocess.run',return_value=Result()) as run:
            self.assertEqual(account_login.private_code('Operations'),'fixture-code#fixture-state')
        self.assertIn('with hidden answer',run.call_args.args[0][2])
        self.assertNotIn('fixture-code',str(run.call_args))

    def test_bad_code_and_terminal_control_bytes_rejected(self):
        for code in ['missing-state','x#wrong','x\n#state','\x1b#state','x#state#extra', '\x7f#state', '\x85#state']:
            self.assertFalse(account_login.validate_code(code,'state'))
        self.assertTrue(account_login.validate_code('ok-code#state','state'))

    def test_wrong_email_never_registers_or_reopens_browser(self):
        self.stored();before=self.config.read_bytes()
        with patch.object(setup,'profile',return_value=self.info(email='someone-else@example.test')),patch.object(setup,'login') as login:
            with self.assertRaisesRegex(RuntimeError,'Signed in as'):self.run_cli()
        self.assertEqual(before,self.config.read_bytes());login.assert_not_called()

    def test_wrong_organization_never_registers(self):
        self.stored()
        with patch.object(setup,'profile',return_value=self.info(organization='Personal')):
            with self.assertRaisesRegex(RuntimeError,'Selected organization'):self.run_cli('--organization','Example Team')
        self.assertEqual(json.loads(self.config.read_text())['accounts'],[])

    def test_duplicate_real_identity_does_not_create_more_capacity(self):
        self.stored();self.stored('existing')
        self.config.write_text(json.dumps({'accounts':[{'name':'existing','label':'Existing'}]}))
        with patch.object(setup,'profile',return_value=self.info()):
            with self.assertRaisesRegex(RuntimeError,'already connected'):self.run_cli()
        self.assertEqual(len(json.loads(self.config.read_text())['accounts']),1)

    def test_same_user_personal_and_team_are_distinct(self):
        self.stored();self.stored('personal',org='personal-org')
        self.config.write_text(json.dumps({'accounts':[{'name':'personal','label':'Personal'}]}))
        with patch.object(setup,'profile',return_value=self.info()):code,out=self.run_cli()
        self.assertEqual(code,0);self.assertIn('Connected Operations',out)
        self.assertEqual(len(json.loads(self.config.read_text())['accounts']),2)

    def test_interrupted_after_oauth_resumes_without_new_auth_and_is_idempotent(self):
        self.stored();before=(self.stores/'operations.json').read_bytes()
        with patch.object(setup,'profile',side_effect=RuntimeError('network unavailable')):
            with self.assertRaisesRegex(RuntimeError,'network'):self.run_cli()
        with patch.object(setup,'profile',return_value=self.info()),patch.object(setup,'login') as login:
            self.run_cli('--label','Operations');self.run_cli('--label','Operations')
        login.assert_not_called()
        self.assertEqual((self.stores/'operations.json').read_bytes(),before)
        self.assertEqual(len(json.loads(self.config.read_text())['accounts']),1)
        self.assertEqual(self.backend.read_text().count('name = "operations"'),1)

    def test_rejected_backend_config_never_enrolls(self):
        self.stored();before=self.config.read_bytes();backend=self.backend.read_bytes()
        with patch.object(setup,'profile',return_value=self.info()),patch.object(setup.subprocess,'run') as run:
            run.return_value.returncode=1
            with self.assertRaisesRegex(RuntimeError,'Backend rejected'):self.run_cli()
        self.assertEqual(self.config.read_bytes(),before);self.assertEqual(self.backend.read_bytes(),backend)

    def test_not_ready_is_pending_then_resume_checks_live_status(self):
        self.stored()
        with patch.object(setup,'profile',return_value=self.info()),patch.object(setup.time,'monotonic',side_effect=[0,331]):
            code,out=self.run_cli(overview=lambda:{'accounts':[]})
        self.assertEqual(code,2);self.assertNotIn(' · ready',out)
        with patch.object(setup,'profile',return_value=self.info()):
            code,out=self.run_cli(overview=lambda:{'accounts':[{'name':'operations','status':'ready'}]})
        self.assertEqual(code,0);self.assertIn('Operations · ready',out)

    def test_unsafe_credential_and_reserved_alias_rejected(self):
        p=self.stored();p.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError,'private 0600'):setup.credential(p)
        p.chmod(0o600)
        with patch.object(setup,'profile') as profile:
            with self.assertRaisesRegex(RuntimeError,'unused shortcut'):self.run_cli('--alias','account')
        profile.assert_not_called()

    def test_metadata_live_labels_and_membership_preserve_other_fields(self):
        original=self.backend.read_text()
        candidate=setup.add_membership(original,'operations')
        self.assertEqual(candidate,setup.add_membership(candidate,'operations'))
        self.config.write_text(json.dumps({'accounts':[{'name':'operations','label':'Operations','plan':'Team Premium'}]}))
        row={'name':'operations','label':'operations','plan':'max 5x','fiveHour':None}
        account_metadata.decorate({'accounts':[row]},self.config)
        self.assertEqual(row['label'],'Operations');self.assertEqual(row['plan'],'Team Premium')
        self.assertIsNone(row['fiveHour'])

    def test_membership_keeps_other_provider_empty_arrays(self):
        text = '[providers.anthropic]\nauth="claude_oauth"\naccounts=[] # start empty\n[providers.other]\naccounts=[]\n'
        value = setup.tomllib.loads(setup.add_membership(text, 'operations'))
        self.assertEqual(value['providers']['other']['accounts'], [])
        self.assertEqual(value['providers']['anthropic']['accounts'], [{'name':'operations'}])

    def test_invalid_identity_and_credential_shapes_fail_cleanly(self):
        for data in ([], {'account':[], 'organization':{}}, {'account':{}, 'organization':None}):
            with self.subTest(data=data), self.assertRaises(RuntimeError): setup.identity(data, {})
        path = self.stored()
        for value in ([], {'claudeAiOauth':[]}):
            path.write_text(json.dumps(value))
            with self.assertRaises(RuntimeError): setup.credential(path)

    def test_unknown_metadata_field_rejected(self):
        self.config.write_text('{"accounts":[{"name":"operations","surprise":true}]}')
        with self.assertRaises(ValueError): account_metadata.load(self.config)

    def test_historical_identity_profiles_keep_team_separate_from_max(self):
        fixtures=[('Personal One','personal','default_claude_max_20x',True,'Max 20x'),
                  ('Personal Two','personal','default_claude_max_20x',True,'Max 20x'),
                  ('Work One','claude_team','default_claude_max_5x',False,'Team Premium'),
                  ('Work Two','claude_team','default_claude_max_5x',False,'Team Premium'),
                  ('Work Three','claude_team','default_claude_max_5x',False,'Team Premium')]
        for label,kind,tier,max_plan,expected in fixtures:
            with self.subTest(account=label):
                data={'account':{'uuid':'u','email':'fixture@example.test','has_claude_max':max_plan},
                      'organization':{'uuid':'o','name':label,'organization_type':kind,'rate_limit_tier':tier}}
                self.assertEqual(setup.identity(data,{'shuntAccountUuid':'u','shuntOrganizationUuid':'o'})['plan'],expected)
        with self.assertRaisesRegex(RuntimeError,'organization differ'):
            setup.identity(data,{'shuntAccountUuid':'u','shuntOrganizationUuid':'wrong-org'})


if __name__=='__main__':unittest.main()
