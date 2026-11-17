"""Freeze local OCR token boxes before structured notice extraction."""
from .source import read, digest, canonical
from .store import connect

SCHEMA = """CREATE TABLE IF NOT EXISTS ocr_scan(
 id TEXT PRIMARY KEY, token_digest TEXT NOT NULL, raw TEXT NOT NULL);
"""


def register(db_path, token_path):
    doc = read(token_path)
    if not doc.get('scan_id') or not isinstance(doc.get('tokens'), list) or not doc['tokens']:
        raise ValueError('invalid_ocr_scan')
    ids = set()
    for token in doc['tokens']:
        if not isinstance(token, dict) or not token.get('id') or token['id'] in ids:
            raise ValueError('invalid_ocr_token')
        ids.add(token['id'])
        box = token.get('box')
        options = token.get('alternatives')
        if (not isinstance(token.get('page'), int) or token['page'] < 1 or
                not isinstance(box, list) or len(box) != 4 or
                not all(isinstance(v, (int, float)) for v in box) or
                box[2] <= box[0] or box[3] <= box[1] or
                not isinstance(options, list) or not options):
            raise ValueError('invalid_ocr_geometry')
        for option in options:
            if (not isinstance(option, dict) or not isinstance(option.get('text'), str) or
                    not 0 <= option.get('confidence', -1) <= 1):
                raise ValueError('invalid_ocr_alternative')
    fp = digest(doc)
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        with db:
            old = db.execute('SELECT token_digest FROM ocr_scan WHERE id=?',
                             (doc['scan_id'],)).fetchone()
            if old and old['token_digest'] != fp:
                raise ValueError('ocr_scan_conflict')
            db.execute('INSERT OR IGNORE INTO ocr_scan VALUES (?,?,?)',
                       (doc['scan_id'], fp, canonical(doc)))
        return {'scan_id': doc['scan_id'], 'token_digest': fp}
    finally:
        db.close()


def preview(db_path, scan_id):
    import json
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT raw,token_digest FROM ocr_scan WHERE id=?', (scan_id,)).fetchone()
        if not row:
            raise ValueError('unknown_ocr_scan')
        tokens = json.loads(row['raw'])['tokens']
        ordered = sorted(tokens, key=lambda x: (x['page'], x['box'][1], x['box'][0], x['id']))
        return {'scan_id': scan_id, 'token_digest': row['token_digest'],
                'flat_text': ' '.join(max(x['alternatives'],
                                          key=lambda a: a['confidence'])['text'] for x in ordered)}
    finally:
        db.close()
