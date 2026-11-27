from pathlib import Path
import tempfile
import unittest

from inferrsi_loop.recovery import archive_incomplete


class RecoveryTests(unittest.TestCase):
    def test_completed_preparation_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "planning"
            folder.mkdir()
            (folder / "proposal.json").write_text('{}')
            self.assertIsNone(archive_incomplete(folder, "proposal.json"))
            self.assertEqual((folder / "proposal.json").read_text(), '{}')

    def test_repeated_interruptions_retain_each_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "planning"
            retained = []
            for evidence in (b'first failure', b'second failure'):
                folder.mkdir()
                (folder / "stdout.log").write_bytes(evidence)
                retained.append(archive_incomplete(folder, "proposal.json"))
                self.assertFalse(folder.exists())
            self.assertNotEqual(*retained)
            self.assertEqual([ (p / "stdout.log").read_bytes() for p in retained],
                             [b'first failure', b'second failure'])

    def test_fresh_preparation_needs_no_archive(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "local-test-preparation"
            self.assertIsNone(archive_incomplete(folder, "prepared.json"))
            self.assertFalse(folder.exists())
