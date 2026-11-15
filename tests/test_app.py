import html
import http.client
import io
import json
import os
import tempfile
import threading
import urllib.parse
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from app import COLUMNS, NOTICE, State, create_server, digest


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server('127.0.0.1', 0, self.temp.name)
        self.port = self.server.server_address[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, values=None, headers=None, body=None):
        if body is None:
            body = urllib.parse.urlencode(values or {}).encode()
        supplied = {'Host': f'127.0.0.1:{self.port}'}
        supplied.update(headers or {})
        if method == 'POST':
            supplied.setdefault('Origin', self.base)
            supplied.setdefault('Referer', self.base + '/')
            supplied.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request(method, path, body=body if method == 'POST' else None, headers=supplied)
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def multipart(self, fields, file_bytes=None):
        boundary = 'reviewtrail-test-boundary'
        chunks = []
        for name, value in fields.items():
            chunks.extend([f'--{boundary}\r\n'.encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), str(value).encode(), b'\r\n'])
        if file_bytes is not None:
            chunks.extend([f'--{boundary}\r\n'.encode(), b'Content-Disposition: form-data; name="file"; filename="runs.csv"\r\nContent-Type: text/csv\r\n\r\n', file_bytes, b'\r\n'])
        chunks.append(f'--{boundary}--\r\n'.encode())
        return b''.join(chunks), {'Content-Type': f'multipart/form-data; boundary={boundary}'}

    def workspace(self, sample=False):
        status, headers, _ = self.request('POST', '/w/sample' if sample else '/w')
        self.assertEqual(status, 303)
        return urllib.parse.urlsplit(headers['Location']).path

    def valid(self, **changes):
        values = {'model_slug': 'claude-code', 'model_version': 'release-2026.10', 'task_category': 'coding', 'task_description': 'Add robust validation to a CSV importer and cover malformed rows with regression tests.', 'stack': 'python/sqlite', 'harness': 'cli-agent', 'outcome': '4', 'retries': '1', 'cost_usd': '1.25', 'used_on': date.today().isoformat()}
        values.update(changes)
        return values

    def import_text(self, workspace, text):
        body, headers = self.multipart({'data': text})
        return self.request('POST', workspace + '/import', headers=headers, body=body)

    def test_create_add_export_share_delete(self):
        workspace = self.workspace()
        status, _, page = self.request('GET', workspace + '?new=1')
        self.assertEqual(status, 200)
        self.assertIn('Bookmark this link', page.decode())
        self.assertIn(NOTICE, html.unescape(page.decode()))
        status, headers, _ = self.request('POST', workspace + '/runs', self.valid())
        self.assertEqual(status, 303)
        self.assertIn('4.0', self.request('GET', urllib.parse.urlsplit(headers['Location']).path)[2].decode())
        exported = json.loads(self.request('GET', workspace + '/export')[2])
        self.assertEqual(len(exported['runs']), 1)
        status, _, share_page = self.request('POST', workspace + '/share', {'action': 'create'})
        self.assertEqual(status, 200)
        match = __import__('re').search(r'/s/[A-Za-z0-9_-]+', share_page.decode())
        shared = self.request('GET', match.group())[2].decode()
        self.assertIn('Read-only shared view', shared)
        self.assertNotIn(match.group() + '/runs', shared)
        self.assertEqual(self.request('POST', workspace + '/delete', {'confirm': 'on'})[0], 303)
        self.assertEqual(self.request('GET', workspace)[0], 404)

    def test_real_workspaces_are_not_silently_expired(self):
        workspace = self.workspace()
        token = workspace.split('/')[-1]
        path = os.path.join(self.temp.name, 'ws', digest(token), 'workspace.json')
        value = json.loads(open(path, encoding='utf-8').read())
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        value['created'] = old
        value['last_active'] = old
        with open(path, 'w', encoding='utf-8') as output:
            json.dump(value, output)
        self.server.state.sweep()
        self.assertEqual(self.request('GET', workspace)[0], 200)

    def test_sample_retention_is_visible_and_does_not_apply_to_real_data(self):
        workspace = self.workspace(sample=True)
        page = self.request('GET', workspace)[2].decode()
        self.assertIn('Sample workspaces expire after 24 hours', page)

    def test_repeated_task_on_different_date_is_retained_but_exact_duplicate_skipped(self):
        workspace = self.workspace()
        header = ','.join(COLUMNS)
        task = 'Repeat this exact parser task while measuring the weekly model outcome over time.'
        today = date.today()
        previous = today - timedelta(days=7)
        csv_text = header + '\n' + f'claude-code,v1-2026,coding,{task},python,cli-agent,5,0,1.00,{previous}\n' + f'claude-code,v1-2026,coding,{task},python,cli-agent,3,2,1.50,{today}\n' + f'claude-code,v1-2026,coding,{task},python,cli-agent,3,2,1.50,{today}\n'
        page = self.import_text(workspace, csv_text)[2].decode()
        self.assertIn('2 added', page)
        self.assertIn('1 exact duplicates skipped', page)
        self.assertIn('1 repeated-task runs retained', page)
        self.assertEqual(len(json.loads(self.request('GET', workspace + '/export')[2])['runs']), 2)

    def test_json_array_and_custom_model_export_restore(self):
        workspace = self.workspace()
        run = {'model': 'my-private-agent', 'version': 'v1-2026', 'category': 'agents', 'task': 'Exercise a private agent on a sufficiently detailed repeatable coding task.', 'stack': 'python', 'harness': 'cli-agent', 'outcome': 4, 'retries': 1, 'cost_cents': 125, 'date': date.today().isoformat()}
        export = {'custom_models': [{'slug': 'my-private-agent', 'name': 'My Private Agent'}], 'runs': [run]}
        self.assertEqual(self.import_text(workspace, json.dumps(export))[0], 200)
        restored = json.loads(self.request('GET', workspace + '/export')[2])
        self.assertEqual(restored['custom_models'][0]['name'], 'My Private Agent')
        second = self.workspace()
        self.assertEqual(self.import_text(second, json.dumps([dict(run, model='Another Local Model')]))[0], 200)
        self.assertEqual(len(json.loads(self.request('GET', second + '/export')[2])['runs']), 1)

    def test_csv_unknown_model_becomes_custom_and_sensitive_name_is_rejected(self):
        workspace = self.workspace()
        task = 'Run a custom local model through a repeatable parser regression task safely.'
        good = ','.join(COLUMNS) + '\nLocal Runner,v1-2026,coding,' + task + ',python,cli-agent,4,0,,' + date.today().isoformat() + '\n'
        self.assertIn('1 added', self.import_text(workspace, good)[2].decode())
        status, _, page = self.request('POST', workspace + '/runs', self.valid(custom_model='person@example.test'))
        self.assertEqual(status, 400)
        self.assertIn('Remove personal data', page.decode())

    def test_validation_error_preserves_every_entered_field(self):
        workspace = self.workspace()
        task = 'x' * 200
        status, _, page = self.request('POST', workspace + '/runs', self.valid(task_description=task, retries='999', custom_model='Local Safe Model'))
        text = page.decode()
        self.assertEqual(status, 400)
        self.assertIn(task, text)
        self.assertIn('value="999"', text)
        self.assertIn('value="Local Safe Model"', text)

    def test_bom_csv_is_accepted_and_invalid_utf8_is_a_400(self):
        workspace = self.workspace()
        task = 'Validate an Excel-style UTF-8 CSV file with a byte order marker safely.'
        text = '\ufeff' + ','.join(COLUMNS) + '\nclaude-code,v1-2026,coding,' + task + ',python,cli-agent,4,0,,' + date.today().isoformat() + '\n'
        body, headers = self.multipart({}, text.encode('utf-8'))
        self.assertIn('1 added', self.request('POST', workspace + '/import', headers=headers, body=body)[2].decode())
        body, headers = self.multipart({}, b'\xff\xfe')
        status, _, page = self.request('POST', workspace + '/import', headers=headers, body=body)
        self.assertEqual(status, 400)
        self.assertIn('UTF-8', page.decode())

    def test_extra_csv_columns_fail_cleanly_before_saving(self):
        workspace = self.workspace()
        text = ','.join(COLUMNS) + '\nclaude-code,v1-2026,coding,"A sufficiently long parser regression task for safe import testing.",python,cli-agent,4,0,,' + date.today().isoformat() + ',extra\n'
        status, _, _ = self.import_text(workspace, text)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(self.request('GET', workspace + '/export')[2])['runs'], [])

    def test_continuation_uses_multipart_to_avoid_percent_encoding_inflation(self):
        workspace = self.workspace()
        row = {'model': 'claude-code', 'version': 'v1-2026', 'category': 'coding', 'task': 'A repeatable continuation import task containing spaces and punctuation for testing.', 'stack': 'python', 'harness': 'cli-agent', 'outcome': '4', 'retries': '0', 'cost_usd': '', 'date': date.today().isoformat()}
        output = io.StringIO()
        writer = __import__('csv').DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerow(row)
        with patch('app.IMPORT_BUDGET', 0):
            page = self.import_text(workspace, output.getvalue())[2].decode()
        self.assertIn('enctype="multipart/form-data"', page)

    def test_shrinking_writes_work_when_global_storage_is_full(self):
        workspace = self.workspace()
        self.request('POST', workspace + '/runs', self.valid())
        exported = json.loads(self.request('GET', workspace + '/export')[2])
        run_id = exported['runs'][0]['id']
        self.server.state.cap = 0
        status, _, _ = self.request('POST', workspace + f'/runs/{run_id}/delete', {})
        self.assertEqual(status, 303)

    def test_health_ignores_proxy_host_and_https_adds_hsts(self):
        self.assertEqual(self.request('GET', '/healthz', headers={'Host': '10.0.0.4'})[0], 200)
        self.server.state.https_terminated = True
        headers = self.request('GET', '/')[1]
        self.assertEqual(headers['Strict-Transport-Security'], 'max-age=31536000')

    def test_referer_origin_is_parsed_not_prefix_matched(self):
        self.assertEqual(self.request('POST', '/w', headers={'Origin': '', 'Referer': self.base + '.evil.test/'})[0], 403)
        self.assertEqual(self.request('POST', '/w', headers={'Origin': '', 'Referer': self.base + '/safe'})[0], 303)
        self.assertEqual(self.request('POST', '/w', headers={'Origin': 'https://evil.test'})[0], 403)

    def test_runs_have_names_tasks_and_pagination_links(self):
        workspace = self.workspace(sample=True)
        token = workspace.split('/')[-1]
        whash = digest(token)
        stored = self.server.state.load_hash(whash)
        while len(stored['runs']) < 51:
            item = dict(stored['runs'][0])
            item['id'] = max(run['id'] for run in stored['runs']) + 1
            item['task'] = 'Pagination-identifying task ' + str(item['id']) + ' with enough descriptive text.'
            stored['runs'].append(item)
        self.server.state.save(whash, stored)
        page = self.request('GET', workspace + '/runs')[2].decode()
        self.assertIn('Claude Code', page)
        self.assertIn('<th>Task</th>', page)
        self.assertIn('Next', page)

    def test_write_budget_keys_are_bounded(self):
        state = self.server.state
        for number in range(5000):
            state.allow_write(str(number))
        self.assertLessEqual(len(state.write_budgets), 4096)


if __name__ == '__main__':
    unittest.main()
