"""DESIGN.md stays true to the tree: its package layout names every module, and its decision list
names every decision record."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = (ROOT / "docs" / "DESIGN.md").read_text()
SOURCE = ROOT / "src" / "opencs"


def _section(heading: str) -> str:
    start = DESIGN.index(heading)
    nxt = re.search(r"^##+ ", DESIGN[start + len(heading) :], re.MULTILINE)
    return DESIGN[start : start + len(heading) + nxt.start()] if nxt else DESIGN[start:]


def _listed_modules() -> set[str]:
    """The module paths under ``src/opencs/`` in DESIGN §3.1's layout block."""
    block = re.search(r"```\nsrc/opencs/\n(.*?)\n(?=\S)", _section("### 3.1 Package layout"), re.S)
    assert block, "DESIGN §3.1 has no layout block starting at src/opencs/"
    listed: set[str] = set()
    folder = ""
    for line in block.group(1).splitlines():
        indent = len(line) - len(line.lstrip())
        name = line.split()[0]
        if indent == 2:
            if name.endswith("/"):
                folder = name
            else:
                folder = ""
                listed.add(name)
        else:
            listed.add(folder + name)
    return listed


def test_every_module_is_in_the_design_layout_and_every_listed_module_exists() -> None:
    on_disk = {
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*.py")
        if path.name != "__init__.py"
    }
    listed = _listed_modules()
    assert sorted(on_disk - listed) == [], "modules missing from DESIGN §3.1"
    assert sorted(listed - on_disk) == [], "DESIGN §3.1 lists modules that do not exist"


def test_every_decision_record_is_in_the_design_list() -> None:
    section = _section("## 9. Decision records")
    records = sorted(
        path.name for path in (ROOT / "docs" / "decisions").glob("[0-9][0-9][0-9][0-9]-*.md")
    )
    assert len(records) >= 12
    assert [r for r in records if f"decisions/{r})" not in section] == []
    index = (ROOT / "docs" / "decisions" / "README.md").read_text()
    assert [r for r in records if f"]({r})" not in index] == []
