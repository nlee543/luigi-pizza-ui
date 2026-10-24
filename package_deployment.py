"""Package the Flask UI, templates, assets, and setup instructions into a ZIP.

Extract the archive, install requirements.txt, configure the API connection as
described in README.md, and run app.py. Python and the API are not bundled.
"""

import argparse
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_FILES = ("app.py", "requirements.txt", "requirements-production.txt", "gunicorn.conf.py", "README.md")
RUNTIME_DIRECTORIES = ("templates", "static", "deploy")
EXCLUDED_DIRECTORIES = {"__pycache__", "node_modules", "instance"}


def create_archive(output: Path) -> int:
    """Create a deployment archive and return the number of files included."""
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("The output filename must end in .zip")

    # An explicit runtime manifest avoids copying local credentials and sessions.
    sources = []
    for name in RUNTIME_FILES:
        source = PROJECT_ROOT / name
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Required deployment file is missing or symlinked: {name}")
        sources.append(source)
    for name in RUNTIME_DIRECTORIES:
        root = PROJECT_ROOT / name
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"Required deployment directory is missing or symlinked: {name}")
        for directory, subdirectories, filenames in os.walk(root):
            subdirectories[:] = sorted(
                child for child in subdirectories
                if not child.startswith(".")
                and child.lower() not in EXCLUDED_DIRECTORIES
                and not (Path(directory) / child).is_symlink()
            )
            for filename in sorted(filenames):
                source = Path(directory) / filename
                if (not filename.startswith(".")
                        and source.suffix.lower() not in {".pyc", ".pyo", ".log", ".zip"}
                        and not source.is_symlink()
                        and source.resolve() != output):
                    sources.append(source)

    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for source in sources:
            archive.write(source, source.relative_to(PROJECT_ROOT).as_posix())
    return len(sources)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o", "--output", type=Path,
        default=PROJECT_ROOT / "deployment.zip",
        help="ZIP destination (default: deployment.zip beside this script; replaces existing ZIP)",
    )
    args = parser.parse_args()
    if args.output.suffix.lower() != ".zip":
        parser.error("The output filename must end in .zip")
    count = create_archive(args.output)
    print(f"Created {args.output.resolve()} with {count} files.")


if __name__ == "__main__":
    main()
