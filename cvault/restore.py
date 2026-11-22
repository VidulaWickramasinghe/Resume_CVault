"""Restore a validated backup into a new, empty workspace only."""
import argparse
import os
import sqlite3
from pathlib import Path

def restore(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('Backup does not exist.')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Restore requires an empty destination; the existing vault was not changed.')
    db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup integrity check failed.')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'documents', 'revisions', 'applications', 'settings', 'categories'}.issubset(tables):
            raise ValueError('This is not a CVault backup.')
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = destination / 'cvault.sqlite3'
        # Exclusive creation prevents an overwrite if another process creates it.
        with target.open('xb'):
            pass
        target.chmod(0o600)
        output = sqlite3.connect(str(target))
        try:
            db.backup(output)
        finally:
            output.close()
    finally:
        db.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup')
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        restore(args.backup, args.data_dir)
    except (ValueError, sqlite3.DatabaseError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    print('Backup restored into the new workspace.')

if __name__ == '__main__':
    main()
