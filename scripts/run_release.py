#!/usr/bin/env python3
"""Bootstrap a release with embedded Python, which ignores PYTHONPATH by design."""
from __future__ import annotations
import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: run_release.py APP_DIR DEPS_DIR [SCRIPT]")
    app = Path(sys.argv[1]).resolve()
    deps = Path(sys.argv[2]).resolve()
    script = sys.argv[3] if len(sys.argv) > 3 else "Start.py"
    sys.path.insert(0, str(deps))
    sys.path.insert(0, str(app))
    os.chdir(app)
    if script == "--module":
        if len(sys.argv) < 5: raise SystemExit("--module requires a module name")
        sys.argv = sys.argv[4:]
        runpy.run_module(sys.argv[0], run_name="__main__")
    elif script == "--code":
        if len(sys.argv) < 5: raise SystemExit("--code requires Python code")
        code = sys.argv[4]
        sys.argv = ["-c", *sys.argv[5:]]
        exec(code, {"__name__": "__main__"})
    else:
        sys.argv = [script, *sys.argv[4:]]
        runpy.run_path(str(app / script), run_name="__main__")
    return 0

if __name__ == "__main__": raise SystemExit(main())
