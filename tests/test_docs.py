"""Documentation layout, local links and agent command descriptions."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def test_docs_root_contains_only_directories():
    assert all(entry.is_dir() for entry in DOCS.iterdir())
    assert {
        "ANALYZE.md",
        "COVERAGE.md",
        "DIAGNOSTICS.md",
        "FEEDBACK.md",
        "PERFORMANCE.md",
        "PERFORMANCE-RUNTIME.md",
        "PLAN.md",
    } <= {path.name for path in (DOCS / "guides").glob("*.md")}


def test_documentation_local_links_exist():
    for document in [ROOT / "README.md", *DOCS.rglob("*.md")]:
        content = document.read_text(encoding="utf-8")
        for match in re.finditer(r"\[[^\]\n]+\]\(([^)\n]+)\)", content):
            target = urlsplit(match[1])
            if target.scheme or target.netloc or not target.path:
                continue
            path = document.parent / unquote(target.path)
            assert path.exists(), f"Broken local link in {document.relative_to(ROOT)}: {match[1]}"


@pytest.mark.parametrize("name", ["devkit-rules.md", "SKILL.md", "AGENTS.fragment.md"])
def test_analysis_templates_describe_capabilities_without_tool_preference(name):
    content = (DOCS / "agents" / name).read_text(encoding="utf-8")
    assert "analyze <file>" in content
    assert "query <pattern>" in content
    assert "performance database --input <capture.json>" in content
    for command in ("runtime", "memory", "cache"):
        assert f"performance {command} --input <capture" in content
    assert "performance compare --before <capture> --after <capture>" in content
    assert "--fail-on-regression" in content
    assert "plan archive <conversation-id>" in content
    assert ".gemini/antigravity-ide/brain/<conversation-id>/" in content
    assert ".gemini/antigravity/brain/" not in content
    for command in ("routes", "isolation", "security", "dependencies", "unused", "duplicates"):
        assert f"audit {command} [path]" in content
    assert "--symbol" in content and "--with-context" in content
    assert not re.search(
        r"\b(?:prefer|must use|always use)\b[^.\n]*\b(?:analyze|query|audit|performance)\b",
        content,
        re.IGNORECASE,
    )
