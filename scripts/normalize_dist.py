"""Normalize source archives after ``uv build`` for reproducible release output."""

from __future__ import annotations

import argparse
import copy
import gzip
import io
import tarfile
from pathlib import Path


def _normalise_source_archive(path: Path, epoch: int) -> None:
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(path, "r:gz") as archive:
        for original in archive.getmembers():
            member = copy.copy(original)
            member.mtime = epoch
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.pax_headers = {}
            data = archive.extractfile(original).read() if original.isfile() else None
            members.append((member, data))

    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
                for member, data in sorted(members, key=lambda item: item[0].name):
                    output.addfile(member, io.BytesIO(data) if data is not None else None)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--source-date-epoch", type=int, required=True)
    args = parser.parse_args()
    for archive in sorted(args.dist.glob("*.tar.gz")):
        _normalise_source_archive(archive, args.source_date_epoch)
        print(f"normalized {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
