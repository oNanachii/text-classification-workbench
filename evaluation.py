"""Freeze one candidate and evaluate the held-out model test exactly once explicitly."""
from pathlib import Path
import os
import sys
import numpy as np
from dataflow import DataStore,canonical,immutable,read_json,sha,now
from experiments import run_directory,validated_candidate,metric
from safe_model import SafeModel


DEPENDENCIES = ('evaluation.py', 'safe_model.py', 'data.py', 'experiments.py', 'dataflow.py')

def source_fingerprints():
    directory = Path(__file__).resolve().parent
    return {name: sha((directory / name).read_bytes()) for name in DEPENDENCIES}


def freeze_final(root,run_id,kind='lr',threshold=.5,reason='Fixed validation selection'):
    root=Path(root)
    if (root/'final-attempt.json').exists():raise ValueError('Final evaluation was already attempted; the existing frozen choice cannot be replaced')
    store=DataStore(root);dataset=store.require_prepared();candidate=validated_candidate(root,run_id,kind,threshold);report=read_json(run_directory(root,run_id)/'report.json')
    if candidate['dataset_id']!=dataset['manifest']['dataset_id']:raise ValueError('Candidate dataset differs')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=500:raise ValueError('Selection reason is required')
    snapshot=store.load_snapshot(candidate['snapshot_id'])
    plan={'schema_version':1,'candidate':candidate,'training_config':report['config'],'training_code_sha256':report['code_sha256'],'validation_sha256':report['validation_sha256'],'heldout_file_sha256':sha((root/'heldout-test.json').read_bytes()),'training_snapshot_sha256':snapshot['sha256'],'selection_reason':reason.strip(),'source_sha256':source_fingerprints(),'runtime_versions':{'python':list(sys.version_info[:3]),'numpy':np.__version__},'policy':'Single final model and threshold; no further selection from test results'}
    immutable(root/'final-plan.json',plan);return {'plan_sha256':sha(canonical(plan)),'candidate':candidate,'test_evaluated':False}


def evaluate_final(root):
    root=Path(root);plan=read_json(root/'final-plan.json')
    if plan['source_sha256'] != source_fingerprints() or plan['runtime_versions'] != {'python':list(sys.version_info[:3]),'numpy':np.__version__}:raise ValueError('Frozen evaluation dependencies or runtime versions changed')
    candidate=validated_candidate(root,plan['candidate']['run_id'],plan['candidate']['kind'],plan['candidate']['threshold'])
    if candidate!=plan['candidate'] or sha((root/'heldout-test.json').read_bytes())!=plan['heldout_file_sha256']:raise ValueError('Frozen candidate or test source changed')
    # Reserve before parsing/model evaluation. Even an unknown/crashed attempt is not automatically replayed.
    attempt=root/'final-attempt.json'
    if attempt.exists():raise ValueError('A final evaluation was already attempted; inspect existing evidence, do not retry or tune against it')
    try: fd=os.open(attempt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:raise ValueError('A final evaluation was already attempted; do not retry') from None
    with os.fdopen(fd,'wb') as stream:
        stream.write(canonical({'plan_sha256':sha(canonical(plan)),'started_at':now(),'policy':'No automatic retries or replacement'})+b'\n');stream.flush();os.fsync(stream.fileno())
    store=DataStore(root);dataset=store.require_prepared();held=read_json(root/'heldout-test.json');snapshot=store.load_snapshot(candidate['snapshot_id'])
    if snapshot['sha256']!=plan['training_snapshot_sha256'] or held['dataset_id']!=candidate['dataset_id']:raise ValueError('Data versions differ')
    train_ids={r['id'] for r in snapshot['content']['samples']};val_ids={r['id'] for r in dataset['validation']};rows=held['rows'];ids=[r['id'] for r in rows]
    if not rows or len(set(ids))!=len(ids) or set(ids)&(train_ids|val_ids):raise ValueError('Held-out identity overlap')
    model=SafeModel(run_directory(root,candidate['run_id']),candidate['kind']);scores=model.scores([r['text'] for r in rows]);values=metric([r['label'] for r in rows],scores,candidate['threshold'])
    result={'schema_version':1,'evaluation_split':'test','completed_at':now(),'plan_sha256':sha(canonical(plan)),'candidate':candidate,'metrics':values,'test_texts_or_error_examples_disclosed':False,'scope':'One fixed grouped English SMS hold-out; not live traffic or Chinese performance','no_further_model_selection':True}
    immutable(root/'final-test-report.json',result);return result
