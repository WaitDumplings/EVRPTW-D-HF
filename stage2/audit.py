"""Reconcile multiple host outputs against the same preregistered city task list."""

from collections import Counter, defaultdict
from pathlib import Path

from .common import digest, read, sha


def audit(plan, outputs):
    plan_id = sha(plan)
    data = read(plan)
    if digest(data["tasks"]) != data["tasks_sha256"]:
        raise ValueError("City task list differs from its preregistered digest")
    counts = defaultdict(Counter)
    issues = []
    for task in data["tasks"]:
        key = (task["city"], task["split"], task["n"], task["day_type"])
        cell = counts[key]
        cell["planned"] += 1
        part = Path(task["city"]) / task["split"] / f"Cus{task['n']}"
        name = f"instance{task['slot']:04d}"
        seen = []
        for out in outputs:
            history = out / "ledger" / part / name
            if not history.is_dir():
                continue
            result = history / "result.json"
            if not result.is_file():
                seen.append("interrupted")
                continue
            row = read(result)
            if (
                row.get("signature") != digest([plan_id, task])
                or row.get("task") != task
            ):
                seen.append("wrong_plan")
                continue
            status = row.get("status")
            if status in {
                "protocol_certified",
                "protocol_uncertified",
                "proven_infeasible",
            }:
                prefix = (
                    "instances" if status == "protocol_certified" else "uncertified"
                )
                archive = Path(prefix) / part / (name + ".evrp.zip")
                if (
                    str(archive) != row.get("file")
                    or not (out / archive).is_file()
                    or sha(out / archive) != row.get("sha256")
                ):
                    status = "damaged"
            elif status not in {"generation_failed", "execution_error"}:
                status = "invalid_status"
            seen.append(status)
        status = "pending" if not seen else seen[0] if len(seen) == 1 else "duplicate"
        cell[status] += 1
        if status in {
            "duplicate",
            "wrong_plan",
            "invalid_status",
            "damaged",
            "execution_error",
            "interrupted",
        }:
            issues.append(dict(task_key=task["task_key"], issue=status, observed=seen))
    rows = [
        dict(city=c, split=s, n=n, day_type=d, **counts[c, s, n, d])
        for c, s, n, d in sorted(counts)
    ]
    return dict(rows=rows, issues=issues)
