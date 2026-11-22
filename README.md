---
configs:
- config_name: instances
  data_files:
  - split: catalog
    path: index.parquet
license: other
license_name: evrptw-d-mixed
license_link: LICENSE.md
pretty_name: EVRPTW-D
language: [en]
size_categories: [10K<n<100K]
tags: [vehicle-routing, evrptw, vrptw, cvrp, combinatorial-optimization, road-network]
---
# EVRPTW-D

EVRPTW-D is a benchmark of vehicle routing instances on real road networks in 11 U.S. cities. Every instance is a nested set of three problems on the same depot, customers and demands: CVRP, VRPTW and EVRPTW. Results on the three problems are therefore directly comparable.

The dataset has two parts:

- **City Environments** (`cities/`): the road network, locations and access data of each city. Use them to draw instances and routes on a map, or with the generator code to create further instances.
- **Routing Instances** (`instances/`, `index.parquet`): 17,400 ready-to-solve instances.

Solvers only need the Routing Instances. The construction method is described in the accompanying paper.

## Three problems in one instance

| Problem | Nodes | What it adds |
|---|---|---|
| CVRP | depot and n customers | vehicle capacity, with `shortest_distance_m` as cost |
| VRPTW | same nodes | time windows, service times, travel times and the working shift |
| EVRPTW | plus charging stations | battery capacity, energy use and charging |

Each level keeps everything from the level before it. All three minimize total road distance.

## Routing Instances

`index.parquet` has one row per instance. Filter by `city`, `split` and `n` (number of customers), then download the file given in `path`.

Each instance file has one row per node. Sort by `node_idx`: node 0 is the depot, nodes 1 to n are customers, the remaining nodes are charging stations.

Node columns: `location_id`, `type`, `lon`, `lat`, `demand_cm3`, `ready_s`, `due_s`, `service_s`, `charge_kw`.

Four matrix columns hold directed road costs. In each row, list element j is the cost from this node to node j:

| Column | Meaning |
|---|---|
| `shortest_distance_m` | Metres along the shortest-distance path. This is the objective. |
| `shortest_distance_time_s` | Seconds along the same path. |
| `fastest_time_distance_m` | Metres along the fastest path. |
| `fastest_time_s` | Seconds along the fastest path. |

Units: distance in metres, time in seconds from local midnight, demand and capacity in cubic centimetres, energy in kWh. Vehicle settings and solving rules are stored in the file metadata under the key `evrptwd.instance`.

```python
import json
import numpy as np
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

path = hf_hub_download("<REPO_ID>", "<path from index.parquet>", repo_type="dataset")
table = pq.read_table(path).sort_by("node_idx")
meta = json.loads(table.schema.metadata[b"evrptwd.instance"])["payload"]
D = np.array(table["shortest_distance_m"].to_pylist())  # D[i, j]: metres from node i to node j
vehicle = json.loads(meta["instance"]["vehicle_json"])
```

### Splits

Ten cities provide `train`, `val` and `test1` at n = 100, 500 and 1000. `xs` extends `test1` to n = 2000 on the same ten cities. `test2` contains instances from Jacksonville only, a city that appears in no other split.

| Split | n | Instances per city | Cities |
|---|---|---|---|
| `train` | 100 / 500 / 1000 | 1000 / 200 / 100 | 10 |
| `val` | 100 / 500 / 1000 | 30 each | 10 |
| `test1` | 100 / 500 / 1000 | 50 each | 10 |
| `xs` (test1 at n = 2000) | 2000 | 50 | 10 |
| `test2` | 100 / 500 / 1000 | 500 each | Jacksonville |

In `index.parquet`, `R` is the target candidate-to-customer ratio and `M` is the realized candidate count. The remaining index columns are sampling descriptors defined in the paper.

## City Environments

`cities/<city>/shared/` holds the frozen directed road network with weekday and weekend speeds, the eligible customer, depot and charging locations, their access to the road network, and the service boundary. `cities/<city>/roads/osm_ways.parquet` holds the road geometries for drawing streets and route shapes.

Each instance records the city environment it was built on in its file metadata. Checksums for all city files are in `release.json`.

Creating further instances also requires the generator code and order data prepared from the Amazon Last Mile Routing Research Challenge dataset, as described with the code.

## Sources and license

Built from OpenStreetMap, Microsoft building footprints, U.S. public data and the Amazon Last Mile Routing Research Challenge dataset. See [NOTICE.md](NOTICE.md).

Mixed license. OpenStreetMap-derived road networks, travel matrices and location coordinates are under ODbL 1.0. Order attributes, instance composition, the catalog and documentation are under CC BY-NC 4.0. A complete instance may be used for non-commercial purposes only. See [LICENSE.md](LICENSE.md).
