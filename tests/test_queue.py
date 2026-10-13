from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('c',str(Path(__file__).resolve().parents[1] / 'cloud_space.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();c.STATE=Path(self.tmp.name);self.q=c.ArchiveQueue()
  self.entries={f'file-{i}':[123,456] for i in range(34)}
  self.q.discover({'project':self.entries})
 def tearDown(self):self.tmp.cleanup()
 def test_batch_and_completion_restart(self):
  self.assertEqual(self.q.snapshot()['waiting_count'],34)
  i=self.q.ready(self.entries);self.q.start(i);self.q.complete(i)
  q=c.ArchiveQueue();self.assertEqual(q.snapshot()['waiting_count'],33)
  self.assertNotIn(i['path'],q.data['items'])
 def test_three_attempts_and_next_file(self):
  i=self.q.ready(self.entries)
  for n in range(1,4):
   self.q.start(i);self.q.fail(i,c.requests.Timeout())
   self.assertEqual(i['attempts'],n)
  self.assertEqual(i['state'],'failed');self.assertNotEqual(self.q.ready(self.entries)['path'],i['path'])
  self.assertEqual(c.ArchiveQueue().data['items'][i['path']]['state'],'failed')
 def test_retry_delay(self):
  with patch.object(c.time,'time',return_value=1000):
   i=self.q.ready(self.entries);self.q.start(i);self.q.fail(i,c.requests.ConnectionError())
   self.assertEqual(i['next_attempt_at'],1030)
   self.assertIsNone(self.q.ready({i['path']}))
  with patch.object(c.time,'time',return_value=1031):self.assertEqual(self.q.ready({i['path']}),i)
 def test_limit_restart_expire_repeat_and_manual_retry(self):
  for code in (20012,31034,9013,None):
   with patch.object(c.time,'time',return_value=1000):
    self.q.data['cooldown_until']=0
    i=self.q.ready(self.entries);self.q.start(i)
    self.q.fail(i,c.ArchiveError('limited',http=429 if code is None else 403,errno=code))
    self.assertEqual(i['attempts'],0);self.assertEqual(self.q.data['cooldown_until'],5200)
    self.assertIsNone(self.q.ready(self.entries))
    q=c.ArchiveQueue();q.retry_failed();self.assertIsNone(q.ready(self.entries))
   with patch.object(c.time,'time',return_value=5201):
    i=q.ready(self.entries);self.assertIsNotNone(i);q.start(i)
    q.fail(i,c.ArchiveError('limited',errno=31034));self.assertEqual(q.data['cooldown_until'],9401)
 def test_permanent_errors_not_retried(self):
  for e in (c.ArchiveError('hash mismatch'),c.ArchiveError('authorization',http=401),PermissionError()):
   i=self.q.ready(self.entries);self.q.start(i);self.q.fail(i,e);self.assertEqual(i['state'],'failed')
  self.q.retry_failed();self.assertEqual(self.q.snapshot()['failed_count'],0)
 def test_interrupted_and_changed_file(self):
  i=self.q.ready(self.entries);self.q.start(i)
  q=c.ArchiveQueue();self.assertEqual(q.data['items'][i['path']]['attempts'],1)
  self.assertEqual(q.data['items'][i['path']]['state'],'pending')
  self.entries[i['path']]=[124,457];q.discover({'project':self.entries})
  self.assertEqual(q.data['items'][i['path']]['attempts'],0)
 def test_corrupt_queue_fails_closed(self):
  self.q.path.write_text('broken',encoding='utf-8')
  with self.assertRaises(ValueError):c.ArchiveQueue()
 def test_missing_sources_removed(self):
  self.q.discover({'project':{}});self.assertEqual(self.q.snapshot()['waiting_count'],0)
if __name__ == '__main__':
    unittest.main()
