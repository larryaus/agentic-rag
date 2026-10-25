"""Inspect the synthesized function asset without loading target-native extensions."""

import importlib.metadata
import json
import stat
import sys
from pathlib import Path

PYTHON_312_MAGIC = (3531).to_bytes(2, "little") + b"\r\n"
out = Path(sys.argv[1]).resolve()
manifest = json.loads((out / "KbApiStack.assets.json").read_text())
assets = [
    out / entry["source"]["path"]
    for entry in manifest["files"].values()
    if (out / entry["source"]["path"] / "run.sh").is_file()
]
assert len(assets) == 1, f"Expected one function asset, found {len(assets)}"
asset = assets[0]
installed = {
    package.metadata["Name"].lower().replace("_", "-"): package.version
    for package in importlib.metadata.distributions(path=[str(asset)])
}
for line in Path("backend/requirements-aws.lock").read_text().splitlines():
    if line and not line[0].isspace() and not line.startswith("#"):
        name, version = line.split("==")
        assert installed[name] == version, f"Artifact dependency drift: {name}"
assert stat.S_IMODE((asset / "run.sh").stat().st_mode) == 0o755, (
    "run.sh must be mode 0755"
)
for package in ["asyncpg", "pgvector", "openai", "pypdf", "bs4"]:
    assert not (asset / package).exists(), f"Local-only dependency included: {package}"
native = list(asset.rglob("*.so"))
assert native, "Expected native Python dependencies"
for library in native:
    header = library.read_bytes()[:20]
    assert header[:4] == b"\x7fELF" and header[4:6] == b"\x02\x01", (
        f"Not a little-endian ELF64 library: {library}"
    )
    assert int.from_bytes(header[18:20], "little") == 183, f"Not aarch64: {library}"
sources = list(asset.rglob("*.py"))
for source in sources:
    bytecode = source.parent / "__pycache__" / f"{source.stem}.cpython-312.pyc"
    header = bytecode.read_bytes()[:8]
    # Lambda runs CPython 3.12, whatever interpreter runs this check.
    assert header[:4] == PYTHON_312_MAGIC, f"Not CPython 3.12 bytecode: {bytecode}"
    assert int.from_bytes(header[4:8], "little") == 1, (
        f"Expected unchecked-hash bytecode: {bytecode}"
    )
print(asset)
print(
    f"Artifact layout verified: mode 0755, AWS-only dependencies, {len(native)} aarch64 libraries, {len(sources)} hash-based bytecode files",
    file=sys.stderr,
)
