import html
import json
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from .reviews import specific


def aggregate(db, slug):
    raw = db.execute("SELECT model_version,iso_week,outcome,retries,cost_usd_cents FROM reviews WHERE model_slug=? AND status='published' ORDER BY iso_week,model_version", (slug,)).fetchall()
    groups = defaultdict(list)
    for row in raw:
        groups[(row['model_version'], row['iso_week'])].append(row)
    out = []
    for (version, week), rows in groups.items():
        n = len(rows)
        costs = [row['cost_usd_cents'] for row in rows if row['cost_usd_cents'] is not None]
        out.append({'version': version, 'week': week, 'n': n, 'suppressed': n < 3,
                    'mean_outcome': None if n < 3 else round(sum(row['outcome'] for row in rows) / n, 1),
                    'mean_retries': None if n < 3 else round(sum(row['retries'] for row in rows) / n, 1),
                    'median_cost': None if n < 3 or not costs else median(costs),
                    'pct_version_specific': None if n < 3 else (100.0 if specific(version) else 0.0)})
    return out


def _next_week(first, second):
    try:
        y, w = first.split('-W')
        start = date.fromisocalendar(int(y), int(w), 1)
        y, w = second.split('-W')
        return date.fromisocalendar(int(y), int(w), 1) == start + timedelta(days=7)
    except ValueError:
        return False


def shifts(db, slug):
    cells = db.execute("SELECT iso_week,model_version,COUNT(*) n,SUM(outcome) total FROM reviews WHERE model_slug=? AND status='published' GROUP BY iso_week,model_version ORDER BY model_version,iso_week", (slug,)).fetchall()
    versions = defaultdict(list)
    for cell in cells:
        if cell['n'] >= 5:
            versions[cell['model_version']].append({'week': cell['iso_week'], 'n': cell['n'], 'mean': cell['total'] / cell['n']})
    out = []
    for version, eligible in versions.items():
        for first, second in zip(eligible, eligible[1:]):
            if _next_week(first['week'], second['week']) and first['mean'] - second['mean'] >= 1:
                out.append({'version': version, 'from': first['week'], 'to': second['week'], 'n_from': first['n'], 'n_to': second['n']})
    return out


def public_rows(rows):
    return [{'suppressed': True, 'n': '<3'} if row['suppressed'] else dict(row) for row in rows]


def render_svg(rows):
    cells = sorted(rows, key=lambda row: (row['week'], row['version']))
    width = max(360, 160 * len(cells))
    maximum = max((row['n'] for row in cells if not row['suppressed']), default=1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Weekly review volume and mean outcome by version" width="{width}" height="240" viewBox="0 0 {width} 240">',
             '<title>Self-reported reviews: volume bars and mean outcome dots (1–5)</title>',
             '<rect width="100%" height="100%" fill="white"/>',
             '<text x="10" y="18" font-size="11">Bars: review count. Dots: mean outcome (1–5).</text>']
    for index, row in enumerate(cells):
        x = 30 + index * 160
        caption = html.escape(f"{row['week']} {row['version']}")
        if row['suppressed']:
            parts.append(f'<text x="{x}" y="145" font-size="11">&lt;3: suppressed</text>')
        else:
            height = 110 * row['n'] / maximum
            y = 160 - 110 * (row['mean_outcome'] - 1) / 4
            parts.extend([
                f'<rect x="{x}" y="{160-height:.1f}" width="42" height="{height:.1f}" fill="#4969a8"/>',
                f'<text x="{x}" y="{150-height:.1f}" font-size="11">n={row["n"]}</text>',
                f'<circle cx="{x+70}" cy="{y:.1f}" r="5" fill="#a84126"/>',
                f'<text x="{x+78}" y="{y:.1f}" font-size="11">{row["mean_outcome"]}/5</text>',
            ])
        parts.append(f'<text x="{x}" y="190" font-size="9">{caption}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def json_series(rows):
    return json.dumps(public_rows(rows), sort_keys=True, separators=(',', ':'))
