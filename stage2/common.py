"""Stable task identities, atomic records and advisory locks for local POSIX filesystems."""

from contextlib import contextmanager
from pathlib import Path
import fcntl
import hashlib
import json
import os
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
FROZEN = PROJECT / "stage1_engine"
CONFIG = PROJECT / "config/stage1_method.json"
CITIES = tuple(json.loads(CONFIG.read_text())["cities"])
DEV = tuple(sorted(set(CITIES) - {"jacksonville"}))
BUDGET = {100: 10, 500: 30, 1000: 40, 2000: 50}


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest(value):
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value, *, sort_keys=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, sort_keys=sort_keys, indent=2, allow_nan=False)
        stream.write("\n")
    os.replace(stream.name, path)


@contextmanager
def lock(path, *, shared=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def code_id():
    files = [
        PROJECT / "pipeline.py",
        *sorted((PROJECT / "stage2").glob("*.py")),
        *sorted((PROJECT / "sources").glob("*.py")),
        *sorted((PROJECT / "evrptw_scenarios").glob("*.py")),
    ]
    return digest([(str(p.relative_to(PROJECT)), sha(p)) for p in files])


class MethodFailure(Exception):
    """The fixed task is outside the support of the registered construction method."""

    def __init__(self, stage, reason, detail=None):
        super().__init__(reason)
        self.result = dict(
            status="generation_failed", stage=stage, reason=reason, detail=detail
        )


def runtime_contract():
    import platform
    from importlib.metadata import version

    import numpy as np

    blas = np.show_config(mode="dicts").get("Build Dependencies", {}).get("blas", {})
    return dict(
        blas={
            key: blas.get(key) for key in ("name", "version", "openblas configuration")
        },
        system=platform.system(),
        machine=platform.machine(),
        python=platform.python_version(),
        packages={
            n: version(n)
            for n in (
                "numpy",
                "scipy",
                "pandas",
                "pyarrow",
                "shapely",
                "geopandas",
                "pyproj",
            )
        },
    )


def shared_identity(root):
    obj = read(Path(root) / "shared.json")
    return (
        digest(
            {
                "city": obj["city"],
                "files": obj["files"],
                "vintage": obj.get("vintage"),
                "calibration": obj.get("calibration"),
            }
        )
        if "files" in obj
        else sha(Path(root) / "shared.json")
    )
