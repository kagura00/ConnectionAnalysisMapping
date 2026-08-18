"""Write a deterministic SHA-256 manifest for built release artifacts."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def artifact_paths(dist: Path, include_all: bool = False) -> list[Path]:
    if include_all:
        return sorted(
            (
                path
                for path in dist.iterdir()
                if path.is_file() and path.name != "SHA256SUMS.txt"
            ),
            key=lambda path: path.name,
        )
    paths = [
        path
        for path in dist.iterdir()
        if path.is_file()
        and path.name.startswith("connection_analysis_mapping-")
        and (path.name.endswith(".tar.gz") or path.name.endswith(".whl"))
    ]
    return sorted(paths, key=lambda path: path.name)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(dist: Path, output: Path, include_all: bool = False) -> tuple[Path, ...]:
    dist = dist.resolve()
    if not dist.is_dir():
        raise ValueError(f"distribution directory does not exist: {dist}")
    artifacts = artifact_paths(dist, include_all=include_all)
    if not artifacts:
        raise ValueError(f"no source archive or wheel found in: {dist}")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{sha256(path)}  {path.name}" for path in artifacts]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return tuple(artifacts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a SHA256SUMS file for release artifacts.")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--output", type=Path, default=Path("dist/SHA256SUMS.txt"))
    parser.add_argument(
        "--all-artifacts",
        action="store_true",
        help="include every regular file in the directory except SHA256SUMS.txt",
    )
    args = parser.parse_args()
    artifacts = write_manifest(args.dist, args.output, include_all=args.all_artifacts)
    print(f"wrote {args.output} ({len(artifacts)} artifacts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
