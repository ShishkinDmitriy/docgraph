"""Thin wrapper around the `pdfinfo` binary (poppler-utils).

Returns the PDF's metadata dict — Title, Author, Pages, dates, etc. — without
introducing any RDF concerns. Caller decides how to map the fields to triples.

Returns an empty dict if pdfinfo isn't installed, the file isn't a PDF, or the
binary errors out for any reason. Never raises.
"""

import shutil
import subprocess
from pathlib import Path


def is_available() -> bool:
    return shutil.which("pdfinfo") is not None


def pdfinfo(path: Path, timeout: float = 15.0) -> dict[str, str]:
    """Run `pdfinfo -isodates <path>` and return its key/value output as a dict.

    -isodates makes CreationDate/ModDate ISO 8601 (e.g. "2020-04-01T12:00:00Z"),
    which round-trips into xsd:dateTime without further parsing.
    """
    if not is_available():
        return {}
    try:
        result = subprocess.run(
            ["pdfinfo", "-isodates", str(path)],
            capture_output=True, text=True, timeout=timeout, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}

    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out
