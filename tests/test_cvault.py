import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from cvault import documents
from cvault.storage import Vault
from cvault.server import Handler
from types import SimpleNamespace
import secrets
from email.parser import BytesParser

class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.original = documents.sample()
        self.model = documents.inspect(self.original)
        self.summary = next(p for p in self.model['paragraphs'] if p['section']=='Professional Summary' and not p['heading'])

    def test_replaces_only_selected_text_and_preserves_other_parts(self):
        changed=documents.edit(self.original, {self.summary['id']:{'0':'Tailored & factual <summary>.'}}, ['Professional Summary'])
        before,bxml,_=documents.unpack(self.original)
        after,axml,_=documents.unpack(changed)
        for name in before:
            if name!='word/document.xml':
                self.assertEqual(before[name],after[name])
        self.assertEqual(axml,bxml.replace(self.summary['text'].encode(),b'Tailored &amp; factual &lt;summary&gt;.'))
        self.assertEqual(documents.inspect(changed)['paragraphs'][3]['text'],'Tailored & factual <summary>.')

    def test_locked_edit_rejected(self):
        with self.assertRaisesRegex(ValueError,'protected'):
            documents.edit(self.original,{self.summary['id']:{'0':'Changed'}},[])

    def test_heading_edit_rejected(self):
        heading=next(p for p in self.model['paragraphs'] if p['heading'])
        with self.assertRaises(ValueError):
            documents.edit(self.original,{heading['id']:{'0':'Changed'}},self.model['sections'])

    def test_invalid_run_and_newlines_rejected(self):
        for runs in ({'999':'Bad'},{'0':'Two\nparagraphs'},{'0':'Invalid\x00'}):
            with self.assertRaises(ValueError):
                documents.edit(self.original,{self.summary['id']:runs},['Professional Summary'])

    def test_bad_zip_rejected(self):
        with self.assertRaises(ValueError):
            documents.inspect(b'not a docx')

    def test_mapping_and_mixed_styles(self):
        parts,xml,_=documents.unpack(self.original)
        text=self.summary['text'].encode()
        xml=xml.replace(text,b'First</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>Bold')
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for name,value in parts.items():z.writestr(name,xml if name=='word/document.xml' else value)
        mapping={self.summary['id']:'Custom'}
        changed=documents.edit(out.getvalue(),{self.summary['id']:{'1':'Still bold'}},['Custom'],mapping)
        self.assertIn(b'<w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Still bold',documents.unpack(changed)[1])
        self.assertIn(b'>First</w:t>',documents.unpack(changed)[1])

    def test_text_boxes_read_only(self):
        parts,xml,_=documents.unpack(self.original)
        xml=xml.replace(b'<w:body>',b'<w:body><w:txbxContent/>')
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for name,value in parts.items():z.writestr(name,xml if name=='word/document.xml' else value)
        self.assertTrue(documents.inspect(out.getvalue())['read_only'])

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.vault=Vault(self.temp.name)
        self.data=documents.sample()
        self.doc=self.vault.import_doc('My resume','Professional Jobs','resume',self.data)
    def tearDown(self):self.temp.cleanup()
    def test_save_restore_and_conflict(self):
        d=self.vault.update(self.doc['id'],{'action':'unlock','revision':0,'sections':['Professional Summary']})
        p=next(p for p in d['model']['paragraphs'] if p['section']=='Professional Summary' and not p['heading'])
        d=self.vault.update(d['id'],{'revision':1,'changes':{p['id']:{'0':'Updated.'}}})
        self.assertEqual(self.vault.blob(d['id'],master=True),self.data)
        self.assertNotEqual(self.vault.blob(d['id']),self.data)
        with self.assertRaisesRegex(ValueError,'another window'):
            self.vault.update(d['id'],{'revision':1,'changes':{}})
        restored=self.vault.update(d['id'],{'action':'restore','revision':2,'target':0})
        self.assertEqual(restored['revision'],3)
        self.assertEqual(self.vault.blob(d['id']),self.data)
        self.assertEqual(restored['unlocked'],[])
    def test_snapshot_survives_future_edits(self):
        cover=self.vault.import_doc('Cover','Cover Letters','cover',documents.sample('cover'))
        self.vault.application({'title':'Analyst','company':'Example','resume_id':self.doc['id'],'cover_id':cover['id']})
        app=self.vault.listing()['applications'][0]
        self.vault.update(self.doc['id'],{'action':'unlock','revision':0,'sections':['Professional Summary']})
        self.assertEqual(app['resume_revision'],0)
        self.assertEqual(self.vault.blob(app['resume_id'],app['resume_revision']),self.data)
    def test_backup_and_restart(self):
        backup=self.vault.backup()
        self.assertTrue(backup.startswith(b'SQLite format 3'))
        self.assertEqual(Vault(self.temp.name).get(self.doc['id'])['name'],'My resume')
    def test_duplicate_keeps_source(self):
        other=self.vault.duplicate(self.doc['id'],'Tailored','Odd Jobs')
        self.assertNotEqual(other['id'],self.doc['id'])
        self.assertEqual(len(self.vault.listing()['documents']),2)
    def test_invalid_category_rejected(self):
        with self.assertRaises(ValueError):self.vault.import_doc('Resume','Missing','resume',self.data)

class RestoreTests(unittest.TestCase):
    def test_restore_backup_into_new_workspace(self):
        from cvault.restore import restore
        with tempfile.TemporaryDirectory() as root:
            source=Vault(Path(root)/'source')
            doc=source.import_doc('Resume','Odd Jobs','resume',documents.sample())
            backup=Path(root)/'backup.sqlite3'
            backup.write_bytes(source.backup())
            destination=Path(root)/'restored'
            restore(backup,destination)
            self.assertEqual(Vault(destination).blob(doc['id']),source.blob(doc['id']))
            with self.assertRaisesRegex(ValueError,'empty destination'):
                restore(backup,destination)

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=SimpleNamespace(vault=Vault(self.temp.name), server_port=9876, bootstrap=secrets.token_urlsafe(32), sessions={}, auth_guard=threading.Lock(), failures=[])
        self.token=''
    def tearDown(self):
        self.temp.cleanup()
    def request(self,path,body=None,headers=None):
        payload=json.dumps(body).encode() if body is not None else b''
        h={'Host':'127.0.0.1:9876','Content-Type':'application/json','X-CVault-Token':self.token,'Content-Length':str(len(payload))}
        h.update(headers or {})
        request=(('POST' if body is not None else 'GET')+' '+path+' HTTP/1.0\r\n'+''.join(k+': '+v+'\r\n' for k,v in h.items())+'\r\n').encode()+payload
        output=io.BytesIO()
        class MemorySocket:
            def makefile(self,*args,**kwargs):return io.BytesIO(request)
            def sendall(self,data):output.write(data)
        Handler(MemorySocket(),('127.0.0.1',12345),self.server)
        head,data=output.getvalue().split(b'\r\n\r\n',1)
        return int(head.split(b' ')[1]),data
    def unlock(self):
        code,data=self.request('/api/unlock',{'bootstrap':self.server.bootstrap})
        self.assertEqual(code,200)
        self.token=json.loads(data)['token']
    def test_access_boundary(self):
        self.assertEqual(self.request('/api/library')[0],401)
        self.assertEqual(self.request('/api/auth',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request('/api/unlock',{}, {'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.request('/../storage.py')[0],404)
        self.unlock()
        self.assertEqual(self.request('/api/library')[0],200)
        self.request('/api/lock',{})
        self.assertEqual(self.request('/api/library')[0],401)
    def test_full_api_workflow(self):
        self.unlock()
        code,data=self.request('/api/sample',{'kind':'resume'})
        self.assertEqual(code,200)
        d=json.loads(data)
        code,data=self.request('/api/documents/'+d['id'],{'action':'unlock','revision':0,'sections':['Professional Summary']})
        self.assertEqual(code,200)
        d=json.loads(data)
        p=next(p for p in d['model']['paragraphs'] if p['section']=='Professional Summary' and not p['heading'])
        self.assertEqual(self.request('/api/documents/'+d['id'],{'revision':1,'changes':{p['id']:{'0':'Ready for this role.'}}})[0],200)
        self.assertEqual(self.request('/api/applications',{'title':'Analyst','company':'Company','resume_id':d['id']})[0],200)
        app=self.server.vault.listing()['applications'][0]
        code,data=self.request('/api/package?id='+app['id'])
        self.assertEqual(code,200)
        with zipfile.ZipFile(io.BytesIO(data)) as z:self.assertEqual(z.namelist(),['Resume.docx'])
        self.assertEqual(self.request('/api/export?id='+d['id'])[0],200)
    def test_password_and_throttle(self):
        self.unlock()
        self.assertEqual(self.request('/api/password',{'password':'long-test-password'})[0],200)
        for _ in range(5):self.assertEqual(self.request('/api/unlock',{'bootstrap':self.server.bootstrap,'password':'wrong'})[0],401)
        self.assertEqual(self.request('/api/unlock',{'bootstrap':self.server.bootstrap,'password':'wrong'})[0],429)

if __name__=='__main__':unittest.main()
