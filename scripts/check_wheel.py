"""Exercise the built wheel away from the checkout, with no paid provider calls."""

import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def extract_wheel(archive: ZipFile, destination: Path) -> None:
    """Extract a wheel only when every member stays inside the disposable directory."""

    root = destination.resolve()
    for member in archive.infolist():
        # ELI5: a crafted archive must not use absolute paths or '..' to write outside the temp folder.
        target = (root / member.filename).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"Wheel member escapes extraction directory: {member.filename}")
        # ELI5: do not allow an archive symlink to redirect later files outside the checked directory.
        if stat.S_ISLNK(member.external_attr >> 16):
            raise ValueError(f"Wheel member is a symlink: {member.filename}")
        archive.extract(member, root)


def main() -> None:
    """Prove the installable artifact carries its templates, assets, cases and archive."""
    # ELI5: this checker accepts exactly one wheel so a missing artifact fails before extraction.
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/check_wheel.py path/to/project.whl")
    # ELI5: resolve and verify the path before opening it, rather than silently checking another file.
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with TemporaryDirectory(prefix="proofline-wheel-") as directory:
        # ELI5: unpack into a disposable directory so imports cannot fall back to this checkout.
        with ZipFile(wheel) as archive:
            extract_wheel(archive, Path(directory))
        # A separate interpreter cannot accidentally reuse app modules from the source tree.
        subprocess.run([sys.executable, "-c", '''
from pathlib import Path
from fastapi.testclient import TestClient
import app
from app.main import create_app
from app.presentation import recorded_evidence
from app.simulation import simulation_settings
assert Path(app.__file__).resolve().is_relative_to(Path.cwd())
assert recorded_evidence()["application"] == "PASS"
with TestClient(create_app(simulation_settings(Path("storage")))) as client:
    assert "A repair is" in client.get("/").text
    assert "A real run." in client.get("/evidence").text
    assert client.get("/cases").status_code == 200
    for asset in ("app.css", "app.js", "favicon.svg", "favicon.ico", "social.png"):
        assert client.get("/static/" + asset).status_code == 200, asset
print("PASS: isolated wheel serves landing, archive, cases, CSS, JS and identity assets.")
'''], cwd=directory, env={**os.environ, "PYTHONPATH": directory}, check=True)


if __name__ == "__main__":
    main()
