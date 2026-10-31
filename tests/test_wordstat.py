import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / 'seo-agent/scripts/wordstat.py'
spec = importlib.util.spec_from_file_location('wordstat', SCRIPT)
wordstat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wordstat)

class WordstatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_literal_env_and_precedence(self):
        env = self.root / '.env'
        env.write_text('export YANDEX_API_KEY="file-key" # comment\nYANDEX_FOLDER_ID=folder\nLITERAL=$(touch should-not-exist)\n')
        values = wordstat.load_env(env, {'YANDEX_API_KEY': 'env-key'})
        self.assertEqual(wordstat.credentials(values), ('env-key', 'folder'))
        self.assertEqual(values['LITERAL'], '$(touch should-not-exist)')
        env.write_text('YANDEX_API_KEY="unclosed')
        with self.assertRaises(wordstat.WordstatError):
            wordstat.load_env(env, {})

    def test_request_contract(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"totalCount":"10","results":[]}'
        with patch.object(wordstat, 'build_opener') as builder:
            builder.return_value.open.return_value = Response()
            payload = {'phrase': 'утепление', 'folderId': 'folder', 'numPhrases': '100'}
            wordstat.call_api('topRequests', payload, 'test-secret')
            request = builder.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, wordstat.BASE + 'topRequests')
            self.assertEqual(request.get_header('Authorization'), 'Api-Key test-secret')
            self.assertEqual(json.loads(request.data), payload)
            self.assertEqual(request.get_method(), 'POST')

    def test_cache_dedup_provenance_and_budget(self):
        response = {'totalCount': '20', 'results': [{'phrase': 'Утепление дома', 'count': '12'}], 'associations': [{'phrase': 'утепление  дома', 'count': '12'}]}
        with patch.object(wordstat, 'call_api', return_value=response) as api:
            first = wordstat.collect(['утепление'], ['225'], 'all', 100, 'folder', 'test-secret', self.root)
            cached = wordstat.collect(['утепление'], ['225'], 'all', 100, 'folder', 'test-secret', self.root)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(cached['network_calls'], 0)
            self.assertEqual(len(first['keywords']), 1)
            self.assertEqual(len(first['keywords'][0]['observations']), 2)
            self.assertNotIn('test-secret', json.dumps(first))
            self.assertEqual(first['reports'][0]['fetched_at'], cached['reports'][0]['fetched_at'])
            with self.assertRaises(wordstat.WordstatError):
                wordstat.collect(['other', 'third'], ['225'], 'all', 100, 'folder', 'test-secret', self.root, max_requests=1)
            self.assertEqual(api.call_count, 2)

    def test_http_failure_does_not_leak_or_retry(self):
        error = HTTPError(wordstat.BASE, 403, 'test-secret', {}, io.BytesIO(b'test-secret'))
        with patch.object(wordstat, 'build_opener') as builder:
            builder.return_value.open.side_effect = error
            with self.assertRaises(wordstat.WordstatError) as caught:
                wordstat.call_api('topRequests', {}, 'test-secret')
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertIn('403', str(caught.exception))
            self.assertEqual(builder.return_value.open.call_count, 1)

    def test_bad_frequency_does_not_cache(self):
        with patch.object(wordstat, 'call_api', return_value={'results': [{'phrase': 'test', 'count': 'bad'}]}):
            with self.assertRaises(wordstat.WordstatError):
                wordstat.collect(['test'], [], 'all', 10, 'folder', 'key', self.root)
            self.assertEqual(list(self.root.glob('*.json')), [])

if __name__ == '__main__':
    unittest.main()
