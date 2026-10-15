"""Live view refreshes safely and restores the terminal on exit."""
import io,json,os,re,signal,unittest
from unittest.mock import patch
import launch

class LiveUsageTests(unittest.TestCase):
    def data(self,pct):
        return {'ready':1,'updatedAt':1900000000000,'accounts':[{'label':'Gmail','status':'ready','seenAt':1900000000000,'plan':'Max',
                'fiveHour':{'pct':pct,'resetsAt':1900003600000},'sevenDay':None}],
                'forecast':{'seconds':None,'eligibleAccounts':1,'estimatedAccounts':0,'partial':True,'nextReset':None},'history':[]}

    def test_refreshes_changed_readings_and_ctrl_c_restores_terminal(self):
        out=io.StringIO();old=signal.getsignal(signal.SIGTERM)
        with patch.object(launch.sys,'stdout',out),patch.object(out,'isatty',return_value=True),patch.dict(os.environ,{'TERM':'xterm'},clear=True), \
             patch.object(launch,'request',side_effect=[json.dumps(self.data(3)).encode(),json.dumps(self.data(4)).encode()]) as request, \
             patch.object(launch.shutil,'get_terminal_size',return_value=os.terminal_size((80,24))), \
             patch.object(launch.time,'sleep',side_effect=[None,KeyboardInterrupt]) as sleep:
            launch.show_usage('fixture')
        text=out.getvalue();self.assertIn('  3%',text);self.assertIn('  4%',text)
        self.assertEqual(text.count('\033[H'),2);self.assertTrue(text.endswith('\033[0m\033[?25h\033[?1049l'))
        self.assertEqual(signal.getsignal(signal.SIGTERM),old)
        self.assertEqual(request.call_count,2);self.assertTrue(all(c.kwargs['path']=='/pool/overview' for c in request.call_args_list))
        self.assertTrue(all(c.args==(5,) for c in sleep.call_args_list))

    def test_failure_keeps_last_state_and_recovers(self):
        out=io.StringIO()
        with patch.object(launch.sys,'stdout',out),patch.object(out,'isatty',return_value=True),patch.dict(os.environ,{'TERM':'xterm'},clear=True), \
             patch.object(launch,'request',side_effect=[json.dumps(self.data(3)).encode(),ConnectionError(),json.dumps(self.data(5)).encode()]), \
             patch.object(launch,'ensure_pool') as recover,patch.object(launch.time,'sleep',side_effect=[None,None,KeyboardInterrupt]):
            launch.show_usage('fixture')
        text=out.getvalue();self.assertIn('Last known state',text);self.assertIn('Offline · retrying',text);self.assertIn('  5%',text)
        recover.assert_called_once_with('fixture');self.assertEqual(text.count('\033[H'),3)

    def test_once_redirected_and_dumb_terminal_are_single_snapshots(self):
        for tty,once,term in [(True,True,'xterm'),(False,False,'xterm'),(True,False,'dumb')]:
            out=io.StringIO()
            with patch.object(launch.sys,'stdout',out),patch.object(out,'isatty',return_value=tty),patch.dict(os.environ,{'TERM':term},clear=True), \
                 patch.object(launch,'request',return_value=json.dumps(self.data(3)).encode()) as request,patch.object(launch.time,'sleep') as sleep:
                launch.show_usage('fixture',once=once)
            self.assertNotIn('\033[?1049',out.getvalue());self.assertEqual(request.call_count,1);sleep.assert_not_called()

    def test_resize_fits_each_frame_and_warns_when_cropped(self):
        out=io.StringIO()
        with patch.object(launch.sys,'stdout',out),patch.object(out,'isatty',return_value=True),patch.dict(os.environ,{'TERM':'xterm','NO_COLOR':''},clear=True), \
             patch.object(launch,'request',return_value=json.dumps(self.data(3)).encode()), \
             patch.object(launch.shutil,'get_terminal_size',side_effect=[os.terminal_size((80,24)),os.terminal_size((20,6))]), \
             patch.object(launch.time,'sleep',side_effect=[None,KeyboardInterrupt]):
            launch.show_usage('fixture')
        frames=out.getvalue().split('\033[H')[1:]
        self.assertIn('Enlarge terminal',frames[1])
        for frame,width,height in zip(frames,[80,20],[24,6]):
            lines=re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]','',frame).split('\r\n')
            self.assertLessEqual(len(lines),height);self.assertTrue(all(len(line)<=width for line in lines))

if __name__=='__main__':unittest.main()
