"""版本存储、并发、跨进程持久化及 API 验收，无远程调用。"""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient
import prompt_manager as pm
from main import app


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prompts.db'
        patcher = patch.object(pm, 'DB_PATH', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_versions_latest_history_and_repeated_content(self):
        self.assertEqual(pm.save_template('greeting', 'Hello {{name}}'), 1)
        self.assertEqual(pm.save_template('greeting', 'Hi {{name}}'), 2)
        self.assertEqual(pm.save_template('greeting', 'Hi {{name}}'), 3)
        self.assertEqual(pm.get_template('greeting'), 'Hi {{name}}')
        self.assertEqual(pm.render_prompt('greeting', {'name': '{{x}}'}, 1), 'Hello {{x}}')
        self.assertEqual(pm.save_template('other', 'Other'), 1)
        for template, version in [('missing', None), ('greeting', 4), ('greeting', 0)]:
            with self.assertRaises(ValueError):
                pm.get_template(template, version)

    def test_defaults_and_composite_primary_key(self):
        pm.init_db()
        pm.save_template('translate', '新版')
        pm.init_db()
        pm.init_db()
        self.assertEqual(pm.get_template('translate'), '新版')
        self.assertEqual(pm.get_template('translate', 1), pm.DEFAULT_TEMPLATES['translate'])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM prompt_templates').fetchone()[0], 3)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO prompt_templates VALUES ('translate', 1, '重复')")

    def test_concurrent_saves(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            versions = list(executor.map(lambda i: pm.save_template('parallel', str(i)), range(20)))
        self.assertEqual(sorted(versions), list(range(1, 21)))

    def test_fresh_process_and_different_working_directory(self):
        module_dir = str(Path(pm.__file__).resolve().parent)
        prelude = ('import sys; from pathlib import Path; sys.path.insert(0, ' + repr(module_dir)
                   + '); import prompt_manager as p; p.DB_PATH = Path(' + repr(str(self.path)) + '); ')
        subprocess.run([sys.executable, '-c', prelude + "p.save_template('restart', 'persistent')"],
                       cwd=self.temp.name, check=True, capture_output=True)
        result = subprocess.run([sys.executable, '-c', prelude + "print(p.get_template('restart'))"],
                                cwd=self.temp.name, check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), 'persistent')
        result = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, ' + repr(module_dir)
                                 + '); import prompt_manager as p; print(p.DB_PATH)'],
                                cwd=self.temp.name, check=True, capture_output=True, text=True)
        self.assertEqual(Path(result.stdout.strip()), Path(module_dir) / 'prompts.db')

    def test_api_create_and_version_selection(self):
        with TestClient(app) as client:
            for version, content in [(1, 'Hello {{name}}'), (2, 'Hi {{name}}')]:
                response = client.post('/templates', json={'template_id': 'greeting', 'content': content})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'template_id': 'greeting', 'version': version})
            with patch('main.complete_reply', return_value={'content': 'ok'}) as complete:
                for version, expected in [(1, 'Hello 张三'), (2, 'Hi 张三'), (None, 'Hi 张三')]:
                    body = {'template_id': 'greeting', 'variables': {'name': '张三'}, 'stream': False}
                    if version is not None:
                        body['template_version'] = version
                    self.assertEqual(client.post('/chat', json=body).status_code, 200)
                    self.assertEqual(complete.call_args.args[0], [{'role': 'user', 'content': expected}])

    def test_invalid_api_input_before_model(self):
        with TestClient(app) as client, patch('main.complete_reply') as complete, patch('main.stream_reply') as stream:
            for body in [{}, {'template_id': 'x'}, {'template_id': ' ', 'content': 'x'},
                         {'template_id': 'x', 'content': ''}, {'template_id': 'x', 'content': '  '},
                         {'template_id': 1, 'content': 'x'}, {'template_id': 'x', 'content': None},
                         {'template_id': 'x', 'content': 'x', 'version': 9}]:
                self.assertEqual(client.post('/templates', json=body).status_code, 422, body)
            for streaming in [True, False]:
                for body in [
                    *({'template_id': 'summarize', 'template_version': v, 'variables': {'text': 'x'}}
                      for v in [0, -1, 1.5, True, '1', 99]),
                    {'prompt': 'hello', 'template_version': 1},
                    {'messages': [{'role': 'user', 'content': 'hello'}], 'template_version': 1},
                    {'template_id': 'unknown', 'template_version': 1},
                    {'template_id': 'translate', 'template_version': 1, 'variables': {'text': 'x'}},
                ]:
                    self.assertEqual(client.post('/chat', json={**body, 'stream': streaming}).status_code, 422, body)
            complete.assert_not_called()
            stream.assert_not_called()
