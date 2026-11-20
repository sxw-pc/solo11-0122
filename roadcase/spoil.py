"""Freeze excavation material lots, weighed transfer tickets and sealed mass-balance verdicts."""
import json
from fractions import Fraction
from .source import read, digest, canonical, utc
from .store import connect
from .policy import TABLES

SCHEMA = """CREATE TABLE IF NOT EXISTS spoil_lot(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 declared_wet_kg INTEGER NOT NULL, approved_destinations TEXT NOT NULL,
 raw TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spoil_ticket(
 id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, at TEXT NOT NULL,
 from_node TEXT NOT NULL, to_node TEXT NOT NULL, net_kg INTEGER,
 dry_fraction REAL, uncertainty_kg INTEGER NOT NULL,
 parents TEXT NOT NULL, raw TEXT NOT NULL, digest TEXT NOT NULL);
"""

CONTRACTS = ('as_weighed', 'dry_solids')
ASSURANCE = ('结论仅覆盖已封账票据在所选计量口径下的全局质量可行性；'
             '票据一致不构成对现实世界无偷排、无遗漏的绝对保证。')


def open_lot(db_path, input_path):
    doc = read(input_path)
    if (not doc.get('id') or not doc.get('source_id') or
            not isinstance(doc.get('source_revision'), int) or
            not isinstance(doc.get('declared_wet_kg'), int) or doc['declared_wet_kg'] <= 0 or
            not isinstance(doc.get('approved_destinations'), list) or
            not doc['approved_destinations']):
        raise ValueError('invalid_spoil_lot')
    fingerprint = digest(doc)
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        notice = db.execute(f"SELECT current_revision,state FROM {TABLES['notice']} WHERE id=?",
                            (doc['source_id'],)).fetchone()
        if not notice or notice['state'] != 'active' or notice['current_revision'] != doc['source_revision']:
            raise ValueError('active_work_revision_required')
        with db:
            old = db.execute('SELECT digest FROM spoil_lot WHERE id=?', (doc['id'],)).fetchone()
            if old and old['digest'] != fingerprint:
                raise ValueError('spoil_lot_conflict')
            db.execute('INSERT OR IGNORE INTO spoil_lot VALUES (?,?,?,?,?,?,?)',
                       (doc['id'], doc['source_id'], doc['source_revision'],
                        doc['declared_wet_kg'], canonical(doc['approved_destinations']),
                        canonical(doc), fingerprint))
        return {'id': doc['id'], 'digest': fingerprint}
    finally:
        db.close()


def record_ticket(db_path, input_path):
    doc = read(input_path)
    for key in ('id', 'lot_id', 'at', 'from_node', 'to_node', 'gross_kg', 'tare_kg',
                'dry_fraction', 'uncertainty_kg', 'parents'):
        if key not in doc:
            raise ValueError('ticket_missing_' + key)
    utc(doc['at'])
    gross, tare, dry = doc['gross_kg'], doc['tare_kg'], doc['dry_fraction']
    unweighed = gross is None and tare is None
    weighed = (isinstance(gross, int) and isinstance(tare, int) and
               not isinstance(gross, bool) and not isinstance(tare, bool) and
               gross > tare and tare >= 0)
    if (not doc['id'] or not doc['lot_id'] or not doc['from_node'] or not doc['to_node'] or
            doc['from_node'] == doc['to_node'] or
            not (unweighed or weighed) or
            (dry is not None and not 0 < dry <= 1) or
            not isinstance(doc['uncertainty_kg'], int) or doc['uncertainty_kg'] < 0 or
            not isinstance(doc['parents'], list) or len(set(doc['parents'])) != len(doc['parents'])):
        raise ValueError('invalid_spoil_ticket')
    net = None if unweighed else gross - tare
    fingerprint = digest(doc)
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM spoil_lot WHERE id=?', (doc['lot_id'],)).fetchone():
            raise ValueError('unknown_spoil_lot')
        with db:
            old = db.execute('SELECT digest FROM spoil_ticket WHERE id=?', (doc['id'],)).fetchone()
            if old and old['digest'] != fingerprint:
                raise ValueError('spoil_ticket_conflict')
            db.execute('INSERT OR IGNORE INTO spoil_ticket VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (doc['id'], doc['lot_id'], utc(doc['at']).isoformat(), doc['from_node'],
                        doc['to_node'], net, dry, doc['uncertainty_kg'],
                        canonical(doc['parents']), canonical(doc), fingerprint))
        return {'id': doc['id'], 'net_kg': net, 'digest': fingerprint}
    finally:
        db.close()


def summary(db_path, lot_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM spoil_lot WHERE id=?', (lot_id,)).fetchone():
            raise ValueError('unknown_spoil_lot')
        rows = [dict(x) for x in db.execute('''SELECT id,from_node,to_node,net_kg,dry_fraction,
            uncertainty_kg,parents FROM spoil_ticket WHERE lot_id=? ORDER BY id''', (lot_id,))]
        return {'lot_id': lot_id, 'tickets': rows, 'ticket_count': len(rows)}
    finally:
        db.close()


def seal(db_path, input_path):
    """Freeze the lot's current ticket ids/digests, measurement contract, work revision
    and receiving nodes into a sealed version that later tickets cannot rewrite."""
    doc = read(input_path)
    balances = doc.get('balances', [])
    if (not doc.get('id') or not doc.get('lot_id') or doc.get('contract') not in CONTRACTS or
            not isinstance(balances, list)):
        raise ValueError('invalid_spoil_seal')
    seen = set()
    for b in balances:
        if (not isinstance(b, dict) or not b.get('node') or
                not isinstance(b.get('wet_kg'), int) or isinstance(b.get('wet_kg'), bool) or
                b['wet_kg'] < 0 or
                not isinstance(b.get('uncertainty_kg'), int) or b['uncertainty_kg'] < 0 or
                (b.get('dry_fraction') is not None and not 0 < b['dry_fraction'] <= 1) or
                b['node'] in seen):
            raise ValueError('invalid_spoil_seal')
        seen.add(b['node'])
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        lot = db.execute('SELECT * FROM spoil_lot WHERE id=?', (doc['lot_id'],)).fetchone()
        if not lot:
            raise ValueError('unknown_spoil_lot')
        notice = db.execute(f"SELECT current_revision,state FROM {TABLES['notice']} WHERE id=?",
                            (lot['source_id'],)).fetchone()
        if (not notice or notice['state'] != 'active' or
                notice['current_revision'] != lot['source_revision']):
            raise ValueError('active_work_revision_required')
        tickets = db.execute('SELECT id,digest FROM spoil_ticket WHERE lot_id=? ORDER BY id',
                             (doc['lot_id'],)).fetchall()
        snapshot = {
            'lot': {'id': lot['id'], 'source_id': lot['source_id'],
                    'source_revision': lot['source_revision'],
                    'declared_wet_kg': lot['declared_wet_kg'],
                    'approved_destinations': json.loads(lot['approved_destinations']),
                    'digest': lot['digest']},
            'contract': doc['contract'],
            'balances': [{'node': b['node'], 'wet_kg': b['wet_kg'],
                          'uncertainty_kg': b['uncertainty_kg'],
                          'dry_fraction': b.get('dry_fraction')} for b in balances],
            'tickets': [{'id': t['id'], 'digest': t['digest']} for t in tickets],
        }
        fingerprint = digest({'input': doc, 'snapshot': snapshot})
        with db:
            old = db.execute('SELECT digest,state FROM spoil_seal WHERE id=?', (doc['id'],)).fetchone()
            if old:
                if old['digest'] != fingerprint:
                    raise ValueError('spoil_seal_conflict')
                state = old['state']
            else:
                state = 'active'
                db.execute('INSERT INTO spoil_seal VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                           (doc['id'], doc['lot_id'], doc['contract'], lot['source_id'],
                            lot['source_revision'], len(tickets), canonical(snapshot),
                            canonical(doc), fingerprint, state))
        return {'id': doc['id'], 'lot_id': doc['lot_id'], 'contract': doc['contract'],
                'source_id': lot['source_id'], 'source_revision': lot['source_revision'],
                'ticket_count': len(tickets), 'digest': fingerprint, 'state': state}
    finally:
        db.close()


def verify(db_path, seal_id):
    """Replay the sealed ticket network from the frozen raw tickets and persist the verdict."""
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT * FROM spoil_seal WHERE id=?', (seal_id,)).fetchone()
        if not row:
            raise ValueError('unknown_spoil_seal')
        if row['state'] != 'active':
            raise ValueError('spoil_seal_stale')
        snapshot = json.loads(row['snapshot'])
        lot = db.execute('SELECT raw FROM spoil_lot WHERE id=?', (row['lot_id'],)).fetchone()
        if not lot or digest(json.loads(lot['raw'])) != snapshot['lot']['digest']:
            raise ValueError('spoil_evidence_tampered')
        raws = []
        for frozen in snapshot['tickets']:
            t = db.execute('SELECT raw FROM spoil_ticket WHERE id=?', (frozen['id'],)).fetchone()
            if not t:
                raise ValueError('spoil_evidence_tampered')
            doc = json.loads(t['raw'])
            if digest(doc) != frozen['digest'] or doc.get('lot_id') != row['lot_id']:
                raise ValueError('spoil_evidence_tampered')
            raws.append(doc)
        detail = _analyze(snapshot, raws)
        fingerprint = digest(detail)
        with db:
            old = db.execute('SELECT * FROM spoil_result WHERE seal_id=?', (seal_id,)).fetchone()
            if old:
                if (old['digest'] != fingerprint or old['verdict'] != detail['verdict'] or
                        old['lot_id'] != row['lot_id'] or
                        old['contract'] != snapshot['contract'] or
                        old['detail'] != canonical(detail)):
                    raise ValueError('spoil_result_conflict')
            else:
                db.execute('INSERT INTO spoil_result VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                           (seal_id, row['lot_id'], snapshot['contract'], detail['verdict'],
                            canonical(detail), fingerprint))
        return {'seal': seal_id, 'lot_id': row['lot_id'], 'contract': snapshot['contract'],
                'verdict': detail['verdict'], 'detail': detail, 'digest': fingerprint}
    finally:
        db.close()


def result(db_path, seal_id):
    """Read the persisted verdict of a seal; old results stay unchanged after staleness."""
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        seal_row = db.execute('SELECT state FROM spoil_seal WHERE id=?', (seal_id,)).fetchone()
        if not seal_row:
            raise ValueError('unknown_spoil_seal')
        res = db.execute('SELECT * FROM spoil_result WHERE seal_id=?', (seal_id,)).fetchone()
        if not res:
            raise ValueError('spoil_result_missing')
        return {'seal': seal_id, 'lot_id': res['lot_id'], 'contract': res['contract'],
                'state': seal_row['state'], 'verdict': res['verdict'],
                'detail': json.loads(res['detail']), 'digest': res['digest']}
    finally:
        db.close()


def _num(value):
    value = Fraction(value)
    return value.numerator if value.denominator == 1 else float(value)


def _pair(interval):
    return None if interval is None else [_num(interval[0]), _num(interval[1])]


def _lp_feasible(rows, n_vars):
    """Exact feasibility of {x >= 0 : c·x <= r} via two-phase simplex with Bland's rule.

    rows: list of (coefficients, rhs) as Fraction-compatible values.
    Returns a feasible assignment list of Fraction, or None if infeasible.
    """
    m = len(rows)
    if m == 0:
        return [Fraction(0)] * n_vars
    norm = []
    for coeffs, rhs in rows:
        c = [Fraction(v) for v in coeffs]
        r = Fraction(rhs)
        sign = 1
        if r < 0:
            c, r, sign = [-v for v in c], -r, -1
        norm.append((c, r, sign))
    n_art = sum(1 for _, _, s in norm if s == -1)
    total = n_vars + m + n_art
    tab, basis, artificial = [], [], set()
    ai = 0
    for i, (c, r, sign) in enumerate(norm):
        row = [Fraction(0)] * (total + 1)
        for j, v in enumerate(c):
            row[j] = v
        row[n_vars + i] = Fraction(sign)
        row[total] = r
        if sign == -1:
            col = n_vars + m + ai
            ai += 1
            row[col] = Fraction(1)
            artificial.add(col)
            basis.append(col)
        else:
            basis.append(n_vars + i)
        tab.append(row)

    def pivot(pr, pc, obj):
        inv = Fraction(1) / tab[pr][pc]
        tab[pr] = [v * inv for v in tab[pr]]
        for k in range(m):
            if k != pr and tab[k][pc]:
                f = tab[k][pc]
                tab[k] = [a - f * b for a, b in zip(tab[k], tab[pr])]
        if obj is not None and obj[pc]:
            f = obj[pc]
            for j in range(total + 1):
                obj[j] -= f * tab[pr][j]
        basis[pr] = pc

    if artificial:
        cost = [Fraction(0)] * total
        for col in artificial:
            cost[col] = Fraction(-1)
        obj = [-v for v in cost] + [Fraction(0)]
        for i in range(m):
            if cost[basis[i]]:
                for j in range(total + 1):
                    obj[j] += cost[basis[i]] * tab[i][j]
        while True:
            entering = next((j for j in range(total) if obj[j] < 0), None)
            if entering is None:
                break
            leave, best = None, None
            for i in range(m):
                if tab[i][entering] > 0:
                    ratio = tab[i][total] / tab[i][entering]
                    if (best is None or ratio < best or
                            (ratio == best and basis[i] < basis[leave])):
                        best, leave = ratio, i
            if leave is None:
                raise ValueError('lp_unbounded')
            pivot(leave, entering, obj)
        if obj[total] < 0:
            return None
        for i in range(m):
            if basis[i] in artificial:
                col = next((j for j in range(total) if j not in artificial and tab[i][j]), None)
                if col is not None:
                    pivot(i, col, None)
    x = [Fraction(0)] * n_vars
    for i in range(m):
        if basis[i] < n_vars:
            x[basis[i]] = tab[i][total]
    return x


def _analyze(snapshot, raws):
    """Independently review the directed transfer network of one sealed lot.

    Hard topology violations (time reversal, location jumps, cycles, duplicate
    identities) block the lot; missing evidence (dangling parents, unweighed
    tickets, incomplete moisture records) makes it indeterminate; otherwise one
    globally feasible mass assignment must exist across every split, merge,
    staging and receiving node.
    """
    contract = snapshot['contract']
    lot = snapshot['lot']
    declared = lot['declared_wet_kg']
    terminals = sorted(lot['approved_destinations'])
    terminal_set = set(terminals)
    balances = snapshot['balances']
    balance_by_node = {b['node']: b for b in balances}
    tickets = sorted(raws, key=lambda d: d['id'])
    by_id = {}
    violations, gaps = [], []
    for t in tickets:
        if t['id'] in by_id:
            violations.append({'kind': 'duplicate_ticket_id', 'ticket': t['id']})
        by_id[t['id']] = t
    children = {}
    for t in tickets:
        if len(set(t['parents'])) != len(t['parents']):
            violations.append({'kind': 'duplicate_parent_edge', 'ticket': t['id']})
        for p in t['parents']:
            parent = by_id.get(p)
            if parent is None:
                gaps.append({'kind': 'missing_parent', 'ticket': t['id'], 'parent': p})
                continue
            children.setdefault(p, []).append(t['id'])
            if not utc(parent['at']) < utc(t['at']):
                violations.append({'kind': 'time_order', 'ticket': t['id'], 'parent': p,
                                   'ticket_at': t['at'], 'parent_at': parent['at']})
            if parent['to_node'] != t['from_node']:
                violations.append({'kind': 'location_jump', 'ticket': t['id'], 'parent': p,
                                   'ticket_from': t['from_node'], 'parent_to': parent['to_node']})
    indegree = {t['id']: 0 for t in tickets}
    for p, cs in children.items():
        for c in cs:
            indegree[c] += 1
    queue = sorted(k for k, v in indegree.items() if v == 0)
    seen = 0
    while queue:
        node = queue.pop(0)
        seen += 1
        for c in sorted(children.get(node, [])):
            indegree[c] -= 1
            if indegree[c] == 0:
                queue.append(c)
    if seen < len(tickets):
        violations.append({'kind': 'cycle',
                           'tickets': sorted(k for k, v in indegree.items() if v > 0)})

    def net_of(t):
        if t.get('gross_kg') is None:
            return None
        return t['gross_kg'] - t['tare_kg']

    for t in tickets:
        if net_of(t) is None:
            gaps.append({'kind': 'unweighed', 'ticket': t['id']})
        if contract == 'dry_solids' and t.get('dry_fraction') is None:
            gaps.append({'kind': 'missing_dry_fraction', 'ticket': t['id']})
    for b in balances:
        if contract == 'dry_solids' and b.get('dry_fraction') is None:
            gaps.append({'kind': 'missing_balance_dry_fraction', 'node': b['node']})

    def wet_interval(t):
        net = net_of(t)
        if net is None:
            return None
        return (Fraction(max(0, net - t['uncertainty_kg'])),
                Fraction(net + t['uncertainty_kg']))

    def dry_fraction_of(x):
        d = x.get('dry_fraction')
        return None if d is None else Fraction(repr(d))

    def coef_of(t):
        return Fraction(1) if contract == 'as_weighed' else dry_fraction_of(t)

    def contract_interval(t):
        wet, coef = wet_interval(t), coef_of(t)
        if wet is None or coef is None:
            return None
        return (coef * wet[0], coef * wet[1])

    def dry_interval(t):
        wet, dry = wet_interval(t), dry_fraction_of(t)
        if wet is None or dry is None:
            return None
        return (dry * wet[0], dry * wet[1])

    node_names = sorted({t['from_node'] for t in tickets} | {t['to_node'] for t in tickets} |
                        {b['node'] for b in balances})
    arrivals = {n: [] for n in node_names}
    dep_nonroot = {n: [] for n in node_names}
    dep_root = {n: [] for n in node_names}
    for t in tickets:
        arrivals[t['to_node']].append(t)
        (dep_nonroot if t['parents'] else dep_root)[t['from_node']].append(t)

    def role_of(n):
        if n in terminal_set:
            return 'terminal'
        if dep_root[n] and not arrivals[n] and not dep_nonroot[n]:
            return 'source'
        return 'transfer'

    def balance_interval(n):
        b = balance_by_node.get(n)
        if b is None:
            return (Fraction(0), Fraction(0))
        lo = Fraction(max(0, b['wet_kg'] - b['uncertainty_kg']))
        hi = Fraction(b['wet_kg'] + b['uncertainty_kg'])
        if contract == 'dry_solids':
            dry = dry_fraction_of(b)
            if dry is None:
                return None
            lo, hi = lo * dry, hi * dry
        return (lo, hi)

    def sum_contract(ts):
        lo = hi = Fraction(0)
        for t in ts:
            iv = contract_interval(t)
            if iv is None:
                return None
            lo, hi = lo + iv[0], hi + iv[1]
        return (lo, hi)

    roots = [t for t in tickets if not t['parents']]

    def solve():
        """One global assignment: ticket masses plus per-parent-edge flows."""
        ids = [t['id'] for t in tickets]
        edges = sorted({(p, c) for p, cs in children.items() for c in cs})
        index = {tid: i for i, tid in enumerate(ids)}
        eindex = {e: len(ids) + i for i, e in enumerate(edges)}
        n_vars = len(ids) + len(edges)
        rows = []

        def le(coeffs, rhs):
            rows.append((coeffs, Fraction(rhs)))

        for t in tickets:
            lo, hi = wet_interval(t)
            c = [Fraction(0)] * n_vars
            c[index[t['id']]] = Fraction(1)
            le(c, hi)
            c = [Fraction(0)] * n_vars
            c[index[t['id']]] = Fraction(-1)
            le(c, -lo)
        for t in tickets:
            if not t['parents']:
                continue
            c = [Fraction(0)] * n_vars
            for p in t['parents']:
                c[eindex[(p, t['id'])]] = Fraction(1)
            c[index[t['id']]] = -coef_of(t)
            le(c, 0)
            le([-v for v in c], 0)
        for p, cs in children.items():
            c = [Fraction(0)] * n_vars
            for child in cs:
                c[eindex[(p, child)]] = Fraction(1)
            c[index[p]] = -coef_of(by_id[p])
            le(c, 0)
        constrained = []
        for n in node_names:
            if role_of(n) != 'transfer' or (not arrivals[n] and not dep_nonroot[n]):
                continue
            bal = balance_interval(n)
            c = [Fraction(0)] * n_vars
            for t in arrivals[n]:
                c[index[t['id']]] += coef_of(t)
            for t in dep_nonroot[n]:
                c[index[t['id']]] -= coef_of(t)
            le(c, bal[1])
            le([-v for v in c], -bal[0])
            constrained.append(n)
        c = [Fraction(0)] * n_vars
        for t in roots:
            c[index[t['id']]] = Fraction(1)
        le(c, declared)
        le([-v for v in c], -declared)
        x = _lp_feasible(rows, n_vars)
        if x is None:
            return None

        def unspent(n):
            total = Fraction(0)
            for t in arrivals[n]:
                total += coef_of(t) * x[index[t['id']]]
            for t in dep_nonroot[n]:
                total -= coef_of(t) * x[index[t['id']]]
            return total

        return {
            'tickets': {tid: {'wet_kg': _num(x[index[tid]]),
                              'contract_kg': _num(coef_of(by_id[tid]) * x[index[tid]])}
                        for tid in ids},
            'edges': [{'parent': p, 'child': ch, 'contract_kg': _num(x[eindex[(p, ch)]])}
                      for (p, ch) in edges],
            'node_balance_kg': {n: _num(unspent(n)) for n in node_names},
        }

    def cuts():
        """Contradiction evidence: single-node and single-parent local failures."""
        out = []
        for n in node_names:
            if role_of(n) != 'transfer' or (not arrivals[n] and not dep_nonroot[n]):
                continue
            iv_in, iv_out, bal = (sum_contract(arrivals[n]), sum_contract(dep_nonroot[n]),
                                  balance_interval(n))
            if iv_in is None or iv_out is None or bal is None:
                continue
            if iv_in[0] - iv_out[1] > bal[1] or iv_in[1] - iv_out[0] < bal[0]:
                out.append({'kind': 'node_imbalance', 'node': n,
                            'in_interval_kg': _pair(iv_in), 'out_interval_kg': _pair(iv_out),
                            'balance_interval_kg': _pair(bal)})
        for p, cs in children.items():
            avail = contract_interval(by_id[p])
            demand = Fraction(0)
            complete = avail is not None
            for ch in cs:
                iv = contract_interval(by_id[ch])
                if iv is None:
                    complete = False
                    break
                demand += iv[0]
            if complete and demand > avail[1]:
                out.append({'kind': 'parent_overspent', 'parent': p, 'children': sorted(cs),
                            'available_kg': _num(avail[1]), 'demanded_kg': _num(demand)})
        if not out:
            out.append({'kind': 'global_flow_infeasible',
                        'detail': 'no single node or parent cut; joint assignment infeasible'})
        return out

    assignment = None
    if violations:
        verdict = 'blocked'
    elif gaps:
        verdict = 'indeterminate'
    else:
        root_lo = sum((wet_interval(t)[0] for t in roots), Fraction(0))
        root_hi = sum((wet_interval(t)[1] for t in roots), Fraction(0))
        if declared < root_lo:
            violations.append({'kind': 'declaration_exceeded', 'declared_wet_kg': declared,
                               'root_wet_interval_kg': [_num(root_lo), _num(root_hi)]})
            verdict = 'blocked'
        elif declared > root_hi:
            gaps.append({'kind': 'declared_not_arrived', 'declared_wet_kg': declared,
                         'root_wet_interval_kg': [_num(root_lo), _num(root_hi)]})
            verdict = 'indeterminate'
        else:
            assignment = solve()
            if assignment is None:
                verdict = 'blocked'
                violations.extend(cuts())
            else:
                verdict = 'consistent_with_records'

    node_entries = []
    for n in node_names:
        role = role_of(n)
        node_entries.append({
            'node': n,
            'role': role,
            'in_interval_kg': _pair(sum_contract(arrivals[n])),
            'out_interval_kg': _pair(sum_contract(dep_nonroot[n])),
            'root_out_interval_kg': _pair(sum_contract(dep_root[n])),
            'balance_interval_kg': _pair(balance_interval(n)) if role == 'transfer' else None,
        })
    return {
        'verdict': verdict,
        'contract': contract,
        'mass_basis': 'wet_kg' if contract == 'as_weighed' else 'dry_solids_kg',
        'lot_id': lot['id'],
        'source_id': lot['source_id'],
        'source_revision': lot['source_revision'],
        'declared_wet_kg': declared,
        'approved_destinations': terminals,
        'balances': [dict(b) for b in sorted(balances, key=lambda b: b['node'])],
        'ticket_count': len(tickets),
        'tickets': [{'id': t['id'], 'at': t['at'], 'from_node': t['from_node'],
                     'to_node': t['to_node'], 'parents': sorted(t['parents']),
                     'wet_interval_kg': _pair(wet_interval(t)),
                     'dry_interval_kg': _pair(dry_interval(t)),
                     'digest': digest(t)} for t in tickets],
        'nodes': node_entries,
        'assignment': assignment,
        'violations': violations,
        'gaps': gaps,
        'scope': 'ticket_records_only',
        'assurance': ASSURANCE,
    }
