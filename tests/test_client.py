"""Protocol failure tests use memory pipes, never Polkit or hardware."""
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from thermaldeck.client import WorkerClient


class ClientTests(unittest.TestCase):
    def client(self, reply=None, error=None):
        client = WorkerClient()
        client.process = SimpleNamespace(stdin=io.BytesIO())
        client._read_reply = Mock(return_value=reply, side_effect=error)
        return client

    def test_timeout_closes_pipe_and_never_consumes_late_response(self):
        client = self.client(error=RuntimeError("timeout"))
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            client._exchange({"action": "set"})
        self.assertTrue(client.process.stdin.closed)
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            client._exchange({"action": "heartbeat"})
        self.assertEqual(client._read_reply.call_count, 1)

    def test_rejected_command_keeps_protocol_usable(self):
        client = self.client(reply={"ok": False, "error": "invalid speed"})
        with self.assertRaisesRegex(RuntimeError, "invalid speed"):
            client._exchange({"action": "set"})
        self.assertFalse(client.process.stdin.closed)
        self.assertIsNone(client.last_error)
        client._read_reply.return_value = {"ok": True, "owned": {}}
        self.assertTrue(client._exchange({"action": "status"})["ok"])

    def test_malformed_reply_closes_pipe(self):
        for reply in [[], None, {"ok": "true"}, {}]:
            with self.subTest(reply=reply):
                client = self.client(reply=reply)
                with self.assertRaises(RuntimeError):
                    client._exchange({"action": "status"})
                self.assertTrue(client.process.stdin.closed)


if __name__ == "__main__":
    unittest.main()
