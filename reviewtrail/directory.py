import json

def load_models(path):
    with open(path, encoding='utf-8') as f: return json.load(f)

def seed_models(db, path):
    for m in load_models(path):
        db.execute('INSERT OR REPLACE INTO models(slug,name,vendor,kind,homepage) VALUES(?,?,?,?,?)',(m['slug'],m['name'],m['vendor'],m['kind'],m.get('homepage')))

def list_models(db):
    return db.execute("SELECT m.*,COUNT(r.id) reviews,MAX(r.iso_week) last_week FROM models m LEFT JOIN reviews r ON r.model_slug=m.slug AND r.status='published' GROUP BY m.slug ORDER BY m.name").fetchall()
