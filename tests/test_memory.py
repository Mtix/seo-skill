import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'seo-agent/scripts/memory.py'
spec = importlib.util.spec_from_file_location('memory', SCRIPT)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'memory.sqlite3'
        self.file = Path(self.tmp.name) / 'state.json'

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(self.db), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
        return json.loads(result.stdout) if ok else result.stderr

    def save(self, state, version, ok=True):
        self.file.write_text(json.dumps(state))
        return self.run_cli('save', 'example.com', '--file', str(self.file), '--expected-version', str(version), '--note', 'Test update', ok=ok)

    def test_persistence_isolation_history_and_conflict(self):
        original = self.run_cli('init', 'https://www.example.com/blog')
        state = original['state']
        state['tasks'].append(dict(id='SEO-001', title='Статья', status='planned'))
        saved = self.save(state, 1)
        self.assertEqual(saved['version'], 2)
        self.assertEqual(self.run_cli('init', 'example.com'), saved)
        self.assertIn('Version conflict', self.save(state, 1, ok=False))
        self.run_cli('init', 'other.example.com')
        self.assertEqual(self.run_cli('show', 'other.example.com')['state']['tasks'], [])
        self.assertEqual(len(self.run_cli('history', 'example.com')), 2)
        self.assertEqual(self.run_cli('show', 'example.com'), saved)

    def test_invalid_changes_rollback(self):
        state = self.run_cli('init', 'example.com')['state']
        state['tasks'] = [dict(id='SEO-001', title='Статья', status='done', completion_evidence='User confirmed URL')]
        state['decisions'] = [{'decision': 'Create first task'}]
        self.save(state, 1)
        bad = copy.deepcopy(state)
        bad['tasks'] = []
        self.save(bad, 2, ok=False)
        bad = copy.deepcopy(state)
        bad['decisions'] = []
        self.save(bad, 2, ok=False)
        bad = copy.deepcopy(state)
        bad['tasks'][0].pop('completion_evidence')
        self.save(bad, 2, ok=False)
        self.assertEqual(self.run_cli('show', 'example.com')['version'], 2)
        self.assertEqual(len(self.run_cli('history', 'example.com')), 2)

    def test_dependencies_and_publication_evidence(self):
        state = self.run_cli('init', 'example.com')['state']
        for tasks in [
            [dict(id='A', title='A', status='planned', depends_on=['B'])],
            [dict(id='A', title='A', status='planned', depends_on=['B']), dict(id='B', title='B', status='planned', depends_on=['A'])],
            [dict(id='A', title='A', status='published')],
            [dict(id='A', title='A', status='planned'), dict(id='A', title='duplicate', status='planned')],
        ]:
            state['tasks'] = tasks
            self.save(state, 1, ok=False)
        self.assertEqual(self.run_cli('show', 'example.com')['version'], 1)

if __name__ == '__main__':
    unittest.main()
