"""Export the road geometries and labels used by the public city maps."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

# Keep bare decimal OSM identifiers distinct from formatted telephone numbers.
_CONTACT_PATTERN = (
    r"website|contact|"
    r"[a-z0-9.!#$%&'*+/=?^_{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}|"
    r"(?:^|[^0-9])(?:\+1[ .-]?[0-9]{10}|(?:\+?1[ .-]?)?"
    r"(?:\([0-9]{3}\)[ .-]?[0-9]{3}[ .-]?[0-9]{4}|"
    r"[0-9]{3}[ .-][0-9]{3}[ .-]?[0-9]{4}|[0-9]{6}[ .-][0-9]{4}))"
    r"(?:$|[^0-9])"
)


def _geometry_metadata(schema):
    source = json.loads((schema.metadata or {})[b"geo"])
    geometry = source["columns"]["geometry"]
    if source["primary_column"] != "geometry" or geometry["encoding"] != "WKB":
        raise ValueError("Expected a GeoParquet WKB road geometry")
    attributes = {key: geometry[key] for key in ("encoding", "geometry_types")}
    if "crs" in geometry:
        attributes["crs"] = geometry["crs"]
    geo = {
        "version": "1.0.0",
        "primary_column": "geometry",
        "columns": {"geometry": attributes},
    }
    return {"geo": json.dumps(geo, sort_keys=True, separators=(",", ":"))}


def export_public_roads(source, target, expected_sha256):
    """Project a verified OSM way file into a new, deterministic GeoParquet."""
    target = Path(target)
    with Path(source).open("rb") as stream:
        before = os.fstat(stream.fileno())
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
            raise ValueError(f"Checksum mismatch: {source}")
        stream.seek(0)
        with pq.ParquetFile(stream) as roads:
            src = roads.schema_arrow
            metadata = _geometry_metadata(src)
            keep = ["osmid", "node_ids_json", "geometry"]
            schema = pa.schema(
                [src.field(name).remove_metadata() for name in keep]
                + [pa.field("highway", pa.string()), pa.field("name", pa.string())]
            )
            target.parent.mkdir(exist_ok=True, parents=True)
            with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
                pending = Path(scratch) / "roads.parquet"
                with pq.ParquetWriter(
                    pending, schema, compression="zstd", store_schema=False
                ) as writer:
                    writer.add_key_value_metadata(metadata)
                    for batch in roads.iter_batches(
                        batch_size=32_768,
                        columns=[*keep, "tags_json"],
                        use_threads=False,
                    ):
                        highways, names = [], []
                        for raw in batch.column("tags_json").to_pylist():
                            tags = json.loads(raw)
                            highways.append(tags.get("highway"))
                            names.append(tags.get("name"))
                        fields = [batch.column(name) for name in keep]
                        fields.append(pa.array(highways, pa.string()))
                        fields.append(pa.array(names, pa.string()))
                        public = pa.RecordBatch.from_arrays(fields, schema=schema)
                        for name in ("osmid", "node_ids_json", "highway", "name"):
                            hits = pc.match_substring_regex(
                                public.column(name), _CONTACT_PATTERN, ignore_case=True
                            )
                            if pc.any(hits).as_py():
                                raise ValueError(
                                    f"Contact text in public road column {name}"
                                )
                        writer.write_batch(public)

                # Both the digest and the export use the same open inode.
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError(f"Road source changed during export: {source}")
                os.link(pending, target)
