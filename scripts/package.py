"""Build a HACS ZIP and a manual-install ZIP from public integration files."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
component = root / "custom_components" / "aperture"
output = root / "dist"
output.mkdir(exist_ok=True)
files = sorted(
    p
    for p in component.rglob("*")
    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
)
for name, manual in [("aperture.zip", False), ("aperture-manual.zip", True)]:
    with ZipFile(output / name, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root if manual else component))
        for document in ("LICENSE", "NOTICE", "README.md"):
            archive.write(
                root / document,
                f"custom_components/aperture/{document}" if manual else document,
            )
    print(f"Built {name}: {len(files)} files")
