"""PC scheduling tests use fake futures and synthetic configs, never real helpers."""
from concurrent.futures import Future
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from src.school import pc


class StopLoop(Exception):
    pass


class SchoolPCTests(unittest.TestCase):
    def test_pending_decisions_requires_missing_terminal_result(self):
        decision = 'school/decisions/one.json'
        self.assertFalse(pc.pending_decisions({'other/one.json': 'a'}))
        self.assertTrue(pc.pending_decisions({decision: 'a'}))
        self.assertFalse(pc.pending_decisions({decision: 'a', 'school/decision-results/one.json': 'b'}))

    def test_long_collection_does_not_occupy_decision_execution_slot(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'queue_repo': 'synthetic/private'}), encoding='utf-8')
            api = MagicMock()
            api.head.return_value = 'revision-one'
            api.tree.return_value = {'school/index.json': 'index-one', 'school/decisions/pending.json': 'pending-one'}
            pool = MagicMock(); pool.__enter__.return_value = pool
            futures = []
            def submit(*args):
                future = Future()  # Both remain running throughout this observation.
                futures.append(future)
                return future
            pool.submit.side_effect = submit
            with patch.object(pc, 'GitHub', return_value=api), patch.object(pc, 'SupervisorLock'), \
                    patch.object(pc, 'ThreadPoolExecutor', return_value=pool) as executor, \
                    patch.object(pc, 'atomic_json') as write, patch.object(pc.time, 'sleep', side_effect=StopLoop):
                with self.assertRaises(StopLoop):
                    pc.run(config)
            executor.assert_called_once_with(max_workers=2)
            self.assertEqual([call.args[2] for call in pool.submit.call_args_list], [False, True])
            self.assertEqual(len(futures), 2)
            self.assertEqual(write.call_args.args[1]['active'], ['decisions', 'collect'])
            self.assertEqual(pc.POLL_SECONDS, 5)

    def test_decision_child_never_decrypts_or_passes_etl_credential(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            (config.parent / 'etl.dpapi').write_bytes(b'synthetic encrypted bytes')
            completed = MagicMock(returncode=0, stdout=b'{"status":"ok"}')
            with patch.dict(pc.os.environ, {'ETL_API_TOKEN': 'SYNTHETIC_SECRET'}), \
                    patch.object(pc, 'protected_bytes', side_effect=AssertionError('must not decrypt')), \
                    patch.object(pc.subprocess, 'run', return_value=completed) as child:
                code, report = pc.invoke(config, False)
                command = child.call_args.args[0]
                self.assertIn('--decisions-only', command)
                self.assertNotIn('--local-root', command)
                self.assertNotIn('ETL_API_TOKEN', child.call_args.kwargs['env'])
                self.assertEqual(code, 0)
                self.assertEqual(report['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
