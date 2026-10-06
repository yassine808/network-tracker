"""The dashboard: loads the self-contained HTML page that sits next to this module."""

from pathlib import Path

PAGE = Path(__file__).resolve().with_name("dashboard.html").read_text(encoding="utf-8")
