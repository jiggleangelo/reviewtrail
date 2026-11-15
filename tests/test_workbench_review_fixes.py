import importlib.util
import json
import socket
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import app
try:
    from tests import test_app as existing
except ImportError:
    import test_app as existing


class WorkbenchReviewFixesTest(unittest.TestCase):
    setUp = existing.AppTest.setUp
    tearDown = existing.AppTest.tearDown
    request = existing.AppTest.request
    multipart = existing.AppTest.multipart
    workspace = existing.AppTest.workspace
    valid = existing.AppTest.valid
    import_text = existing.AppTest.import_text

    def export(self, workspace):
        return json.loads(self.request('GET', workspace + '/export')[2])

    def row(self, model='claude-code', task=None):
        return {'model': model, 'version': 'v1-2026', 'category': 'coding', 'task': task or 'Exercise the parser with representative inputs and regression tests safely.', 'stack': 'python', 'harness': 'cli-agent', 'outcome': 4, 'retries': 0, 'cost_cents': None, 'date': date.today().isoformat()}

    def test_same_origin_policy_and_opaque_origin_rejection(self):
        status, headers, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        self.assertEqual(self.request('POST', '/w', headers={'Origin': 'null'})[0], 403)
        self.assertEqual(self.request('POST', '/w', headers={'Origin': 'https://outside.test'})[0], 403)
        self.assertEqual(self.request('POST', '/w')[0], 303)

    def test_creation_budget_shared_by_empty_and_sample_and_survives_restart(self):
        with patch('app.GLOBAL_CREATIONS_PER_HOUR', 2):
            self.workspace()
            self.workspace(sample=True)
            self.assertEqual(self.request('POST', '/w')[0], 503)
            restored = app.State(self.temp.name, self.base)
            with self.assertRaises(app.RequestError) as error:
                restored.create(sample=True)
            self.assertEqual(error.exception.status, 503)
            budget = json.loads((restored.root / 'meta' / 'budgets.json').read_text())
            self.assertEqual(budget['creations'], 2)
            with patch('app.time.time', return_value=(budget['hour'] + 1) * 3600):
                self.assertTrue(restored.create())

    def test_global_write_and_growth_budgets_span_workspaces(self):
        first, second = self.workspace(), self.workspace()
        state = self.server.state
        with patch('app.GLOBAL_WRITES_PER_HOUR', state.budgets['writes'] + 1):
            self.assertEqual(self.request('POST', first + '/runs', self.valid())[0], 303)
            self.assertEqual(self.request('POST', second + '/runs', self.valid())[0], 503)
        with patch('app.GLOBAL_GROWTH_PER_HOUR', state.budgets['growth']):
            self.assertEqual(self.request('POST', second + '/runs', self.valid())[0], 503)
        self.assertEqual(self.export(second)['runs'], [])

    def test_sample_creation_uses_index_and_deletion_releases_capacity(self):
        state = self.server.state
        with patch('app.MAX_SAMPLE_WORKSPACES', 1):
            with patch.object(state, 'load_hash', side_effect=AssertionError('Creation must not load other workspaces')):
                token = state.create(sample=True)
                with self.assertRaises(app.RequestError):
                    state.create(sample=True)
            whash, workspace = state.load_token(token)
            state.delete(whash, workspace)
            with patch.object(state, 'load_hash', side_effect=AssertionError('Creation must not scan')):
                self.assertTrue(state.create(sample=True))
        restored = app.State(self.temp.name, self.base)
        self.assertEqual(restored.samples, state.samples)

    def test_dated_custom_model_names_work_in_form_and_csv(self):
        workspace = self.workspace()
        self.assertEqual(self.request('POST', workspace + '/runs', self.valid(custom_model='claude-sonnet-4-20250514'))[0], 303)
        row = self.row('gpt-4.1-2025-04-14')
        text = ','.join(app.COLUMNS) + '\n' + ','.join([row['model'], row['version'], row['category'], row['task'], row['stack'], row['harness'], '4', '0', '', row['date']]) + '\n'
        self.assertIn('1 added', self.import_text(workspace, text)[2].decode())
        status, _, page = self.request('POST', workspace + '/runs', self.valid(custom_model='person@example.test'))
        self.assertEqual(status, 400)
        self.assertIn('Dated model names are allowed', page.decode())

    def test_git_sha_is_preserved_but_explicit_secret_is_rejected(self):
        workspace = self.workspace()
        sha = '0123456789abcdef0123456789abcdef01234567'
        task = 'Check commit ' + sha + ' and verify the parser regression tests.'
        self.assertEqual(self.request('POST', workspace + '/runs', self.valid(task_description=task))[0], 303)
        run = self.export(workspace)['runs'][0]
        self.assertEqual(run['task'], task)
        self.assertEqual(run['hash'], app.content_hash(task, run['stack'], run['version']))
        self.assertEqual(self.request('POST', workspace + '/runs', self.valid(task_description='Inspect this parser failure with api_key=' + sha))[0], 400)
        self.assertEqual(self.request('POST', workspace + '/runs', self.valid(task_description='Inspect this parser failure and contact person@example.test.'))[0], 400)

    def test_restore_existing_seed_does_not_index_empty_custom_list(self):
        workspace = self.workspace()
        data = {'custom_models': [{'slug': 'legacy-claude', 'name': 'Claude Code'}], 'runs': [self.row('legacy-claude')]}
        self.assertIn('1 added', self.import_text(workspace, json.dumps(data))[2].decode())
        exported = self.export(workspace)
        self.assertEqual(exported['custom_models'], [])
        self.assertEqual(exported['runs'][0]['model'], 'claude-code')

    def test_restore_existing_custom_does_not_rename_unrelated_model(self):
        workspace = self.workspace()
        for name in ('Local One', 'Local Two'):
            self.assertEqual(self.request('POST', workspace + '/runs', self.valid(custom_model=name))[0], 303)
        before = self.export(workspace)['custom_models']
        data = {'custom_models': [{'slug': 'old-local-one', 'name': 'Local One'}], 'runs': [self.row('old-local-one')]}
        self.assertEqual(self.import_text(workspace, json.dumps(data))[0], 200)
        exported = self.export(workspace)
        self.assertEqual(exported['custom_models'], before)
        self.assertEqual(exported['runs'][-1]['model'], 'local-one')
        for model in before:
            self.assertEqual(self.request('GET', workspace + '/models/' + model['slug'])[0], 200)
        collision = {'custom_models': [{'slug': 'local-two', 'name': 'Local One'}], 'runs': []}
        self.assertEqual(self.import_text(workspace, json.dumps(collision))[0], 400)
        self.assertEqual(self.export(workspace)['custom_models'], before)

    def test_import_upload_outlives_normal_connection_deadline(self):
        workspace = self.workspace()
        body, headers = self.multipart({'data': json.dumps([self.row()])})
        with patch.object(app.Handler, 'request_deadline', 0.15), patch.object(app.Handler, 'import_deadline', 2):
            with socket.create_connection(('127.0.0.1', self.port), timeout=3) as connection:
                request = 'POST ' + workspace + '/import HTTP/1.0\r\nHost: 127.0.0.1:' + str(self.port) + '\r\nOrigin: ' + self.base + '\r\nContent-Type: ' + headers['Content-Type'] + '\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n'
                connection.sendall(request.encode() + body[:1])
                time.sleep(0.35)
                connection.sendall(body[1:])
                response = b''
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    response += chunk
                self.assertTrue(response.startswith(b'HTTP/1.0 200'), response[:200])
        self.assertEqual(len(self.export(workspace)['runs']), 1)

    def test_busy_data_requests_reject_before_loading_and_health_stays_available(self):
        workspace = self.workspace()
        slots = self.server._data_slots
        for _ in range(app.MAX_ACTIVE_DATA_REQUESTS):
            self.assertTrue(slots.acquire(blocking=False))
        try:
            with patch.object(self.server.state, 'load_hash', side_effect=AssertionError('Must reject before loading')):
                self.assertEqual(self.request('GET', workspace)[0], 503)
                self.assertEqual(self.request('POST', workspace + '/import', {'data': 'x'})[0], 503)
                self.assertEqual(self.request('GET', '/healthz')[0], 200)
        finally:
            for _ in range(app.MAX_ACTIVE_DATA_REQUESTS):
                slots.release()
        self.assertEqual(self.request('GET', workspace)[0], 200)

    def test_oversized_persisted_workspace_is_not_decoded(self):
        workspace = self.workspace()
        with patch('app.MAX_WORKSPACE_BYTES', 1), patch('app.json.loads', side_effect=AssertionError('Must check size before JSON decoding')):
            self.assertIsNone(self.server.state.load_hash(app.digest(workspace.split('/')[-1])))

    def test_workspace_lock_survives_more_than_4096_other_keys(self):
        state = self.server.state
        lock = state.lock('target')
        entered = threading.Event()
        lock.acquire()
        try:
            for number in range(5000):
                state.lock(str(number))
            self.assertIs(state.lock('target'), lock)
            def contender():
                with state.lock('target'):
                    entered.set()
            thread = threading.Thread(target=contender)
            thread.start()
            self.assertFalse(entered.wait(0.05))
        finally:
            lock.release()
        thread.join(timeout=2)
        self.assertTrue(entered.is_set())
        self.assertEqual(len(state.locks), 256)

    def test_storage_accounting_includes_tombstones_and_directory_reserves(self):
        workspace = self.workspace()
        state = self.server.state
        self.assertEqual(state.usage(), state._scan_usage())
        whash = app.digest(workspace.split('/')[-1])
        self.assertEqual(self.request('POST', workspace + '/delete', {'confirm': 'on'})[0], 303)
        self.assertEqual(state.usage(), state._scan_usage())
        tombstone = state.root / 'tombstones' / whash
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat().encode('ascii')
        state._write(tombstone, old, allow_growth=False)
        before = state.usage()
        charge = state._charge(tombstone)
        state.sweep()
        self.assertFalse(tombstone.exists())
        self.assertEqual(state.usage(), before - charge)
        self.assertEqual(state.usage(), state._scan_usage())
        self.assertGreaterEqual(state.usage(), 5 * app.ENTRY_OVERHEAD)

    def test_full_workspace_has_no_impossible_continue_or_import_form(self):
        workspace = self.workspace()
        with patch('app.MAX_RUNS', 1):
            payload = json.dumps([self.row(), self.row(task='A second repeatable parser task with representative test inputs and validation.')])
            status, _, response = self.import_text(workspace, payload)
            self.assertEqual(status, 200)
            self.assertIn('Remaining rows were not imported', response.decode())
            self.assertNotIn('<button>Continue</button>', response.decode())
            page = self.request('GET', workspace + '/import')[2].decode()
            self.assertIn('Export or delete runs', page)
            self.assertNotIn('<button>Import</button>', page)
        self.assertEqual(len(self.export(workspace)['runs']), 1)

    def test_real_browser_forms_emit_accepted_origins(self):
        try:
            available = importlib.util.find_spec('playwright.sync_api') is not None
        except ModuleNotFoundError:
            available = False
        if not available:
            self.skipTest('Playwright and its Chromium browser are required for the real-browser regression')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                posts = []
                page.on('request', lambda request: posts.append(request) if request.method == 'POST' else None)
                page.goto(self.base)
                page.get_by_role('button', name='Start a workspace', exact=True).click()
                page.wait_for_url('**/w/**?new=1')
                workspace = page.url.split('?')[0]
                page.get_by_role('link', name='Add run', exact=True).click()
                for name, value in self.valid().items():
                    control = page.locator('[name="' + name + '"]')
                    if name in ('model_slug', 'task_category', 'harness'):
                        control.select_option(value)
                    else:
                        control.fill(value)
                page.get_by_role('button', name='Add run', exact=True).click()
                page.wait_for_url('**/models/claude-code')
                page.goto(workspace)
                page.get_by_role('button', name='Create share link', exact=True).click()
                self.assertIn('Copy this read-only link now', page.content())
                page.goto(workspace)
                page.get_by_role('button', name='Revoke', exact=True).click()
                page.wait_for_url(workspace)
                page.locator('[name="confirm"]').check()
                page.get_by_role('button', name='Delete this workspace now', exact=True).click()
                page.wait_for_url('**/?deleted=1')
                self.assertGreaterEqual(len(posts), 5)
                for request in posts:
                    self.assertEqual(request.all_headers().get('origin'), self.base)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
