# CVault

**Your career. Your documents. Your control.**

CVault is a local resume customisation workspace. Import Word resumes and cover letters, unlock selected sections, edit text without rebuilding the document, keep recoverable versions, and prepare application packages.

## Current release

Version **0.1.0 — local browser edition**. Implemented in Python 3.9+ (standard library), SQLite and dependency-free JavaScript. This is a working first implementation, **not the completed Tauri/macOS product described in the original brief**. The native desktop shell, macOS authentication, automatic cover-letter field substitution, reusable content library, section reordering, adding/removing paragraphs and encrypted backups remain future work. Existing paragraph text can be edited, including each separate styled segment.

No AI, analytics, external fonts, cloud APIs or network document processing are used.

## Run

On macOS, double-click **Launch CVault.command**, or run from the repository:

```sh
python3 -m cvault.server
```

The launcher opens a private localhost URL in your browser. Keep the terminal running; press Ctrl+C to quit. Python 3.9 or later is required. No pip installation is needed for the application.

For a workspace stored in this project:

```sh
python3 -m cvault.server --data-dir .cvault-data
```

The default data directory on macOS is `~/Library/Application Support/CVault`. On other platforms it is `~/.local/share/CVault`. Data is never stored in the source repository by default.

## Workflow

1. Unlock the local workspace. Optionally set a password in Workspace settings.
2. Import a `.docx` file or try the clearly labelled sample resume.
3. Choose editable sections. Use **Map sections** if automatic headings do not match your document.
4. Edit the exposed text fields. Unselected content and styles are protected. Changes autosave after a short pause; confirm the saved indicator before closing.
5. Review changes, restore a previous revision, or save a named version in a category.
6. Import and tailor a cover letter the same way.
7. Save an application with the exact current resume and optional cover-letter revisions.
8. Download the Word files, individual PDFs, or a ZIP containing the separate application documents.

Original imported bytes remain recoverable. Application snapshots refer to immutable revisions, so later editing cannot change a historical package.

## PDF export and preview

Install [LibreOffice](https://www.libreoffice.org/download/download-libreoffice/) locally. On macOS, install it in `/Applications`. Linux installations can expose `libreoffice` or `soffice` on PATH. Alternatively set `CVAULT_LIBREOFFICE` to the executable's absolute path.

CVault invokes the local renderer with a temporary isolated profile. It never sends documents to an online converter. DOCX editing/export works without LibreOffice; PDF export reports a clear error if it is absent. Preview shows the actual rendered PDF, not reconstructed HTML. LibreOffice may paginate differently from Microsoft Word; review your final PDF. Word automation is not implemented in this release.

## Document support and fidelity

- Standard transitional OOXML `.docx`, using the conventional `w` namespace prefix.
- Plain text in existing paragraphs, including mixed-style runs and table-cell paragraphs.
- Text changes patch XML text-node payloads in place. Unchanged package-part contents and surrounding XML are retained; ZIP container bytes are regenerated for edited copies. Imported originals remain byte-for-byte unchanged.
- Heading detection recognises common names and Heading1–Heading6 styles. Mapping overrides are stored per document.
- Headings, fields, hyperlinks, drawings, tabs, explicit line breaks and other complex inline content are protected.
- Documents with text boxes, tracked changes or embedded alternative content are read-only. Accept tracked changes/simplify unsupported structures in Word before importing.
- Older `.doc`, macro-enabled files, strict OOXML and arbitrary rich-text editing are not supported.
- Changing text length can change page breaks. The editor is a **content editor**, not a Word layout engine. A text-growth warning is only guidance; use PDF preview for pagination.
- Readiness checks report observable properties, not an ATS score or hiring prediction.

## Privacy and recovery

The server binds only to `127.0.0.1` on a random port. It validates Host and Origin, requires a launch secret and session token, uses no CORS, and expires sessions after 15 minutes without API activity. The UI also locks after 15 minutes without user interaction. Optional passwords use salted PBKDF2-HMAC-SHA256 (300,000 iterations), with login throttling. This is application access control, **not encryption** or protection from a compromised OS account.

SQLite transactions commit each revision and the latest document together. An interrupted save retains the previous committed revision. Edits typed since the last successful autosave are not guaranteed to survive a crash. Failed-save text stays in the open editor, and closing with unsaved edits triggers a browser warning.

Download a consistent backup in Workspace settings. It contains personal documents and is **unencrypted**; store it securely. To restore, stop CVault, keep a copy of the current data directory, and use:

```sh
python3 -m cvault.restore /path/to/CVault-backup.sqlite3 --data-dir /path/to/new-empty-vault
python3 -m cvault.server --data-dir /path/to/new-empty-vault
```

Restore only accepts a new empty destination. It does not overwrite a working vault.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node --check cvault/static/app.js
```

Tests cover protected sections, unchanged XML/ZIP parts, styled text, malformed imports, immutable originals, save conflicts, restore, backups, application snapshots, authentication and HTTP endpoints. Endpoint tests use an in-memory transport and do not bind a network port.

The CI workflow additionally installs LibreOffice and Playwright to exercise a real browser, local server and PDF conversion. Those integration checks must pass before release. The initial development environment did not have a PDF renderer, and localhost runtime permission was declined, so local browser/PDF verification was not completed.

## Architecture

- `cvault/documents.py`: conservative DOCX parsing and text patching.
- `cvault/storage.py`: SQLite library, revision history and immutable application references.
- `cvault/server.py`: loopback-only API, access control and file exports.
- `cvault/rendering.py`: local LibreOffice PDF conversion.
- `cvault/static/`: responsive library, editor, application tracker and settings.
- `tests/`: document, persistence and in-memory API tests.
- `scripts/browser_smoke.py`: actual browser and renderer smoke test for CI.

Reference formats: [Microsoft WordprocessingML structure](https://learn.microsoft.com/en-us/office/open-xml/word/structure-of-a-wordprocessingml-document), [LibreOffice startup parameters](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html).
