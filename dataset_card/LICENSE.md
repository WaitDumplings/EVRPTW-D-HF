# License

EVRPTW-D is a mixed-license dataset. Different components are provided under different terms, listed below. This file is a summary; the linked license texts govern.

| Component | Content | License |
|---|---|---|
| `cities/` | Road networks, road geometries, locations and access data derived from OpenStreetMap, Microsoft building footprints and U.S. public sources | ODbL 1.0 |
| `instances/`: `shortest_distance_m`, `shortest_distance_time_s`, `fastest_time_distance_m`, `fastest_time_s`, `lon`, `lat` | Travel matrices computed on the OpenStreetMap-derived road network, and location coordinates from mixed sources that include OpenStreetMap | ODbL 1.0 |
| `instances/`: all other columns and file metadata | Order attributes (demand, time windows, service time), instance selection and composition | CC BY-NC 4.0 |
| `index.parquet`, `release.json`, `README.md` and other top-level files | Catalog, release metadata and documentation | CC BY-NC 4.0 |

A complete instance combines both kinds of content. Using an instance as a whole is therefore limited to non-commercial purposes under CC BY-NC 4.0. The ODbL-licensed parts may be extracted and used on their own under ODbL 1.0.

## Attribution

- Road and map data © OpenStreetMap contributors, available under the Open Database License. https://www.openstreetmap.org/copyright
- Order attributes are derived, with modifications, from the 2021 Amazon Last Mile Routing Research Challenge Dataset, licensed under CC BY-NC 4.0.
- Customer locations are derived in part from Microsoft GlobalMLBuildingFootprints (2026-08-13 release), provided by Microsoft under CDLA-Permissive-2.0 as stated in the source repository. https://github.com/microsoft/GlobalMLBuildingFootprints
- U.S. Census Bureau, EPA, USACE National Structure Inventory and AFDC/NLR data are used as listed in `NOTICE.md`.

## License texts

- ODbL 1.0: https://opendatacommons.org/licenses/odbl/1-0/
- CC BY-NC 4.0: https://creativecommons.org/licenses/by-nc/4.0/
- CDLA-Permissive-2.0: https://cdla.dev/permissive-2-0/

Copyright © 2026 The EVRPTW-D authors, for original contributions.
