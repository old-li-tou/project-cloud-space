from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

spec=importlib.util.spec_from_file_location('cloud_space',str(Path(__file__).resolve().parents[1] / 'cloud_space.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class MonitorChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        c.STATE=Path(self.temp.name)
        c.STATUS_FILE=c.STATE/'status.json';c.EVENT_LOG=c.STATE/'events.jsonl';c.REGISTRY=c.STATE/'projects.json'
        self.worker=SimpleNamespace(paused=False,failed={},isRunning=lambda:True)
    def tearDown(self):self.temp.cleanup()
    def test_absent_corrupt_stale_and_dead_process(self):
        self.assertEqual(c.query_status()[1],2)
        c.STATUS_FILE.write_text('bad-json',encoding='utf-8')
        self.assertEqual(c.query_status()[1],2)
        data={'schema_version':1,'pid':os.getpid(),'heartbeat_epoch':time.time()-30,'lifecycle':'running'}
        c.save_json(c.STATUS_FILE,data)
        self.assertEqual(c.query_status()[0]['availability'],'stale')
        child=subprocess.Popen([sys.executable,'-c','pass']);child.wait()
        data.update(pid=child.pid,heartbeat_epoch=time.time());c.save_json(c.STATUS_FILE,data)
        self.assertEqual(c.query_status()[0]['availability'],'stopped')
    def test_query_does_not_write(self):
        c.save_json(c.STATUS_FILE,{'schema_version':1,'pid':os.getpid(),
                                 'heartbeat_epoch':time.time(),'lifecycle':'running'})
        before=c.STATUS_FILE.read_bytes();mtime=c.STATUS_FILE.stat().st_mtime_ns
        self.assertEqual(c.query_status()[1],0)
        self.assertEqual(c.STATUS_FILE.read_bytes(),before)
        self.assertEqual(c.STATUS_FILE.stat().st_mtime_ns,mtime)
        self.assertEqual(len(list(c.STATE.iterdir())),1)
    def test_pause_heartbeat_errors_and_shutdown(self):
        monitor=c.AgentMonitor()
        try:
            progress=c.TransferProgress(monitor.progress)
            progress.begin('sample.txt');progress.stage('上传',100);progress.update(40,force=True)
            monitor.heartbeat(self.worker)
            first=c.query_status()[0]
            self.assertTrue(first['accepting_archives'])
            self.assertEqual(first['latest_transfer']['percent'],40)
            self.worker.paused=True;monitor.heartbeat(self.worker)
            self.assertFalse(c.query_status()[0]['accepting_archives'])
            self.assertTrue(c.query_status()[0]['pause_requested'])
            progress.fail(c.ArchiveError('rate limit',method='upload',http=200,errno=20012))
            monitor.heartbeat(self.worker)
            self.assertEqual(c.query_status()[0]['last_error']['errno'],20012)
        finally:monitor.close(self.worker)
        self.assertEqual(c.query_status()[1],3)
        events=[json.loads(line) for line in c.EVENT_LOG.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(e['event']=='transfer_error' and e['errno']==20012 for e in events))
        self.assertEqual(events[-1]['event'],'app_stopped')
    def test_log_history_survives_restart_and_rotates(self):
        m=c.AgentMonitor();m.handler.maxBytes=500
        for i in range(30):m.log('sample',index=i)
        m.close(self.worker)
        self.assertTrue(Path(str(c.EVENT_LOG)+'.1').exists())
        self.assertLessEqual(len(list(c.STATE.glob('events.jsonl*'))),4)
        before=c.EVENT_LOG.read_text(encoding='utf-8')
        m=c.AgentMonitor();m.close(self.worker)
        self.assertTrue(c.EVENT_LOG.read_text(encoding='utf-8').startswith(before))
    def test_real_cli_is_json_and_does_not_open_gui(self):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1] / 'cloud_space.py'),'status'],
                              capture_output=True,encoding='utf-8',timeout=10)
        data=json.loads(result.stdout)
        self.assertIn(result.returncode,(0,2,3))
        self.assertIn('availability',data)
        self.assertNotIn('access_token',result.stdout)

if __name__ == '__main__':
    unittest.main(verbosity=2)
