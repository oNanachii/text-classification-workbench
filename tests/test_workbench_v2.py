import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from fastapi.testclient import TestClient
from dataflow import DataStore,Conflict,canonical,immutable,sha,atomic
from app_v2 import create_app
import experiments


def row(text,label):return {'id':sha(text.encode()),'text':text,'label':label,'group_id':sha(('group:'+text).encode())}


def seeded(root):
 s=DataStore(root);train=[row('hello friend meet tomorrow','ham'),row('win prize offer claim now','spam')];validation=[row('validation hello information note','ham'),row('validation promotional winning offer','spam')]
 immutable(s.root/'dataset.json',{'manifest':{'dataset_id':'fixture-v2','counts':{},'rule':'isolated fixture'},'train':train,'validation':validation})
 immutable(s.root/'heldout-exclusion.json',{'dataset_id':'fixture-v2','rows':validation+[row('secret heldout exact content','ham')]})
 immutable(s.root/'split-index.json',{'dataset_id':'fixture-v2','index':[{'id':r['id'],'split':'validation'} for r in validation+[row('secret heldout exact content','ham')]]})
 with s.transaction() as c:
  for r in train:c.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?,?)',(r['id'],r['text'],r['label'],r['label'],0,'uci:fixture',1,'2026-01-01'))
 return s

class DataFlowTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=seeded(self.root)
 def tearDown(self):self.temp.cleanup()
 def test_preview_does_not_write_confirm_is_idempotent(self):
  p=self.store.preview_csv('text,label\nplease bring red notebook tomorrow,ham\n','authored fixture');self.assertEqual(self.store.samples()['total'],2)
  a=self.store.confirm_import(p['preview_id']);b=self.store.confirm_import(p['preview_id']);self.assertTrue(a['created']);self.assertFalse(b['created']);self.assertEqual(self.store.samples()['total'],3)
  snapshot=self.store.load_snapshot(self.store.snapshot()['snapshot_id']);self.assertEqual(snapshot['content']['count'],2)
  new=self.store.samples(pending=True)['items'][0];self.store.review(new['id'],new['label'],'clear literal fixture meaning',0)
  snapshot=self.store.load_snapshot(self.store.snapshot()['snapshot_id']);self.assertEqual(snapshot['content']['count'],3)
 def test_whole_conflicting_group_rejected(self):
  p=self.store.preview_csv('text,label\nnew message text here,ham\nNEW MESSAGE TEXT HERE,spam\n','fixture');self.assertEqual(p['summary']['conflict_groups'],1);self.assertEqual(p['summary']['accepted_rows'],0)
 def test_heldout_import_rejected(self):
  p=self.store.preview_csv('text,label\nsecret heldout exact content,ham\n','fixture');self.assertEqual(p['summary']['heldout_overlap_rejected'],1)
 def test_bad_csv_rows_never_enter_database(self):
  p=self.store.preview_csv('text,label\n,ham\nnew message,unknown\n','fixture');self.assertEqual(p['summary']['invalid_rows'],2)
  with self.assertRaises(ValueError):self.store.confirm_import(p['preview_id'])
  self.assertEqual(self.store.samples()['total'],2)
 def test_revision_conflict_and_history_atomic(self):
  r=self.store.samples()['items'][0];label='spam' if r['label']=='ham' else 'ham';x=self.store.review(r['id'],label,'isolated fault injection only',0);self.assertTrue(x['changed']);self.assertEqual(len(x['sample']['history']),1)
  with self.assertRaises(Conflict):self.store.review(r['id'],r['label'],'stale',0)
  x=self.store.review(r['id'],label,'same label',1);self.assertFalse(x['changed']);self.assertEqual(len(x['sample']['history']),1)
 def test_snapshot_changes_only_after_review(self):
  a=self.store.snapshot();r=self.store.samples()['items'][0];label='spam' if r['label']=='ham' else 'ham';self.store.review(r['id'],label,'isolated fault injection',0)
  # Avoid single-class training snapshot in this 2-row fixture.
  other=self.store.samples()['items'][1];self.store.review(other['id'],'ham' if label=='spam' else 'spam','isolated fault injection',0)
  b=self.store.snapshot();self.assertNotEqual(a['snapshot_id'],b['snapshot_id']);self.assertEqual(self.store.load_snapshot(a['snapshot_id'])['content']['corrections'],[])
 def test_tampered_snapshot_rejected(self):
  s=self.store.snapshot();p=Path(s['path']);d=json.loads(p.read_text());d['content']['count']=500;p.write_text(json.dumps(d))
  with self.assertRaises(ValueError):self.store.load_snapshot(s['snapshot_id'])
 def test_preview_stale_conflicting_import_is_atomic(self):
  content='text,label\nnew notebook reminder,ham\nnew voucher promotion,spam\n';p=self.store.preview_csv(content,'fixture')
  other=self.store.preview_csv('text,label\nnew notebook reminder,ham\n','other');self.store.confirm_import(other['preview_id'])
  with self.assertRaises(Conflict):self.store.confirm_import(p['preview_id'])
  self.assertEqual(self.store.samples()['total'],3)

class APITests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=seeded(self.root);self.auth={'Authorization':'Bearer isolated-test-token'}
 def tearDown(self):self.temp.cleanup()
 def test_anonymous_private_reads_and_writes_rejected(self):
  with TestClient(create_app(self.root,'isolated-test-token',False)) as c:
   self.assertEqual(c.get('/api/samples').status_code,401);self.assertEqual(c.get('/api/runs').status_code,401);self.assertEqual(c.post('/api/snapshots').status_code,401)
 def test_authorized_preview_review_snapshot_and_stale_revision(self):
  with TestClient(create_app(self.root,'isolated-test-token',False),headers=self.auth) as c:
   p=c.post('/api/imports/preview',json={'csv':'text,label\nplease bring a green notebook,ham\n','source':'authored fixture'});self.assertEqual(p.status_code,200)
   result=c.post('/api/imports/confirm',json={'preview_id':p.json()['preview_id']});self.assertEqual(result.status_code,200)
   r=c.get('/api/samples?pending=true').json()['items'][0]
   body={'label':'spam','note':'isolated API fault injection','expected_revision':0};self.assertEqual(c.patch('/api/samples/'+r['id'],json=body).status_code,200);self.assertEqual(c.patch('/api/samples/'+r['id'],json=body).status_code,409)
   self.assertEqual(c.post('/api/snapshots').status_code,200)
 def test_public_mode_remains_readonly_with_operator_token(self):
  with TestClient(create_app(self.root,'isolated-test-token',True),headers=self.auth) as c:
   self.assertEqual(c.get('/api/samples').status_code,403);self.assertEqual(c.post('/api/imports/preview',json={'csv':'text,label\nnew text,ham','source':'fixture'}).status_code,403)
 def test_input_is_not_echoed_in_errors(self):
  with TestClient(create_app(self.root,'isolated-test-token',False)) as c:
   r=c.post('/api/predict',json={'text':'private marker', 'unexpected':'secret-extra'});self.assertEqual(r.status_code,422);self.assertNotIn('secret-extra',r.text);self.assertEqual(r.headers['cache-control'],'no-store')

class NumericArtifactTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.project=Path(__file__).resolve().parents[1];cls.run_path=cls.project/'var/runs/v2-base-seed42'
 def setUp(self):
  if not self.run_path.exists() or json.loads((self.run_path/'report.json').read_text()).get('state')!='completed':self.skipTest('real training artifact not available')
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'runs').mkdir();shutil.copytree(self.run_path,self.root/'runs/v2-base-seed42')
 def tearDown(self):
  if hasattr(self,'temp'):self.temp.cleanup()
 def test_select_loaded_version_restart_and_rollback(self):
  first=experiments.select(self.root,'v2-base-seed42','lr',.5)
  with TestClient(create_app(self.root,'test-token',False)) as c:
   self.assertEqual(c.get('/api/health').json()['loaded']['kind'],'lr');experiments.select(self.root,'v2-base-seed42','mlp16',.5)
   self.assertTrue(c.get('/api/service').json()['restart_required']);self.assertEqual(c.post('/api/predict',json={'text':'normal meeting reminder'}).json()['model'],'lr')
  with TestClient(create_app(self.root,'test-token',False)) as c:self.assertEqual(c.get('/api/health').json()['loaded']['kind'],'mlp16')
  experiments.rollback(self.root)
  with TestClient(create_app(self.root,'test-token',False)) as c:self.assertEqual(c.get('/api/health').json()['loaded']['kind'],'lr')
 def test_corrupt_artifact_cannot_be_selected(self):
  path=self.root/'runs/v2-base-seed42/weights.npz';path.write_bytes(path.read_bytes()+b'corrupt')
  with self.assertRaises(ValueError):experiments.select(self.root,'v2-base-seed42')
 def test_public_export_excludes_private_data(self):
  experiments.select(self.root,'v2-base-seed42');destination=self.root/'export';experiments.export_public(self.root,destination)
  self.assertFalse((destination/'review.sqlite3').exists());self.assertFalse((destination/'dataset.json').exists())
  with TestClient(create_app(destination,'',True)) as c:
   r=c.post('/api/predict',json={'text':'project meeting tomorrow afternoon'});self.assertEqual(r.status_code,200);self.assertEqual(r.json()['model_version'],'v2-base-seed42');self.assertEqual(c.get('/api/samples').status_code,403)


class FailureAndFinalGuardTests(unittest.TestCase):
 def test_offline_worker_without_lock_is_interrupted(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'runs'/'crashed';p.mkdir(parents=True);atomic(p/'report.json',{'run_id':'crashed','state':'running'})
   self.assertEqual(experiments.list_runs(t)[0]['state'],'interrupted')
 def test_review_approval_advances_revision_without_fake_correction(self):
  with tempfile.TemporaryDirectory() as t:
   store=seeded(t);p=store.preview_csv('text,label\nbring the orange training notebook next week,ham\n','fixture');store.confirm_import(p['preview_id']);r=store.samples(pending=True)['items'][0]
   a=store.review(r['id'],'ham','literal fixture reminder',0);self.assertFalse(a['changed']);self.assertEqual(a['sample']['revision'],1);self.assertEqual(a['sample']['history'],[])
   with self.assertRaises(Conflict):store.review(r['id'],'spam','stale concurrent editor',0)
 def test_failed_training_keeps_a_failed_report_and_old_snapshot(self):
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as t:
   store=seeded(t);s=store.snapshot()
   with patch('sklearn.linear_model.LogisticRegression.fit',side_effect=RuntimeError('isolated training failure')):
    with self.assertRaises(RuntimeError):experiments.train(t,s['snapshot_id'],'failed-fixture',epochs=1)
   report=json.loads((Path(t)/'runs/failed-fixture/report.json').read_text());self.assertEqual(report['state'],'failed');self.assertEqual(store.load_snapshot(s['snapshot_id'])['snapshot_id'],s['snapshot_id'])
 def test_final_evaluation_once_on_synthetic_fixture_only(self):
  from unittest.mock import patch
  from evaluation import freeze_final,evaluate_final
  project=Path(__file__).resolve().parents[1]
  if not (project/'var/runs/v2-base-seed42/report.json').exists():self.skipTest('real numeric artifact absent')
  with tempfile.TemporaryDirectory() as t:
   root=Path(t)/'copy';shutil.copytree(project/'var',root,ignore=shutil.ignore_patterns('operator-token.txt','final-plan.json','final-attempt.json','final-test-report.json'))
   dataset=json.loads((root/'dataset.json').read_text());rows=[row('synthetic final guard normal example','ham'),row('synthetic final guard prize example','spam')]
   atomic(root/'heldout-test.json',{'dataset_id':dataset['manifest']['dataset_id'],'rows':rows})
   freeze_final(root,'v2-base-seed42','lr',.5,'Isolated synthetic repeat guard; not actual heldout evaluation')
   with patch('evaluation.SafeModel') as cls:
    cls.return_value.scores.return_value=[.1,.9]
    result=evaluate_final(root)
   self.assertEqual(result['metrics']['count'],2)
   with self.assertRaises(ValueError):evaluate_final(root)


class PublicationAndDependencyTests(unittest.TestCase):
 def test_immutable_is_not_visible_until_complete_hardlink_publication(self):
  import os,threading
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as t:
   path=Path(t)/'snapshot.json';ready=threading.Event();release=threading.Event();errors=[];real_link=os.link
   value={'large_payload':'x'*100000}
   def paused_link(source,destination):
    self.assertEqual(json.loads(Path(source).read_text()),value);ready.set();release.wait(3);return real_link(source,destination)
   def publish():
    try:immutable(path,value)
    except BaseException as exc:errors.append(exc)
   with patch('dataflow.os.link',side_effect=paused_link):
    thread=threading.Thread(target=publish);thread.start();self.assertTrue(ready.wait(3));self.assertFalse(path.exists());release.set();thread.join(3)
   self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(json.loads(path.read_text()),value)
   self.assertEqual(list(Path(t).glob('.immutable-*')),[])
 def test_failed_publication_does_not_leave_partial_destination(self):
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as t:
   path=Path(t)/'snapshot.json'
   with patch('dataflow.os.link',side_effect=OSError('isolated publication failure')):
    with self.assertRaises(OSError):immutable(path,{'complete':True})
   self.assertFalse(path.exists());self.assertEqual(list(Path(t).glob('.immutable-*')),[])
   immutable(path,{'complete':True})
   with self.assertRaises(ValueError):immutable(path,{'different':True})
   self.assertEqual(json.loads(path.read_text()),{'complete':True})
 def test_final_plan_locks_metric_normalization_and_storage_dependencies(self):
  from unittest.mock import patch
  from evaluation import freeze_final,evaluate_final,source_fingerprints
  project=Path(__file__).resolve().parents[1]
  if not (project/'var/runs/v2-base-seed42/report.json').exists():self.skipTest('real numeric artifact absent')
  with tempfile.TemporaryDirectory() as t:
   root=Path(t)/'copy';shutil.copytree(project/'var',root,ignore=shutil.ignore_patterns('operator-token.txt','final-plan.json','final-attempt.json','final-test-report.json'))
   freeze_final(root,'v2-base-seed42','lr',.5,'Dependency guard only, no actual held-out evaluation')
   plan=json.loads((root/'final-plan.json').read_text());self.assertTrue({'data.py','experiments.py','dataflow.py'}.issubset(plan['source_sha256']))
   changed=source_fingerprints();changed['experiments.py']='0'*64
   with patch('evaluation.source_fingerprints',return_value=changed):
    with self.assertRaises(ValueError):evaluate_final(root)
   self.assertFalse((root/'final-attempt.json').exists())

if __name__=='__main__':unittest.main()
