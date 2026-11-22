import argparse
import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .storage import Vault
from . import documents, rendering

STATIC = Path(__file__).parent / 'static'

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, vault):
        super().__init__(address, Handler)
        self.vault = vault
        self.bootstrap = secrets.token_urlsafe(32)
        self.sessions = {}
        self.auth_guard = threading.Lock()
        self.failures = []

class Handler(BaseHTTPRequestHandler):
    server_version = 'CVault'
    def log_message(self, *_):
        pass  # Do not log private filenames or access tokens.

    def send(self, status, data, content_type='application/json', filename=None):
        if content_type == 'application/json':
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        size = int(self.headers.get('Content-Length', '0'))
        if size <= 0 or size > 29 * 1024 * 1024:
            raise ValueError('Request size is invalid (20 MB document limit).')
        result = json.loads(self.rfile.read(size))
        if not isinstance(result, dict):
            raise ValueError('Expected a JSON object.')
        return result

    def valid_host(self):
        expected = '127.0.0.1:%s' % self.server.server_port
        if self.headers.get('Host') != expected:
            self.send(403, {'error': 'Local access only.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + expected:
            self.send(403, {'error': 'Cross-origin requests are not allowed.'})
            return False
        return True

    def auth(self):
        token = self.headers.get('X-CVault-Token', '')
        with self.server.auth_guard:
            last = self.server.sessions.get(token)
            if last and time.monotonic() - last < 15 * 60:
                self.server.sessions[token] = time.monotonic()
                return True
        self.send(401, {'error': 'Workspace locked. Unlock to continue.'})
        return False

    def do_GET(self):
        if not self.valid_host():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == '/api/auth':
                return self.send(200, {'password': bool(self.server.vault.setting('password'))})
            if path.startswith('/api/'):
                if not self.auth():
                    return
                vault = self.server.vault
                if path == '/api/library':
                    return self.send(200, dict(vault.listing(), renderer=bool(rendering.renderer())))
                if path.startswith('/api/documents/'):
                    return self.send(200, vault.get(path.rsplit('/', 1)[-1]))
                if path == '/api/backup':
                    return self.send(200, vault.backup(), 'application/vnd.sqlite3', 'CVault-backup.sqlite3')
                if path == '/api/export':
                    q = urllib.parse.parse_qs(parsed.query)
                    doc_id = q.get('id', [''])[0]
                    rev = q.get('revision', [None])[0]
                    doc = vault.get(doc_id)
                    data = vault.blob(doc_id, rev, q.get('master', [''])[0] == '1')
                    fmt = q.get('format', ['docx'])[0]
                    if fmt not in ('docx', 'pdf'):
                        raise ValueError('Unsupported export format.')
                    if fmt == 'pdf':
                        data = rendering.pdf(data)
                    name = ''.join(c for c in doc['name'] if c.isalnum() or c in ' -_').strip() or 'Resume'
                    return self.send(200, data, 'application/pdf' if fmt == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', name + '.' + fmt)
                if path == '/api/package':
                    q = urllib.parse.parse_qs(parsed.query)
                    app = next((a for a in vault.listing()['applications'] if a['id'] == q.get('id', [''])[0]), None)
                    if not app:
                        raise ValueError('Application not found.')
                    fmt = q.get('format', ['docx'])[0]
                    if fmt not in ('docx', 'pdf'):
                        raise ValueError('Unsupported export format.')
                    out = io.BytesIO()
                    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
                        for kind, label in [('resume', 'Resume'), ('cover', 'Cover-Letter')]:
                            if app[kind + '_id']:
                                data = vault.blob(app[kind + '_id'], app[kind + '_revision'])
                                z.writestr(label + '.' + fmt, rendering.pdf(data) if fmt == 'pdf' else data)
                    return self.send(200, out.getvalue(), 'application/zip', 'Application-' + fmt.upper() + '.zip')
                return self.send(404, {'error': 'Not found.'})
            name = 'index.html' if path == '/' else path.lstrip('/')
            if name not in ('index.html', 'app.js', 'style.css'):
                return self.send(404, {'error': 'Not found.'})
            return self.send(200, (STATIC / name).read_bytes(), mimetypes.guess_type(name)[0] or 'text/plain')
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': 'An internal error occurred. Your saved revisions are retained.'})

    def do_POST(self):
        if not self.valid_host():
            return
        try:
            body = self.body()
            path = urllib.parse.urlparse(self.path).path
            vault = self.server.vault
            if path == '/api/unlock':
                with self.server.auth_guard:
                    self.server.failures = [t for t in self.server.failures if time.monotonic() - t < 60]
                    if len(self.server.failures) >= 5:
                        return self.send(429, {'error': 'Too many attempts. Wait one minute.'})
                    valid = hmac.compare_digest(str(body.get('bootstrap', '')), self.server.bootstrap)
                    stored = vault.setting('password')
                    if stored:
                        salt, expected = stored.split(':')
                        actual = hashlib.pbkdf2_hmac('sha256', str(body.get('password', '')).encode(), bytes.fromhex(salt), 300000).hex()
                        valid = valid and hmac.compare_digest(actual, expected)
                    if not valid:
                        self.server.failures.append(time.monotonic())
                        return self.send(401, {'error': 'Incorrect password or launch link. Open CVault from its launcher.'})
                    token = secrets.token_urlsafe(32)
                    self.server.sessions[token] = time.monotonic()
                    return self.send(200, {'token': token})
            if not self.auth():
                return
            if path == '/api/lock':
                with self.server.auth_guard:
                    self.server.sessions.pop(self.headers.get('X-CVault-Token'), None)
                return self.send(200, {'ok': True})
            if path == '/api/password':
                password = body.get('password', '')
                if not isinstance(password, str) or len(password) < 10 or len(password) > 200:
                    raise ValueError('Use a password between 10 and 200 characters.')
                salt = secrets.token_bytes(16)
                hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 300000).hex()
                vault.setting('password', salt.hex() + ':' + hashed)
                with self.server.auth_guard:
                    current = self.headers.get('X-CVault-Token')
                    self.server.sessions = {current: time.monotonic()}
                return self.send(200, {'ok': True})
            if path == '/api/import':
                if not str(body.get('filename', '')).lower().endswith('.docx'):
                    raise ValueError('Import a .docx file. Convert older .doc files in Word first.')
                return self.send(200, vault.import_doc(body.get('name'), body.get('category'), body.get('kind'), base64.b64decode(body.get('data', ''), validate=True)))
            if path == '/api/sample':
                kind = body.get('kind', 'resume')
                return self.send(200, vault.import_doc('Sample cover letter' if kind == 'cover' else 'Sample professional resume', 'Cover Letters' if kind == 'cover' else 'Professional Jobs', kind, documents.sample(kind)))
            if path == '/api/categories':
                vault.category(body.get('name'))
                return self.send(200, vault.listing())
            if path.startswith('/api/documents/'):
                doc_id = path.rsplit('/', 1)[-1]
                if body.get('action') == 'duplicate':
                    return self.send(200, vault.duplicate(doc_id, body.get('name'), body.get('category')))
                return self.send(200, vault.update(doc_id, body))
            if path == '/api/applications':
                return self.send(200, vault.application(body))
            if path == '/api/status':
                vault.status(body.get('id'), body.get('status'))
                return self.send(200, vault.listing())
            self.send(404, {'error': 'Not found.'})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': 'An internal error occurred. Your saved revisions are retained.'})

def main():
    parser = argparse.ArgumentParser(description='CVault private local workspace')
    parser.add_argument('--data-dir', default=str(Path.home() / 'Library' / 'Application Support' / 'CVault') if os.sys.platform == 'darwin' else str(Path.home() / '.local' / 'share' / 'CVault'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    server = Server(('127.0.0.1', args.port), Vault(args.data_dir))
    url = 'http://127.0.0.1:%s/#%s' % (server.server_port, server.bootstrap)
    print('CVault is running locally. Keep this terminal open. Press Ctrl+C to stop.', flush=True)
    print(url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
