from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
import hashlib
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('cloud_space',str(Path(__file__).resolve().parents[1] / 'cloud_space.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class FakeCloud(c.Cloud):
    def __init__(self,outcomes):
        self.progress=c.TransferProgress();self.progress.begin('test');self.progress.stage('上传',20)
        self.upload_route=None;self.outcomes=list(outcomes);self.calls=[];self.locates=0
    def api(self,method,data=None,**kw):
        self.calls.append((method,kw))
        if method=='locateupload':
            self.locates+=1
            return {'servers':[{'server':f'https://c{self.locates}.pcs.baidu.com'}],'expire':60}
        kw['upload_callback'](len(kw['files']['file'][1]))
        item=self.outcomes.pop(0)
        if isinstance(item,Exception):raise item
        return item

class RetryChecks(unittest.TestCase):
    chunk=b'test';md5=hashlib.md5(chunk).hexdigest()
    def run_part(self,client):
        return client.upload_part('/test','same-upload-id',2,self.chunk,self.md5,8)
    def test_same_part_refreshes_route_after_11015(self):
        client=FakeCloud([c.ArchiveError('failure',http=403,errno=-11015),{'md5':self.md5}])
        with patch.object(c.time,'sleep') as wait:
            self.run_part(client);wait.assert_called_once_with(2)
        uploads=[kw for method,kw in client.calls if method=='upload']
        self.assertEqual(len(uploads),2)
        self.assertEqual(uploads[0]['extra'],uploads[1]['extra'])
        self.assertEqual(uploads[0]['files'],uploads[1]['files'])
        self.assertNotEqual(uploads[0]['upload_origin'],uploads[1]['upload_origin'])
        self.assertEqual(client.progress.data['done'],12)
    def test_retry_bound_and_delays(self):
        client=FakeCloud([c.requests.Timeout() for _ in range(3)])
        with patch.object(c.time,'sleep') as wait:
            with self.assertRaises(c.requests.Timeout):self.run_part(client)
            self.assertEqual([call.args[0] for call in wait.call_args_list],[2,5])
        self.assertEqual(client.locates,3)
    def test_rate_limits_and_permissions_not_retried(self):
        for http,code in [(200,20012),(200,31034),(403,9013),(403,31024),(429,None)]:
            client=FakeCloud([c.ArchiveError('stop',http=http,errno=code)])
            with patch.object(c.time,'sleep') as wait:
                with self.assertRaises(c.ArchiveError):self.run_part(client)
                wait.assert_not_called()
            self.assertEqual(client.locates,1)
    def test_md5_mismatch_stops(self):
        client=FakeCloud([{'md5':'wrong'}])
        with patch.object(c.time,'sleep') as wait:
            with self.assertRaises(c.ArchiveError):self.run_part(client)
            wait.assert_not_called()
    def test_expired_address_is_refreshed(self):
        client=FakeCloud([{'md5':self.md5}])
        client.upload_route={'origin':'https://old.pcs.baidu.com','path':'/test','uploadid':'same-upload-id',
                             'expires_at':time.monotonic()-1}
        self.run_part(client)
        self.assertEqual(client.locates,1)
        self.assertNotEqual(client.calls[-1][1]['upload_origin'],'https://old.pcs.baidu.com')
    def test_untrusted_upload_host_is_rejected(self):
        client=FakeCloud([])
        client.api=lambda *a,**kw: {'servers':[{'server':'https://pcs.baidu.com.attacker.example'}],'expire':60}
        with self.assertRaises(c.ArchiveError):client.locate_upload('/test','id')
        self.assertIsNone(client.upload_route)

if __name__ == '__main__':
    unittest.main(verbosity=2)
