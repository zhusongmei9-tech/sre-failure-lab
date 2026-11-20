"""Catch stale configuration links and malformed shell examples in the public tutorial."""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs/from-zero").glob("*.md"))]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_guide_links_and_shell_syntax(page):
    markdown = page.read_text(encoding="utf-8")
    fences = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
    for block in fences.finditer(markdown):
        if block[1].strip() in {"bash", "sh"}:
            result = subprocess.run(
                ["bash", "-n"], input=block[2], text=True, capture_output=True, check=False
            )
            assert result.returncode == 0, f"{page.name}: {result.stderr}"
    prose = fences.sub("", markdown)
    for target in re.findall(r"\]\(([^)\s]+)\)", prose):
        url = urlsplit(target)
        if not url.scheme and not url.netloc and url.path:
            assert (page.parent / unquote(url.path)).exists(), f"{page.name}: {target}"
