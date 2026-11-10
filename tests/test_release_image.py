import unittest
from unittest.mock import patch
import urllib.error

from scripts.release_image import get_json, needs_publish


class ReleaseImageTests(unittest.TestCase):
    source = 'a' * 40
    version = 'v2000.01.01-00'

    def fetch(self, url, headers=None, expected=None):
        if '/token?' in url:
            return {'token': 'synthetic-registry-token'}
        self.assertEqual(headers['Authorization'], 'Bearer synthetic-registry-token')
        if '/blobs/' in url:
            self.assertEqual(expected, 'sha256:synthetic-config')
            return {'config': {'Labels': self.labels}}
        return {'config': {'digest': 'sha256:synthetic-config'}}

    def setUp(self):
        self.labels = {'org.opencontainers.image.version': self.version,
                       'org.opencontainers.image.revision': self.source}

    def test_existing_exact_source_is_retained_without_republication(self):
        self.assertFalse(needs_publish('synthetic/application', self.version, self.source, self.fetch))

    def test_missing_or_changed_provenance_blocks_overwrite(self):
        for value in [{}, {**self.labels, 'org.opencontainers.image.revision': 'b' * 40},
                      {**self.labels, 'org.opencontainers.image.version': 'v2000.01.01-01'}]:
            self.labels = value
            with self.assertRaises(ValueError):
                needs_publish('synthetic/application', self.version, self.source, self.fetch)

    def test_index_requires_amd64_and_verifies_selected_manifest_digest(self):
        platforms = [{'platform': {'os': 'linux', 'architecture': 'arm64'}, 'digest': 'sha256:synthetic-arm'}]
        def fetch(url, headers=None, expected=None):
            if url.endswith('/' + self.version): return {'manifests': platforms}
            if url.endswith('/sha256:synthetic-amd'):
                self.assertEqual('sha256:synthetic-amd', expected)
                return {'config': {'digest': 'sha256:synthetic-config'}}
            return self.fetch(url, headers, expected)
        with self.assertRaises(ValueError):
            needs_publish('synthetic/application', self.version, self.source, fetch)
        platforms.append({'platform': {'os': 'linux', 'architecture': 'amd64'}, 'digest': 'sha256:synthetic-amd'})
        self.assertFalse(needs_publish('synthetic/application', self.version, self.source, fetch))

    def test_only_manifest_not_found_permits_new_version(self):
        def fetch(url, *args):
            if '/token?' in url: return {'token': 'synthetic-token'}
            raise urllib.error.HTTPError(url, status[0], 'synthetic response', {}, None)
        for code in [404, 401, 403, 429, 500]:
            status = [code]
            if code == 404:
                self.assertTrue(needs_publish('synthetic/application', self.version, self.source, fetch))
            else:
                with self.assertRaises(urllib.error.HTTPError):
                    needs_publish('synthetic/application', self.version, self.source, fetch)

    def test_invalid_version_source_or_repository_never_requests_registry(self):
        def fetch(*args): self.fail('Invalid publication reached registry')
        for repository, version, source in [('synthetic/../../invalid', self.version, self.source),
                                           ('synthetic/application', 'latest', self.source),
                                           ('synthetic/application', self.version, 'main')]:
            with self.assertRaises(ValueError): needs_publish(repository, version, source, fetch)

    @patch('scripts.release_image.time.sleep')
    @patch('scripts.release_image.urllib.request.urlopen')
    def test_transient_transfer_retries_with_verification_retained(self, open_url, sleep):
        from io import BytesIO
        open_url.side_effect = [urllib.error.URLError('decryption failed or bad record mac'), BytesIO(b'{"verified":true}')]
        self.assertEqual({'verified': True}, get_json('https://registry.invalid/manifest'))
        self.assertEqual(2, open_url.call_count)
        sleep.assert_called_once_with(5)

    @patch('scripts.release_image.urllib.request.urlopen')
    def test_digest_mismatch_is_not_retried(self, open_url):
        from io import BytesIO
        open_url.return_value = BytesIO(b'{}')
        with self.assertRaises(ValueError): get_json('https://registry.invalid/config', expected='sha256:incorrect')
        self.assertEqual(1, open_url.call_count)
