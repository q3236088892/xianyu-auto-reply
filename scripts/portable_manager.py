#!/usr/bin/env python3
"""Portable deployment manager for zhinianboke/xianyu-auto-reply."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence


# 允许的上游仓库：原作者仓库 zhinianboke，以及本部署所用的个人 fork q3236088892。
# 两者都放行，便于既能从个人 fork 拉取（含本地修复），也保留从原上游同步的可能。
APPROVED_UPSTREAM = re.compile(
    r"https://github\.com/(?:zhinianboke|q3236088892)/xianyu-auto-reply(?:\.git)?$"
)


def _upstream_owner_repo(upstream_url: str) -> str:
    """从 upstream_url 解析出 GitHub 的 ``owner/repo``。

    用于 GitHub API 兜底地址（commits / zipball）。此前这些地址硬编码为原作者
    仓库 zhinianboke，会导致更新时始终从原上游拉取而非配置的个人 fork。
    改为从配置动态解析，保证更新目标与 upstream_url 一致。
    """
    match = re.match(
        r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?$",
        upstream_url,
    )
    if not match:
        raise ValueError(f"cannot parse owner/repo from upstream_url: {upstream_url}")
    return match.group(1)


MAIN_SERVICE_ORDER = ("backend-web", "websocket", "scheduler", "promotion", "frontend")
PYTHON_SERVICE_ORDER = ("backend-web", "websocket", "scheduler")
DEPENDENCY_SERVICE_ORDER = ("mysql", "redis")
MARIADB_VERSION = "10.11.18"
MARIADB_DOWNLOAD_URL = (
    f"https://archive.mariadb.org/mariadb-{MARIADB_VERSION}/winx64-packages/"
    f"mariadb-{MARIADB_VERSION}-winx64.zip"
)
REDIS_VERSION = "8.8.0"
REDIS_DOWNLOAD_URL = (
    f"https://github.com/redis-windows/redis-windows/releases/download/{REDIS_VERSION}/"
    f"Redis-{REDIS_VERSION}-Windows-x64-msys2.zip"
)


class ConfigError(ValueError):
    """Raised when portable.json violates the deployment contract."""


class RuntimeResolutionError(RuntimeError):
    """Raised when a required portable runtime executable is absent."""


class UpdateInProgressError(RuntimeError):
    """Raised when an update lock already exists."""


class ServiceSpec(NamedTuple):
    name: str
    kind: str
    port: int
    directory: str
    entry: str | None = None
    health_path: str = "/health"


class DependencySpec(NamedTuple):
    name: str
    port: int
    install_dir: Path
    data_dir: Path
    log_dir: Path
    download_url: str
    server_executable: Path
    client_executables: tuple[Path, ...]
    init_executables: tuple[Path, ...]
    settings: Mapping[str, Any]


def portable_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _json_path(root: Path | str, relative: str) -> Path:
    path = Path(root).resolve() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _require_int(config: Mapping[str, Any], key: str, minimum: int, maximum: int | None = None) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{key} is outside the allowed range")
    return value


def _require_str(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ConfigError(f"{key} must not contain line breaks")
    return value.strip()


def _require_mapping(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a mapping")
    return dict(value)


def _validate_port_map(
    value: Mapping[str, Any],
    *,
    key: str,
    required_names: Sequence[str],
) -> dict[str, int]:
    names = set(value)
    expected = set(required_names)
    if names != expected:
        raise ConfigError(f"{key} must define exactly: {', '.join(required_names)}")
    ports: dict[str, int] = {}
    seen: set[int] = set()
    for name in required_names:
        port = value.get(name)
        if isinstance(port, bool) or not isinstance(port, int):
            raise ConfigError(f"{key}.{name} must be an integer")
        if port < 1 or port > 65535:
            raise ConfigError(f"{key}.{name} is outside the allowed range")
        if port in seen:
            raise ConfigError(f"{key} contains duplicate ports")
        seen.add(port)
        ports[name] = port
    return ports


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "upstream_url",
        "branch",
        "service_host",
        "service_ports",
        "candidate_service_ports",
        "mysql",
        "redis",
        "schedule_time",
        "auto_apply",
        "release_retention",
        "maintenance_log_retention_days",
        "health_timeout_seconds",
        "task_name",
    }
    missing = sorted(required.difference(config))
    if missing:
        raise ConfigError("missing required keys: " + ", ".join(missing))

    if config["schema_version"] != 2:
        raise ConfigError("unsupported schema_version")

    upstream = _require_str(config, "upstream_url")
    if not APPROVED_UPSTREAM.fullmatch(upstream):
        raise ConfigError("upstream_url must point to zhinianboke/xianyu-auto-reply")

    branch = _require_str(config, "branch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise ConfigError("branch contains unsafe characters")

    if _require_str(config, "service_host") != "127.0.0.1":
        raise ConfigError("service_host must be 127.0.0.1")

    ports = _validate_port_map(config["service_ports"], key="service_ports", required_names=MAIN_SERVICE_ORDER)
    candidate_ports = _validate_port_map(
        config["candidate_service_ports"],
        key="candidate_service_ports",
        required_names=MAIN_SERVICE_ORDER,
    )
    if set(ports.values()) & set(candidate_ports.values()):
        raise ConfigError("candidate_service_ports must not overlap service_ports")

    mysql = _require_mapping(config, "mysql")
    for key in ("host", "user", "password", "database"):
        _require_str(mysql, key)
    _require_int(mysql, "port", 1, 65535)

    redis = _require_mapping(config, "redis")
    for key in ("host", "password"):
        if not isinstance(redis.get(key), str):
            raise ConfigError(f"redis.{key} must be a string")
        if "\n" in redis[key] or "\r" in redis[key]:
            raise ConfigError(f"redis.{key} must not contain line breaks")
    _require_int(redis, "port", 1, 65535)
    _require_int(redis, "db", 0, 65535)

    schedule = _require_str(config, "schedule_time")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule):
        raise ConfigError("schedule_time must be HH:MM")

    if not isinstance(config["auto_apply"], bool):
        raise ConfigError("auto_apply must be boolean")
    _require_int(config, "release_retention", 2, 50)
    _require_int(config, "maintenance_log_retention_days", 1, 3650)
    _require_int(config, "health_timeout_seconds", 5, 3600)
    _require_str(config, "task_name")

    normalized = dict(config)
    normalized["upstream_url"] = upstream
    normalized["branch"] = branch
    normalized["service_host"] = "127.0.0.1"
    normalized["service_ports"] = ports
    normalized["candidate_service_ports"] = candidate_ports
    normalized["mysql"] = dict(mysql)
    normalized["redis"] = dict(redis)
    normalized["schedule_time"] = schedule
    return normalized


def load_config(root: Path | str | None = None) -> dict[str, Any]:
    base = Path(root).resolve() if root is not None else portable_root()
    path = base / "config" / "portable.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read valid configuration: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("portable.json must contain a JSON object")
    return validate_config(payload)


def service_specs(config: Mapping[str, Any], *, candidate: bool = False) -> list[ServiceSpec]:
    ports = config["candidate_service_ports"] if candidate else config["service_ports"]
    return [
        ServiceSpec("backend-web", "python", ports["backend-web"], "backend-web", "main.py"),
        ServiceSpec("websocket", "python", ports["websocket"], "websocket", "main.py"),
        ServiceSpec("scheduler", "python", ports["scheduler"], "scheduler", "main.py"),
        ServiceSpec("promotion", "python", ports["promotion"], "promotion/backend", "main.py"),
        ServiceSpec("frontend", "frontend", ports["frontend"], "frontend", None, "/"),
    ]


def write_maintenance_event(
    root: Path | str,
    *,
    command: str,
    stage: str,
    result: str,
    old_commit: str | None = None,
    new_commit: str | None = None,
    run_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> Path:
    base = Path(root).resolve()
    now = datetime.now(timezone.utc)
    log_dir = base / "logs" / "maintenance"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"maintenance-{now.astimezone().strftime('%Y-%m-%d')}.jsonl"
    event = {
        "timestamp": now.isoformat(),
        "run_id": run_id or uuid.uuid4().hex,
        "command": command,
        "old_commit": old_commit,
        "new_commit": new_commit,
        "stage": stage,
        "result": result,
    }
    if details:
        event["details"] = dict(details)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return log_path


def runtime_paths(root: Path | str | None = None) -> dict[str, Path]:
    base = Path(root).resolve() if root is not None else portable_root()
    return {
        "python": base / "runtime" / "python" / "python.exe",
        "node": base / "runtime" / "node" / "node.exe",
        "git": base / "runtime" / "git" / "cmd" / "git.exe",
    }


def dependency_runtime_paths(root: Path | str | None = None) -> dict[str, Path]:
    base = Path(root).resolve() if root is not None else portable_root()
    mysql_bin = base / "runtime" / "mysql" / "bin"
    redis_dir = base / "runtime" / "redis"
    return {
        "mysql_server": mysql_bin / "mariadbd.exe",
        "mysql_server_legacy": mysql_bin / "mysqld.exe",
        "mysql_install_db": mysql_bin / "mariadb-install-db.exe",
        "mysql_install_db_legacy": mysql_bin / "mysql_install_db.exe",
        "mysql_client": mysql_bin / "mariadb.exe",
        "mysql_client_legacy": mysql_bin / "mysql.exe",
        "mysql_admin": mysql_bin / "mariadb-admin.exe",
        "mysql_admin_legacy": mysql_bin / "mysqladmin.exe",
        "redis_server": redis_dir / "redis-server.exe",
        "redis_cli": redis_dir / "redis-cli.exe",
    }


def dependency_service_specs(root: Path | str, config: Mapping[str, Any]) -> list[DependencySpec]:
    base = Path(root).resolve()
    paths = dependency_runtime_paths(base)
    mysql = config["mysql"]
    redis = config["redis"]
    return [
        DependencySpec(
            "mysql",
            int(mysql["port"]),
            base / "runtime" / "mysql",
            base / "state" / "mysql-data",
            base / "logs" / "deps" / "mysql",
            MARIADB_DOWNLOAD_URL,
            _first_existing_path((paths["mysql_server"], paths["mysql_server_legacy"])),
            (paths["mysql_client"], paths["mysql_client_legacy"], paths["mysql_admin"], paths["mysql_admin_legacy"]),
            (paths["mysql_install_db"], paths["mysql_install_db_legacy"]),
            dict(mysql),
        ),
        DependencySpec(
            "redis",
            int(redis["port"]),
            base / "runtime" / "redis",
            base / "state" / "redis-data",
            base / "logs" / "deps" / "redis",
            REDIS_DOWNLOAD_URL,
            paths["redis_server"],
            (paths["redis_cli"],),
            tuple(),
            dict(redis),
        ),
    ]


def portable_environment(
    root: Path | str | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
    app_dir: Path | str | None = None,
    deps_dir: Path | str | None = None,
) -> dict[str, str]:
    base = Path(root).resolve() if root is not None else portable_root()
    env = dict(os.environ if base_env is None else base_env)
    prefix = [
        base / "runtime" / "python",
        base / "runtime" / "python" / "Scripts",
        base / "runtime" / "node",
        base / "runtime" / "git" / "cmd",
        base / "runtime" / "mysql" / "bin",
        base / "runtime" / "redis",
    ]
    path_parts = [str(path) for path in prefix]
    existing_path = env.get("PATH", "")
    if existing_path:
        path_parts.append(existing_path)
    env["PATH"] = os.pathsep.join(path_parts)
    env["PYTHONHOME"] = str(base / "runtime" / "python")
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(base / "runtime" / "playwright")
    python_paths: list[str] = []
    if deps_dir is not None:
        python_paths.append(str(Path(deps_dir).resolve()))
    if app_dir is not None:
        python_paths.append(str(Path(app_dir).resolve()))
    if python_paths:
        existing = env.get("PYTHONPATH")
        if existing:
            python_paths.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return env


def validate_runtime(root: Path | str | None = None) -> dict[str, Path]:
    paths = runtime_paths(root)
    missing = [f"{name}: {path}" for name, path in paths.items() if not path.is_file()]
    if missing:
        raise RuntimeResolutionError("missing portable runtimes: " + "; ".join(missing))
    return paths


def set_active_release(root: Path | str, commit: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise ValueError("invalid commit")
    path = _json_path(root, "state/active-release.json")
    path.write_text(
        json.dumps({"commit": commit, "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def get_active_release(root: Path | str) -> str | None:
    path = Path(root).resolve() / "state" / "active-release.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        commit = value.get("commit")
        return commit if isinstance(commit, str) else None
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_service_map(
    services: Mapping[str, Mapping[str, Any]],
    *,
    commit: str,
    source_dir: Path | str | None,
    started_at: str | None,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, info in services.items():
        pid = info.get("pid")
        port = info.get("port")
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise ValueError(f"services.{name}.pid must be an integer")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError(f"services.{name}.port must be an integer")
        normalized[name] = {
            "pid": pid,
            "port": port,
            "command_line": str(info.get("command_line") or ""),
        }
        if "health_path" in info:
            normalized[name]["health_path"] = str(info["health_path"])
        if "kind" in info:
            normalized[name]["kind"] = str(info["kind"])
    return {
        "schema_version": 2,
        "commit": commit,
        "source_dir": str(Path(source_dir)) if source_dir is not None else None,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "services": normalized,
    }


def write_process_state(
    root: Path | str,
    *,
    commit: str,
    services: Mapping[str, Mapping[str, Any]] | None = None,
    source_dir: Path | str | None = None,
    pid: int | None = None,
    port: int | None = None,
    command_line: str | None = None,
    started_at: str | None = None,
) -> Path:
    path = _json_path(root, "state/process.json")
    if services is None:
        if pid is None or port is None:
            raise TypeError("pid and port are required when services are omitted")
        services = {
            "app": {
                "pid": pid,
                "port": port,
                "command_line": command_line or "",
                "kind": "legacy",
            }
        }
    payload = _normalize_service_map(services, commit=commit, source_dir=source_dir, started_at=started_at)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_process_state(root: Path | str) -> dict[str, Any] | None:
    path = Path(root).resolve() / "state" / "process.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_dependency_map(dependencies: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name in DEPENDENCY_SERVICE_ORDER:
        info = dependencies.get(name)
        if not isinstance(info, Mapping):
            continue
        pid = info.get("pid")
        port = info.get("port")
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise ValueError(f"dependencies.{name}.pid must be an integer")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError(f"dependencies.{name}.port must be an integer")
        normalized[name] = {
            "pid": pid,
            "port": port,
            "command_line": str(info.get("command_line") or ""),
        }
        if "data_dir" in info:
            normalized[name]["data_dir"] = str(info["data_dir"])
    return normalized


def write_dependency_state(
    root: Path | str,
    *,
    dependencies: Mapping[str, Mapping[str, Any]],
    started_at: str | None = None,
) -> Path:
    path = _json_path(root, "state/dependencies.json")
    payload = {
        "schema_version": 1,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "dependencies": _normalize_dependency_map(dependencies),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_dependency_state(root: Path | str) -> dict[str, Any] | None:
    path = Path(root).resolve() / "state" / "dependencies.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def is_dependency_process_running(state: Mapping[str, Any] | None) -> bool:
    if not state:
        return False
    dependencies = state.get("dependencies")
    if not isinstance(dependencies, Mapping):
        return False
    pids = [info.get("pid") for info in dependencies.values() if isinstance(info, Mapping)]
    return bool(pids) and all(isinstance(pid, int) and _pid_exists(pid) for pid in pids)


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_process_running(state: Mapping[str, Any] | None) -> bool:
    if not state:
        return False
    services = state.get("services")
    if isinstance(services, Mapping):
        pids = [info.get("pid") for info in services.values() if isinstance(info, Mapping)]
        return bool(pids) and all(isinstance(pid, int) and _pid_exists(pid) for pid in pids)
    pid = state.get("pid")
    return isinstance(pid, int) and _pid_exists(pid)


def acquire_update_lock(root: Path | str) -> Path:
    path = _json_path(root, "state/update.lock")
    payload = {"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    except FileExistsError as exc:
        raise UpdateInProgressError(f"update lock exists: {path}") from exc
    return path


def release_update_lock(root: Path | str) -> None:
    path = Path(root).resolve() / "state" / "update.lock"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _http_check(url: str, timeout: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
            return response.status == 200, body
    except Exception as exc:
        return False, str(exc)


def wait_health(url: str, timeout: float, *, checker=None, interval: float = 0.5) -> tuple[bool, str]:
    check = checker or _http_check
    deadline = time.monotonic() + timeout
    last = "not checked"
    while time.monotonic() < deadline:
        ok, last = check(url, min(3.0, max(0.2, deadline - time.monotonic())))
        if ok:
            return True, last
        if interval:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return False, last


def _default_source_dir(root: Path) -> Path:
    return root


def _release_paths(root: Path, commit: str) -> tuple[Path, Path]:
    release = root / "releases" / commit
    return release / "app", release / "deps"


def _source_commit(root: Path) -> str:
    git = runtime_paths(root)["git"]
    try:
        result = subprocess.run(
            [str(git), "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        sha = result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except Exception:
        pass
    return "0" * 40


def _source_layout_ok(source: Path) -> bool:
    return all((source / folder / "main.py").is_file() for folder in PYTHON_SERVICE_ORDER)


def _dependency_dirs(root: Path) -> dict[str, Path]:
    base = Path(root).resolve()
    return {
        "mysql": base / "runtime" / "mysql",
        "mysql_bin": base / "runtime" / "mysql" / "bin",
        "redis": base / "runtime" / "redis",
    }


def _dependency_download_dir(root: Path) -> Path:
    return Path(root).resolve() / "cache" / "downloads"


def _dependency_stage_dir(root: Path) -> Path:
    return Path(root).resolve() / "staging" / "dependencies"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _download_file(url: str, path: Path, *, timeout: int = 300) -> Path:
    def action() -> Path:
        request = urllib.request.Request(url, headers={"User-Agent": "xianyu-portable-deps"})
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=timeout) as response, path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        return path

    return retry_call(action, attempts=3, delay=2)


def _extract_zip_archive(archive: Path, target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target_dir)


def _single_child_directory(root: Path) -> Path:
    children = [child for child in root.iterdir() if child.is_dir()]
    if len(children) != 1:
        raise RuntimeError(f"unexpected archive layout in {root}")
    return children[0]


def _move_dependency_payload(extract_root: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    child = _single_child_directory(extract_root)
    shutil.move(str(child), str(destination))


def _first_existing_path(paths: Sequence[Path]) -> Path:
    for path in paths:
        if path.is_file():
            return path
    if not paths:
        raise RuntimeError("no executable candidates were provided")
    return paths[0]


def _to_ini_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def _pid_for_port(port: int) -> int | None:
    if os.name != "nt":
        return None
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        return None
    pattern = re.compile(rf"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(result.stdout)
    return int(match.group(1)) if match else None


def _dependency_service_running(spec: DependencySpec) -> bool:
    return _tcp_reachable("127.0.0.1", spec.port)


def _init_mysql_data(spec: DependencySpec) -> None:
    if spec.data_dir.is_dir() and any(spec.data_dir.iterdir()):
        return
    spec.data_dir.mkdir(parents=True, exist_ok=True)
    init_exe = _first_existing_path(spec.init_executables)
    if not init_exe.is_file():
        raise RuntimeError(f"missing MySQL initialization executable: {init_exe}")
    password = str(spec.settings.get("password") or "")
    init_commands = [
        [
            str(init_exe),
            f"--datadir={spec.data_dir}",
            f"--password={password}",
            f"--port={spec.port}",
            "--allow-remote-root-access",
        ],
    ]
    last_error: Exception | None = None
    for command in init_commands:
        try:
            result = subprocess.run(
                command,
                cwd=spec.install_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                return
            raise RuntimeError((result.stdout or "") + (result.stderr or ""))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            shutil.rmtree(spec.data_dir, ignore_errors=True)
            spec.data_dir.mkdir(parents=True, exist_ok=True)
    raise RuntimeError(f"failed to initialize MySQL data directory: {last_error}")


def _write_mysql_config(spec: DependencySpec) -> Path:
    config_dir = spec.data_dir.parent / "mysql"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "my.ini"
    config_path.write_text(
        "\n".join(
            [
                "[mysqld]",
                f"basedir={_to_ini_path(spec.install_dir)}",
                f"datadir={_to_ini_path(spec.data_dir)}",
                f"port={spec.port}",
                "bind-address=127.0.0.1",
                "character-set-server=utf8mb4",
                "collation-server=utf8mb4_unicode_ci",
                "skip-name-resolve=1",
                f"log-error={_to_ini_path(spec.log_dir / 'mysql.log')}",
                f"pid-file={_to_ini_path(config_dir / 'mysql.pid')}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _write_redis_config(spec: DependencySpec) -> Path:
    spec.install_dir.mkdir(parents=True, exist_ok=True)
    config_path = spec.install_dir / "portable-redis.conf"
    data_dir = os.path.relpath(spec.data_dir, spec.install_dir).replace("\\", "/")
    log_file = os.path.relpath(spec.log_dir / "redis.log", spec.install_dir).replace("\\", "/")
    config_path.write_text(
        "\n".join(
            [
                "bind 127.0.0.1",
                f"port {spec.port}",
                f"dir {data_dir}",
                "protected-mode yes",
                "appendonly yes",
                f"logfile {log_file}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def ensure_dependency_runtimes(root: Path | str) -> dict[str, Path]:
    base = Path(root).resolve()
    _ensure_dir(base / "runtime")
    _ensure_dir(base / "logs" / "deps")
    download_dir = _dependency_download_dir(base)
    stage_dir = _dependency_stage_dir(base)
    dirs = _dependency_dirs(base)
    runtime_files = dependency_runtime_paths(base)

    downloads = {
        "mysql": (
            MARIADB_DOWNLOAD_URL,
            dirs["mysql"],
            (runtime_files["mysql_server"], runtime_files["mysql_server_legacy"]),
        ),
        "redis": (REDIS_DOWNLOAD_URL, dirs["redis"], (runtime_files["redis_server"],)),
    }
    for name, (url, install_dir, executables) in downloads.items():
        if any(executable.is_file() for executable in executables):
            continue
        archive = download_dir / f"{name}.zip"
        stage = stage_dir / name
        _download_file(url, archive)
        _extract_zip_archive(archive, stage)
        _move_dependency_payload(stage, install_dir)
    return dependency_runtime_paths(base)


def _mysql_client_command(spec: DependencySpec, password: str | None = None) -> list[str]:
    client = _first_existing_path(spec.client_executables)
    user = str(spec.settings.get("user") or "root")
    command = [str(client), "--protocol=tcp", "-h", "127.0.0.1", "-P", str(spec.port), f"-u{user}"]
    if password is not None:
        command.append(f"-p{password}")
    return command


def _redis_client_command(spec: DependencySpec, password: str | None = None) -> list[str]:
    client = _first_existing_path(spec.client_executables)
    command = [str(client), "-h", "127.0.0.1", "-p", str(spec.port)]
    if password:
        command.extend(["-a", password])
    return command


def _run_mysql_sql(spec: DependencySpec, sql: str) -> None:
    password = str(spec.settings.get("password") or "")
    attempts = [None]
    if password:
        attempts.append(password)
    errors: list[str] = []
    for candidate_password in attempts:
        command = _mysql_client_command(spec, candidate_password)
        command.extend(["-e", sql])
        result = subprocess.run(
            command,
            cwd=spec.install_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return
        errors.append((result.stdout or "") + (result.stderr or ""))
    raise RuntimeError("MySQL bootstrap SQL failed: " + " | ".join(error.strip() for error in errors if error.strip()))


def _bootstrap_mysql(spec: DependencySpec) -> subprocess.Popen:
    _init_mysql_data(spec)
    config_path = _write_mysql_config(spec)
    mysql_log = spec.log_dir / "mysql.log"
    mysql_log.parent.mkdir(parents=True, exist_ok=True)
    mysql_log.touch(exist_ok=True)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    log_handle = mysql_log.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(spec.server_executable), f"--defaults-file={config_path}"],
        cwd=spec.install_dir,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        if _tcp_reachable("127.0.0.1", spec.port, timeout=1.0):
            break
        if proc.poll() is not None:
            log_handle.close()
            raise RuntimeError("MySQL exited during bootstrap")
        time.sleep(0.5)
    else:
        log_handle.close()
        raise RuntimeError("MySQL did not become reachable during bootstrap")

    database = str(spec.settings.get("database") or "xianyu_auto_reply").replace("`", "``")
    user = str(spec.settings.get("user") or "root").replace("'", "''")
    password = str(spec.settings.get("password") or "").replace("'", "''")
    sql = (
        f"CREATE DATABASE IF NOT EXISTS `{database}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
        f"CREATE USER IF NOT EXISTS '{user}'@'127.0.0.1' IDENTIFIED BY '{password}'; "
        f"ALTER USER '{user}'@'127.0.0.1' IDENTIFIED BY '{password}'; "
        f"GRANT ALL PRIVILEGES ON *.* TO '{user}'@'127.0.0.1' WITH GRANT OPTION; "
        "FLUSH PRIVILEGES;"
    )
    _run_mysql_sql(spec, sql)
    log_handle.close()
    if proc.poll() is not None:
        raise RuntimeError("MySQL exited during bootstrap")
    return proc


def _bootstrap_redis(spec: DependencySpec) -> subprocess.Popen:
    spec.data_dir.mkdir(parents=True, exist_ok=True)
    config_path = _write_redis_config(spec)
    redis_log = spec.log_dir / "redis.log"
    redis_log.parent.mkdir(parents=True, exist_ok=True)
    redis_log.touch(exist_ok=True)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    log_handle = redis_log.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(spec.server_executable), config_path.name],
        cwd=spec.install_dir,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if _tcp_reachable("127.0.0.1", spec.port, timeout=1.0):
            break
        if proc.poll() is not None:
            log_handle.close()
            raise RuntimeError("Redis exited during bootstrap")
        time.sleep(0.5)
    else:
        log_handle.close()
        raise RuntimeError("Redis did not become reachable during bootstrap")
    log_handle.close()
    return proc


def ensure_dependency_processes(root: Path | str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    base = Path(root).resolve()
    cfg = dict(config or load_config(base))
    ensure_dependency_runtimes(base)
    specs = dependency_service_specs(base, cfg)
    state = read_dependency_state(base)
    if is_dependency_process_running(state):
        return state or {"dependencies": {}}
    if state:
        stop_dependencies(base)
    for spec in specs:
        spec.log_dir.mkdir(parents=True, exist_ok=True)
    mysql_spec = next(spec for spec in specs if spec.name == "mysql")
    redis_spec = next(spec for spec in specs if spec.name == "redis")
    mysql_proc = _bootstrap_mysql(mysql_spec)
    redis_proc = _bootstrap_redis(redis_spec)
    dependencies = {
        "mysql": {
            "pid": mysql_proc.pid,
            "port": mysql_spec.port,
            "command_line": str(mysql_spec.server_executable),
            "data_dir": str(mysql_spec.data_dir),
        },
        "redis": {
            "pid": redis_proc.pid,
            "port": redis_spec.port,
            "command_line": str(redis_spec.server_executable),
            "data_dir": str(redis_spec.data_dir),
        },
    }
    write_dependency_state(base, dependencies=dependencies)
    return read_dependency_state(base) or {"dependencies": dependencies}


def _resolve_source_dir(root: Path) -> tuple[str, Path]:
    active = get_active_release(root)
    if active:
        release_app, _ = _release_paths(root, active)
        if _source_layout_ok(release_app):
            return active, release_app
    current = _source_commit(root)
    if _source_layout_ok(root):
        if active != current:
            set_active_release(root, current)
        return current, root
    raise RuntimeError("no usable source tree is available")


def _quote_env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_env_file(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_service_env_files(
    source_root: Path | str,
    config: Mapping[str, Any],
    *,
    candidate: bool = False,
) -> dict[str, Path]:
    base = Path(source_root).resolve()
    ports = config["candidate_service_ports"] if candidate else config["service_ports"]
    mysql = config["mysql"]
    redis = config["redis"]
    backend_static_dir = (base / "backend-web" / "static").resolve()
    uploads_dir = backend_static_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    for folder in ("browser_data", "backend-web/browser_data", "websocket/browser_data", "scheduler/browser_data"):
        (base / folder).mkdir(parents=True, exist_ok=True)
    backup_dir = (base / "backups").resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)

    backend_env = [
        "ENVIRONMENT=production",
        "LOG_LEVEL=INFO",
        f"HOST=127.0.0.1",
        f"MYSQL_HOST={_quote_env_value(mysql['host'])}",
        f"MYSQL_PORT={_quote_env_value(mysql['port'])}",
        f"MYSQL_USER={_quote_env_value(mysql['user'])}",
        f"MYSQL_PASSWORD={_quote_env_value(mysql['password'])}",
        f"MYSQL_DATABASE={_quote_env_value(mysql['database'])}",
        f"REDIS_HOST={_quote_env_value(redis['host'])}",
        f"REDIS_PORT={_quote_env_value(redis['port'])}",
        f"REDIS_PASSWORD={_quote_env_value(redis['password'])}",
        f"REDIS_DB={_quote_env_value(redis['db'])}",
        f"BACKEND_WEB_PORT={ports['backend-web']}",
        "CORS_ORIGINS=*",
        "AUTO_START_CRAWL_JOBS=true",
        f"WEBSOCKET_SERVICE_URL=http://127.0.0.1:{ports['websocket']}",
        f"SCHEDULER_SERVICE_URL=http://127.0.0.1:{ports['scheduler']}",
        f"STATIC_DIR={backend_static_dir}",
        f"BACKUP_DIR={backup_dir}",
        f"FRONTEND_PUBLIC_URL=http://127.0.0.1:{ports['frontend']}",
        f"BACKEND_WEB_PUBLIC_URL=http://127.0.0.1:{ports['backend-web']}",
    ]

    websocket_env = [
        "ENVIRONMENT=production",
        "LOG_LEVEL=INFO",
        "HOST=127.0.0.1",
        f"MYSQL_HOST={_quote_env_value(mysql['host'])}",
        f"MYSQL_PORT={_quote_env_value(mysql['port'])}",
        f"MYSQL_USER={_quote_env_value(mysql['user'])}",
        f"MYSQL_PASSWORD={_quote_env_value(mysql['password'])}",
        f"MYSQL_DATABASE={_quote_env_value(mysql['database'])}",
        f"REDIS_HOST={_quote_env_value(redis['host'])}",
        f"REDIS_PORT={_quote_env_value(redis['port'])}",
        f"REDIS_PASSWORD={_quote_env_value(redis['password'])}",
        f"REDIS_DB={_quote_env_value(redis['db'])}",
        f"WEBSOCKET_PORT={ports['websocket']}",
        "AUTO_START_WEBSOCKET=true",
        "MAX_CAPTCHA_CONCURRENT=1",
        "BROWSER_HEADLESS=true",
        f"BACKEND_WEB_SERVICE_URL=http://127.0.0.1:{ports['backend-web']}",
        f"STATIC_DIR={backend_static_dir}",
    ]

    scheduler_env = [
        "ENVIRONMENT=production",
        "LOG_LEVEL=INFO",
        "HOST=127.0.0.1",
        f"MYSQL_HOST={_quote_env_value(mysql['host'])}",
        f"MYSQL_PORT={_quote_env_value(mysql['port'])}",
        f"MYSQL_USER={_quote_env_value(mysql['user'])}",
        f"MYSQL_PASSWORD={_quote_env_value(mysql['password'])}",
        f"MYSQL_DATABASE={_quote_env_value(mysql['database'])}",
        f"REDIS_HOST={_quote_env_value(redis['host'])}",
        f"REDIS_PORT={_quote_env_value(redis['port'])}",
        f"REDIS_PASSWORD={_quote_env_value(redis['password'])}",
        f"REDIS_DB={_quote_env_value(redis['db'])}",
        f"SCHEDULER_PORT={ports['scheduler']}",
        f"WEBSOCKET_SERVICE_URL=http://127.0.0.1:{ports['websocket']}",
        f"BACKEND_WEB_SERVICE_URL=http://127.0.0.1:{ports['backend-web']}",
        f"STATIC_DIR={backend_static_dir}",
        f"BACKUP_DIR={backup_dir}",
    ]

    promotion_env = [
        "ENVIRONMENT=production",
        "LOG_LEVEL=INFO",
        "HOST=127.0.0.1",
        f"MYSQL_HOST={_quote_env_value(mysql['host'])}",
        f"MYSQL_PORT={_quote_env_value(mysql['port'])}",
        f"MYSQL_USER={_quote_env_value(mysql['user'])}",
        f"MYSQL_PASSWORD={_quote_env_value(mysql['password'])}",
        f"MYSQL_DATABASE={_quote_env_value(mysql['database'])}",
        f"REDIS_HOST={_quote_env_value(redis['host'])}",
        f"REDIS_PORT={_quote_env_value(redis['port'])}",
        f"REDIS_PASSWORD={_quote_env_value(redis['password'])}",
        f"REDIS_DB={_quote_env_value(redis['db'])}",
        f"PROMOTION_PORT={ports['promotion']}",
        f"BACKEND_WEB_SERVICE_URL=http://127.0.0.1:{ports['backend-web']}",
        f"STATIC_DIR={backend_static_dir}",
        f"BACKUP_DIR={backup_dir}",
    ]

    # 关键修复:服务运行时 cwd = source(app 根), 而 BaseConfig 用相对路径 env_file=".env",
    # pydantic 从 cwd 解析。因此 .env 必须写到 <app>/<service_dir>/.env 而非仓库根的
    # <base>/<service_dir>/.env, 否则服务读不到、回退到默认库 xianyu_data。
    files = {
        "backend-web": base / "backend-web" / ".env",
        "websocket": base / "websocket" / ".env",
        "scheduler": base / "scheduler" / ".env",
        "promotion": base / "promotion" / "backend" / ".env",
    }
    _write_env_file(files["backend-web"], backend_env)
    _write_env_file(files["websocket"], websocket_env)
    _write_env_file(files["scheduler"], scheduler_env)
    _write_env_file(files["promotion"], promotion_env)
    return files


def _list_python_dependencies(service_dir: Path) -> list[str]:
    pyproject = service_dir / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return []
    project = data.get("project") or {}
    deps = project.get("dependencies") or []
    result: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        if not isinstance(dep, str):
            continue
        dep = dep.strip()
        if not dep or dep in seen:
            continue
        seen.add(dep)
        result.append(dep)
    return result


def _dependency_target(base: Path, source: Path) -> Path:
    if source.parent.name == "releases" and source.name == "app":
        return source.parent / "deps"
    return base / "state" / "deps" / _source_commit(base)


def _ensure_python_deps(base: Path, source: Path, config: Mapping[str, Any]) -> Path:
    deps_dir = _dependency_target(base, source)
    marker = deps_dir / ".installed"
    if marker.is_file():
        return deps_dir
    deps_dir.mkdir(parents=True, exist_ok=True)
    requirements: list[str] = []
    for service_name in PYTHON_SERVICE_ORDER:
        requirements.extend(_list_python_dependencies(source / service_name))
    unique_requirements: list[str] = []
    seen: set[str] = set()
    for dep in requirements:
        if dep not in seen:
            seen.add(dep)
            unique_requirements.append(dep)
    if not unique_requirements:
        marker.write_text("ok\n", encoding="utf-8")
        return deps_dir
    py = runtime_paths(base)["python"]
    env = portable_environment(base)
    cmd = [
        str(py),
        "-m",
        "pip",
        "install",
        "--no-warn-script-location",
        "--disable-pip-version-check",
        "--cache-dir",
        str(base / "cache" / "pip"),
        "--target",
        str(deps_dir),
        *unique_requirements,
    ]
    subprocess.run(
        cmd,
        cwd=source,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    marker.write_text("ok\n", encoding="utf-8")
    return deps_dir


def _build_frontend(base: Path, source: Path) -> None:
    frontend = source / "frontend"
    dist = frontend / "dist"
    if (dist / "index.html").is_file():
        return
    node = runtime_paths(base)["node"]
    npm = node.parent / "npm.cmd"
    if not npm.is_file():
        raise RuntimeResolutionError(f"missing npm executable: {npm}")
    env = portable_environment(base, app_dir=source)
    env["CI"] = "1"
    if not (frontend / "node_modules").is_dir():
        subprocess.run(
            ["cmd", "/d", "/c", str(npm), "ci"],
            cwd=frontend,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    subprocess.run(
        ["cmd", "/d", "/c", str(npm), "run", "build"],
        cwd=frontend,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not (dist / "index.html").is_file():
        raise RuntimeError("frontend build did not produce dist/index.html")


def _launch_python_service(
    base: Path,
    source: Path,
    deps_dir: Path,
    service: ServiceSpec,
) -> subprocess.Popen:
    if not _port_available("127.0.0.1", service.port):
        raise RuntimeError(f"port is occupied: {service.port}")
    service_dir = source / service.directory
    if not (service_dir / service.entry).is_file():
        raise RuntimeError(f"service entry is missing: {service_dir / service.entry}")
    env = portable_environment(base, app_dir=source, deps_dir=deps_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["HOST"] = "127.0.0.1"
    env[f"{service.name.replace('-', '_').upper()}_PORT"] = str(service.port)
    # 兜底: 即便服务因 cwd 相对路径读不到 .env, 注入的 MYSQL_*/REDIS_* 环境变量也能让
    # pydantic-settings 取到正确值(环境变量优先级高于 .env 与默认值), 避免回退默认库 xianyu_data。
    cfg = load_config(base)
    env["MYSQL_HOST"] = str(cfg["mysql"]["host"])
    env["MYSQL_PORT"] = str(cfg["mysql"]["port"])
    env["MYSQL_USER"] = str(cfg["mysql"]["user"])
    env["MYSQL_PASSWORD"] = str(cfg["mysql"]["password"])
    env["MYSQL_DATABASE"] = str(cfg["mysql"]["database"])
    env["REDIS_HOST"] = str(cfg["redis"]["host"])
    env["REDIS_PORT"] = str(cfg["redis"]["port"])
    env["REDIS_PASSWORD"] = str(cfg["redis"].get("password", ""))
    env["REDIS_DB"] = str(cfg["redis"].get("db", 0))
    # 静态文件目录指向 release 之外的持久位置: 上传保存(paths.py)与 /static 挂载
    # (_bootstrap)都读 STATIC_DIR, 缺省时二者分别解析到 release 内不同子目录
    # (backend-web/static vs app/static, 互相看不见)且随 update 部署丢失。
    # 持久化到 portable 根目录后上传即挂载, 部署后历史文件仍可见。
    static_dir = base / "backend-web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    env["STATIC_DIR"] = str(static_dir)
    log_dir = base / "logs" / "app"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"{service.name}.log").open("a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    release_bootstrap = base / "scripts" / "run_release.py"
    script = f"{service.directory}/{service.entry}"
    try:
        proc = subprocess.Popen(
            [str(runtime_paths(base)["python"]), str(release_bootstrap), str(source), str(deps_dir), script],
            cwd=source,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    finally:
        log.close()
    time.sleep(0.8)
    if proc.poll() is not None:
        raise RuntimeError(f"{service.name} exited during startup")
    return proc


def _frontend_proxy_target(config: Mapping[str, Any]) -> str:
    return f"http://127.0.0.1:{config['service_ports']['backend-web']}"


class FrontendRequestHandler(http.server.SimpleHTTPRequestHandler):
    # 禁用所有代理的 opener：转发目标永远是本地后端 127.0.0.1:8089，
    # 绝不能经过任何系统代理。Windows 上 urllib 默认会读取注册表里的系统
    # 代理设置（如 v2rayN 的 127.0.0.1:7890），并在进程内缓存；一旦代理软件
    # 关闭，进程仍死守旧代理去连本地后端，导致连接失败、返回 502——表现为
    # “开着代理软件能登录、关了就登录失败”。用 ProxyHandler({}) 彻底关闭代理，
    # 让转发始终直连本地后端，摆脱对任何代理软件的依赖。
    _no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __init__(self, *args, dist_dir: str = "", backend_url: str = "", **kwargs):
        self._dist_dir = dist_dir
        self._backend_url = backend_url.rstrip("/")
        super().__init__(*args, directory=dist_dir, **kwargs)

    def _proxy(self, method: str) -> None:
        target_url = f"{self._backend_url}{self.path}"
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None
        request = urllib.request.Request(target_url, data=body, method=method)
        for header, value in self.headers.items():
            if header.lower() not in {"host", "content-length"}:
                request.add_header(header, value)
        # 商品发布走 Playwright 无头浏览器，单品/批量发布可能耗时数分钟，
        # 90 秒的默认超时会在后端仍在发布时把连接掐断，前端因而误报“网络错误”。
        # 对发布类慢接口放宽到 600 秒（与前端 axios 的 10 分钟超时对齐），
        # 其余接口维持较短超时，避免异常请求长期占用连接。
        timeout = 600 if "/product-publish/publish/" in self.path else 120
        try:
            with self._no_proxy_opener.open(request, timeout=timeout) as response:
                self._relay_response(response, method)
        except urllib.error.HTTPError as http_error:
            # 后端返回的 4xx/5xx（如 401 未授权）会被 urllib 当成异常抛出。
            # 必须把真实状态码和响应体原样透传给前端，否则前端拦截器无法识别
            # 401 触发静默刷新 token，会误判为“网络错误”并被强制登出。
            self._relay_response(http_error, method)
        except Exception as exc:
            # 仅在真正的连接失败/超时等情况下返回 502。
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            payload = {"success": False, "code": 502, "message": f"proxy request failed: {exc}", "data": None}
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _relay_response(self, response: Any, method: str) -> None:
        """把后端（或 HTTPError）响应的状态码、响应头、响应体原样转发给客户端。"""
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        self.send_response(status)
        for header, value in response.getheaders():
            if header.lower() != "transfer-encoding":
                self.send_header(header, value)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(response.read())

    def do_GET(self) -> None:
        if self.path.startswith("/api/") or self.path == "/api":
            self._proxy("GET")
            return
        if self.path.startswith("/static/uploads/"):
            self._proxy("GET")
            return
        file_path = Path(self._dist_dir) / self.path.lstrip("/").split("?")[0]
        if not file_path.exists() and not file_path.suffix:
            self.path = "/index.html"
        super().do_GET()

    def do_HEAD(self) -> None:
        if self.path.startswith("/api/") or self.path == "/api" or self.path.startswith("/static/uploads/"):
            self._proxy("HEAD")
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        if self.path.startswith("/api/") or self.path == "/api" or self.path.startswith("/static/uploads/"):
            self._proxy("POST")
            return
        self.send_error(404)

    def do_PUT(self) -> None:
        if self.path.startswith("/api/") or self.path == "/api" or self.path.startswith("/static/uploads/"):
            self._proxy("PUT")
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        if self.path.startswith("/api/") or self.path == "/api" or self.path.startswith("/static/uploads/"):
            self._proxy("DELETE")
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _serve_frontend(root: Path, source: Path, host: str, port: int, backend_url: str) -> None:
    dist = source / "frontend" / "dist"
    if not dist.is_dir():
        raise RuntimeError(f"frontend dist directory is missing: {dist}")
    handler = lambda *args, **kwargs: FrontendRequestHandler(  # noqa: E731
        *args,
        dist_dir=str(dist),
        backend_url=backend_url,
        **kwargs,
    )
    server = http.server.ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _run_frontend_server(root: Path, source: Path, port: int, backend_url: str) -> None:
    _serve_frontend(root, source, "0.0.0.0", port, backend_url)


def copy_runtime_data(source_app: Path | str, target_app: Path | str, paths: list[str]) -> None:
    source = Path(source_app)
    target = Path(target_app)
    for relative in paths:
        src = source / relative
        dst = target / relative
        if not src.exists():
            continue
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_runtime_data(root: Path | str, app: Path | str, paths: list[str]) -> Path:
    base = Path(root).resolve()
    source = Path(app).resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = base / "backups" / stamp
    payload = backup / "payload"
    payload.mkdir(parents=True, exist_ok=False)
    copy_runtime_data(source, payload, paths)
    files = []
    for file in sorted(payload.rglob("*")):
        if file.is_file():
            files.append({"path": file.relative_to(payload).as_posix(), "sha256": _sha256(file), "size": file.stat().st_size})
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "source_app": str(source), "files": files}
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return backup


def protected_paths(root: Path | str) -> list[str]:
    path = Path(root).resolve() / "config" / "protected-paths.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("paths", []))


def parse_ls_remote(output: str, branch: str) -> str:
    expected = f"refs/heads/{branch}"
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == expected and re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            return fields[0]
    raise ValueError(f"remote branch not found: {branch}")


def retry_call(action, *, attempts: int = 3, delay: float = 2.0):
    last = None
    for index in range(attempts):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if index + 1 < attempts and delay:
                time.sleep(delay * (index + 1))
    raise last


def remote_commit(root: Path | str, config: Mapping[str, Any] | None = None) -> str:
    base = Path(root).resolve()
    cfg = dict(config or load_config(base))
    git = runtime_paths(base)["git"]
    action = lambda: subprocess.run(  # noqa: E731
        [str(git), "ls-remote", "--heads", cfg["upstream_url"], f"refs/heads/{cfg['branch']}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )
    try:
        result = retry_call(action, attempts=2, delay=2)
        return parse_ls_remote(result.stdout, cfg["branch"])
    except Exception:
        owner_repo = _upstream_owner_repo(cfg["upstream_url"])
        api = f"https://api.github.com/repos/{owner_repo}/commits/{cfg['branch']}"
        request = urllib.request.Request(api, headers={"User-Agent": "xianyu-portable-updater", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            sha = json.loads(response.read().decode("utf-8"))["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("GitHub API returned an invalid commit")
        return sha


def download_source_archive(root: Path, commit: str, app: Path) -> None:
    import zipfile

    owner_repo = _upstream_owner_repo(load_config(root)["upstream_url"])
    url = f"https://api.github.com/repos/{owner_repo}/zipball/{commit}"
    request = urllib.request.Request(url, headers={"User-Agent": "xianyu-portable-updater", "Accept": "application/vnd.github+json"})
    archive = root / "cache" / "downloads" / f"source-{commit}.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=300) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    extract = root / "staging" / f".extract-{commit}"
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extract)
    children = [item for item in extract.iterdir() if item.is_dir()]
    if len(children) != 1:
        raise RuntimeError("unexpected source archive layout")
    shutil.move(str(children[0]), str(app))
    shutil.rmtree(extract)


def _release_manifest(root: Path, release: Path, commit: str, *, verified: bool) -> None:
    app = release / "app"
    version = "unknown"
    version_path = app / "frontend" / "package.json"
    if version_path.is_file():
        try:
            version = json.loads(version_path.read_text(encoding="utf-8")).get("version", "unknown")
        except Exception:
            version = "unknown"
    manifest = {
        "commit": commit,
        "app_version": version,
        "upstream_url": load_config(root)["upstream_url"],
        "built_at": datetime.now(timezone.utc).isoformat(),
        "verified": verified,
    }
    (release / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify(root: Path | str) -> dict[str, Any]:
    base = Path(root).resolve()
    config = load_config(base)
    validate_runtime(base)
    commit, source = _resolve_source_dir(base)
    write_service_env_files(source, config)
    mysql = config["mysql"]
    redis = config["redis"]
    health: dict[str, Any] = {"runtime": "ok"}
    health["mysql"] = "ok" if _tcp_reachable(mysql["host"], mysql["port"]) else "unreachable"
    health["redis"] = "ok" if _tcp_reachable(redis["host"], redis["port"]) else "unreachable"
    if health["mysql"] != "ok" or health["redis"] != "ok":
        health["ok"] = False
        health["commit"] = commit
        return health

    deps_dir = _ensure_python_deps(base, source, config)
    _build_frontend(base, source)
    compile_cmd = [
        str(runtime_paths(base)["python"]),
        "-m",
        "compileall",
        "-q",
        str(source / "backend-web"),
        str(source / "websocket"),
        str(source / "scheduler"),
        str(source / "promotion" / "backend"),
        str(source / "common"),
    ]
    result = subprocess.run(
        compile_cmd,
        cwd=source,
        env=portable_environment(base, app_dir=source, deps_dir=deps_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError((result.stdout or "") + (result.stderr or ""))

    health["compileall"] = "ok"
    for spec in service_specs(config):
        url = f"http://127.0.0.1:{spec.port}{spec.health_path}"
        ok, body = wait_health(url, 2.0, interval=0)
        health[f"{spec.name}_health"] = "ok" if ok else body
    health["ok"] = all(
        value == "ok"
        for key, value in health.items()
        if key not in {"ok", "commit"} and isinstance(value, str)
    )
    health["commit"] = commit
    return health


def _kill_pid(pid: int) -> None:
    if _pid_exists(pid):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def stop(root: Path | str, timeout: float = 20) -> bool:
    base = Path(root).resolve()
    state = read_process_state(base)
    stopped = False
    if not state or not is_process_running(state):
        try:
            (base / "state" / "process.json").unlink()
        except FileNotFoundError:
            pass
    else:
        services = state.get("services")
        if isinstance(services, Mapping):
            for info in services.values():
                if isinstance(info, Mapping) and isinstance(info.get("pid"), int):
                    _kill_pid(info["pid"])
        elif isinstance(state.get("pid"), int):
            _kill_pid(state["pid"])

        deadline = time.time() + timeout
        while time.time() < deadline and is_process_running(state):
            time.sleep(0.2)
            state = read_process_state(base) or state

        try:
            (base / "state" / "process.json").unlink()
        except FileNotFoundError:
            pass
        stopped = True

    dependency_stopped = stop_dependencies(base, timeout)
    return stopped or dependency_stopped


def stop_dependencies(root: Path | str, timeout: float = 20) -> bool:
    base = Path(root).resolve()
    state = read_dependency_state(base)
    if not state or not is_dependency_process_running(state):
        try:
            (base / "state" / "dependencies.json").unlink()
        except FileNotFoundError:
            pass
        return False

    dependencies = state.get("dependencies")
    if isinstance(dependencies, Mapping):
        for info in dependencies.values():
            if isinstance(info, Mapping) and isinstance(info.get("pid"), int):
                _kill_pid(info["pid"])

    deadline = time.time() + timeout
    while time.time() < deadline and is_dependency_process_running(state):
        time.sleep(0.2)
        state = read_dependency_state(base) or state

    try:
        (base / "state" / "dependencies.json").unlink()
    except FileNotFoundError:
        pass
    return True


def _candidate_release(root: Path, commit: str) -> tuple[Path, Path]:
    release = root / "releases" / commit
    return release / "app", release / "deps"


def _prepare_release(root: Path, config: Mapping[str, Any], commit: str) -> tuple[Path, Path]:
    stage = root / "staging" / commit
    app = stage / "app"
    deps = stage / "deps"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [str(runtime_paths(root)["git"]), "clone", "--no-checkout", "--branch", config["branch"], config["upstream_url"], str(app)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        subprocess.run(
            [str(runtime_paths(root)["git"]), "-C", str(app), "checkout", "--detach", commit],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception:
        if app.exists():
            shutil.rmtree(app)
        download_source_archive(root, commit, app)
    write_service_env_files(app, config)
    _ensure_python_deps(root, app, config)
    _build_frontend(root, app)
    _release_manifest(root, stage, commit, verified=True)
    return app, stage / "deps"


def update(root: Path | str, *, check_only: bool = False, force: bool = False) -> dict[str, Any]:
    base = Path(root).resolve()
    cfg = load_config(base)
    lock = acquire_update_lock(base)
    run_id = uuid.uuid4().hex
    old = get_active_release(base)
    was_running = is_process_running(read_process_state(base))
    try:
        target = remote_commit(base, cfg)
        if check_only:
            return {"result": "update_available" if target != old else "up_to_date", "active_commit": old, "remote_commit": target}
        if target == old and not force:
            return {"result": "up_to_date", "active_commit": old, "remote_commit": target}
        app, deps = _prepare_release(base, cfg, target)
        release = base / "releases" / target
        if release.exists():
            shutil.rmtree(release)
        release.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(app.parent), str(release))
        if was_running:
            stop(base)
        set_active_release(base, target)
        if was_running:
            _start_services(base)
        result = {
            "result": "updated",
            "previous_commit": old,
            "active_commit": target,
            "run_id": run_id,
        }
        (base / "state" / "last-update.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_maintenance_event(base, command="update", stage="complete", result="success", old_commit=old, new_commit=target, run_id=run_id)
        return result
    except Exception as exc:  # noqa: BLE001
        write_maintenance_event(base, command="update", stage="failed", result="failure", old_commit=old, new_commit=locals().get("target"), run_id=run_id, details={"error": str(exc)})
        raise
    finally:
        release_update_lock(base)


def rollback_release(root: Path | str) -> dict[str, Any]:
    base = Path(root).resolve()
    cfg = load_config(base)
    path = base / "state" / "last-update.json"
    if not path.is_file():
        raise RuntimeError("no successful update record is available")
    info = json.loads(path.read_text(encoding="utf-8"))
    target = info.get("previous_commit")
    current = get_active_release(base)
    if not target or not (base / "releases" / target).is_dir():
        raise RuntimeError("previous release is unavailable")
    was_running = is_process_running(read_process_state(base))
    if was_running:
        stop(base)
    set_active_release(base, target)
    if was_running:
        _start_services(base)
    result = {"result": "rolled_back", "from_commit": current, "active_commit": target}
    (base / "state" / "last-rollback.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _start_services(base: Path) -> dict[str, Any]:
    cfg = load_config(base)
    commit, source = _resolve_source_dir(base)
    write_service_env_files(source, cfg)
    ensure_dependency_processes(base, cfg)
    if not _port_available("127.0.0.1", cfg["service_ports"]["backend-web"]):
        raise RuntimeError("backend-web port is occupied")
    mysql = cfg["mysql"]
    redis = cfg["redis"]
    if not _tcp_reachable(mysql["host"], mysql["port"]):
        raise RuntimeError("MySQL is not reachable")
    if not _tcp_reachable(redis["host"], redis["port"]):
        raise RuntimeError("Redis is not reachable")
    deps_dir = _ensure_python_deps(base, source, cfg)
    _build_frontend(base, source)
    service_procs: dict[str, Mapping[str, Any]] = {}
    for spec in service_specs(cfg):
        if spec.kind == "python":
            proc = _launch_python_service(base, source, deps_dir, spec)
            service_procs[spec.name] = {"pid": proc.pid, "port": spec.port, "command_line": f"{spec.entry}"}
    frontend_log_dir = base / "logs" / "app"
    frontend_log_dir.mkdir(parents=True, exist_ok=True)
    frontend_log = (frontend_log_dir / "frontend.log").open("a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen(
        [
            str(runtime_paths(base)["python"]),
            str(Path(__file__).resolve()),
            "serve-frontend",
            "--source",
            str(source),
            "--port",
            str(cfg["service_ports"]["frontend"]),
            "--backend-url",
            _frontend_proxy_target(cfg),
        ],
        cwd=source,
        env=portable_environment(base, app_dir=source, deps_dir=deps_dir),
        stdout=frontend_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    time.sleep(0.8)
    if proc.poll() is not None:
        raise RuntimeError("frontend exited during startup")
    service_procs["frontend"] = {"pid": proc.pid, "port": cfg["service_ports"]["frontend"], "command_line": "serve-frontend"}
    write_process_state(base, commit=commit, source_dir=source, services=service_procs)
    for spec in service_specs(cfg):
        url = f"http://127.0.0.1:{spec.port}{spec.health_path}"
        ok, body = wait_health(url, cfg["health_timeout_seconds"], interval=0.5)
        if not ok:
            stop(base)
            raise RuntimeError(f"{spec.name} health check failed: {body}")
    return {"started": True, "commit": commit, "services": service_procs}


def start(root: Path | str) -> dict[str, Any]:
    base = Path(root).resolve()
    cfg = load_config(base)
    validate_runtime(base)
    return _start_services(base)


def status(root: Path | str) -> dict[str, Any]:
    base = Path(root).resolve()
    state = read_process_state(base)
    dependency_state = read_dependency_state(base)
    active = get_active_release(base)
    services: dict[str, Any] = {}
    dependencies: dict[str, Any] = {}
    cfg = None
    try:
        cfg = load_config(base)
    except Exception:
        pass
    if cfg:
        for spec in service_specs(cfg):
            info = None
            if state and isinstance(state.get("services"), Mapping):
                info = state["services"].get(spec.name)
            running = isinstance(info, Mapping) and isinstance(info.get("pid"), int) and _pid_exists(info["pid"])
            services[spec.name] = {
                "port": spec.port,
                "pid": info.get("pid") if isinstance(info, Mapping) else None,
                "running": running,
            }
        for spec in dependency_service_specs(base, cfg):
            info = None
            if dependency_state and isinstance(dependency_state.get("dependencies"), Mapping):
                info = dependency_state["dependencies"].get(spec.name)
            running = isinstance(info, Mapping) and isinstance(info.get("pid"), int) and _pid_exists(info["pid"])
            dependencies[spec.name] = {
                "port": spec.port,
                "pid": info.get("pid") if isinstance(info, Mapping) else None,
                "running": running,
            }
    return {
        "active_commit": active,
        "process": state,
        "dependencies": dependency_state,
        "running": is_process_running(state),
        "services": services,
        "dependency_services": dependencies,
    }


def serve_frontend_command(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backend-url", type=str, required=True)
    args = parser.parse_args(argv)
    _serve_frontend(portable_root(), args.source.resolve(), "0.0.0.0", args.port, args.backend_url)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["verify", "start", "stop", "status", "rollback", "update", "check-only", "serve-frontend"])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--backend-url", type=str)
    args = parser.parse_args(argv)
    base = (args.root or portable_root()).resolve()
    try:
        if args.command == "verify":
            result = verify(base)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        elif args.command == "status":
            print(json.dumps(status(base), ensure_ascii=False, indent=2))
        elif args.command == "stop":
            print(json.dumps({"stopped": stop(base)}, ensure_ascii=False))
        elif args.command == "start":
            print(json.dumps(start(base), ensure_ascii=False, indent=2))
        elif args.command in {"update", "check-only"}:
            print(json.dumps(update(base, check_only=args.check_only or args.command == "check-only", force=args.force), ensure_ascii=False, indent=2))
        elif args.command == "rollback":
            print(json.dumps(rollback_release(base), ensure_ascii=False, indent=2))
        elif args.command == "serve-frontend":
            if not args.source or not args.port or not args.backend_url:
                raise RuntimeError("serve-frontend requires --source, --port, and --backend-url")
            _serve_frontend(base, args.source.resolve(), "0.0.0.0", args.port, args.backend_url)
        return 0
    except Exception as exc:  # noqa: BLE001
        write_maintenance_event(base, command=args.command, stage="cli", result="failure", details={"error": str(exc)})
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
