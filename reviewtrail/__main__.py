import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from wsgiref.simple_server import WSGIRequestHandler, make_server
from .auth import FakeOAuth, GitHubOAuth
from .config import Config
from .db import connect, init, now
from .directory import seed_models
from .report import generate
from .reviews import CATEGORIES, VERSION, content_hash, has_sensitive, iso_week, specific
from .vault import FileVault
from .web import App


class QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def PathRoot(relative):
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), relative)


def _validated_seed(item):
    required = {'model_slug', 'model_version', 'task_category', 'task_description', 'stack', 'harness', 'outcome', 'used_on'}
    if not required <= item.keys():
        raise SystemExit('Seed review is missing required fields')
    version = str(item['model_version']).strip().lower()
    if not VERSION.fullmatch(version) or item['task_category'] not in CATEGORIES:
        raise SystemExit('Seed review contains invalid review fields')
    if not 40 <= len(str(item['task_description']).strip()) <= 1000:
        raise SystemExit('Seed review task description must contain 40–1,000 characters')
    if not 2 <= len(str(item['stack']).strip()) <= 80:
        raise SystemExit('Seed review stack must contain 2–80 characters')
    if not isinstance(item['outcome'], int) or not 1 <= item['outcome'] <= 5:
        raise SystemExit('Seed review outcome must be an integer from 1 to 5')
    retries = item.get('retries', 0)
    if not isinstance(retries, int) or not 0 <= retries <= 50:
        raise SystemExit('Seed review retries must be an integer from 0 to 50')
    for field in ('model_version', 'task_description', 'stack', 'harness'):
        if has_sensitive(str(item[field]), phone=field != 'model_version'):
            raise SystemExit('Seed review contains personal data or secrets')
    try:
        used = datetime.fromisoformat(str(item['used_on'])).date()
    except ValueError as error:
        raise SystemExit('Seed review has an invalid used_on date') from error
    return version, retries, used


def _seed(db, args):
    seed_models(db, PathRoot('seed/models.json'))
    for path, source in ((args.founder, 'founder_seed'), (args.import_file, 'consented_import')):
        if not path:
            continue
        with open(path, encoding='utf-8') as source_file:
            data = json.load(source_file)
        if not isinstance(data, list):
            raise SystemExit('Seed input must be a JSON array')
        for item in data:
            if not isinstance(item, dict):
                raise SystemExit('Every seed review must be an object')
            if source == 'consented_import' and {'user', 'email', 'handle'} & item.keys():
                raise SystemExit('Import contains prohibited personal-data keys')
            version, retries, used = _validated_seed(item)
            db.execute('''INSERT OR IGNORE INTO reviews(user_id,model_slug,model_version,task_category,task_description,stack,harness,outcome,retries,cost_usd_cents,used_on,iso_week,proof_sha256,source,status,content_hash,created_at) VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (item['model_slug'], version, item['task_category'], item['task_description'], item['stack'], item['harness'], item['outcome'], retries, item.get('cost_usd_cents'), used.isoformat(), iso_week(used), None, source, 'published', content_hash(item['task_description'], item['stack'], version), now()))
    db.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(description='ReviewTrail local site and maintenance CLI')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init-db')
    seed = sub.add_parser('seed')
    seed.add_argument('--founder')
    seed.add_argument('--import', dest='import_file')
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8000)
    hide = sub.add_parser('hide')
    hide.add_argument('id', type=int)
    sub.add_parser('metrics')
    report = sub.add_parser('report')
    report.add_argument('slug')
    report.add_argument('--weeks', type=int, default=8)
    sub.add_parser('retention')
    args = parser.parse_args(argv)
    config = Config.from_env()
    if args.cmd == 'serve' and config.oauth_mode == 'fake' and args.host not in {'127.0.0.1', '::1', 'localhost'}:
        raise SystemExit('OAUTH_MODE=fake may only bind to a loopback address')
    init(config.db_path)
    if args.cmd == 'init-db':
        return
    db = connect(config.db_path)
    try:
        if args.cmd == 'seed':
            _seed(db, args)
        elif args.cmd == 'serve':
            if not db.execute('SELECT 1 FROM models LIMIT 1').fetchone():
                seed_models(db, PathRoot('seed/models.json'))
                db.commit()
        elif args.cmd == 'hide':
            db.execute("UPDATE reviews SET status='hidden' WHERE id=?", (args.id,))
            db.commit()
        elif args.cmd == 'report':
            if args.weeks < 1:
                parser.error('--weeks must be positive')
            sys.stdout.write(generate(db, args.slug, args.weeks))
        elif args.cmd == 'metrics':
            stranger = db.execute("SELECT COUNT(*),COUNT(DISTINCT user_id) FROM reviews WHERE source='user'").fetchone()
            signups = db.execute('SELECT COUNT(DISTINCT contact_digest) FROM waitlist').fetchone()[0]
            vendors = db.execute("SELECT COUNT(*) FROM waitlist WHERE kind='vendor' AND role IN ('devrel','pmm')").fetchone()[0]
            reproducible = sum(1 for row in db.execute('SELECT model_version,task_description FROM reviews') if specific(row[0]) and len(row[1]) >= 120)
            prices = dict(db.execute("SELECT price_interest,COUNT(*) FROM waitlist WHERE kind='vendor' GROUP BY price_interest"))
            print(f'stranger_reviews (source=user, distinct users): {stranger[0]} / 60 ({stranger[1]} users)\nemail_signups (reader+vendor, distinct digests): {signups} / 150\nvendor_contacts (kind=vendor, role in devrel/pmm): {vendors} / 3\nversion_specific_reproducible reviews: {reproducible} / 5\nprice_interest: report_500={prices.get("report_500",0)} report_1500={prices.get("report_1500",0)} sub_500_month={prices.get("sub_500_month",0)} none={prices.get("none",0)}\nPASS: {"yes" if stranger[0]>=60 and signups>=150 and vendors>=3 and reproducible>=5 else "no"}')
        elif args.cmd == 'retention':
            current = datetime.now(timezone.utc)
            cutoff = (current - timedelta(days=config.retention_days)).isoformat()
            vault = FileVault(os.path.join(config.vault_dir, 'contacts.jsonl'), config.effective_vault_key())
            for row in db.execute('SELECT vault_token FROM waitlist WHERE created_at<?', (cutoff,)).fetchall():
                vault.forget(row['vault_token'])
            db.execute('DELETE FROM waitlist WHERE created_at<?', (cutoff,))
            db.execute('DELETE FROM sessions WHERE expires_at<=?', (current.isoformat(),))
            db.execute('DELETE FROM oauth_states WHERE expires_at<=?', (current.isoformat(),))
            db.execute('DELETE FROM users WHERE created_at<? AND NOT EXISTS(SELECT 1 FROM reviews WHERE reviews.user_id=users.id)', ((current - timedelta(days=180)).isoformat(),))
            db.commit()
    finally:
        db.close()
    if args.cmd == 'serve':
        vault = FileVault(os.path.join(config.vault_dir, 'contacts.jsonl'), config.effective_vault_key())
        oauth = FakeOAuth() if config.oauth_mode == 'fake' else GitHubOAuth(config.github_client_id, config.github_client_secret)
        with make_server(args.host, args.port, App(config, vault, oauth), handler_class=QuietRequestHandler) as server:
            print(f'http://{args.host}:{args.port}')
            server.serve_forever()


if __name__ == '__main__':
    main()
