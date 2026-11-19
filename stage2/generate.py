"""Generate one fixed task, store its realized attributes, and reconstruct road arrays by ID."""

from collections import Counter
import json
from pathlib import Path
import platform
import tempfile
import zipfile

import numpy as np
import scipy

from .city import CityDay, verify_shared
from .common import shared_identity
from .common import PROJECT, digest, read, sha
from .customers import activate_customers
from .exchange import (
    array_hashes,
    encode,
    hash_bytes,
    node_table,
    node_text,
    put_zip,
    write_all,
)
from .instance_orders import build_orders
from .instance_witness import check_witness, make_witness
from .stations import route_arrays, station_selection

PAYLOAD = (
    "instance.json",
    "nodes.txt",
    "vehicle.json",
    "orders.json",
    "station_selection.json",
    "customer_activation.json",
    "witness.json",
)


def generate(task, day, catalog, shared_hash, plan_id, out, namespace=None):
    try:
        f = activate_customers(
            city=task["city"],
            n=task["n"],
            stock=day.stock(task["depot"]),
            neighbors=day.neighbors,
            fastest_times_s=day.travel,
            find_medoid=day.medoid,
            center_times=day.center_times,
            seed=task["seed"],
            task_key=task["task_key"],
        )
    finally:
        day.clear_centers()
    if f["status"] != "ok":
        return {
            "status": "generation_failed",
            "stage": "customers",
            "reason": f["reason"],
            "activation": f,
        }
    ids = f["customer_ids"]
    veh = read(PROJECT / "config/vehicle.json")
    orders = build_orders(f, day.loc, catalog)
    power = day.loc.loc[day.loc.location_type == "charging", "power_kw"].sort_index()
    core = [task["depot"], *ids]
    d, t, reverse_d, reverse_t = day.router.station_costs(
        [day.bound[k] for k in core], [day.bound[k] for k in power.index]
    )
    selected, cs = station_selection(
        d,
        t,
        power.index.tolist(),
        ids,
        power.to_numpy(),
        veh,
        task["cs_budget"],
        from_stations=(reverse_d, reverse_t),
    )
    del d, t, reverse_d, reverse_t
    cs.update(
        budget=task["cs_budget"],
        full_pool=len(power),
        selected=selected,
        shortfall=task["cs_budget"] - len(selected),
    )
    nodes = node_table(core + selected, orders, selected, day.loc, veh)
    arrays, meta = route_arrays(day.router, day.bound, core + selected, veh)
    witness = make_witness(nodes, arrays, veh)
    check_witness(witness, nodes, arrays, veh)
    inst = dict(
        **task,
        instance_id=digest([namespace or plan_id, task["task_key"], task["seed"]])[:24],
        plan_sha256=plan_id,
        schema="evrptwd_instance_v1",
        vehicle_file="vehicle.json",
        external_charging_station_count=len(selected),
        customer_count=len(ids),
        node_count=len(nodes),
        method_version=f["profile_version"],
        execution_path_policy="shortest_distance",
        primary_objective="total_distance",
        protocol_status=witness["status"],
        shared_sha256=shared_hash,
        matrix_hashes=array_hashes(arrays, len(nodes)),
        routing=meta,
    )
    if namespace is not None and namespace != plan_id:
        inst["instance_id_namespace_sha256"] = namespace
    files = {
        k: encode(v)
        for k, v in [
            ("instance.json", inst),
            ("vehicle.json", veh),
            ("witness.json", witness),
            ("orders.json", orders),
            ("station_selection.json", cs),
            ("customer_activation.json", f),
        ]
    }
    files["nodes.txt"] = node_text(nodes)
    put_zip(
        out,
        {
            **files,
            "checksums.json": encode({k: hash_bytes(v) for k, v in files.items()}),
        },
    )
    stem = arrays["shortest_distance_time_s"][0, 1 : 1 + len(ids)]
    bounded = [o for o in orders if o["donor_local_tw_s"] is not None]
    stats = dict(
        K_seed=f["K_seed"],
        K_geo=f["K_geo"],
        tracts=f["footprint"]["T"],
        candidate_to_served_ratio=f["footprint"]["M"] / len(ids),
        max_stem_s=float(np.max(stem)),
        median_stem_s=float(np.median(stem)),
        specified_tw_share=len(bounded) / len(ids),
        earliest_specified_due_s=min((o["due_s"] for o in bounded), default=None),
        median_specified_width_s=float(
            np.median([o["due_s"] - o["ready_s"] for o in bounded])
        )
        if bounded
        else None,
    )
    return dict(
        environment=dict(
            python=platform.python_version(),
            platform=platform.platform(),
            numpy=np.__version__,
            scipy=scipy.__version__,
        ),
        status=witness["status"],
        certified_customers=len(witness["routes"]),
        failed_customers=len(witness["failures"]),
        failure_reasons=dict(Counter(item["reason"] for item in witness["failures"])),
        statistics=stats,
        n=len(ids),
        stations=len(selected),
        instance_id=inst["instance_id"],
    )


def restore(archive, shared, out):
    with zipfile.ZipFile(archive) as z:
        if sorted(z.namelist()) != sorted((*PAYLOAD, "checksums.json")):
            raise ValueError("Unknown compact format")
        files = {k: z.read(k) for k in PAYLOAD}
        if {k: hash_bytes(v) for k, v in files.items()} != json.loads(
            z.read("checksums.json")
        ):
            raise ValueError("Damaged compact record")
    inst, veh, orders, cs, witness, act = (
        json.loads(files[k])
        for k in (
            "instance.json",
            "vehicle.json",
            "orders.json",
            "station_selection.json",
            "witness.json",
            "customer_activation.json",
        )
    )
    if (
        shared_identity(shared) != inst["shared_sha256"]
        or verify_shared(shared)["city"] != inst["city"]
    ):
        raise ValueError("Wrong shared city assets")
    if [x["location_id"] for x in orders] != act["customer_ids"]:
        raise ValueError("Order and customer IDs disagree")
    ids = [inst["depot"], *act["customer_ids"], *cs["selected"]]
    day = CityDay(shared, inst["day_type"], ids=ids)
    nodes = node_table(ids, orders, cs["selected"], day.loc, veh)
    if files["nodes.txt"] != node_text(nodes):
        raise ValueError("Restored node attributes differ")
    arrays, _ = route_arrays(day.router, day.bound, ids, veh)
    if array_hashes(arrays, len(nodes)) != inst["matrix_hashes"]:
        raise ValueError("Matrix hashes differ")
    check_witness(witness, nodes, arrays, veh)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise FileExistsError("Restore output already exists: " + str(out))
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        stage = Path(tmp) / "expanded"
        write_all(stage, files, arrays)
        stage.rename(out)
    return inst["instance_id"]
