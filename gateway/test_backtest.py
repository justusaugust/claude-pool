import unittest
from backtest import replay, summarize
NOW=1900000000000
class ReplayTests(unittest.TestCase):
    def history(self):
        return [{'label':'Fixture','samples':[{'at':NOW+i*300000,'fiveHour':{'pct':10+i*5,'resetsAt':NOW+86400000}} for i in range(12)]}]
    def test_linear_replay_and_future_changes_do_not_leak_into_earlier_prediction(self):
        history=self.history();a=replay(history)
        self.assertTrue(any(f['predictions']['new'] is not None for f in a))
        history[0]['samples'][-1]['fiveHour']['pct']=90
        b=replay(history)
        self.assertEqual([(f['originAt'],f['predictions']) for f in a],[(f['originAt'],f['predictions']) for f in b])
        scored=summarize(a);self.assertTrue(any(g['pairedForecasts']>0 and g['metrics']['new']['mae']==0 for g in scored))
    def test_empty_reset_and_missing_forecasts_are_not_successes(self):
        self.assertEqual(replay([]),[]);self.assertEqual(summarize([]),[])
        history=self.history()
        for p in history[0]['samples']:p['fiveHour']['resetsAt']=p['at']+60000
        self.assertEqual(replay(history),[])
        history=self.history();history[0]['samples']=history[0]['samples'][:3]
        scored=summarize(replay(history))
        self.assertTrue(all(g['pairedForecasts']==0 and g['metrics']['new']['mae'] is None for g in scored))
if __name__=='__main__':unittest.main()
