import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "portable_manager.py"


def load_manager(path=MODULE_PATH):
    spec = importlib.util.spec_from_file_location("portable_manager", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PortableRootTests(unittest.TestCase):
    def test_root_is_derived_from_script_location_not_cwd(self):
        manager = load_manager()
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as other:
            try:
                import os
                os.chdir(other)
                self.assertEqual(ROOT, manager.portable_root())
            finally:
                os.chdir(old_cwd)

    def test_root_works_when_directory_contains_spaces(self):
        with tempfile.TemporaryDirectory(prefix="portable root with spaces ") as temp:
            copied_root = Path(temp) / "Xianyu Portable Green"
            (copied_root / "scripts").mkdir(parents=True)
            copied_script = copied_root / "scripts" / "portable_manager.py"
            shutil.copy2(MODULE_PATH, copied_script)
            manager = load_manager(copied_script)
            self.assertEqual(copied_root.resolve(), manager.portable_root())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_manager()
        self.valid = json.loads((ROOT / "config" / "portable.json").read_text(encoding="utf-8"))

    def write_config(self, root, data):
        config_dir = root / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "portable.json").write_text(json.dumps(data), encoding="utf-8")

    def test_loads_expected_configuration(self):
        config = self.manager.load_config(ROOT)
        self.assertEqual(2, config["schema_version"])
        self.assertEqual("main", config["branch"])
        self.assertEqual("https://github.com/zhinianboke/xianyu-auto-reply.git", config["upstream_url"])
        self.assertEqual(
            {
                "backend-web": 8089,
                "websocket": 8090,
                "scheduler": 8091,
                "frontend": 9000,
            },
            config["service_ports"],
        )
        self.assertNotIn("promotion", config["service_ports"])
        self.assertEqual("03:30", config["schedule_time"])
        self.assertEqual(2, config["release_retention"])
        self.assertEqual("XianyuAutoReply-Zhinianboke-Portable-Update", config["task_name"])

    def test_rejects_invalid_values(self):
        mutations = {
            "schedule_time": ["3:30", "24:00", "03:60"],
            "branch": ["", "main branch", "../main"],
            "release_retention": [0, 1, "2"],
            "maintenance_log_retention_days": [0, "7"],
            "upstream_url": [
                "http://github.com/zhinianboke/xianyu-auto-reply.git",
                "https://github.com/GuDong2003/xianyu-auto-reply-fix.git",
                "https://example.com/repo.git",
                "not-a-url",
            ],
        }
        for key, bad_values in mutations.items():
            for bad in bad_values:
                with self.subTest(key=key, bad=bad), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    candidate = dict(self.valid)
                    candidate[key] = bad
                    self.write_config(root, candidate)
                    with self.assertRaises(self.manager.ConfigError):
                        self.manager.load_config(root)

    def test_rejects_invalid_or_duplicate_service_ports(self):
        cases = [
            {"backend-web": 0, "websocket": 8090, "scheduler": 8091, "frontend": 9000},
            {"backend-web": 8089, "websocket": 8089, "scheduler": 8091, "frontend": 9000},
            {"backend-web": 8089, "websocket": "8090", "scheduler": 8091, "frontend": 9000},
            {"backend-web": 8089, "websocket": 8090, "scheduler": 8091},
            {"backend-web": 8089, "websocket": 8090, "scheduler": 8091, "frontend": 9000, "promotion": 8092},
        ]
        for ports in cases:
            with self.subTest(ports=ports), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                candidate = dict(self.valid)
                candidate["service_ports"] = ports
                self.write_config(root, candidate)
                with self.assertRaises(self.manager.ConfigError):
                    self.manager.load_config(root)

    def test_rejects_missing_required_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = dict(self.valid)
            del candidate["branch"]
            self.write_config(root, candidate)
            with self.assertRaises(self.manager.ConfigError):
                self.manager.load_config(root)

    def test_service_specs_cover_main_system_only(self):
        config = self.manager.load_config(ROOT)
        specs = self.manager.service_specs(config)
        self.assertEqual(["backend-web", "websocket", "scheduler", "frontend"], [spec.name for spec in specs])
        self.assertEqual(["backend-web", "websocket", "scheduler"], [spec.name for spec in specs if spec.kind == "python"])
        self.assertEqual([8089, 8090, 8091, 9000], [spec.port for spec in specs])

    def test_dependency_specs_use_configured_local_ports(self):
        config = self.manager.load_config(ROOT)
        specs = self.manager.dependency_service_specs(ROOT, config)
        self.assertEqual(["mysql", "redis"], [spec.name for spec in specs])
        self.assertEqual([3306, 6379], [spec.port for spec in specs])
        self.assertTrue(str(specs[0].data_dir).endswith("state\\mysql-data"))
        self.assertTrue(str(specs[1].data_dir).endswith("state\\redis-data"))


class EnvGenerationTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_manager()
        self.config = json.loads((ROOT / "config" / "portable.json").read_text(encoding="utf-8"))

    def test_writes_service_env_files_from_external_mysql_and_redis_config(self):
        self.config["mysql"] = {
            "host": "db.internal",
            "port": 3307,
            "user": "xy_user",
            "password": "xy_password",
            "database": "xy_data",
        }
        self.config["redis"] = {
            "host": "redis.internal",
            "port": 6380,
            "password": "redis_password",
            "db": 2,
        }
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            for service in ("backend-web", "websocket", "scheduler", "promotion"):
                (source / service).mkdir(parents=True)

            self.manager.write_service_env_files(source, self.manager.validate_config(self.config))

            backend = (source / "backend-web" / ".env").read_text(encoding="utf-8")
            websocket = (source / "websocket" / ".env").read_text(encoding="utf-8")
            scheduler = (source / "scheduler" / ".env").read_text(encoding="utf-8")

            self.assertIn("MYSQL_HOST=db.internal", backend)
            self.assertIn("MYSQL_PORT=3307", backend)
            self.assertIn("MYSQL_USER=xy_user", backend)
            self.assertIn("MYSQL_PASSWORD=xy_password", backend)
            self.assertIn("MYSQL_DATABASE=xy_data", backend)
            self.assertIn("REDIS_HOST=redis.internal", backend)
            self.assertIn("REDIS_PORT=6380", backend)
            self.assertIn("REDIS_PASSWORD=redis_password", backend)
            self.assertIn("REDIS_DB=2", backend)
            self.assertIn("HOST=127.0.0.1", backend)
            self.assertIn("BACKEND_WEB_PORT=8089", backend)
            self.assertIn("WEBSOCKET_SERVICE_URL=http://127.0.0.1:8090", backend)
            self.assertIn("SCHEDULER_SERVICE_URL=http://127.0.0.1:8091", backend)
            self.assertIn("STATIC_DIR=", backend)
            self.assertIn("WEBSOCKET_PORT=8090", websocket)
            self.assertIn("BACKEND_WEB_SERVICE_URL=http://127.0.0.1:8089", websocket)
            self.assertIn("SCHEDULER_PORT=8091", scheduler)
            self.assertIn("WEBSOCKET_SERVICE_URL=http://127.0.0.1:8090", scheduler)
            self.assertFalse((source / "promotion" / ".env").exists())


class DependencyConfigTests(unittest.TestCase):
    def test_redis_config_uses_install_dir_relative_paths(self):
        manager = load_manager()
        config = manager.load_config(ROOT)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = [item for item in manager.dependency_service_specs(root, config) if item.name == "redis"][0]
            spec.install_dir.mkdir(parents=True)
            spec.data_dir.mkdir(parents=True)
            spec.log_dir.mkdir(parents=True)

            config_path = manager._write_redis_config(spec)
            text = config_path.read_text(encoding="utf-8")

            self.assertEqual(spec.install_dir / "portable-redis.conf", config_path)
            self.assertIn("dir ../../state/redis-data", text)
            self.assertIn("logfile ../../logs/deps/redis/redis.log", text)
            self.assertNotIn("C:", text)


class ProcessStateTests(unittest.TestCase):
    def test_tracks_all_service_processes_in_one_state_file(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "releases" / ("a" * 40) / "app"
            state_path = manager.write_process_state(
                root,
                commit="a" * 40,
                source_dir=source,
                services={
                    "backend-web": {"pid": 1001, "port": 8089, "command_line": "python main.py"},
                    "websocket": {"pid": 1002, "port": 8090, "command_line": "python main.py"},
                    "scheduler": {"pid": 1003, "port": 8091, "command_line": "python main.py"},
                    "frontend": {"pid": 1004, "port": 9000, "command_line": "portable_manager.py serve-frontend"},
                },
            )

            self.assertTrue(state_path.is_file())
            state = manager.read_process_state(root)
            self.assertEqual(2, state["schema_version"])
            self.assertEqual("a" * 40, state["commit"])
            self.assertEqual(str(source), state["source_dir"])
            self.assertEqual(["backend-web", "websocket", "scheduler", "frontend"], list(state["services"]))
            self.assertEqual(9000, state["services"]["frontend"]["port"])


class ServiceLaunchTests(unittest.TestCase):
    def test_python_services_launch_through_release_bootstrap(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "app"
            deps = root / "deps"
            service_dir = source / "backend-web"
            service_dir.mkdir(parents=True)
            deps.mkdir()
            (service_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "run_release.py").write_text("", encoding="utf-8")

            captured = {}

            class FakeProcess:
                pid = 1234

                def poll(self):
                    return None

            original_popen = manager.subprocess.Popen
            original_runtime_paths = manager.runtime_paths
            original_port_available = manager._port_available
            original_sleep = manager.time.sleep
            try:
                manager.runtime_paths = lambda base: {"python": root / "runtime" / "python" / "python.exe"}
                manager._port_available = lambda host, port: True
                manager.time.sleep = lambda seconds: None

                def fake_popen(command, **kwargs):
                    captured["command"] = command
                    captured["cwd"] = kwargs.get("cwd")
                    return FakeProcess()

                manager.subprocess.Popen = fake_popen
                manager._launch_python_service(
                    root,
                    source,
                    deps,
                    manager.ServiceSpec("backend-web", "python", 8089, "backend-web", "main.py"),
                )
            finally:
                manager.subprocess.Popen = original_popen
                manager.runtime_paths = original_runtime_paths
                manager._port_available = original_port_available
                manager.time.sleep = original_sleep

            self.assertEqual(str(root / "scripts" / "run_release.py"), captured["command"][1])
            self.assertEqual(str(source), captured["command"][2])
            self.assertEqual(str(deps), captured["command"][3])
            self.assertEqual("backend-web/main.py", captured["command"][4])
            self.assertEqual(source, captured["cwd"])


class OldReferenceTests(unittest.TestCase):
    def test_current_deployment_files_do_not_reference_old_upstream(self):
        checked = [
            ROOT / "config" / "portable.json",
            ROOT / "README-绿色版.md",
            ROOT / "scripts" / "portable_manager.py",
            ROOT / "scripts" / "install-update-task.ps1",
            ROOT / "scripts" / "remove-update-task.ps1",
        ]
        forbidden = ("GuDong2003/xianyu-auto-reply-fix", "xianyu-auto-reply-fix")
        for path in checked:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8", errors="replace")
                for value in forbidden:
                    self.assertNotIn(value, text)


class MaintenanceLogTests(unittest.TestCase):
    def test_writes_structured_json_line(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = manager.write_maintenance_event(
                root,
                command="verify",
                stage="config",
                result="success",
                old_commit=None,
                new_commit="abc123",
                run_id="run-test",
            )
            payload = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual("run-test", payload["run_id"])
            self.assertEqual("verify", payload["command"])
            self.assertEqual("config", payload["stage"])
            self.assertEqual("success", payload["result"])
            self.assertIn("timestamp", payload)


if __name__ == "__main__":
    unittest.main()
