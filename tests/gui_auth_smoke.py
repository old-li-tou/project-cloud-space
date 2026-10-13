import os,sys,tempfile
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cloud_space as c
from PySide6.QtWidgets import QApplication,QDialog,QLineEdit,QPushButton
from PySide6.QtCore import QTimer
real_exec=QApplication.exec
with tempfile.TemporaryDirectory() as temp:
 c.STATE=Path(temp);c.REGISTRY=c.STATE/'projects.json';c.EVENT_LOG=c.STATE/'events.jsonl';c.STATUS_FILE=c.STATE/'status.json'
 captured=[]
 def fake_authorize(self,**values):captured.append(dict(values))
 c.BaiduClient.authorize=fake_authorize
 def intercepted(app):
  window=next(w for w in app.topLevelWidgets() if hasattr(w,'worker'))
  def fill():
   dialog=next(w for w in app.topLevelWidgets() if isinstance(w,QDialog))
   fields=dialog.findChildren(QLineEdit);assert len(fields)==3
   assert fields[1].echoMode()==QLineEdit.EchoMode.Password and fields[2].echoMode()==QLineEdit.EchoMode.Password
   for field,value in zip(fields,['fake-client','fake-secret','fake-code']):field.setText(value)
   next(b for b in dialog.findChildren(QPushButton) if b.text()=='提交授权').click()
  QTimer.singleShot(0,fill)
  window.configure_auth()
  def finish():
   if captured:
    assert captured==[{'client_id':'fake-client','client_secret':'fake-secret','code':'fake-code'}]
    log=window.log.toPlainText();assert 'fake-secret' not in log and 'fake-code' not in log
    window.close()
  timer=QTimer();timer.timeout.connect(finish);timer.start(200)
  QTimer.singleShot(15000,window.close)
  return real_exec()
 QApplication.exec=intercepted;c.run_gui()
 assert captured
 print('PASS: installed GUI auth dialog masks credentials, queues authorization and keeps secrets out of logs')
