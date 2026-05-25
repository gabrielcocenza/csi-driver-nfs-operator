#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""Update the vendored csi-driver-nfs Helm chart in upstream/charts/.

Usage:
    python3 upstream/update.py [--version VERSION]

The script fetches the specified chart version from the upstream Helm
repository and stores the .tgz in upstream/charts/, replacing any
previously vendored version.  A plain-text upstream/version file is
written so that the pinned version is visible without unpacking the archive.

Example:
    python3 upstream/update.py --version 4.13.2
    tox -e update -- --version 4.14.0
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

CHART_NAME = "csi-driver-nfs"
CHART_REPO = "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts"
DEFAULT_VERSION = "4.13.2"

BASE_DIR = Path(__file__).parent
CHART_DIR = BASE_DIR / "charts"
VERSION_FILE = BASE_DIR / "version"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help=f"Chart version to fetch (default: {DEFAULT_VERSION})",
    )
    return parser.parse_args()


def check_requirements() -> None:
    """Ensure required external tools are available on PATH."""
    missing = [tool for tool in ("helm",) if shutil.which(tool) is None]
    if missing:
        print(f"ERROR: missing required tool(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def remove_old_charts() -> None:
    """Remove any previously vendored chart archives."""
    old = list(CHART_DIR.glob(f"{CHART_NAME}-*.tgz"))
    for path in old:
        print(f"Removing old chart: {path.name}")
        path.unlink()


def fetch_chart(version: str) -> None:
    """Download the requested chart version using helm pull."""
    print(f"Fetching {CHART_NAME} chart version {version} ...")
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "helm",
            "pull",
            CHART_NAME,
            "--repo",
            CHART_REPO,
            "--version",
            version,
            "--destination",
            str(CHART_DIR),
        ],
        check=True,
    )
    fetched = list(CHART_DIR.glob(f"{CHART_NAME}-*.tgz"))
    if not fetched:
        print("ERROR: helm pull succeeded but no .tgz found in charts/", file=sys.stderr)
        sys.exit(1)
    print(f"Chart saved: {fetched[0].name}")


def write_version(version: str) -> None:
    """Record the pinned version in a plain-text file."""
    VERSION_FILE.write_text(f"{version}\n")
    print(f"Version recorded: {VERSION_FILE}")


def main() -> None:
    """Entry point."""
    args = parse_args()
    check_requirements()
    remove_old_charts()
    fetch_chart(args.version)
    write_version(args.version)
    print("Done.")


if __name__ == "__main__":
    main()
