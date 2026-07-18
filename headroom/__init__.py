"""headroom — usage tracking, a live dashboard, and fail-closed routing."""

from pathlib import Path
import re
import sys


def _release_version():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    try:
        value = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("Headroom release VERSION is missing") from error
    if re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", value) is None:
        raise RuntimeError("Headroom release VERSION is invalid")
    return value


__version__ = _release_version()
