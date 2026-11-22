"""Use a local office renderer; never substitute an HTML reconstruction."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def renderer():
    candidates = [os.environ.get('CVAULT_LIBREOFFICE'), '/Applications/LibreOffice.app/Contents/MacOS/soffice', shutil.which('libreoffice'), shutil.which('soffice')]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def pdf(data):
    executable = renderer()
    if not executable:
        raise ValueError('PDF export needs LibreOffice installed locally. Install LibreOffice, restart CVault, and try again. You can export DOCX now.')
    with tempfile.TemporaryDirectory(prefix='cvault-render-') as folder:
        root = Path(folder)
        source = root / 'document.docx'
        source.write_bytes(data)
        try:
            result = subprocess.run([executable, '-env:UserInstallation=' + (root / 'profile').as_uri(), '--headless', '--convert-to', 'pdf:writer_pdf_Export', '--outdir', folder, str(source)], capture_output=True, timeout=90)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('PDF rendering timed out. Your Word document is saved; retry or export DOCX.') from exc
        output = root / 'document.pdf'
        if result.returncode or not output.exists() or not output.read_bytes().startswith(b'%PDF-'):
            raise ValueError('LibreOffice could not render this document. Export DOCX and inspect it in your word processor.')
        return output.read_bytes()
