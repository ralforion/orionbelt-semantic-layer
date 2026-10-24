"""Install the ADBC MySQL driver for ``ob_mysql``, pinned and checksummed.

The driver (ADBC Driver Foundry, Apache-2.0) is not on PyPI. ``dbc install
mysql`` installs the latest release; the Docker images take this pinned one
instead, verified against the checksum GitHub publishes for the asset.

    python scripts/install_adbc_mysql.py /opt/adbc

writes ``/opt/adbc/lib/libadbc_driver_mysql.so``, with its LICENSE and NOTICE,
and the manifest ``/opt/adbc/drivers/mysql.toml``; set
``ADBC_DRIVER_PATH=/opt/adbc/drivers`` so the ADBC driver manager finds it.
Linux only (amd64, arm64).
"""

from __future__ import annotations

import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.7.0"
RELEASE = f"https://github.com/adbc-drivers/mysql/releases/download/go/v{VERSION}"
SHA256 = {
    "amd64": "5800af8ac8158895d16361ff8c7338ec62c980ffb7ca5921aa59719d2af024b2",
    "arm64": "b646f810dfcc8ce0a33bd6e5bdceeeb6f6669d518478b9c47fa2e34be4124520",
}
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
LIBRARY = "libadbc_driver_mysql.so"


def install(dest: Path) -> Path:
    arch = ARCH[platform.machine().lower()]
    asset = f"mysql_linux_{arch}_v{VERSION}.tar.gz"
    with urllib.request.urlopen(f"{RELEASE}/{asset}", timeout=120) as response:
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256[arch]:
        raise SystemExit(f"{asset}: sha256 {digest}, expected {SHA256[arch]}")

    library = dest / "lib" / LIBRARY
    library.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        # The library, and the Apache-2.0 LICENSE and NOTICE it ships under.
        for name, target in (
            (LIBRARY, library),
            ("LICENSE", library.parent / "LICENSE.adbc-driver-mysql"),
            ("NOTICE", library.parent / "NOTICE.adbc-driver-mysql"),
        ):
            member = archive.extractfile(name)
            assert member is not None, f"{asset} has no {name}"
            target.write_bytes(member.read())

    manifest = dest / "drivers" / "mysql.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "manifest_version = 1\n"
        "name = 'ADBC Driver Foundry Driver for MySQL'\n"
        "license = 'Apache-2.0'\n"
        f"version = '{VERSION}'\n"
        "\n"
        "[ADBC]\n"
        "version = '1.1.0'\n"
        "\n"
        "[Driver]\n"
        "[Driver.shared]\n"
        f"linux_{arch} = '{library}'\n"
    )
    return manifest


if __name__ == "__main__":
    print(install(Path(sys.argv[1])))
