"""Interactive repository installer used by release media."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .distribution import DistributionError, _install_core_from_info, inspect_archive
from .scaffold import initialize_target

EMBEDDED_ARCHIVE_NAME = "installer-core.tar.gz"
MIN_PYTHON = (3, 11)


class InstallerError(ValueError):
    """Raised when an installer target or payload is not usable."""


def _is_cli_executable(program: str | None = None) -> bool:
    name = Path(program or sys.argv[0]).stem.lower()
    return name.endswith(("-cli", "_cli"))


def _embedded_archive() -> Path | None:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "connection_map" / EMBEDDED_ARCHIVE_NAME)
        candidates.append(Path(meipass) / EMBEDDED_ARCHIVE_NAME)
    candidates.append(Path(__file__).resolve().with_name(EMBEDDED_ARCHIVE_NAME))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _expand_path(value: str | Path) -> Path:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


def _select_cli_target() -> Path | None:
    print("Connection Analysis Mapping インストーラー")
    print("対象リポジトリのパスを入力してください。")
    try:
        value = input("対象パス: ").strip()
    except EOFError:
        return None
    return _expand_path(value) if value else None


def _select_windows_target() -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print("フォルダー選択画面を利用できないため、CUIへ切り替えます。")
        return _select_cli_target()

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askdirectory(
            title="Connection Analysis Mappingの対象リポジトリを選択",
            initialdir=str(Path.cwd()),
            mustexist=True,
        )
        return _expand_path(selected) if selected else None
    except tk.TclError as exc:
        print(f"フォルダー選択画面を開けませんでした: {exc}")
        return _select_cli_target()
    finally:
        if root is not None:
            root.destroy()


def _confirm_target(target: Path, yes: bool) -> bool:
    if yes:
        return True
    print(f"対象リポジトリ: {target}")
    if not (target / ".git").exists():
        print("警告: .gitが見つかりません。Gitリポジトリではない可能性があります。")
    if (target / ".connection-map").exists():
        print("既存の .connection-map を検出しました。coreを更新します。")
    try:
        answer = input("この場所へインストールしますか？ [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connection-map-install",
        description="Install Connection Analysis Mapping into an existing repository.",
    )
    parser.add_argument("--target", type=Path, help="target repository path")
    parser.add_argument("--archive", type=Path, help="source archive; omitted for bundled installers")
    parser.add_argument("--install-dir", default=".connection-map", help="single directory inside target")
    parser.add_argument("--cli", action="store_true", help="use the text interface")
    parser.add_argument("--yes", action="store_true", help="skip target confirmation")
    parser.add_argument(
        "--version",
        action="version",
        version="connection-map-install (core version is read from the embedded archive)",
    )
    return parser


def install_target(target: Path, archive: Path, install_dir: str) -> int:
    """Initialize the target and install the bundled core archive."""

    if not target.is_dir():
        raise InstallerError(f"target repository directory does not exist: {target}")
    # Validate the payload before creating any target files.  A failed or
    # tampered archive must leave a first-time installation untouched.
    archive_info = inspect_archive(archive)
    initialize_target(target, install_dir)
    result = _install_core_from_info(target, archive_info, install_dir)
    print(f"installed core {result.version} at {result.core_path}")
    print(f"archive sha256: {result.sha256}")
    if result.backup_path is not None:
        print(f"previous core backed up at {result.backup_path}")
    print(f"target: {target}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cli_mode = args.cli or _is_cli_executable()
    try:
        archive = _expand_path(args.archive) if args.archive else _embedded_archive()
        if archive is None:
            raise InstallerError(
                "bundled source archive was not found; pass --archive or use a release installer"
            )

        if args.target is not None:
            target = _expand_path(args.target)
        elif cli_mode or os.name != "nt":
            target = _select_cli_target()
        else:
            target = _select_windows_target()
        if target is None:
            print("インストールをキャンセルしました。")
            return 1
        if not _confirm_target(target, args.yes):
            print("インストールをキャンセルしました。")
            return 1
        return install_target(target, archive, args.install_dir)
    except (DistributionError, InstallerError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nインストールを中断しました。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
