from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util,hashlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('c',str(Path(__file__).resolve().parents[1] / 'cloud_space.py'));c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Fake(c.Cloud):
 def __init__(self):self.progress=c.TransferProgress();self.uploads=0;self.index_fails=True
 def ensure_remote(self,p):pass
 def api(self,method,data=None,**kw):
  if method=='precreate':self.uploads+=1;return {'return_type':1,'uploadid':'x','block_list':[]}
  if method=='create':return {'fs_id':123,'size':data['size'],'path':data['path']}
  raise AssertionError(method)
 def download(self,r,output=None,**kw):
  if output is not None:output.write(b'content')
 def backup_index(self,p,r):
  if self.index_fails:raise c.ArchiveError('index failure')
  return {'verified':True}
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);c.STATE=self.root/'.state';c.STATE.mkdir();c.REGISTRY=c.STATE/'projects.json'
 def tearDown(self):self.tmp.cleanup()
 def test_index_failure_retains_source_and_reuses_payload(self):
  cloud=self.root/'cloud';cloud.mkdir();(cloud/c.RECORDS).mkdir();p=cloud/'a.txt';p.write_bytes(b'content')
  project={'cloud':str(cloud),'remote':'/test','id':'a'*32,'project':str(self.root)};client=Fake()
  with self.assertRaises(c.ArchiveError):client.archive(project,p)
  self.assertEqual(p.read_bytes(),b'content');self.assertEqual(client.uploads,1)
  self.assertEqual(len(list((cloud/c.RECORDS).glob('*.json'))),1)
  client.index_fails=False;client.archive(project,p)
  self.assertFalse(p.exists());self.assertEqual(client.uploads,1)
 def test_unsafe_windows_paths(self):
  for path in ('../x','/x','C:/x','a/../../x','a\\x','a/x:ads','a/CON.txt','a/x.','a//x','a/./x'):
   with self.assertRaises(c.ArchiveError,msg=path):c.safe_relative(path)
 def record(self):
  return {'id':'a'*32,'project_id':'b'*32,'relative_path':'sub/a.txt','remote_path':'/项目云空间/test/a',
   'fs_id':123,'size':7,'sha256':hashlib.sha256(b'content').hexdigest(),'mtime':1700000000,
   'archived_at':'2026-10-13','verification':'full-download-sha256'}
 def test_validation_rejects_cross_project(self):
  r=self.record();e={'schema_version':1,'project':{'remote':'/项目云空间/test','id':'b'*32},'record':r}
  self.assertEqual(c.validate_index(e,'/项目云空间/test'),r)
  r['remote_path']='/unrelated/a'
  with self.assertRaises(c.ArchiveError):c.validate_index(e,'/项目云空间/test')
 def test_recover_resume_and_nonoverwrite(self):
  client=Fake();r=self.record();dest=self.root/'restored'
  target=client.restore({},r,root=dest,resume=True);self.assertEqual(target.read_bytes(),b'content')
  client.download=lambda *a,**k:self.fail('must skip verified file')
  self.assertEqual(client.restore({},r,root=dest,resume=True),target)
  target.write_bytes(b'existing');client.download=Fake().download
  second=client.restore({},r,root=dest,resume=True)
  self.assertNotEqual(target,second);self.assertEqual(target.read_bytes(),b'existing')
  self.assertEqual(client.restore({},r,root=dest,resume=True),second)
 def test_corrupt_index_does_not_succeed_silently(self):
  client=Fake();client.list_all=lambda remote:[{'path':remote+'/bad.json','size':3,'isdir':0,'fs_id':1}]
  report=client.recover_project('/项目云空间/test',self.root/'restored')
  self.assertEqual(report['state'],'partial');self.assertEqual(len(report['errors']),1)
 def test_recovery_failure_keeps_existing_file(self):
  client=Fake();r=self.record();client.read_project_indexes=lambda remote:([r],[])
  def fail(*args,**kw):raise c.ArchiveError('sha mismatch')
  client.download=fail
  report=client.recover_project('/项目云空间/test',self.root/'restore')
  self.assertEqual(report['state'],'partial');self.assertEqual(len(report['errors']),1)
  self.assertFalse(any((self.root/'restore').rglob('*.part-*')))
if __name__ == '__main__':
    unittest.main(verbosity=2)
