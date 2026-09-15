import base64
import unittest
from unittest.mock import Mock

from src.bridge.github import GitHub


class LargePrivateIndexTests(unittest.TestCase):
    def test_contents_size_limit_uses_pinned_blob(self):
        api = GitHub('synthetic')
        sha = 'a' * 40
        api.api = Mock(side_effect=[{'type':'file','encoding':'none','sha':sha,'size':1100000},
                                   {'encoding':'base64','sha':sha,'content':base64.b64encode(b'{"items":[]}').decode()}])
        self.assertEqual(api.read_json('owner/private', 'school/index.json', 'old-commit'), ({'items':[]}, sha))
        self.assertIn('ref=old-commit', api.api.call_args_list[0].args[0])
        self.assertEqual(api.api.call_args_list[1].args[0], 'repos/owner/private/git/blobs/' + sha)

    def test_oversized_metadata_is_rejected_before_download(self):
        api = GitHub('synthetic')
        api.api = Mock(return_value={'type':'file','encoding':'none','sha':'a'*40,'size':9000000})
        with self.assertRaises(RuntimeError): api.read('owner/private','school/index.json')
        self.assertEqual(api.api.call_count, 1)

    def test_unexpected_blob_version_is_rejected(self):
        api = GitHub('synthetic')
        api.api = Mock(side_effect=[{'type':'file','encoding':'none','sha':'a'*40,'size':1100000},
                                   {'encoding':'base64','sha':'b'*40,'content':'e30='}])
        with self.assertRaises(RuntimeError): api.read('owner/private','school/index.json')
