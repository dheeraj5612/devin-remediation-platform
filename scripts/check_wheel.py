"""Exercise the built wheel away from the checkout, with no paid provider calls."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def main() -> None:
    """Prove the installable artifact carries its templates, assets, cases and archive."""
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/check_wheel.py path/to/project.whl")
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with TemporaryDirectory(prefix="proofline-wheel-") as directory:
        with ZipFile(wheel) as archive:
            archive.extractall(directory)
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
