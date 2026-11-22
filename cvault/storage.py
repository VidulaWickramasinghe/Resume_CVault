import contextlib
import datetime
import json
import sqlite3
import uuid
from pathlib import Path
from . import documents


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def identifier():
    return uuid.uuid4().hex

class Vault:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'cvault.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY);
            INSERT OR IGNORE INTO categories VALUES ('Odd Jobs'), ('Professional Jobs'), ('Cover Letters');
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL,
                master BLOB NOT NULL, latest BLOB NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                mapping TEXT NOT NULL DEFAULT '{}', unlocked TEXT NOT NULL DEFAULT '[]',
                created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions (
                doc_id TEXT NOT NULL REFERENCES documents(id), revision INTEGER NOT NULL, data BLOB NOT NULL,
                label TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(doc_id, revision));
            CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, location TEXT NOT NULL,
                description TEXT NOT NULL, status TEXT NOT NULL, resume_id TEXT NOT NULL,
                resume_revision INTEGER NOT NULL, cover_id TEXT, cover_revision INTEGER,
                created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def listing(self):
        with self.connect() as db:
            return {'documents': [dict(r) for r in db.execute('SELECT id,name,category,kind,revision,created,updated FROM documents ORDER BY updated DESC')],
                    'categories': [r[0] for r in db.execute('SELECT name FROM categories ORDER BY name')],
                    'applications': [dict(r) for r in db.execute('SELECT * FROM applications ORDER BY updated DESC')]}

    def import_doc(self, name, category, kind, data):
        documents.inspect(data)
        if kind not in ('resume', 'cover') or not isinstance(name, str) or not name.strip() or len(name) > 150:
            raise ValueError('Enter a document name of up to 150 characters.')
        doc_id, date = identifier(), now()
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM categories WHERE name=?', (category,)).fetchone():
                raise ValueError('Select an existing category.')
            db.execute('INSERT INTO documents (id,name,category,kind,master,latest,created,updated) VALUES (?,?,?,?,?,?,?,?)', (doc_id, name.strip(), category, kind, data, data, date, date))
            db.execute('INSERT INTO revisions VALUES (?,?,?,?,?)', (doc_id, 0, data, 'Imported original', date))
        return self.get(doc_id)

    def get(self, doc_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if not row:
                raise ValueError('Document not found.')
            result = dict(row)
            latest, master = result.pop('latest'), result.pop('master')
            result['mapping'] = json.loads(result['mapping'])
            result['unlocked'] = json.loads(result['unlocked'])
            result['model'] = documents.inspect(latest, result['mapping'])
            result['original'] = documents.inspect(master, result['mapping'])
            result['history'] = [dict(r) for r in db.execute('SELECT revision,label,created FROM revisions WHERE doc_id=? ORDER BY revision DESC', (doc_id,))]
            return result

    def blob(self, doc_id, revision=None, master=False):
        with self.connect() as db:
            if revision is not None:
                row = db.execute('SELECT data FROM revisions WHERE doc_id=? AND revision=?', (doc_id, int(revision))).fetchone()
            else:
                row = db.execute('SELECT %s FROM documents WHERE id=?' % ('master' if master else 'latest'), (doc_id,)).fetchone()
            if not row:
                raise ValueError('Document revision not found.')
            return row[0]

    def update(self, doc_id, body):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if not row:
                raise ValueError('Document not found.')
            if body.get('revision') != row['revision']:
                raise ValueError('This document changed in another window. Reopen it before saving.')
            mapping = json.loads(row['mapping'])
            unlocked = json.loads(row['unlocked'])
            data = row['latest']
            action = body.get('action', 'save')
            label = 'Saved changes'
            if action == 'unlock':
                allowed = documents.inspect(data, mapping)['sections']
                unlocked = body.get('sections', [])
                if not isinstance(unlocked, list) or any(s not in allowed for s in unlocked):
                    raise ValueError('Invalid section selection.')
                label = 'Updated editing permissions'
            elif action == 'map':
                mapping = body.get('mapping')
                ids = {p['id'] for p in documents.inspect(data)['paragraphs']}
                if not isinstance(mapping, dict) or any(k not in ids or not isinstance(v, str) or not v.strip() or len(v) > 80 for k,v in mapping.items()):
                    raise ValueError('Invalid section mapping.')
                unlocked = []
                label = 'Updated section mapping'
            elif action == 'restore':
                old = db.execute('SELECT data FROM revisions WHERE doc_id=? AND revision=?', (doc_id, body.get('target'))).fetchone()
                if not old:
                    raise ValueError('Revision not found.')
                data = old[0]
                unlocked = []
                label = 'Restored revision %s' % body['target']
            elif action == 'save':
                data = documents.edit(data, body.get('changes', {}), unlocked, mapping)
            else:
                raise ValueError('Unknown document action.')
            rev, date = row['revision'] + 1, now()
            db.execute('UPDATE documents SET latest=?,revision=?,mapping=?,unlocked=?,updated=? WHERE id=?', (data, rev, json.dumps(mapping), json.dumps(unlocked), date, doc_id))
            db.execute('INSERT INTO revisions VALUES (?,?,?,?,?)', (doc_id, rev, data, label, date))
        return self.get(doc_id)

    def duplicate(self, doc_id, name, category):
        source = self.get(doc_id)
        result = self.import_doc(name, category, source['kind'], self.blob(doc_id))
        with self.connect() as db:
            db.execute('UPDATE documents SET mapping=? WHERE id=?', (json.dumps(source['mapping']), result['id']))
        return self.get(result['id'])

    def category(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('Use a category name of 1–80 characters.')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO categories VALUES (?)', (name.strip(),))

    def application(self, body):
        title, company = str(body.get('title', '')).strip(), str(body.get('company', '')).strip()
        if not title or not company:
            raise ValueError('Job title and company are required.')
        if body.get('status', 'Draft') not in ('Draft', 'Applied', 'Interview', 'Rejected', 'Offer'):
            raise ValueError('Invalid application status.')
        resume = self.get(body.get('resume_id'))
        cover = self.get(body['cover_id']) if body.get('cover_id') else None
        if resume['kind'] != 'resume' or (cover and cover['kind'] != 'cover'):
            raise ValueError('Select a resume and an optional cover letter.')
        app_id, date = identifier(), now()
        with self.connect() as db:
            db.execute('INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (app_id, title[:200], company[:200], str(body.get('location', ''))[:200], str(body.get('description', ''))[:30000], body.get('status', 'Draft'), resume['id'], resume['revision'], cover['id'] if cover else None, cover['revision'] if cover else None, date, date))
        return self.listing()

    def status(self, app_id, status):
        if status not in ('Draft', 'Applied', 'Interview', 'Rejected', 'Offer'):
            raise ValueError('Invalid status.')
        with self.connect() as db:
            if not db.execute('UPDATE applications SET status=?,updated=? WHERE id=?', (status, now(), app_id)).rowcount:
                raise ValueError('Application not found.')

    def setting(self, key, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def backup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'backup.sqlite3'
            with self.connect() as db:
                dest = sqlite3.connect(str(target))
                try:
                    db.backup(dest)
                finally:
                    dest.close()
            return target.read_bytes()
