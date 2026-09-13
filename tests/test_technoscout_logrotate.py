import gzip
from pathlib import Path
import tempfile
import unittest

from technoscout_logrotate import rotate_logs


class TechnoScoutLogRotateTests(unittest.TestCase):
    def test_below_threshold_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            path = log_dir / "technoscout.log"
            path.write_bytes(b"abc")

            results = rotate_logs(log_dir, max_bytes=10, keep=3)

            self.assertEqual(path.read_bytes(), b"abc")
            self.assertFalse((log_dir / "technoscout.log.1.gz").exists())
            self.assertEqual(results[0]["state"], "SKIPPED")

    def test_rotates_compresses_and_preserves_active_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            path = log_dir / "technoscout.log"
            payload = (b"hello world\n" * 100)
            path.write_bytes(payload)
            inode_before = path.stat().st_ino

            results = rotate_logs(log_dir, max_bytes=100, keep=3)

            self.assertEqual(results[0]["state"], "ROTATED")
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual(path.stat().st_ino, inode_before)
            with gzip.open(log_dir / "technoscout.log.1.gz", "rb") as fh:
                self.assertEqual(fh.read(), payload)

    def test_keeps_only_requested_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            path = log_dir / "job-shadow.log"

            for index in range(1, 5):
                path.write_bytes((f"generation-{index}\n" * 50).encode())
                rotate_logs(log_dir, max_bytes=10, keep=2)

            self.assertTrue((log_dir / "job-shadow.log.1.gz").exists())
            self.assertTrue((log_dir / "job-shadow.log.2.gz").exists())
            self.assertFalse((log_dir / "job-shadow.log.3.gz").exists())

    def test_dry_run_does_not_modify_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            path = log_dir / "technoscout.err.log"
            payload = b"x" * 100
            path.write_bytes(payload)

            results = rotate_logs(log_dir, max_bytes=10, keep=3, dry_run=True)

            self.assertEqual(results[0]["state"], "WOULD_ROTATE")
            self.assertEqual(path.read_bytes(), payload)
            self.assertFalse((log_dir / "technoscout.err.log.1.gz").exists())

    def test_own_logrotate_logs_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            own = log_dir / "logrotate.log"
            own.write_bytes(b"x" * 100)
            target = log_dir / "technoscout.log"
            target.write_bytes(b"x" * 100)

            rotate_logs(log_dir, max_bytes=10, keep=3)

            self.assertEqual(own.stat().st_size, 100)
            self.assertFalse((log_dir / "logrotate.log.1.gz").exists())
            self.assertTrue((log_dir / "technoscout.log.1.gz").exists())


if __name__ == "__main__":
    unittest.main()
