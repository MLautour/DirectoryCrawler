"""Synthetic hierarchy generator, for both integration tests and perf runs.

Usage as a script: `python tests/make_tree.py <dest> [--types N] [--assets N] [--variants N] [--file-size N]`
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def build_brief_example(dest: Path) -> dict[str, int]:
    """Characters/Dragon/{High,Low}, Props/Sword/Default, with known byte counts.

    Returns the expected byte count per leaf directory, for assertions.
    """
    layout = {
        ("Characters", "Dragon", "High"): [("diffuse_4k.exr", 4000), ("normal_4k.exr", 3000)],
        ("Characters", "Dragon", "Low"): [("diffuse_1k.exr", 500)],
        ("Props", "Sword", "Default"): [("sword.abc", 1200), ("sword_tex.exr", 800)],
    }
    expected: dict[str, int] = {}
    for parts, files in layout.items():
        d = dest.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        total = 0
        for name, size in files:
            (d / name).write_bytes(b"\0" * size)
            total += size
        expected["/".join(parts)] = total
    return expected


def build_archive_example(dest: Path) -> None:
    """A variant with a mixed-format ARCHIVE (dated folders in more than one
    naming convention, one unparsed name, one containing RSTEXBIN), a variant
    with an empty ARCHIVE, and a variant with no ARCHIVE at all -- exercises
    every case `archive.analyze()` needs to handle.
    """
    high = dest / "Characters" / "Dragon" / "High" / "ARCHIVE"
    (high / "2024-01-15" / "RSTEXBIN").mkdir(parents=True, exist_ok=True)
    (high / "2024-01-15" / "RSTEXBIN" / "tex.bin").write_bytes(b"a" * 100)
    (high / "2024-02-20").mkdir(parents=True, exist_ok=True)
    (high / "2024-02-20" / "notes.txt").write_bytes(b"b" * 10)
    (high / "2024_04_05").mkdir(parents=True, exist_ok=True)
    (high / "2024_04_05" / "notes.txt").write_bytes(b"c" * 10)
    (high / "misc_backup").mkdir(parents=True, exist_ok=True)
    (high / "misc_backup" / "notes.txt").write_bytes(b"d" * 10)

    low_archive = dest / "Characters" / "Dragon" / "Low" / "ARCHIVE"
    low_archive.mkdir(parents=True, exist_ok=True)

    default_variant = dest / "Props" / "Sword" / "Default"
    default_variant.mkdir(parents=True, exist_ok=True)
    (default_variant / "sword.abc").write_bytes(b"e" * 50)


def build_perf_tree(
    dest: Path,
    *,
    n_types: int = 5,
    n_assets: int = 20,
    n_variants: int = 3,
    n_files_per_variant: int = 20,
    file_size: int = 256,
) -> None:
    """Roughly n_types * n_assets * n_variants * n_files_per_variant files."""
    for t in range(n_types):
        type_dir = dest / f"Type{t:03d}"
        for a in range(n_assets):
            asset_dir = type_dir / f"Asset{a:03d}"
            for v in range(n_variants):
                variant_dir = asset_dir / f"Variant{v:03d}"
                variant_dir.mkdir(parents=True, exist_ok=True)
                for f in range(n_files_per_variant):
                    (variant_dir / f"file{f:04d}.dat").write_bytes(b"\0" * file_size)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--types", type=int, default=5)
    parser.add_argument("--assets", type=int, default=20)
    parser.add_argument("--variants", type=int, default=3)
    parser.add_argument("--files-per-variant", type=int, default=20)
    parser.add_argument("--file-size", type=int, default=256)
    args = parser.parse_args()

    args.dest.mkdir(parents=True, exist_ok=True)
    build_perf_tree(
        args.dest,
        n_types=args.types,
        n_assets=args.assets,
        n_variants=args.variants,
        n_files_per_variant=args.files_per_variant,
        file_size=args.file_size,
    )
    total_files = args.types * args.assets * args.variants * args.files_per_variant
    total_dirs = args.types * (1 + args.assets * (1 + args.variants))
    print(f"Built {total_files} files across ~{total_dirs} directories under {args.dest}")


if __name__ == "__main__":
    main()
