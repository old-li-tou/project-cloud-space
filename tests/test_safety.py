from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
import tempfile
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('cloud_space', str(Path(__file__).resolve().parents[1] / 'cloud_space.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

class FailVerification(c.Cloud):
    def __init__(self):
        self.progress = c.TransferProgress()
    def ensure_remote(self, project):
        pass
    def api(self, method, data=None, **kwargs):
        if method == 'precreate':
            return {'return_type':1, 'uploadid':'test', 'block_list':[]}
        if method == 'create':
            return {'fs_id':123,'size':data['size'],'path':data['path']}
        raise AssertionError(method)
    def download(self, record, output=None):
        if output:
            output.write(b'partial')
        raise c.ArchiveError('injected verification failure')

class Safety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def test_windows_lock_blocks_write_and_rename(self):
        p = self.root/'source.txt'; p.write_bytes(b'keep me')
        with c.LockedSource(p):
            with self.assertRaises(PermissionError):
                p.write_bytes(b'changed')
            with self.assertRaises(PermissionError):
                p.rename(self.root/'moved.txt')
        self.assertEqual(p.read_bytes(),b'keep me')
    def test_delete_same_handle(self):
        p = self.root/'source.txt'; p.write_bytes(b'generated test')
        with c.LockedSource(p) as handle:
            handle.release()
        self.assertFalse(p.exists())
    def test_failed_verification_keeps_source_and_no_receipt(self):
        cloud=self.root/'cloud'; cloud.mkdir(); p=cloud/'source.txt'; p.write_bytes(b'keep me')
        project={'cloud':str(cloud),'remote':'/test','id':'test'}
        with self.assertRaises(c.ArchiveError):
            FailVerification().archive(project,p)
        self.assertEqual(p.read_bytes(),b'keep me')
        self.assertFalse((cloud/c.RECORDS).exists())
    def test_restore_failure_preserves_existing_and_removes_partial(self):
        dest=self.root/'云空间恢复'; dest.mkdir(); (dest/'file.txt').write_bytes(b'existing')
        with self.assertRaises(c.ArchiveError):
            FailVerification().restore({'project':str(self.root)}, {'relative_path':'file.txt'})
        self.assertEqual((dest/'file.txt').read_bytes(),b'existing')
        self.assertEqual(len(list(dest.iterdir())),1)
    def test_restore_rejects_parent_escape(self):
        with self.assertRaises(c.ArchiveError):
            FailVerification().restore({'project':str(self.root)}, {'relative_path':'../escape.txt'})

    def test_api_rate_limit_and_http_codes_are_preserved(self):
        from unittest.mock import Mock
        for status, code in [(200,20012),(200,31034),(429,9013)]:
            client=c.Cloud.__new__(c.Cloud)
            client.progress=c.TransferProgress()
            client.auth=Mock();client.auth.get_access_token.return_value='fake-token-not-sent'
            response=Mock();response.status_code=status;response.json.return_value={'errno':code}
            context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
            client.session=Mock();client.session.request.return_value=context
            with self.assertRaises(c.ArchiveError) as error:
                client.api('precreate',{'path':'/test'})
            client.progress.fail(error.exception)
            self.assertEqual(client.progress.data['http'],status)
            self.assertEqual(client.progress.data['errno'],code)
            self.assertEqual(client.progress.data['method'],'precreate')
            self.assertNotIn('fake-token',str(error.exception))

    def test_windows_errno_is_not_a_baidu_error_code(self):
        progress=c.TransferProgress();progress.begin('test')
        progress.fail(PermissionError(13,'denied'))
        self.assertEqual(progress.data['errno'],'—')

    def test_upload_meter_excludes_multipart_headers(self):
        seen=[]
        with c.UploadBody(b'HEADERpayloadTRAILER',6,7,seen.append) as body:
            while body.read(3):
                pass
        self.assertEqual(seen[-1],7)
        self.assertTrue(all(0 <= n <= 7 for n in seen))

if __name__ == '__main__':
    unittest.main(verbosity=2)
