"""Standard-library utilities shared by the launcher and native worker."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import platform
import sys

ROOT = Path(__file__).resolve().parents[2]
STATUSES = {"PASS", "FAIL", "UNSUPPORTED", "ERROR"}


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contained(base, relative):
    """Submission paths are relative; never follow ../ or external symlinks."""
    base = Path(base).resolve()
    relative = Path(relative)
    if relative.is_absolute():
        raise ValueError("Submission paths must be relative")
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Submission path leaves its root")
    return path


def code_fingerprint():
    data = b"".join(p.relative_to(ROOT).as_posix().encode() + p.read_bytes()
                    for p in sorted((ROOT / "src" / "mechcad").rglob("*.py")))
    return hashlib.sha256(data).hexdigest()


def environment():
    import FreeCAD as App
    import Part
    binaries = {}
    for name in ["python.exe", "FreeCAD.pyd", "Part.pyd"]:
        path = Path(sys.executable).parent / name
        if path.is_file():
            binaries[name] = sha256(path)
    import numpy, scipy
    from scipy.ndimage import _nd_image
    from numpy.core import _multiarray_umath
    from PySide import QtCore, QtSvg
    import importlib
    qt_backend = [importlib.import_module("PySide6."+name) for name in ("QtCore","QtGui","QtSvg")]
    drawing_dependencies = {"numpy": numpy.__version__, "scipy": scipy.__version__, "qt": QtCore.qVersion(),
        "native_modules_sha256": {str(Path(m.__file__).name): sha256(m.__file__) for m in (_nd_image, _multiarray_umath, QtCore, QtSvg, *qt_backend)}}
    return {"python": sys.version, "python_executable": sys.executable,
            "freecad": App.Version(), "occt": Part.OCC_VERSION,
            "platform": platform.platform(), "binaries_sha256": binaries,
            "evaluator_source_sha256": code_fingerprint(), "drawing_dependencies": drawing_dependencies}


def runtime_python():
    override = os.environ.get("MECHCAD_FREECAD_PYTHON")
    path = Path(override) if override else Path(r"E:\freecad\freecad\bin\python.exe")
    if not path.is_file():
        raise FileNotFoundError("Set MECHCAD_FREECAD_PYTHON to FreeCAD's bundled python.exe")
    return path


def task_path(task_id):
    return ROOT / "tasks" / task_id / "task.json"

