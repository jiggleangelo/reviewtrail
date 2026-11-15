from collections import Counter
from .timeseries import aggregate, public_rows, shifts


def generate(db, slug, weeks=8):
    rows = public_rows(aggregate(db, slug)[-weeks:])
    lines = [f'# ReviewTrail drift report: {slug}', '', 'Self-reported developer reviews; this is not a benchmark. Small cells (n < 3) are suppressed.', '', '## Weekly aggregates', '', '| Week | Version | n | Outcome | Retries | Median cost cents |', '|---|---|---:|---:|---:|---:|']
    for row in rows:
        if row['suppressed']:
            lines.append('| suppressed | suppressed | <3 | suppressed | suppressed | suppressed |')
        else:
            lines.append(f"| {row['week']} | {row['version']} | {row['n']} | {row['mean_outcome']} | {row['mean_retries']} | {row['median_cost']} |")
    ss = shifts(db, slug)
    lines += ['', '## Possible shifts']
    lines += [f"- Possible shift in reported outcomes for {s['version']} ({s['from']} n={s['n_from']}, {s['to']} n={s['n_to']}). Self-reported, not a benchmark." for s in ss] or ['- None.']
    mix = Counter(row['task_category'] for row in db.execute("SELECT task_category FROM reviews WHERE model_slug=? AND status='published'", (slug,)))
    lines += ['', '## Task-category mix', ''] + [f'- {key}: n={value}' for key, value in sorted(mix.items())]
    lines += ['', '## Methodology', '', 'Aggregates include published reviews only. Reviewer inputs are self-reported. Suppression and possible-shift rules reduce overclaiming but do not establish causality.']
    return '\n'.join(lines) + '\n'
