import unittest
from forecast import estimate, pace
from views import usage, history
NOW=1900000000000
class ForecastTests(unittest.TestCase):
    def account(self,name,pct,week=None,status='ready'):
        return {'id':name,'name':name,'label':name,'seenAt':NOW,'status':status,'fiveHour':{'pct':pct,'resetsAt':NOW+3600000},'sevenDay':week,'plan':'fixture'}
    def points(self,account,values):
        return [{'at':NOW-1200000+i*600000,'fiveHour':{'pct':v,'resetsAt':account['fiveHour']['resetsAt']},'sevenDay':account['sevenDay']} for i,v in enumerate(values)]
    def test_pool_uses_separate_burn_rates_and_excludes_unmeasured_windows(self):
        a=self.account('gmail',30);b=self.account('team',50)
        result=estimate([a,b],{'gmail':self.points(a,[10,20,30]),'team':self.points(b,[40,45,50])},NOW)
        self.assertEqual(result['seconds'],3900+5400)
        self.assertEqual(result['estimatedAccounts'],2);self.assertTrue(result['partial'])
        self.assertEqual(result['nextReset']['account'],'gmail')
    def test_stale_reset_changed_zero_burn_and_paused_stay_unknown(self):
        a=self.account('gmail',30);points=self.points(a,[30,30,30])
        self.assertIsNone(estimate([a],{'gmail':points},NOW)['seconds'])
        points=self.points(a,[10,20,30]);a['fiveHour']['resetsAt']+=3600000
        self.assertIsNone(estimate([a],{'gmail':points},NOW)['seconds'])
        a['seenAt']=NOW-700000
        self.assertEqual(estimate([a],{'gmail':points},NOW)['accounts'][0]['reason'],'reading stale')
        a['status']='paused';self.assertEqual(estimate([a],{},NOW)['eligibleAccounts'],0)
    def test_weekly_pace_can_limit_capacity_and_partial_coverage_is_explicit(self):
        a=self.account('gmail',30,{'pct':92,'resetsAt':NOW+10000000});b=self.account('team',10)
        points=self.points(a,[10,20,30])
        for p,value in zip(points,[90,91,92]):p['sevenDay']={**a['sevenDay'],'pct':value}
        result=estimate([a,b],{'gmail':points},NOW)
        self.assertEqual(result['seconds'],1800);self.assertTrue(result['partial'])
        self.assertEqual(result['estimatedAccounts'],1)
    def test_duplicate_out_of_order_and_too_close_samples_do_not_inflate_speed(self):
        a=self.account('gmail',30);points=self.points(a,[10,20,30])
        result=estimate([a],{'gmail':[points[2],points[0],points[1],points[1]]},NOW)
        self.assertEqual(result['accounts'][0]['windows']['fiveHour']['ratePerHour'],60)
        self.assertEqual(result['accounts'][0]['windows']['fiveHour']['samples'],3)

    def test_low_signal_expired_reading_and_quota_correction_wait_for_evidence(self):
        a=self.account('gmail',30);points=self.points(a,[29.5,29.7,30])
        result=estimate([a],{'gmail':points},NOW)
        self.assertIsNone(result['seconds']);self.assertEqual(result['accounts'][0]['windows']['fiveHour']['reason'],'low signal')
        a['fiveHour']['resetsAt']=NOW-1
        result=estimate([a],{'gmail':points},NOW)
        self.assertIsNone(result['seconds']);self.assertEqual(result['accounts'][0]['windows']['fiveHour']['reason'],'awaiting reset reading')
        a['fiveHour']['resetsAt']=NOW+3600000
        result=estimate([a],{'gmail':self.points(a,[10,40,30])},NOW)
        self.assertIsNone(result['seconds'])

    def test_a_single_burst_then_flat_readings_is_low_signal_not_zero_burn(self):
        a=self.account('team',5)
        values=[0,5,5,5,5,5,5,5,5]
        points=[{'at':NOW-1200000+i*150000,'fiveHour':{**a['fiveHour'],'pct':v}} for i,v in enumerate(values)]
        speed=pace(points,'fiveHour',NOW)
        self.assertIsNone(speed['ratePerHour']);self.assertEqual(speed['reason'],'low signal')

    def test_one_second_reset_rounding_keeps_same_window_history(self):
        a=self.account('team',30);points=self.points(a,[10,20,30])
        points[1]['fiveHour']['resetsAt']-=1000
        result=estimate([a],{'team':points},NOW)
        self.assertEqual(result['seconds'],3900)
        a['fiveHour']['resetsAt']+=3600000
        self.assertIsNone(estimate([a],{'team':points},NOW)['seconds'])

    def test_one_point_measurement_jitter_does_not_erase_pace_history(self):
        a=self.account('team',5)
        values=[2,3,4,5,4,5]
        points=[{'at':NOW-1200000+i*240000,'fiveHour':{**a['fiveHour'],'pct':v}} for i,v in enumerate(values)]
        speed=pace(points,'fiveHour',NOW)
        self.assertEqual(speed['samples'],6);self.assertGreater(speed['ratePerHour'],0)
        result=estimate([a],{'team':points},NOW)
        self.assertIsNotNone(result['seconds'])

    def test_recent_idle_and_workload_change_replace_old_active_pace(self):
        a=self.account('gmail',20)
        values=[0,5,10,15,20,20,20]
        points=[{'at':NOW-3600000+i*600000,'fiveHour':{**a['fiveHour'],'pct':v}} for i,v in enumerate(values)]
        self.assertEqual(pace(points,'fiveHour',NOW)['ratePerHour'],0)
        for p,v in zip(points,[0,1,2,3,4,14,24]):p['fiveHour']['pct']=v
        speed=pace(points,'fiveHour',NOW)
        self.assertEqual(speed['ratePerHour'],60);self.assertEqual(speed['samples'],3)

    def test_variable_burn_has_range_and_limited_account_still_has_speed(self):
        a=self.account('gmail',30,status='limited')
        points=[{'at':NOW-1200000+i*300000,'fiveHour':{**a['fiveHour'],'pct':v}} for i,v in enumerate([0,1,2,3,30])]
        result=estimate([a],{'gmail':points},NOW)
        self.assertEqual(result['eligibleAccounts'],0);self.assertIsNone(result['seconds'])
        self.assertGreater(result['accounts'][0]['windows']['fiveHour']['ratePerHour'],0)
        a['status']='ready';result=estimate([a],{'gmail':points},NOW)
        self.assertEqual(result['paceQuality'],'changing')
        self.assertLess(result['secondsLow'],result['secondsHigh'])
        self.assertTrue(result['resetBeforeLimit'])

    def test_usage_history_show_unknowns_and_no_prompt_content(self):
        a=self.account('gmail',30)
        data={'ready':1,'accounts':[a],'updatedAt':NOW,'forecast':estimate([a],{},NOW),'history':[]}
        text=usage(data);self.assertIn('Collecting pace',text);self.assertIn('not reported',text)
        self.assertIn('No account switches',history(data))
if __name__=='__main__':unittest.main()
