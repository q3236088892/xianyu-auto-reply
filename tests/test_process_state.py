import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_manager():
    spec = importlib.util.spec_from_file_location("portable_manager_process", ROOT / "scripts" / "portable_manager.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProcessStateTests(unittest.TestCase):
    def setUp(self):
        self.m = load_manager()

    def test_active_release_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path=self.m.set_active_release(root, "abc1234")
            self.assertEqual("abc1234", self.m.get_active_release(root))
            self.assertEqual("abc1234", json.loads(path.read_text())["commit"])

    def test_stale_pid_is_not_reported_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.m.write_process_state(root, pid=999999, commit="abc1234", port=8090, command_line="not-owned")
            state=self.m.read_process_state(root)
            self.assertFalse(self.m.is_process_running(state))

    def test_update_lock_rejects_second_lock_and_can_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            lock=self.m.acquire_update_lock(root)
            self.assertTrue(lock.exists())
            with self.assertRaises(self.m.UpdateInProgressError):
                self.m.acquire_update_lock(root)
            self.m.release_update_lock(root)
            self.assertFalse(lock.exists())

    def test_wait_health_retries_until_ready(self):
        calls=[]
        def fake(url, timeout):
            calls.append(url)
            return (len(calls) >= 3, "ok" if len(calls) >= 3 else "not ready")
        ok, body = self.m.wait_health("http://127.0.0.1:1/health", 1, checker=fake, interval=0)
        self.assertTrue(ok)
        self.assertEqual("ok", body)
        self.assertEqual(3, len(calls))

    def test_dependency_state_round_trip_and_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.m.write_dependency_state(
                root,
                dependencies={
                    "mysql": {"pid": 111, "port": 3306, "command_line": "mariadbd.exe"},
                    "redis": {"pid": 222, "port": 6379, "command_line": "redis-server.exe"},
                },
            )
            state = self.m.read_dependency_state(root)
            self.assertEqual(1, state["schema_version"])
            self.assertEqual(3306, state["dependencies"]["mysql"]["port"])

            killed = []
            alive = {111, 222}
            original_exists = self.m._pid_exists
            original_kill = self.m._kill_pid
            try:
                self.m._pid_exists = lambda pid: pid in alive
                def fake_kill(pid):
                    killed.append(pid)
                    alive.discard(pid)
                self.m._kill_pid = fake_kill
                self.assertTrue(self.m.stop_dependencies(root))
            finally:
                self.m._pid_exists = original_exists
                self.m._kill_pid = original_kill

            self.assertEqual([111, 222], killed)
            self.assertIsNone(self.m.read_dependency_state(root))


if __name__ == "__main__": unittest.main()

