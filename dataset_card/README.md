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

- **City Environments** (`cities/`): the road network, locations and access data of each city. Use them to draw instances and routes on a map, or to recompute travel costs.
- **Routing Instances** (`instances/`, `index.parquet`): 17,400 ready-to-solve instances.

Solvers only need the Routing Instances.

## Three problems in one instance

| Problem | Nodes | What it adds |
|---|---|---|
| CVRP | depot and n customers | vehicle capacity, with `shortest_distance_m` as cost |
| VRPTW | same nodes | time windows, service times, travel times and the working shift |
| EVRPTW | plus charging stations | battery capacity, energy use and charging |

Each level keeps everything from the level before it. All three minimize total road distance.

## How the data was built

Each city environment starts from the OpenStreetMap road network, turned into a directed graph with weekday and weekend travel speeds. Candidate customer locations come from building footprints and structure records, depots from mapped logistics facilities, and charging stations from the public AFDC station list.

An instance fixes a city, a service date and a depot, then samples customers from the candidate locations around it and selects charging stations. Demands, service times and time windows follow an order model built on the Amazon Last Mile Routing Research Challenge dataset. Travel distances and times between all nodes are computed on the frozen road network. Instances are sampled subject to customer availability and a common construction protocol.

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

| Split | Purpose | n | Instances per city | Cities |
|---|---|---|---|---|
| `train` | training | 100 / 500 / 1000 | 1000 / 200 / 100 | 10 |
| `val` | model selection | 100 / 500 / 1000 | 30 each | 10 |
| `test1` | new instances in the training cities | 100 / 500 / 1000 | 50 each | 10 |
| `xs` | transfer to a larger scale in the training cities | 2000 | 50 | 10 |
| `test2` | transfer to an unseen city | 100 / 500 / 1000 | 500 each | Jacksonville |

`xs` and `test2` use their own instances. Jacksonville appears in no other split.

### Index columns

| Column | Meaning |
|---|---|
| `city` | City containing the instance. |
| `split` | Dataset split assigned to the instance. |
| `n` | Number of customers. |
| `cs_budget` | Number of charging stations allocated to the instance. |
| `instance_id` | Unique 24-character hexadecimal instance identifier. |
| `path` | Path to the instance Parquet file relative to the dataset root. |
| `bytes` | Instance file size in bytes for integrity checks. |
| `sha256` | SHA-256 checksum of the complete instance file. |
| `service_date` | Calendar date assigned to the instance. |
| `day_type` | Weekday or weekend, determined by the service date. |
| `depot_id` | Location identifier of the depot. |
| `p` | Modeled probability that a household has a delivery on the service date. |
| `K_seed` | Number of starting census tracts sampled for the service footprint. |
| `K_geo` | Number of connected components among the selected census tracts. |
| `R` | target candidate-to-customer ratio |
| `M_target` | Target candidate count, equal to n multiplied by R. |
| `M` | realized candidate count |
| `tract_count` | Number of census tracts in the selected service footprint. |
| `content_sha256` | SHA-256 checksum of instance metadata, city binding and solving rules. |
| `solver_input_sha256` | SHA-256 checksum covering node attributes, matrix checksums, vehicle settings, path policy and objective. |

## City Environments

`cities/<city>/shared/` holds the frozen directed road network with weekday and weekend speeds, the eligible customer, depot and charging locations, their access to the road network, and the service boundary. `cities/<city>/roads/osm_ways.parquet` holds the road geometries for drawing streets and route shapes.

Each instance records the city environment it was built on in its file metadata. Checksums for all city files are in `release.json`.

The generator code is not part of this release and will be published separately.

## Sources and license

Built from OpenStreetMap, Microsoft building footprints, U.S. public data and the Amazon Last Mile Routing Research Challenge dataset. See [NOTICE.md](NOTICE.md).

Mixed license. OpenStreetMap-derived road networks, travel matrices and location coordinates are under ODbL 1.0. Order attributes, instance composition, the catalog and documentation are under CC BY-NC 4.0. A complete instance may be used for non-commercial purposes only. See [LICENSE.md](LICENSE.md).
