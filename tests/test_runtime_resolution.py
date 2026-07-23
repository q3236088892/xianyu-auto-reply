import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "portable_manager.py"


def load_manager():
    spec = importlib.util.spec_from_file_location("portable_manager_runtime", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RuntimeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_manager()

    def test_resolves_only_runtime_binaries_under_root(self):
        expected = {
            "python": ROOT / "runtime" / "python" / "python.exe",
            "node": ROOT / "runtime" / "node" / "node.exe",
            "git": ROOT / "runtime" / "git" / "cmd" / "git.exe",
        }
        resolved = self.manager.runtime_paths(ROOT)
        self.assertEqual(expected, resolved)

    def test_resolves_dependency_runtime_binaries_under_root(self):
        resolved = self.manager.dependency_runtime_paths(ROOT)
        self.assertEqual(ROOT / "runtime" / "mysql" / "bin" / "mariadbd.exe", resolved["mysql_server"])
        self.assertEqual(ROOT / "runtime" / "mysql" / "bin" / "mariadb-install-db.exe", resolved["mysql_install_db"])
        self.assertEqual(ROOT / "runtime" / "mysql" / "bin" / "mariadb.exe", resolved["mysql_client"])
        self.assertEqual(ROOT / "runtime" / "redis" / "redis-server.exe", resolved["redis_server"])
        self.assertEqual(ROOT / "runtime" / "redis" / "redis-cli.exe", resolved["redis_cli"])

    def test_portable_environment_precedes_empty_system_path(self):
        env = self.manager.portable_environment(ROOT, base_env={"PATH": ""})
        entries = env["PATH"].split(os.pathsep)
        self.assertEqual(str(ROOT / "runtime" / "python"), entries[0])
        self.assertEqual(str(ROOT / "runtime" / "node"), entries[2])
        self.assertEqual(str(ROOT / "runtime" / "git" / "cmd"), entries[3])
        self.assertEqual(str(ROOT / "runtime" / "mysql" / "bin"), entries[4])
        self.assertEqual(str(ROOT / "runtime" / "redis"), entries[5])
        self.assertEqual(str(ROOT / "runtime" / "python"), env["PYTHONHOME"])
        self.assertEqual(str(ROOT / "runtime" / "playwright"), env["PLAYWRIGHT_BROWSERS_PATH"])

    def test_missing_runtime_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(self.manager.RuntimeResolutionError):
                self.manager.validate_runtime(Path(temp))


    def test_release_bootstrap_exists_for_embedded_python(self):
        bootstrap = ROOT / "scripts" / "run_release.py"
        self.assertTrue(bootstrap.is_file())
        text = bootstrap.read_text(encoding="utf-8")
        self.assertIn("sys.path.insert", text)
        self.assertIn("runpy.run_path", text)


if __name__ == "__main__":
    unittest.main()
