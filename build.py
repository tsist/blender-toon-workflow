"""Create a deterministic Blender legacy add-on archive from the source registry."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parent / "toon_workflow"
manifest = (root / "bundle.json").read_bytes()
payload = {"bundle.json": manifest}
for path, expected in json.loads(manifest)["files"].items():
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in path:
        raise ValueError(f"Invalid source path: {path}")
    data = (root / relative).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Source SHA-256 differs: {path}")
    payload[path] = data
with ZipFile(args.output, "x") as archive:
    for path, data in sorted(payload.items()):
        archive.writestr(ZipInfo(f"toon_workflow/{path}"), data, compress_type=ZIP_DEFLATED)
with ZipFile(args.output) as archive:
    if archive.testzip() or any(archive.read(f"toon_workflow/{path}") != data for path, data in payload.items()):
        raise RuntimeError("Archive readback failed")
print(hashlib.sha256(args.output.read_bytes()).hexdigest(), args.output)
