"""Real browser/renderer smoke test. Requires Playwright and LibreOffice."""
import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright

with tempfile.TemporaryDirectory() as directory:
    process = subprocess.Popen([sys.executable, '-m', 'cvault.server', '--data-dir', directory, '--no-browser'], stdout=subprocess.PIPE, text=True)
    try:
        process.stdout.readline()
        url = process.stdout.readline().strip()
        assert url.startswith('http://127.0.0.1:')
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url)
            page.get_by_role('button', name='Unlock CVault').click()
            page.get_by_role('heading', name='Your document library').wait_for()
            page.get_by_role('button', name='Try a sample resume').click()
            page.get_by_role('heading', name='Sample professional resume').wait_for()
            page.get_by_label('Professional Summary Protected').check()
            editor = page.get_by_role('textbox', name='Professional Summary text segment 0')
            editor.fill('Experienced administrator with a strong record of customer service.')
            page.get_by_text('All changes saved · revision 2', exact=True).wait_for()
            page.get_by_role('button', name='Review changes').click()
            page.locator('ins').filter(has_text='Experienced administrator').wait_for()
            page.get_by_role('button', name='Close', exact=True).click()
            page.get_by_role('button', name='Export documents').click()
            with page.expect_download() as event:
                page.get_by_role('button', name='Download Word', exact=True).click()
            data = Path(event.value.path()).read_bytes()
            with zipfile.ZipFile(io.BytesIO(data)) as doc:
                assert b'Experienced administrator' in doc.read('word/document.xml')
            with page.expect_download() as event:
                page.get_by_role('button', name='Download PDF', exact=True).click()
            assert Path(event.value.path()).read_bytes().startswith(b'%PDF-')
            page.get_by_role('button', name='Close', exact=True).click()
            page.get_by_role('button', name='Workspace settings').click()
            page.get_by_role('button', name='Try a sample cover letter').click()
            page.get_by_role('heading', name='Sample cover letter', exact=True).wait_for()
            page.get_by_role('button', name='Save application', exact=True).click()
            page.get_by_label('Job title', exact=True).fill('Administrator')
            page.get_by_label('Company', exact=True).fill('Example Company')
            page.get_by_label('Cover letter (optional)').select_option(label='Sample cover letter')
            page.get_by_role('button', name='Save application', exact=True).last.click()
            page.get_by_role('heading', name='Your applications').wait_for()
            with page.expect_download() as event:
                page.get_by_role('button', name='PDF ZIP').click()
            with zipfile.ZipFile(event.value.path()) as package:
                assert set(package.namelist()) == {'Resume.pdf','Cover-Letter.pdf'}
                assert all(package.read(n).startswith(b'%PDF-') for n in package.namelist())
            page.get_by_role('button', name='Document library').click()
            Path('artifacts').mkdir(exist_ok=True)
            page.screenshot(path='artifacts/library-desktop.png', full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path='artifacts/library-mobile.png', full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors, errors
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=15)
print('Browser, DOCX export, PDF export and two-document application package passed.')
