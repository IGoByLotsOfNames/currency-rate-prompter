"""Verify built resources and extract a source archive for standalone demo checks."""

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def check(distributions, destination):
    wheels = sorted(distributions.glob("*.whl"))
    archives = sorted(distributions.glob("*.tar.gz"))
    if len(wheels) != 1 or len(archives) != 1:
        raise ValueError("Expected one wheel and one source archive in the build directory.")
    root = Path(__file__).resolve().parents[1]
    expected = {
        path.relative_to(root / "src").as_posix()
        for path in (root / "src" / "currency_prompter").rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".html", ".css", ".js"}
    }
    with zipfile.ZipFile(wheels[0]) as wheel:
        if not expected <= set(wheel.namelist()):
            raise ValueError("Built wheel is missing application resources.")
        for name in expected:
            if wheel.read(name) != (root / "src" / name).read_bytes():
                raise ValueError(f"Built resource differs from the source: {name}")
    with tarfile.open(archives[0]) as archive:
        members = archive.getmembers()
        paths = [PurePosixPath(member.name) for member in members]
        if any(
            path.is_absolute() or ".." in path.parts or len(path.parts) < 1 for path in paths
        ) or any(not (member.isfile() or member.isdir()) for member in members):
            raise ValueError("Source archive contains unsafe paths or special files.")
        roots = {path.parts[0] for path in paths}
        if len(roots) != 1:
            raise ValueError("Source archive needs one root directory.")
        files = {
            PurePosixPath(*path.parts[1:]).as_posix(): member
            for path, member in zip(paths, members)
            if member.isfile()
        }
        required = {"scripts/demo_app.py", "tests/test_demo_launcher.py", "LICENSE"}
        required |= {"src/" + name for name in expected}
        if not required <= files.keys():
            raise ValueError("Source archive is missing standalone demo resources.")
        destination.mkdir(parents=True, exist_ok=False)
        for name, member in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream:
                target.write_bytes(stream.read())
    print(f"Verified {len(expected)} wheel resources and {len(files)} source files.")
    print(f"Extracted standalone demo to {destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distributions", type=Path)
    parser.add_argument("--extract-to", type=Path, required=True)
    args = parser.parse_args()
    check(args.distributions, args.extract_to)


if __name__ == "__main__":
    main()
