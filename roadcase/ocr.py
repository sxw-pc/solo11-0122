"""Freeze local OCR token boxes, then extract structured notices under reviewable contracts.

Extraction never regexes the flattened preview text.  It first assigns every frozen
token exactly one geometric role (table header / row cell / column, cross-line row
continuation, page header/footer, label, value, statement), then solves joint
constraints per segment: valid dates, 0 <= closed <= total, total = closed +
remaining, and cross-page agreement of repeated facts.  Candidate pruning is local
to a row (<= 3 cells x <= 3 alternatives) plus per-field candidate sets, so the
solver never enumerates the Cartesian product of every token candidate in a scan.
"""
import json
import re
from itertools import islice, product
from pathlib import Path
from .source import read, digest, canonical, utc
from .store import connect

LAYOUT_VERSION = 1
POLICIES = ("strict_literal", "corroborated")
STRICT_CONFIDENCE = 0.9

FIELD_LABELS = {
    "公告编号": "source_id",
    "修订": "revision",
    "发布": "published_at",
    "起始": "effective_from",
    "终止": "effective_until",
    "机关": "authority",
    "剩余通行能力": "remaining_capacity",
}
COLUMN_LABELS = {"路段": "row_segment", "总车道": "total_lanes",
                 "封闭": "lanes_closed", "剩余": "remaining_lanes"}
COLUMN_NAMES = {field: label for label, field in COLUMN_LABELS.items()}
LANE_FIELDS = ("total_lanes", "lanes_closed", "remaining_lanes")
INT_FIELDS = frozenset({"revision", "remaining_capacity"} | set(LANE_FIELDS))
DATE_FIELDS = ("published_at", "effective_from", "effective_until")
BULLETIN_REQUIRED = ("source_id", "revision", "published_at", "effective_from",
                     "effective_until", "authority", "remaining_capacity")
SEGMENT_RE = re.compile(r"^[A-Za-z]+\d+$")
PAGE_MARK_RE = re.compile(r"第\s*\d+\s*页|共\s*\d+\s*页|^\s*\d+\s*/\s*\d+\s*$")
STATEMENT_RE = re.compile(r"([A-Za-z]+\d+)\s*总车道\s*(\d+)")
INT_RE = re.compile(r"-?\d+")
LABELS_BY_LEN = sorted(FIELD_LABELS, key=len, reverse=True)


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
    db = connect(db_path)
    try:
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


# ---------------------------------------------------------------- geometry

def _top(alternatives):
    """Highest-confidence alternative; lowest index breaks ties (deterministic)."""
    return max(enumerate(alternatives), key=lambda p: (p[1]['confidence'], -p[0]))


def _page_lines(tokens):
    """Cluster one page's tokens into lines by vertical-interval overlap."""
    lines = []
    for tok in sorted(tokens, key=lambda t: (t['box'][1], t['box'][0], t['id'])):
        y0, y1 = tok['box'][1], tok['box'][3]
        for line in lines:
            if y0 <= line['y1'] and y1 >= line['y0']:
                line['tokens'].append(tok)
                line['y0'] = min(line['y0'], y0)
                line['y1'] = max(line['y1'], y1)
                break
        else:
            lines.append({'y0': y0, 'y1': y1, 'tokens': [tok]})
    changed = True
    while changed:  # a token may bridge two clusters; merge to fixpoint
        changed = False
        lines.sort(key=lambda l: (l['y0'], l['y1']))
        for i in range(len(lines) - 1):
            a, b = lines[i], lines[i + 1]
            if a['y1'] >= b['y0']:
                a['y1'] = max(a['y1'], b['y1'])
                a['tokens'].extend(b['tokens'])
                del lines[i + 1]
                changed = True
                break
    for line in lines:
        line['tokens'].sort(key=lambda t: (t['box'][0], t['id']))
    return lines


def _line_text(line):
    return ' '.join(_top(t['alternatives'])[1]['text'] for t in line['tokens'])


def _label_of(text):
    for label in LABELS_BY_LEN:
        if text.startswith(label):
            return label, text[len(label):].strip(' ：:，,')
    return None, None


def _edge_guard(line):
    """Table headers and label/value lines are never page header/footer."""
    tops = [_top(t['alternatives'])[1]['text'] for t in line['tokens']]
    if sum(1 for x in tops if x in COLUMN_LABELS) >= 2:
        return True
    return any(_label_of(x)[0] for x in tops)


def _mark_page_edges(pages):
    """Role page_header/page_footer: page-number marks or lines repeated across pages."""
    first_texts, last_texts = {}, {}
    for lines in pages.values():
        if not lines:
            continue
        if not _edge_guard(lines[0]):
            first_texts[_line_text(lines[0])] = first_texts.get(_line_text(lines[0]), 0) + 1
        if not _edge_guard(lines[-1]):
            last_texts[_line_text(lines[-1])] = last_texts.get(_line_text(lines[-1]), 0) + 1
    multi = len(pages) > 1
    for lines in pages.values():
        if not lines:
            continue
        first, last = lines[0], lines[-1]
        if not _edge_guard(first) and (PAGE_MARK_RE.search(_line_text(first)) or
                                       (multi and first_texts.get(_line_text(first), 0) > 1)):
            first['role'] = 'page_header'
        if not _edge_guard(last) and (PAGE_MARK_RE.search(_line_text(last)) or
                                      (multi and last_texts.get(_line_text(last), 0) > 1)):
            last['role'] = 'page_footer'


def _scan_page_tables(lines):
    """Detect tables on one page: header line -> column layout -> rows/continuations.

    Column boundaries come from this page's own header tokens, so a table whose
    columns are shifted on another page (跨页错列) is still attributed correctly.
    """
    tables, consumed = [], set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.get('role'):
            i += 1
            continue
        header_tokens = sorted((t for t in line['tokens']
                                if _top(t['alternatives'])[1]['text'] in COLUMN_LABELS),
                               key=lambda t: t['box'][0])
        fields = {COLUMN_LABELS[_top(t['alternatives'])[1]['text']] for t in header_tokens}
        if len(header_tokens) < 2 or 'row_segment' not in fields:
            i += 1
            continue
        columns = [(COLUMN_LABELS[_top(t['alternatives'])[1]['text']], t) for t in header_tokens]
        bounds = [(a['box'][2] + b['box'][0]) / 2 for (_, a), (_, b) in zip(columns, columns[1:])]
        consumed.add(id(line))
        rows, current, prev_line = [], None, line
        j = i + 1
        while j < len(lines):
            row_line = lines[j]
            if row_line.get('role'):
                break
            tops = [_top(t['alternatives'])[1]['text'] for t in row_line['tokens']]
            if any(_label_of(x)[0] for x in tops) or any(x in COLUMN_LABELS for x in tops):
                break
            cells = {}
            for tok in row_line['tokens']:
                cx = (tok['box'][0] + tok['box'][2]) / 2
                idx = sum(1 for b in bounds if cx > b)
                cells.setdefault(columns[idx][0], []).append(tok)
            seg_tokens = cells.get('row_segment', [])
            seg_text = _top(seg_tokens[0]['alternatives'])[1]['text'] if seg_tokens else None
            gap = row_line['y0'] - prev_line['y1']
            limit = max(2.0, 0.6 * (prev_line['y1'] - prev_line['y0']))
            if seg_tokens and SEGMENT_RE.match(seg_text):
                current = {'segment': seg_text, 'segment_tokens': seg_tokens,
                           'cells': {k: v for k, v in cells.items() if k != 'row_segment'}}
                rows.append(current)
                consumed.add(id(row_line))
            elif not seg_tokens and cells and current is not None and gap <= limit:
                # 跨行续接: a line with only numeric-column cells continues the row above
                for field, toks in cells.items():
                    current['cells'].setdefault(field, []).extend(toks)
                consumed.add(id(row_line))
            else:
                break
            prev_line = row_line
            j += 1
        tables.append({'columns': columns, 'rows': rows})
        i = j
    return tables, consumed


def _evidence(tok, alt_idx, role):
    alt = tok['alternatives'][alt_idx]
    return {'token_id': tok['id'], 'alternative': alt_idx, 'page': tok['page'],
            'box': tok['box'], 'confidence': alt['confidence'], 'role': role}


def _candidate(tok, alt_idx, value, role, basis):
    return {'value': value, 'evidence': _evidence(tok, alt_idx, role), 'basis': list(basis)}


def _rejection(field, value, token_id, alternative, reason):
    return {'field': field, 'value': value, 'token_id': token_id,
            'alternative': alternative, 'reason': reason}


def _parse_int(text):
    return int(text.strip()) if INT_RE.fullmatch(text.strip()) else None


def _add_field_candidate(analysis, field, tok, ai, text, role, basis):
    if field in INT_FIELDS:
        value = _parse_int(text)
        if value is None:
            analysis['rejected'].append(_rejection(field, text, tok['id'], ai, 'not_an_integer'))
            return
        if field == 'revision' and value < 1:
            analysis['rejected'].append(_rejection(field, text, tok['id'], ai, 'bad_revision'))
            return
    elif field in DATE_FIELDS:
        try:
            utc(text)
        except ValueError:
            analysis['rejected'].append(_rejection(field, text, tok['id'], ai, 'invalid_date'))
            return
        value = text
    else:
        value = text
    analysis['fields'].setdefault(field, []).append(_candidate(tok, ai, value, role, basis))


def _analyze(tokens, segment):
    """Assign every token exactly one role and collect per-field candidates."""
    pages = {}
    for tok in tokens:
        pages.setdefault(tok['page'], []).append(tok)
    page_lines = {no: _page_lines(toks) for no, toks in pages.items()}
    _mark_page_edges(page_lines)
    analysis = {'fields': {}, 'lane_rows': [], 'statements': [], 'action': [], 'rejected': []}
    for no in sorted(page_lines):
        lines = page_lines[no]
        tables, consumed = _scan_page_tables(lines)
        for table in tables:
            for field, tok in table['columns']:
                if field == 'lanes_closed':
                    # the 封闭 column makes this bulletin a closure notice
                    analysis['action'].append(
                        _candidate(tok, _top(tok['alternatives'])[0], 'close',
                                   'table_header', ['column:封闭']))
            for row in table['rows']:
                if row['segment'] != segment:
                    continue  # other segments' rows stay isolated (R9 never feeds R8)
                lane = {f: [] for f in LANE_FIELDS}
                for field in LANE_FIELDS:
                    for tok in row['cells'].get(field, []):
                        for ai, alt in enumerate(tok['alternatives']):
                            value = _parse_int(alt['text'])
                            if value is None:
                                analysis['rejected'].append(
                                    _rejection(field, alt['text'], tok['id'], ai, 'not_an_integer'))
                                continue
                            lane[field].append(_candidate(
                                tok, ai, value, 'table_cell',
                                ['table_row', 'column:' + COLUMN_NAMES[field]]))
                analysis['lane_rows'].append({
                    'page': no, 'cells': lane,
                    'segment_evidence': [_evidence(t, _top(t['alternatives'])[0], 'table_cell')
                                         for t in row['segment_tokens']]})
        for line in lines:
            if line.get('role') or id(line) in consumed:
                continue
            toks = line['tokens']
            consumed_next = set()
            labeled = False
            for ti, tok in enumerate(toks):
                label, inline = _label_of(_top(tok['alternatives'])[1]['text'])
                if not label:
                    continue
                labeled = True
                field = FIELD_LABELS[label]
                if inline:
                    for ai, alt in enumerate(tok['alternatives']):
                        same, rest = _label_of(alt['text'])
                        if same == label and rest:
                            _add_field_candidate(analysis, field, tok, ai, rest,
                                                 'labeled_value', ['label:' + label])
                elif ti + 1 < len(toks) and (ti + 1) not in consumed_next:
                    nxt = toks[ti + 1]
                    if not _label_of(_top(nxt['alternatives'])[1]['text'])[0]:
                        consumed_next.add(ti + 1)
                        for ai, alt in enumerate(nxt['alternatives']):
                            _add_field_candidate(analysis, field, nxt, ai, alt['text'],
                                                 'labeled_value', ['label:' + label])
            if labeled:
                continue
            for tok in toks:
                for ai, alt in enumerate(tok['alternatives']):
                    m = STATEMENT_RE.search(alt['text'])
                    if m and m.group(1) == segment:
                        analysis['statements'].append(
                            _candidate(tok, ai, int(m.group(2)), 'statement',
                                       ['independent_statement']))
    return analysis


# ---------------------------------------------------------------- solvers

def _row_solutions(cells):
    """(total, closed, remaining) tuples consistent with one row's own cells.

    Bounded: at most 3 cells x 3 alternatives plus arithmetic derivation of a
    single missing cell; never enumerates other rows or other tokens.
    """
    totals = cells['total_lanes'] or [None]
    closeds = cells['lanes_closed'] or [None]
    remains = cells['remaining_lanes'] or [None]
    sols = {}
    for ct in totals:
        for cc in closeds:
            for cr in remains:
                t = ct['value'] if ct else None
                c = cc['value'] if cc else None
                r = cr['value'] if cr else None
                if t is None and c is not None and r is not None:
                    t = c + r
                if c is None and t is not None and r is not None:
                    c = t - r
                if r is None and t is not None and c is not None:
                    r = t - c
                if t is None or c is None or r is None:
                    continue
                if not (0 <= c <= t and t == c + r and r >= 0):
                    continue
                entry = sols.setdefault((t, c, r), {f: [] for f in LANE_FIELDS})
                for field, cand in (('total_lanes', ct), ('lanes_closed', cc),
                                    ('remaining_lanes', cr)):
                    if cand:
                        entry[field].append(cand)
    return sols


def _lane_rejections(row_sols, pre_statement, final):
    out = []
    for row, sols in row_sols:
        for field in LANE_FIELDS:
            for cand in row['cells'][field]:
                own = [k for k, entry in sols.items()
                       if any(c is cand for c in entry[field])]
                if any(k in final for k in own):
                    continue
                if not own:
                    reason = 'arithmetic_inconsistent'
                elif not any(k in pre_statement for k in own):
                    reason = 'cross_page_inconsistent'
                else:
                    reason = 'contradicts_independent_statement'
                ev = cand['evidence']
                out.append(_rejection(field, cand['value'], ev['token_id'],
                                      ev['alternative'], reason))
    return out


def _sorted_rejected(items):
    return sorted(items, key=lambda r: (r['field'], r['token_id'], r['alternative'],
                                        canonical(r['value'])))


def _dedup_ev(evs):
    seen = {}
    for ev in evs:
        seen[(ev['token_id'], ev['alternative'])] = ev
    return [seen[k] for k in sorted(seen)]


def _solve_corroborated(analysis, segment):
    rejected = list(analysis['rejected'])
    rows = analysis['lane_rows']
    statements = analysis['statements']
    if not rows:
        return {'status': 'conflict', 'reason': 'segment_not_found', 'solutions': 0,
                'conflicts': [{'reason': 'segment_not_found', 'segment': segment}],
                'rejected': _sorted_rejected(rejected)}
    row_sols = []
    bad_rows = []
    for row in rows:
        sols = _row_solutions(row['cells'])
        row_sols.append((row, sols))
        if not sols:
            bad_rows.append({'reason': 'row_inconsistent', 'page': row['page']})
    if bad_rows:
        return {'status': 'conflict', 'reason': 'row_inconsistent', 'solutions': 0,
                'conflicts': bad_rows, 'rejected': _sorted_rejected(rejected)}
    common = None
    for row, sols in row_sols:
        common = set(sols) if common is None else common & set(sols)
    if not common:
        return {'status': 'conflict', 'reason': 'cross_page_inconsistent', 'solutions': 0,
                'conflicts': [{'reason': 'cross_page_inconsistent',
                               'pages': [r['page'] for r, _ in row_sols]}],
                'rejected': _sorted_rejected(
                    rejected + _lane_rejections(row_sols, set(), set()))}
    pre_statement = set(common)
    for st in statements:
        common = {k for k in common if k[0] == st['value']}
    if not common:
        return {'status': 'conflict', 'reason': 'statement_contradiction', 'solutions': 0,
                'conflicts': [{'reason': 'statement_contradiction',
                               'statement_totals': sorted({s['value'] for s in statements}),
                               'row_totals': sorted({k[0] for k in pre_statement})}],
                'rejected': _sorted_rejected(
                    rejected + _lane_rejections(row_sols, pre_statement, set()))}
    pool = analysis['fields']
    missing = [f for f in BULLETIN_REQUIRED if not pool.get(f)]
    if not analysis['action']:
        missing.append('action')
    if missing:
        return {'status': 'conflict', 'reason': 'missing_field:' + missing[0], 'solutions': 0,
                'conflicts': [{'reason': 'missing_field', 'field': f} for f in missing],
                'rejected': _sorted_rejected(
                    rejected + _lane_rejections(row_sols, pre_statement, common))}
    froms = sorted({c['value'] for c in pool['effective_from']})
    untils = sorted({c['value'] for c in pool['effective_until']})
    pairs = [(f, u) for f in froms for u in untils if utc(f) < utc(u)]
    if not pairs:
        return {'status': 'conflict', 'reason': 'bad_window', 'solutions': 0,
                'conflicts': [{'reason': 'bad_window', 'from': froms, 'until': untils}],
                'rejected': _sorted_rejected(
                    rejected + _lane_rejections(row_sols, pre_statement, common))}
    lane = sorted(common)
    value_options = {name: sorted({c['value'] for c in pool[name]})
                     for name in ('source_id', 'revision', 'published_at',
                                  'authority', 'remaining_capacity')}
    total = len(lane) * len(pairs)
    for values in value_options.values():
        total *= len(values)
    rejected += _lane_rejections(row_sols, pre_statement, common)
    if total == 1:
        return _ready_corroborated(analysis, segment, lane[0], row_sols, statements,
                                   pairs[0], _sorted_rejected(rejected))
    combos = product(lane, value_options['source_id'], value_options['revision'],
                     value_options['published_at'], pairs,
                     value_options['authority'], value_options['remaining_capacity'])
    witnesses = [{'source_id': sid, 'revision': rev, 'published_at': pub,
                  'effective_from': fr, 'effective_until': un, 'authority': auth,
                  'action': 'close', 'segment': segment, 'total_lanes': key[0],
                  'lanes_closed': key[1], 'remaining_lanes': key[2],
                  'remaining_capacity': cap}
                 for key, sid, rev, pub, (fr, un), auth, cap in islice(combos, 2)]
    return {'status': 'review', 'reason': 'multiple_solutions', 'solutions': total,
            'witnesses': witnesses, 'rejected': _sorted_rejected(rejected)}


def _ready_corroborated(analysis, segment, lane_key, row_sols, statements, pair, rejected):
    t, c, r = lane_key
    pool = analysis['fields']
    multi_row = len(row_sols) > 1
    lane_ev = {f: [] for f in LANE_FIELDS}
    for row, sols in row_sols:
        entry = sols.get(lane_key)
        if entry:
            for f in LANE_FIELDS:
                lane_ev[f].extend(cd['evidence'] for cd in entry[f])
    fields = {}
    for f, v in (('total_lanes', t), ('lanes_closed', c), ('remaining_lanes', r)):
        ev = list(lane_ev[f])
        basis = ['table_row', 'arithmetic:total_lanes=lanes_closed+remaining_lanes']
        if f == 'total_lanes' and statements:
            ev += [s['evidence'] for s in statements]
            basis.append('independent_statement')
        if multi_row:
            basis.append('cross_page_consistent')
        fields[f] = {'value': v, 'evidence': _dedup_ev(ev), 'basis': basis}
    for name in BULLETIN_REQUIRED:
        value = pair[0] if name == 'effective_from' else \
            pair[1] if name == 'effective_until' else \
            next(iter({cd['value'] for cd in pool[name]}))
        cands = [cd for cd in pool[name] if cd['value'] == value]
        fields[name] = {'value': value,
                        'evidence': _dedup_ev([cd['evidence'] for cd in cands]),
                        'basis': sorted({b for cd in cands for b in cd['basis']})}
    fields['action'] = {'value': 'close',
                        'evidence': _dedup_ev([cd['evidence'] for cd in analysis['action']]),
                        'basis': ['table_header:封闭']}
    fields['segment'] = {'value': segment,
                         'evidence': _dedup_ev([ev for row, _ in row_sols
                                                for ev in row['segment_evidence']]),
                         'basis': ['table_row']}
    return {'status': 'ready_for_adoption', 'reason': 'unique_solution', 'solutions': 1,
            'fields': fields, 'rejected': rejected}


def _solve_strict(analysis, segment):
    """ready only if every required field rests on single uncontested boxes >= 0.9."""
    pools = {name: list(cands) for name, cands in analysis['fields'].items()}
    pools['action'] = list(analysis['action'])
    lane_pools = {f: [] for f in LANE_FIELDS}
    for row in analysis['lane_rows']:
        for f in LANE_FIELDS:
            lane_pools[f].extend(row['cells'][f])
    lane_pools['total_lanes'].extend(analysis['statements'])
    pools.update(lane_pools)
    failures, resolved = [], {}
    for field in ('source_id', 'revision', 'published_at', 'effective_from',
                  'effective_until', 'authority', 'remaining_capacity', 'action',
                  'total_lanes', 'lanes_closed', 'remaining_lanes'):
        cands = pools.get(field, [])
        if not cands:
            failures.append({'field': field, 'reason': 'missing_field'})
            continue
        values = {}
        for cd in cands:
            values.setdefault(cd['value'], []).append(cd)
        if len(values) != 1:
            failures.append({'field': field, 'reason': 'competing_candidates',
                             'values': sorted(values, key=canonical)})
            continue
        value, group = next(iter(values.items()))
        by_token = {}
        for cd in group:
            ev = cd['evidence']
            by_token.setdefault(ev['token_id'], []).append(ev['confidence'])
        low = sorted(tok for tok, confs in by_token.items()
                     if max(confs) < STRICT_CONFIDENCE)
        if low:
            failures.append({'field': field, 'reason': 'low_confidence', 'tokens': low})
            continue
        resolved[field] = (value, group)
    if {'total_lanes', 'lanes_closed', 'remaining_lanes'} <= resolved.keys():
        t = resolved['total_lanes'][0]
        c = resolved['lanes_closed'][0]
        r = resolved['remaining_lanes'][0]
        if not (0 <= c <= t and t == c + r):
            failures.append({'field': 'lanes_closed', 'reason': 'arithmetic_inconsistent'})
    if {'effective_from', 'effective_until'} <= resolved.keys():
        if utc(resolved['effective_from'][0]) >= utc(resolved['effective_until'][0]):
            failures.append({'field': 'effective_until', 'reason': 'bad_window'})
    if failures:
        return {'status': 'review', 'reason': 'strict_literal_unmet', 'solutions': 0,
                'failures': failures, 'rejected': _sorted_rejected(analysis['rejected'])}
    fields = {}
    for name, (value, cands) in resolved.items():
        basis = sorted({b for cd in cands for b in cd['basis']})
        if name in LANE_FIELDS:
            basis = sorted(set(basis) | {'arithmetic:total_lanes=lanes_closed+remaining_lanes'})
        fields[name] = {'value': value,
                        'evidence': _dedup_ev([cd['evidence'] for cd in cands]),
                        'basis': basis}
    fields['segment'] = {'value': segment,
                         'evidence': _dedup_ev([ev for row in analysis['lane_rows']
                                                for ev in row['segment_evidence']]),
                         'basis': ['table_row']}
    return {'status': 'ready_for_adoption', 'reason': 'single_literal_reading',
            'solutions': 1, 'fields': fields,
            'rejected': _sorted_rejected(analysis['rejected'])}


# ---------------------------------------------------------------- revisions

def _extract_detail(tokens, scan_id, scan_digest, segment, policy, extract_id):
    analysis = _analyze(tokens, segment)
    outcome = _solve_strict(analysis, segment) if policy == 'strict_literal' \
        else _solve_corroborated(analysis, segment)
    detail = {'extract_id': extract_id, 'layout_version': LAYOUT_VERSION,
              'scan_id': scan_id, 'scan_digest': scan_digest,
              'segment': segment, 'policy': policy}
    detail.update(outcome)
    return detail


def extract(db_path, scan_id, segment, policy):
    """Run extraction and store an immutable extract_revision; replays identically."""
    if policy not in POLICIES:
        raise ValueError('unknown_extraction_policy')
    if not isinstance(segment, str) or not segment:
        raise ValueError('segment_required')
    db = connect(db_path)
    try:
        row = db.execute('SELECT token_digest, raw FROM ocr_scan WHERE id=?',
                         (scan_id,)).fetchone()
        if not row:
            raise ValueError('unknown_ocr_scan')
        tokens = json.loads(row['raw'])['tokens']
        extract_id = 'ext-' + digest({'scan_id': scan_id, 'scan_digest': row['token_digest'],
                                      'segment': segment, 'policy': policy,
                                      'layout_version': LAYOUT_VERSION})[:32]
        old = db.execute('SELECT detail FROM ocr_extract WHERE id=?', (extract_id,)).fetchone()
        if old:
            return json.loads(old['detail'])
        detail = _extract_detail(tokens, scan_id, row['token_digest'],
                                 segment, policy, extract_id)
        with db:
            db.execute('INSERT INTO ocr_extract'
                       '(id,scan_id,segment,policy,layout_version,scan_digest,status,detail)'
                       ' VALUES (?,?,?,?,?,?,?,?)',
                       (extract_id, scan_id, segment, policy, LAYOUT_VERSION,
                        row['token_digest'], detail['status'], canonical(detail)))
        return detail
    finally:
        db.close()


def _export_doc(detail):
    f = detail['fields']
    return {
        'source_id': f['source_id']['value'],
        'revision': f['revision']['value'],
        'published_at': f['published_at']['value'],
        'effective_from': f['effective_from']['value'],
        'effective_until': f['effective_until']['value'],
        'payload': {'authority': f['authority']['value'],
                    'action': f['action']['value'],
                    'segment': detail['segment'],
                    'total_lanes': f['total_lanes']['value'],
                    'lanes_closed': f['lanes_closed']['value'],
                    'remaining_capacity': f['remaining_capacity']['value']},
        'provenance': {
            'extract_id': detail['extract_id'],
            'scan_id': detail['scan_id'],
            'scan_digest': detail['scan_digest'],
            'policy': detail['policy'],
            'layout_version': detail['layout_version'],
            'fields': {name: [{'token_id': e['token_id'], 'alternative': e['alternative'],
                               'page': e['page']} for e in fld['evidence']]
                       for name, fld in sorted(f.items())}},
    }


def export(db_path, extract_id, out_path=None):
    """Export an adopt-compatible JSON notice; only ready_for_adoption may export."""
    db = connect(db_path)
    try:
        row = db.execute('SELECT detail FROM ocr_extract WHERE id=?', (extract_id,)).fetchone()
        if not row:
            raise ValueError('unknown_extract')
        detail = json.loads(row['detail'])
        if detail['status'] != 'ready_for_adoption':
            raise ValueError('extract_not_ready')
        doc = _export_doc(detail)
        fp = digest(doc)
        with db:
            db.execute('INSERT OR IGNORE INTO ocr_export VALUES (?,?,?,CURRENT_TIMESTAMP)',
                       (extract_id, fp, canonical(doc)))
        if out_path:
            target = Path(out_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.pending')
            temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(target)
        return {'extract_id': extract_id, 'export_digest': fp, 'doc': doc}
    finally:
        db.close()


def verify(db_path, extract_id, export_path=None):
    """Recompute the extraction from frozen tokens; old ready conclusions must replay."""
    db = connect(db_path)
    try:
        row = db.execute('SELECT * FROM ocr_extract WHERE id=?', (extract_id,)).fetchone()
        if not row:
            raise ValueError('unknown_extract')
        try:
            stored = json.loads(row['detail'])
        except ValueError:
            stored = None
        result = {'extract_id': extract_id, 'extract_status': row['status']}
        scan = db.execute('SELECT token_digest, raw FROM ocr_scan WHERE id=?',
                          (row['scan_id'],)).fetchone()
        if not scan:
            return {**result, 'status': 'stale', 'reason': 'scan_tokens_changed'}
        scan_doc = json.loads(scan['raw'])
        if digest(scan_doc) != scan['token_digest'] or scan['token_digest'] != row['scan_digest']:
            return {**result, 'status': 'stale', 'reason': 'scan_tokens_changed'}
        if row['layout_version'] != LAYOUT_VERSION:
            return {**result, 'status': 'stale', 'reason': 'layout_version_changed'}
        recomputed = _extract_detail(scan_doc['tokens'], row['scan_id'], row['scan_digest'],
                                     row['segment'], row['policy'], extract_id)
        if stored is None or canonical(recomputed) != canonical(stored) \
                or row['status'] != recomputed['status']:
            return {**result, 'status': 'stale', 'reason': 'extract_mismatch'}
        exp = db.execute('SELECT digest, doc FROM ocr_export WHERE extract_id=?',
                         (extract_id,)).fetchone()
        if exp:
            rebuilt = _export_doc(stored) if stored['status'] == 'ready_for_adoption' else None
            if rebuilt is None or digest(rebuilt) != exp['digest'] \
                    or canonical(rebuilt) != exp['doc']:
                return {**result, 'status': 'stale', 'reason': 'export_mismatch'}
        if export_path is not None:
            if stored['status'] != 'ready_for_adoption':
                return {**result, 'status': 'stale', 'reason': 'extract_not_ready'}
            if digest(read(export_path)) != digest(_export_doc(stored)):
                return {**result, 'status': 'stale', 'reason': 'export_file_changed'}
        return {**result, 'status': 'verified', 'reason': 'recomputed_match'}
    finally:
        db.close()
