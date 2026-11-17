from pathlib import Path
import json
import pytest
from roadcase.ocr import register, preview

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_frozen_ocr_token_preview(tmp_path):
    db = tmp_path / 'state.db'
    first = register(db, FIX / 'ocr-bulletin.json')
    assert register(db, FIX / 'ocr-bulletin.json') == first
    text = preview(db, 'scan-road-8')['flat_text']
    assert 'road-ocr-8' in text and 'R8' in text
    altered = json.loads((FIX / 'ocr-bulletin.json').read_text(encoding='utf-8'))
    next(x for x in altered['tokens'] if x['id'] == 'p2-07')['alternatives'][0]['text'] = '3'
    path = tmp_path / 'altered.json'
    path.write_text(json.dumps(altered), encoding='utf-8')
    with pytest.raises(ValueError, match='ocr_scan_conflict'):
        register(db, path)
