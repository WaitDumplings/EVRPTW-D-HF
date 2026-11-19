"""Run a deterministic plan on one or more independent POSIX hosts."""

from concurrent.futures import ProcessPoolExecutor
from collections import Counter
from datetime import datetime, timezone
import gc
from .common import shared_identity
from .common import MethodFailure, runtime_contract
import multiprocessing
import os
from pathlib import Path
import tempfile
import traceback

from .city import CityDay, verify_shared
from .common import PROJECT, code_id, digest, lock, read, sha, write
from .generate import generate


_worker_day = None
_worker_key = None


def _day(shared, kind):
    global _worker_day, _worker_key
    key = (shared, kind)
    if key != _worker_key:
        if _worker_day is not None:
            _worker_day.stock.cache_clear()
            _worker_day = None
            gc.collect()
        _worker_key = None
        _worker_day = CityDay(shared, kind)
        _worker_key = key
    return _worker_day


def one(args):
    task, shared, catalog, out, plan_id, shared_hash, retry_method = args
    name = f"instance{task['slot']:04d}"
    sub = Path(task["city"]) / task["split"] / f"Cus{task['n']}"
    history = out / "ledger" / sub / name
    receipt = history / "result.json"
    with lock(history / "task.lock"):
        signature = digest([plan_id, task])
        if receipt.exists():
            prior = read(receipt)
            if prior["signature"] != signature:
                raise ValueError("Task path belongs to another plan")
            if prior["status"] == "generation_failed" and not retry_method:
                return "skipped_method_failure"
            if (
                prior.get("file")
                and (out / prior["file"]).is_file()
                and sha(out / prior["file"]) == prior["sha256"]
            ):
                return "skipped"
        attempts = sorted(history.glob("attempt-*.json"))
        if attempts:
            previous = read(attempts[-1])
            if previous["signature"] != signature:
                raise ValueError("An interrupted attempt belongs to another plan")
            if previous["status"] == "started":
                previous.update(
                    status="execution_error",
                    reason="interrupted_before_receipt",
                    error_type="Interrupted",
                )
                write(attempts[-1], previous)
        attempt = history / f"attempt-{len(attempts) + 1:04d}.json"
        entry = dict(
            signature=signature,
            task=task,
            pid=os.getpid(),
            started_utc=datetime.now(timezone.utc).isoformat(),
            status="started",
        )
        write(attempt, entry)
        try:
            with tempfile.TemporaryDirectory(dir=history) as tmp:
                product = Path(tmp) / "instance.zip"
                day = _day(shared, task["day_type"])
                result = generate(task, day, catalog, shared_hash, plan_id, product)
                if product.is_file():
                    prefix = (
                        "instances"
                        if result["status"] == "protocol_certified"
                        else "uncertified"
                    )
                    target = out / prefix / sub / (name + ".evrp.zip")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    result.update(
                        file=str(target.relative_to(out)), sha256=sha(product)
                    )
                    os.replace(product, target)
        except MethodFailure as err:
            result = err.result
        except Exception as err:
            result = dict(
                status="execution_error",
                reason=str(err),
                error_type=type(err).__name__,
                traceback=traceback.format_exc(),
            )
        entry.update(result, finished_utc=datetime.now(timezone.utc).isoformat())
        write(attempt, entry)
        write(receipt, entry)
        return result["status"]


def run(
    plan, shared, catalog, out, workers, split, n, limit, shard, retry_method=False
):
    data = read(plan)
    release = plan.parent / "release.json"
    if (
        release.exists()
        and plan.stem == data["city"]
        and read(release)["plans_sha256"].get(data["city"]) != sha(plan)
    ):
        raise ValueError("City plan disagrees with the global release index")
    shared_hash = shared_identity(shared)
    if (
        data["runtime"] != runtime_contract()
        or data["tasks_sha256"] != digest(data["tasks"])
        or data["code_sha256"] != code_id()
        or data["shared_sha256"] != shared_hash
        or data["city"] != verify_shared(shared)["city"]
        or data["resource_sha256"]
        != {
            f.name: sha(f)
            for f in sorted((PROJECT / "config").iterdir())
            if f.is_file()
        }
    ):
        raise ValueError(
            "Plan refers to a different runtime, task list, code, calibration or shared city files"
        )
    tasks = [
        t
        for t in data["tasks"]
        if (split is None or t["split"] == split) and (n is None or t["n"] == n)
    ]
    if not tasks:
        raise ValueError(
            "No tasks match the requested split and size in this city plan"
        )
    if limit is not None:
        tasks = tasks[:limit]
    index, total = map(int, shard.split("/"))
    if not 0 <= index < total:
        raise ValueError("Shard must satisfy 0 <= index < count")
    tasks = [t for t in tasks if t["serial"] % total == index]
    plan_id = sha(plan)
    jobs = [
        (t, shared, catalog, out, plan_id, shared_hash, retry_method)
        for t in sorted(tasks, key=lambda t: (t["day_type"], t["depot"], t["serial"]))
    ]
    if workers == 1:
        return dict(Counter(map(one, jobs)))
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        return dict(Counter(pool.map(one, jobs, chunksize=1)))
