"""Check that built distributions contain current runtime and verification files."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path


def source_files(root: Path) -> list[Path]:
    files = [root / name for name in (
        "LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "uv.lock", "MANIFEST.in",
    )]
    patterns = {
        "docs": {".md"}, "examples": {".json", ".toml"}, "schemas": {".json"},
        "scripts": {".py"}, "src/connection_map": {".py", ".html", ".js", ".css"},
        "tests": {".py", ".cjs"}, "tests/fixtures": None, "tests/completeness": None,
    }
    for directory, suffixes in patterns.items():
        for path in (root / directory).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if suffixes is None or path.suffix in suffixes:
                files.append(path)
    return sorted(set(files))


def verify_distribution(root: Path, dist: Path) -> tuple[int, int]:
    sources = list(dist.glob("*.tar.gz"))
    wheels = list(dist.glob("*.whl"))
    if len(sources) != 1 or len(wheels) != 1:
        raise ValueError("expected exactly one source archive and one wheel")
    expected = source_files(root)
    with tarfile.open(sources[0], "r:gz") as archive:
        payloads = {}
        for member in archive.getmembers():
            if member.isfile():
                relative = member.name.partition("/")[2]
                content = archive.extractfile(member)
                if content is not None:
                    payloads[relative] = content.read()
    problems = [path.relative_to(root).as_posix() for path in expected
                if payloads.get(path.relative_to(root).as_posix()) != path.read_bytes()]
    if problems:
        raise ValueError("source archive has missing or outdated files: " + ", ".join(problems))

    runtime = [path for path in expected if path.is_relative_to(root / "src/connection_map")]
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        problems = [path.relative_to(root / "src").as_posix() for path in runtime
                    if path.relative_to(root / "src").as_posix() not in names
                    or archive.read(path.relative_to(root / "src").as_posix()) != path.read_bytes()]
    if problems:
        raise ValueError("wheel has missing or outdated files: " + ", ".join(problems))
    return len(expected), len(runtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    source_count, runtime_count = verify_distribution(Path(__file__).resolve().parents[1], args.dist)
    print(f"distribution contents verified: source={source_count}, wheel={runtime_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
