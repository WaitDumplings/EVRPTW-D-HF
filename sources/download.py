"""Verified cache of publisher responses for independent POSIX workers."""

import hashlib
import os
from pathlib import Path
import tempfile
import time

import requests

from stage2.common import lock, read, sha, write


def lock_applies(reference, key):
    if reference is None:
        return False
    return (
        reference.get("schema") != "evrptwd_live_inputs_v1"
        or key in reference["files"]
        or key.startswith(("afdc/", "nsi/"))
    )


class Store:
    def __init__(self, work, expected=None):
        self.root = Path(work) / "raw"
        self.expected = expected
        self.records = {}

    def fetch(self, key, url, known=None, *, params=None, post=None):
        if Path(key).is_absolute() or ".." in Path(key).parts:
            raise ValueError(f"Invalid cache key {key}")
        if lock_applies(self.expected, key):
            record = self.expected["files"].get(key)
            if (
                not record
                or record["url"] != url
                or (known and known != record["sha256"])
            ):
                raise ValueError(f"Source differs from supplied lock: {key}")
            known = record["sha256"]
        path = self.root / key
        meta = path.with_name(path.name + ".source.json")
        with lock(path.with_name(path.name + ".lock")):
            if meta.exists() and path.exists():
                record = read(meta)
                if (
                    record["url"] != url
                    or sha(path) != record["sha256"]
                    or (known and record["sha256"] != known)
                ):
                    raise ValueError(
                        f"Cached source differs: {key}. Use a fresh work directory for another vintage."
                    )
            elif path.is_file() and known is not None and sha(path) == known:
                record = {"url": url, "sha256": known, "bytes": path.stat().st_size}
                write(meta, record)
            else:
                for attempt in range(3):
                    try:
                        request = (
                            post()
                            if post
                            else requests.get(
                                url, params=params, stream=True, timeout=(30, 300)
                            )
                        )
                        with request as response:
                            if not response.ok:
                                if (
                                    response.status_code
                                    in (408, 429, 500, 502, 503, 504)
                                    and attempt < 2
                                ):
                                    response.close()
                                    time.sleep(2**attempt)
                                    continue
                                raise RuntimeError(
                                    f"{key}: publisher returned HTTP {response.status_code}"
                                )
                            if (
                                "text/html"
                                in getattr(response, "headers", {})
                                .get("Content-Type", "")
                                .lower()
                            ):
                                raise ValueError(
                                    f"{key}: expected source data, received HTML"
                                )
                            with tempfile.NamedTemporaryFile(
                                dir=path.parent, delete=False
                            ) as f:
                                tmp = Path(f.name)
                                try:
                                    h = hashlib.sha256()
                                    for chunk in response.iter_content(1 << 20):
                                        if chunk:
                                            h.update(chunk)
                                            f.write(chunk)
                                    f.flush()
                                    headers = getattr(response, "headers", {})
                                    length = headers.get("Content-Length")
                                    if (
                                        length is not None
                                        and headers.get(
                                            "Content-Encoding", "identity"
                                        ).lower()
                                        == "identity"
                                        and f.tell() != int(length)
                                    ):
                                        raise requests.exceptions.ChunkedEncodingError(
                                            "Incomplete publisher response"
                                        )
                                    checksum = h.hexdigest()
                                    if f.tell() == 0 or (known and checksum != known):
                                        raise ValueError(
                                            f"Empty or changed publisher response: {key}"
                                        )
                                    os.replace(tmp, path)
                                finally:
                                    tmp.unlink(missing_ok=True)
                        break
                    except requests.RequestException:
                        if attempt == 2:
                            raise RuntimeError(
                                f"{key}: download failed after three attempts"
                            ) from None
                        time.sleep(2**attempt)
                record = {"url": url, "sha256": checksum, "bytes": path.stat().st_size}
                write(meta, record)
        self.records[key] = record
        return path
