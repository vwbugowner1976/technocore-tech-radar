import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock

from technoscout.llm_backend import ManagedMLXBackend
from technoscout.mlx_process_lock import InterprocessFileLock


REPO_ROOT = Path(__file__).resolve().parent.parent


class MLXProcessLockTests(unittest.TestCase):
    def test_file_lock_blocks_a_second_process_until_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mlx.lock"
            first = InterprocessFileLock(path, timeout_seconds=1.0, poll_seconds=0.01)
            first.acquire()
            try:
                code = textwrap.dedent(
                    """
                    import sys
                    from technoscout.mlx_process_lock import InterprocessFileLock

                    try:
                        lock = InterprocessFileLock(sys.argv[1], timeout_seconds=0.12, poll_seconds=0.01)
                        lock.acquire()
                    except TimeoutError:
                        print("TIMEOUT")
                    else:
                        print("ACQUIRED")
                        lock.release()
                    """
                )
                result = subprocess.run(
                    [sys.executable, "-c", code, str(path)],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), "TIMEOUT")
            finally:
                first.release()

            result = subprocess.run(
                [sys.executable, "-c", code, str(path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "ACQUIRED")

    def test_backend_does_not_enter_chat_while_process_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "mlx.lock"
            holder = InterprocessFileLock(lock_path, timeout_seconds=1.0, poll_seconds=0.01)
            holder.acquire()
            backend = ManagedMLXBackend(
                {
                    "mlx_worker_log": str(root / "worker.log"),
                    "mlx_process_lock_file": str(lock_path),
                    "mlx_process_lock_timeout_seconds": 0.08,
                    "mlx_process_lock_poll_seconds": 0.01,
                }
            )
            fake_chat = Mock(return_value="ok")
            backend._chat_locked = fake_chat
            try:
                with self.assertRaises(TimeoutError):
                    backend.chat("model", [], 1, 0.0, 1.0)
                fake_chat.assert_not_called()
            finally:
                backend.close()
                holder.release()

    def test_backend_enters_chat_after_acquiring_process_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ManagedMLXBackend(
                {
                    "mlx_worker_log": str(root / "worker.log"),
                    "mlx_process_lock_file": str(root / "mlx.lock"),
                    "mlx_process_lock_timeout_seconds": 0.2,
                    "mlx_process_lock_poll_seconds": 0.01,
                }
            )
            fake_chat = Mock(return_value="ok")
            backend._chat_locked = fake_chat
            try:
                result = backend.chat("model", [{"role": "user", "content": "x"}], 4, 0.0, 1.0)
                self.assertEqual(result, "ok")
                fake_chat.assert_called_once()
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
