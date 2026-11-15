from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median


def _next_week(first, second):
    try:
        y, w = first.split('-W')
        start = date.fromisocalendar(int(y), int(w), 1)
        y, w = second.split('-W')
        return date.fromisocalendar(int(y), int(w), 1) == start + timedelta(days=7)
    except (TypeError, ValueError):
        return False


def aggregate(runs, model):
    groups = defaultdict(list)
    for run in runs:
        if run.get('model') == model:
            groups[(run.get('week'), run.get('version'))].append(run)
    rows = []
    for (week, version), items in sorted(groups.items()):
        costs = [item['cost_cents'] for item in items if item.get('cost_cents') is not None]
        rows.append({
            'week': week,
            'version': version,
            'n': len(items),
            'suppressed': False,
            'low_n': len(items) < 3,
            'mean_outcome': round(sum(item['outcome'] for item in items) / len(items), 1),
            'mean_retries': round(sum(item['retries'] for item in items) / len(items), 1),
            'median_cost': median(costs) if costs else None,
            'pct_version_specific': 100.0,
        })
    return rows


def shifts(runs, model):
    versions = defaultdict(list)
    for row in aggregate(runs, model):
        if row['n'] >= 5:
            versions[row['version']].append(row)
    found = []
    for version, rows in versions.items():
        rows.sort(key=lambda row: row['week'])
        for first, second in zip(rows, rows[1:]):
            drop = first['mean_outcome'] - second['mean_outcome']
            if _next_week(first['week'], second['week']) and drop >= 1.0:
                found.append({
                    'version': version,
                    'from': first['week'],
                    'to': second['week'],
                    'n_from': first['n'],
                    'n_to': second['n'],
                    'from_mean': first['mean_outcome'],
                    'to_mean': second['mean_outcome'],
                    'drop': round(drop, 1),
                })
    return found


def compare(runs, category=None):
    groups = defaultdict(list)
    for run in runs:
        if not category or run.get('category') == category:
            groups[run.get('model')].append(run)
    rows = []
    for model, items in groups.items():
        costs = [item['cost_cents'] for item in items if item.get('cost_cents') is not None]
        rows.append({
            'model': model,
            'n': len(items),
            'mean_outcome': round(sum(item['outcome'] for item in items) / len(items), 1),
            'mean_retries': round(sum(item['retries'] for item in items) / len(items), 1),
            'median_cost': median(costs) if costs else None,
        })
    return sorted(rows, key=lambda row: (-row['mean_outcome'], row['model']))


def report_md(runs, model, weeks=8):
    rows = aggregate(runs, model)
    selected = set(sorted({row['week'] for row in rows})[-weeks:])
    rows = [row for row in rows if row['week'] in selected]
    lines = [
        '# ReviewTrail drift report: ' + model,
        '',
        'Self-reported outcomes from this private workspace. This is not a benchmark or a confirmed regression.',
        '',
        '## Weekly aggregates',
        '',
        '| Week | Version | n | Mean outcome | Mean retries | Median cost | Note |',
        '|---|---|---:|---:|---:|---:|---|',
    ]
    for row in rows:
        cost = '—' if row['median_cost'] is None else f"${row['median_cost'] / 100:.2f}"
        note = 'low n' if row['low_n'] else ''
        lines.append(f"| {row['week']} | {row['version']} | {row['n']} | {row['mean_outcome']} | {row['mean_retries']} | {cost} | {note} |")
    lines += ['', '## Possible shifts', '']
    flags = [flag for flag in shifts(runs, model) if flag['to'] in selected]
    if flags:
        for flag in flags:
            lines.append(f"- {flag['version']}: {flag['from']} to {flag['to']}, mean outcome {flag['from_mean']} to {flag['to_mean']} (drop {flag['drop']}; n={flag['n_from']} and n={flag['n_to']}).")
    else:
        lines.append('- None detected.')
    mix = Counter(run['category'] for run in runs if run.get('model') == model)
    lines += ['', '## Task-category mix', '']
    lines += [f'- {name}: n={count}' for name, count in sorted(mix.items())]
    lines += [
        '',
        '## Methodology caveat',
        '',
        'Possible-shift is a fixed heuristic: consecutive ISO weeks for the same version must each have n≥5, and mean outcome must fall by at least 1.0. Inputs are self-reported; the signal is not a benchmark, causal finding, or confirmed model regression. Cells with n<3 remain visible and are labelled low n, but are never flagged.',
    ]
    return '\n'.join(lines) + '\n'
