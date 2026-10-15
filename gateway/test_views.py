"""Terminal views keep quota meaning and width with and without color."""
import re
import unittest
from views import usage

ANSI = re.compile(r'\x1b\[[0-9;]*m')
NOW = 1900000000000

class UsageViewTests(unittest.TestCase):
    def data(self):
        return {'ready':1, 'updatedAt':NOW, 'accounts':[
            {'label':'Gmail','status':'limited','plan':'Max 20×','seenAt':NOW-60000,
             'fiveHour':{'pct':97,'resetsAt':NOW+4920000}, 'sevenDay':{'pct':26,'resetsAt':NOW+500000000}},
            {'label':'Work','status':'ready','plan':'Team Premium','seenAt':NOW-60000,
             'fiveHour':{'pct':3,'resetsAt':NOW+7320000}, 'sevenDay':None}],
            'forecast':{'seconds':27600,'eligibleAccounts':1,'estimatedAccounts':1,'partial':True,'threshold':95,
                        'nextReset':{'account':'Gmail','window':'5h','at':NOW+4920000}},'history':[{}]}

    def test_unknown_is_not_zero_and_estimate_is_explicitly_partial(self):
        text=usage(self.data())
        self.assertIn('97%',text);self.assertIn('26%',text);self.assertIn('3%',text)
        self.assertRegex(text,r'Week\s+┄+\s+not reported')
        self.assertIn('~7h 40m',text);self.assertIn('1/1 ready accounts estimated · partial',text)
        self.assertIn('Unmeasured accounts/windows excluded.',text)
        self.assertIn('Anthropic has not supplied a reading',text)
        self.assertIn('Capacity may run out sooner',text)
        self.assertIn('Fable = separate weekly limit',text)
        self.assertIn('1 switch retained',text)

    def test_fable_weekly_zero_and_reset_do_not_replace_shared_week(self):
        data = self.data()
        data['accounts'][1]['fableWeekly'] = {'pct': 0, 'resetsAt': NOW+3600000}
        text = usage(data)
        self.assertRegex(text, r'Fable\s+─+\s+0%.*resets in 1h 00m')
        self.assertRegex(text, r'Week\s+┄+\s+not reported')
        self.assertIn('All-model windows only', text)

    def test_color_is_optional_and_keeps_visible_text_identical(self):
        plain=usage(self.data());colored=usage(self.data(),color=True)
        self.assertNotIn('\x1b',plain);self.assertIn('\x1b[31m',colored);self.assertIn('\x1b[32m',colored)
        self.assertEqual(ANSI.sub('',colored),plain)

    def test_narrow_width_long_labels_and_terminal_control_text(self):
        data=self.data();data['accounts'][0]['label']='Organization '+('verylongname'*8)+'\x1b[2J'
        for width in [20,40,64,80,120]:
            text=usage(data,width=width,color=True)
            self.assertTrue(all(len(line)<=min(width,96) for line in ANSI.sub('',text).splitlines()))
            self.assertNotIn('\x1b[2J',text)

    def test_pace_quality_range_and_reset_caveat_fit_narrow_width(self):
        data=self.data();data['accounts'][0]['name']='gmail'
        data['forecast'].update(paceQuality='changing',secondsLow=10000,secondsHigh=40000,resetBeforeLimit=True,
            accounts=[{'name':'gmail','label':'Gmail','windows':{'fiveHour':{'ratePerHour':3.2,'reason':'changing'},'sevenDay':{'ratePerHour':None,'reason':'low signal'}}}])
        text=usage(data)
        self.assertIn('Pace · 5h ~3.2%/h · week low signal',text)
        self.assertIn('pace changing',text);self.assertIn('Allowance range',text)
        self.assertIn('Resets arrive before projected depletion',text)
        for width in [20,40,64,80]:
            self.assertTrue(all(len(line)<=width for line in usage(data,width=width).splitlines()))

    def test_empty_pool_backup_and_unsaved_state_remain_visible(self):
        data=self.data();data.update(ready=0,accounts=[],history=[],persistenceError=True,recovery={'restoredBackup':True})
        data['forecast'].update(seconds=None,eligibleAccounts=0,estimatedAccounts=0)
        text=usage(data)
        self.assertIn('No ready accounts confirmed',text)
        self.assertIn('recovery state could not be saved',text)
        self.assertIn('last good backup',text)

if __name__=='__main__':unittest.main()
