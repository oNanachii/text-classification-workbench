"""Versioned training data and private human review. No model fitting occurs here."""
from __future__ import annotations
import csv
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

import data

LABELS = {'ham', 'spam'}
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10000
ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,95}\Z')


def now(): return datetime.now(timezone.utc).isoformat()
def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def sha(value): return hashlib.sha256(value).hexdigest()
def read_json(path): return json.loads(Path(path).read_text())


def immutable(path, value):
    """Publish complete bytes via same-directory fsync + no-overwrite hard link."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical(value) + b'\n'
    fd, staging = tempfile.mkstemp(prefix='.immutable-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staging, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError('Existing immutable artifact differs')
        return path
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def atomic(path, value):
    path=Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value)+b'\n');stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def template_key(text):
    """Predeclared conservative template grouping; not semantic paraphrase detection."""
    text=data.normalize(text)
    text=re.sub(r'https?://\S+|www\.\S+', ' URL ', text)
    text=re.sub(r'\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b',' EMAIL ',text)
    text=re.sub(r'\d+', ' NUMBER ', text)
    return ' '.join(re.findall(r'[a-z]+',text))


def char_grams(text):
    text=template_key(text)
    return {text[i:i+5] for i in range(max(0,len(text)-4))}


class Conflict(ValueError): pass


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()


class DataStore:
    def __init__(self, root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.root.chmod(0o700)
        self.db=self.root/'review.sqlite3'
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,text TEXT NOT NULL,original_label TEXT NOT NULL,label TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL,reviewed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS corrections(id INTEGER PRIMARY KEY,sample_id TEXT NOT NULL REFERENCES samples(id),old_label TEXT NOT NULL,new_label TEXT NOT NULL,note TEXT NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(sample_id,revision));
            CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,sample_id TEXT NOT NULL REFERENCES samples(id),note TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,source TEXT NOT NULL,summary TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')
        self.db.chmod(0o600)

    def connect(self):
        c=sqlite3.connect(self.db,timeout=10,factory=ClosingConnection);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

    @contextmanager
    def transaction(self):
        c=self.connect()
        try:
            c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except BaseException:
            c.rollback();raise
        finally:c.close()

    def prepared(self): return (self.root/'dataset.json').is_file()

    def prepare(self, raw_path=data.DATA/'raw'/'SMSSpamCollection'):
        """Fixed v2 grouping before new model results. Original v1 files stay untouched."""
        raw=Path(raw_path)
        expected=read_json(data.DATA/'source.json')['raw_sha256']
        if sha(raw.read_bytes())!=expected:raise ValueError('Source bytes differ from the pinned UCI archive; import own data through CSV')
        records=[]
        for i,line in enumerate(raw.read_text().splitlines(),1):
            label,text=line.split('\t',1)
            if label not in LABELS:raise ValueError('Unknown source label')
            records.append({'source_line':i,'raw_text':text,'label':label})
        rows,conflicts,empty,duplicates=data.clean(records)
        # Union exact normalized templates, then high-similarity character candidates.
        parent=list(range(len(rows)))
        def find(i):
            while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
            return i
        def union(a,b):
            a,b=find(a),find(b)
            if a!=b:parent[max(a,b)]=min(a,b)
        templates={};grams=[char_grams(r['text']) for r in rows];buckets=defaultdict(list);near_pairs=0
        for i,row in enumerate(rows):
            key=template_key(row['text'])
            if key and key in templates:union(i,templates[key])
            elif key:templates[key]=i
            # Token prefixes block candidates; compare actual 5-gram Jaccard >= .85.
            tokens=key.split();keys=set(tokens[:2])|set(tokens[-2:])
            candidates=set(j for token in keys for j in buckets[token])
            if len(row['text'])>=35:
                for j in candidates:
                    if len(rows[j]['text'])<35:continue
                    a,b=grams[i],grams[j]
                    if a and b and min(len(a),len(b))/max(len(a),len(b))>=.85 and len(a&b)/len(a|b)>=.85:
                        union(i,j);near_pairs+=1
            for token in keys:buckets[token].append(i)
        groups=defaultdict(list)
        for i,row in enumerate(rows):groups[find(i)].append(row)
        split={k:[] for k in ('train','validation','test')};index=[];mixed=0
        for members in groups.values():
            gid=sha(canonical(sorted(x['id'] for x in members)))
            if len({r['label'] for r in members})>1:
                mixed+=len(members);continue
            bucket=int(sha(('v2-seed42:'+gid).encode())[:12],16)%10000
            name='train' if bucket<7000 else 'validation' if bucket<8500 else 'test'
            for row in members:
                clean={'id':row['id'],'text':row['text'],'label':row['label'],'group_id':gid}
                split[name].append(clean)
                index.append({'id':row['id'],'group_id':gid,'template_hash':sha(template_key(row['text']).encode()),'split':name})
        for rows_ in split.values():rows_.sort(key=lambda r:r['id'])
        if any(len({r['label'] for r in rset})<2 for rset in split.values()):raise ValueError('Split needs both classes')
        manifest={'schema_version':2,'source':'UCI SMS Spam Collection','raw_sha256':sha(raw.read_bytes()),'normalization':'casefold + collapse whitespace + trim',
                  'rule':'predeclared template union + blocked character-5gram Jaccard>=0.85; deterministic group SHA256 seed42 70/15/15',
                  'near_duplicate_scope':'conservative blocked lexical/template audit, not all semantic paraphrases',
                  'counts':{k:dict(Counter(r['label'] for r in v)) for k,v in split.items()},'raw_rows':len(records),'unique_rows':len(rows),'duplicates_removed':duplicates,'conflicts_quarantined':sum(len(x['records']) for x in conflicts),'mixed_group_rows_quarantined':mixed,'near_candidate_pairs_linked':near_pairs,'groups':len(groups),'test_model_evaluated':False}
        dataset_id='sms-v2-'+sha(canonical({'manifest':manifest,'index':index}))[:20]
        manifest['dataset_id']=dataset_id
        dataset={'manifest':manifest,'train':split['train'],'validation':split['validation']}
        if self.prepared() and read_json(self.root/'dataset.json')!=dataset:raise ValueError('Existing v2 dataset differs; use new root')
        immutable(self.root/'dataset.json',dataset)
        immutable(self.root/'heldout-test.json',{'dataset_id':dataset_id,'rows':split['test']})
        immutable(self.root/'split-index.json',{'dataset_id':dataset_id,'index':index})
        # A private full-text exclusion index protects imported additions from held-out overlap.
        immutable(self.root/'heldout-exclusion.json',{'dataset_id':dataset_id,'rows':split['validation']+split['test']})
        with self.transaction() as c:
            m=dict(c.execute('SELECT key,value FROM metadata'))
            if m and m.get('dataset_id')!=dataset_id:raise ValueError('Review database source mismatch')
            c.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',('dataset_id',dataset_id))
            for row in split['train']:
                c.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?)',(row['id'],row['text'],row['label'],row['label'],0,'uci:'+dataset_id,1,now()))
        return manifest

    def require_prepared(self):
        if not self.prepared():raise ValueError('Run workbench.py prepare first')
        return read_json(self.root/'dataset.json')

    def preview_csv(self,content,source):
        self.require_prepared()
        if not isinstance(content,str) or len(content.encode())>MAX_CSV_BYTES:raise ValueError('CSV exceeds 5 MiB')
        if not isinstance(source,str) or not 1<=len(source.strip())<=200:raise ValueError('A source description is required')
        try:
            reader=csv.DictReader(io.StringIO(content.lstrip('\ufeff')),strict=True)
            if reader.fieldnames!=['text','label']:raise ValueError('CSV columns must be exactly text,label')
            raw=list(reader)
        except csv.Error as e:raise ValueError('Malformed CSV') from e
        if not raw or len(raw)>MAX_ROWS:raise ValueError('CSV must contain 1..10000 rows')
        accepted={};bad=[];conflicts=set();duplicates=0
        for n,row in enumerate(raw,2):
            text=row.get('text');label=row.get('label')
            if set(row)!= {'text','label'} or not isinstance(text,str) or not isinstance(label,str):bad.append(n);continue
            text=data.normalize(text);label=label.strip().lower()
            if not text or len(text)>2000 or label not in LABELS or not re.search(r'[a-zA-Z]{2}',text):bad.append(n);continue
            identity=sha(text.encode())
            if identity in accepted:
                duplicates+=1
                if accepted[identity]['label']!=label:conflicts.add(identity)
            else:accepted[identity]={'id':identity,'text':text,'label':label}
        for identity in conflicts:accepted.pop(identity,None)
        held=read_json(self.root/'heldout-exclusion.json')['rows'];held_ids={r['id'] for r in held};held_templates={sha(template_key(r['text']).encode()) for r in held};held_grams=[char_grams(r['text']) for r in held]
        with self.connect() as c:existing={r['id']:r['label'] for r in c.execute('SELECT id,label FROM samples')}
        blocked=[];existing_conflicts=[];items=[]
        for row in accepted.values():
            g=char_grams(row['text'])
            overlap=row['id'] in held_ids or sha(template_key(row['text']).encode()) in held_templates
            if not overlap and len(row['text'])>=35:
                overlap=any(h and g and min(len(g),len(h))/max(len(g),len(h))>=.85 and len(g&h)/len(g|h)>=.85 for h in held_grams)
            if overlap:blocked.append(row['id']);continue
            if row['id'] in existing:
                duplicates+=1
                if existing[row['id']]!=row['label']:existing_conflicts.append(row['id'])
                continue
            items.append(row)
        summary={'input_rows':len(raw),'accepted_rows':len(items),'invalid_rows':len(bad),'invalid_line_numbers':bad[:30],'duplicate_rows':duplicates,'conflict_groups':len(conflicts)+len(existing_conflicts),'heldout_overlap_rejected':len(blocked),'source':source.strip()}
        payload={'dataset_id':self.require_prepared()['manifest']['dataset_id'],'source':source.strip(),'items':sorted(items,key=lambda r:r['id']),'summary':summary}
        identity='import-'+sha(canonical(payload));immutable(self.root/'import-previews'/(identity+'.json'),payload)
        return {'preview_id':identity,'summary':summary,'sample_preview':payload['items'][:10]}

    def confirm_import(self,identity):
        if not re.fullmatch(r'import-[a-f0-9]{64}',identity):raise ValueError('Invalid preview identity')
        payload=read_json(self.root/'import-previews'/(identity+'.json'))
        if 'import-'+sha(canonical(payload))!=identity:raise ValueError('Preview checksum mismatch')
        if payload['dataset_id']!=self.require_prepared()['manifest']['dataset_id']:raise ValueError('Dataset changed')
        if not payload['items']:raise ValueError('No accepted new rows')
        with self.transaction() as c:
            previous=c.execute('SELECT id FROM imports WHERE id=?',(identity,)).fetchone()
            if previous:return {'import_id':identity,'created':False,'count':len(payload['items'])}
            if c.execute('SELECT COUNT(*) FROM samples').fetchone()[0]+len(payload['items'])>20000:raise ValueError('Review pool exceeds 20000 rows; use a separate data version')
            for row in payload['items']:
                if c.execute('SELECT id FROM samples WHERE id=?',(row['id'],)).fetchone():raise Conflict('Sample appeared after preview; preview again')
                c.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?,?)',(row['id'],row['text'],row['label'],row['label'],0,payload['source'],0,now()))
            c.execute('INSERT INTO imports VALUES (?,?,?,?)',(identity,payload['source'],json.dumps(payload['summary']),now()))
        return {'import_id':identity,'created':True,'count':len(payload['items'])}

    def samples(self,limit=30,offset=0,q='',label='',pending=False):
        limit=max(1,min(int(limit),20000));offset=max(0,int(offset));where=[];args=[]
        if q:where.append('text LIKE ?');args.append('%'+q+'%')
        if label:where.append('label=?');args.append(label)
        if pending:where.append('reviewed=0')
        suffix=(' WHERE '+' AND '.join(where)) if where else ''
        with self.connect() as c:
            total=c.execute('SELECT COUNT(*) FROM samples'+suffix,args).fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT * FROM samples'+suffix+' ORDER BY reviewed,updated_at DESC,id LIMIT ? OFFSET ?',args+[limit,offset])]
        return {'items':rows,'total':total,'limit':limit,'offset':offset}

    def sample(self,identity):
        with self.connect() as c:
            row=c.execute('SELECT * FROM samples WHERE id=?',(identity,)).fetchone()
            if not row:raise LookupError('Sample not found')
            result=dict(row);result['history']=[dict(r) for r in c.execute('SELECT * FROM corrections WHERE sample_id=? ORDER BY revision',(identity,))];return result

    def review(self,identity,label,note,expected_revision):
        if label not in LABELS or not isinstance(note,str) or len(note)>500 or type(expected_revision)!=int:raise ValueError('Invalid review fields')
        with self.transaction() as c:
            row=c.execute('SELECT * FROM samples WHERE id=?',(identity,)).fetchone()
            if row is None:raise LookupError('Sample not found')
            if row['revision']!=expected_revision:raise Conflict('A newer revision exists; reload before saving')
            changed=row['label']!=label
            if changed and not note.strip():raise ValueError('Label corrections require a reason')
            if not row['reviewed'] and not note.strip():raise ValueError('Review confirmation requires a reason')
            revision=row['revision']+int(changed or not row['reviewed']);stamp=now()
            if changed:
                c.execute('INSERT INTO corrections(sample_id,old_label,new_label,note,revision,created_at) VALUES (?,?,?,?,?,?)',(identity,row['label'],label,note.strip(),revision,stamp))
            if not row['reviewed'] or changed:
                c.execute('UPDATE samples SET label=?,revision=?,reviewed=1,updated_at=? WHERE id=?',(label,revision,stamp,identity))
                c.execute('INSERT INTO reviews(sample_id,note,created_at) VALUES (?,?,?)',(identity,note.strip(),stamp))
        return {'sample':self.sample(identity),'changed':changed}

    def snapshot(self):
        dataset=self.require_prepared()
        with self.connect() as c:
            c.execute('BEGIN')
            rows=[dict(r) for r in c.execute('SELECT * FROM samples WHERE reviewed=1 ORDER BY id')]
            history=[dict(r) for r in c.execute('SELECT * FROM corrections ORDER BY sample_id,revision')]
        if not rows or len({r['label'] for r in rows})<2:raise ValueError('Reviewed training data requires both classes')
        content={'dataset_id':dataset['manifest']['dataset_id'],'split':'train','samples':rows,'corrections':history,'count':len(rows),'class_counts':dict(Counter(r['label'] for r in rows)),'validation_sha256':sha(canonical(dataset['validation']))}
        identity='snapshot-'+sha(canonical(content));value={'snapshot_id':identity,'sha256':sha(canonical(content)),'content':content};immutable(self.root/'snapshots'/(identity+'.json'),value)
        return {k:v for k,v in value.items() if k!='content'}|{'count':len(rows),'corrections':len(history),'path':str(self.root/'snapshots'/(identity+'.json'))}

    def snapshots(self):
        result=[]
        for p in sorted((self.root/'snapshots').glob('snapshot-*.json')):
            d=read_json(p);result.append({'snapshot_id':d['snapshot_id'],'count':d['content']['count'],'corrections':len(d['content']['corrections']),'dataset_id':d['content']['dataset_id']})
        return result

    def load_snapshot(self,identity):
        if not re.fullmatch(r'snapshot-[a-f0-9]{64}',identity):raise ValueError('Invalid snapshot identity')
        d=read_json(self.root/'snapshots'/(identity+'.json'));content=d['content']
        if d['snapshot_id']!=identity or d['sha256']!=sha(canonical(content)) or identity!='snapshot-'+d['sha256']:raise ValueError('Snapshot checksum mismatch')
        dataset=self.require_prepared()
        if content['dataset_id']!=dataset['manifest']['dataset_id'] or content['validation_sha256']!=sha(canonical(dataset['validation'])) or content['split']!='train':raise ValueError('Snapshot dataset mismatch')
        held={r['id'] for r in read_json(self.root/'split-index.json')['index'] if r['split']!='train'}
        rows=content['samples']
        if len(rows)!=content['count'] or len({r['id'] for r in rows})!=len(rows) or dict(Counter(r['label'] for r in rows))!=content['class_counts']:raise ValueError('Snapshot count or class identity mismatch')
        if any(r['id'] in held or sha(r['text'].encode())!=r['id'] or r['label'] not in LABELS or not r['reviewed'] for r in content['samples']):raise ValueError('Invalid or held-out training rows')
        return d
