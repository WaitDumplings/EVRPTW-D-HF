"""Offline city overview and local instance map using the published Solomon rows."""

import io
import json
import zipfile

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from .common import PROJECT


def draw(instance, target, boundary=None, shared_root=None):
    if instance.is_dir():
        nodes = pd.read_csv(instance / "nodes.txt", sep="\t")
        info = json.loads((instance / "instance.json").read_text())
    else:
        with zipfile.ZipFile(instance) as z:
            nodes = pd.read_csv(io.BytesIO(z.read("nodes.txt")), sep="\t")
            info = json.loads(z.read("instance.json"))
    city = info["city"]
    if not boundary:
        options = [
            instance.parent / "boundary.geojson",
            (shared_root or PROJECT / "work/shared") / city / "boundary.geojson",
        ]
        boundary = next((p for p in options if p.is_file()), None)
    if boundary is None:
        raise ValueError("Supply --boundary for this city")
    land = (
        gpd.read_parquet(boundary)
        if boundary.suffix == ".parquet"
        else gpd.read_file(boundary)
    )
    crs = land.estimate_utm_crs()
    land = land.to_crs(crs)
    pts = gpd.GeoDataFrame(
        nodes, geometry=gpd.points_from_xy(nodes.LON, nodes.LAT), crs=4326
    ).to_crs(crs)
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(11, 6),
        gridspec_kw={"width_ratios": [1, 1.45]},
        layout="constrained",
    )
    labels = [
        ("customer", "Customers", "#117e79", "o", 19),
        ("charging", "Charging stations", "#da7d2c", "^", 61),
        ("depot", "Depot", "#513477", "*", 180),
    ]
    from matplotlib.patches import Rectangle

    for ax in axes:
        land.plot(ax=ax, color="#f3f1eb", edgecolor="#c4c9c1", linewidth=0.55)
        for typ, label, color, mark, size in labels:
            group = pts[pts.TYPE == typ]
            ax.scatter(
                group.geometry.x,
                group.geometry.y,
                label=f"{label} ({len(group)})",
                c=color,
                s=size,
                marker=mark,
                edgecolors="white",
                linewidths=0.45,
                alpha=0.9,
                zorder=3,
            )
        ax.set_aspect("equal")
        ax.set_axis_off()
    customers = pts[pts.TYPE == "customer"]
    x, y, xx, yy = customers.total_bounds
    pad = max(xx - x, yy - y, 100) * 0.07
    axes[1].set(xlim=(x - pad, xx + pad), ylim=(y - pad, yy + pad))
    axes[0].add_patch(
        Rectangle(
            (x - pad, y - pad),
            xx - x + 2 * pad,
            yy - y + 2 * pad,
            fill=False,
            color="#504e48",
            lw=0.9,
            linestyle="--",
        )
    )
    axes[0].set_title("City extent", loc="left")
    axes[1].set_title("Customer footprint", loc="left")
    fig.legend(
        *axes[0].get_legend_handles_labels(),
        loc="outside lower center",
        ncol=3,
        fontsize=9,
    )
    fig.suptitle(
        f"{city.replace('-', ' ').title()}  ·  Cus{info['customer_count']}", fontsize=16
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=180, bbox_inches="tight")
    plt.close(fig)
