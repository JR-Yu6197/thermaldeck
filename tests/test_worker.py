"""Worker protocol and shutdown tests with real local pipes, fake hardware."""
from contextlib import ExitStack, contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from thermaldeck import worker


class WorkerTests(unittest.TestCase):
    @contextmanager
    def environment(self, payload=b"", clock=None):
        # A short payload fits in the real pipe before the reader starts.
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            lock_path = Path(directory) / "session.lock"
            folder = MagicMock()
            folder.is_symlink.return_value = False
            folder.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o40700)
            folder.__truediv__.return_value = lock_path
            read_fd, write_fd = os.pipe()
            try:
                if payload:
                    os.write(write_fd, payload)
            finally:
                os.close(write_fd)
            stdin = stack.enter_context(os.fdopen(read_fd, "rb", buffering=0))
            stdout = io.StringIO()
            backend, session = MagicMock(), MagicMock()
            opened = []
            real_open = os.open

            def open_lock(path, flags, mode=0o777):
                self.assertEqual(path, lock_path)
                fd = real_open(path, flags, mode)
                opened.append(fd)
                return fd

            stack.enter_context(patch.object(worker, "Path", return_value=folder))
            stack.enter_context(patch.object(worker.os, "geteuid", return_value=0))
            stack.enter_context(patch.object(worker.os, "open", side_effect=open_lock))
            stack.enter_context(patch.object(worker.signal, "signal"))
            stack.enter_context(patch.object(worker, "Backend", return_value=backend))
            stack.enter_context(patch.object(worker, "ControlSession", return_value=session))
            stack.enter_context(patch.object(worker.sys, "stdin", stdin))
            stack.enter_context(patch.object(worker.sys, "stdout", stdout))
            stack.enter_context(patch.object(worker.time, "sleep"))
            stack.enter_context(patch.object(worker.time, "monotonic", **(
                {"side_effect": clock} if clock is not None else {"return_value": 0})))
            yield SimpleNamespace(backend=backend, session=session, stdout=stdout, opened=opened)

    def assert_ready_and_closed(self, env):
        self.assertEqual(json.loads(env.stdout.getvalue().splitlines()[0]), {"ready": True})
        self.assertEqual(len(env.opened), 1)
        with self.assertRaises(OSError):
            os.fstat(env.opened[0])

    def test_eof_restores_owned_controls_and_closes_backend(self):
        with self.environment() as env:
            worker.run_worker()
            env.session.restore_all.assert_called_once_with()
            env.backend.close.assert_called_once_with()
            env.session.request.assert_not_called()
            self.assert_ready_and_closed(env)

    def test_heartbeat_expiry_restores_without_reading_a_request(self):
        with self.environment(clock=[0, 13]) as env:
            worker.run_worker()
            env.session.restore_all.assert_called_once_with()
            env.backend.close.assert_called_once_with()
            env.session.request.assert_not_called()
            self.assert_ready_and_closed(env)

    def test_oversized_unterminated_request_recovers_before_raising(self):
        # Lower only the bound, avoiding a blocking prefill of a real OS pipe.
        with self.environment(payload=b"x" * 33) as env, patch.object(worker, "MAX_REQUEST", 32):
            with self.assertRaisesRegex(ValueError, "요청 크기"):
                worker.run_worker()
            env.session.request.assert_not_called()
            env.session.restore_all.assert_called_once_with()
            env.backend.close.assert_called_once_with()
            self.assert_ready_and_closed(env)

    def test_restore_retries_and_close_failure_preserve_both_errors(self):
        with self.environment() as env:
            env.session.restore_all.side_effect = RuntimeError("restore failed")
            env.backend.close.side_effect = RuntimeError("NVML close failed")
            with self.assertRaises(RuntimeError) as caught:
                worker.run_worker()
            self.assertIn("restore failed", str(caught.exception))
            self.assertIn("NVML close failed", str(caught.exception))
            self.assertEqual(env.session.restore_all.call_count, 3)
            env.backend.close.assert_called_once_with()
            self.assert_ready_and_closed(env)


if __name__ == "__main__":
    unittest.main()
