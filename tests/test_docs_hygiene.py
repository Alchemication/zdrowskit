"""Guards that keep prose from restating values the code owns.

Documentation drifts silently: nothing fails when a model identifier in a table
stops matching the routing that actually runs. These tests fail instead.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Route strings look like ``provider/model``. Matching the provider prefix
# catches renamed and version-bumped models without listing every identifier.
MODEL_ID_RE = re.compile(r"\b(?:anthropic|deepseek|openai|gemini)/[a-z0-9.\-]+")

# Files allowed to name a model. Eval invocations compare specific models
# against each other, so a placeholder would erase the point of the example.
MODEL_ID_ALLOWED = {"docs/commands.md", "docs/evals.md"}


def _prose_files() -> list[Path]:
    """Return the checked-in prose that users read."""
    files = sorted((REPO_ROOT / "docs").glob("*.md"))
    files.append(REPO_ROOT / "README.md")
    files.append(REPO_ROOT / ".env_example")
    return [path for path in files if path.is_file()]


class TestModelIdentifiersStayInCode:
    def test_prose_does_not_pin_model_identifiers(self) -> None:
        """Model IDs live in config.py; a copy in prose goes stale unnoticed."""
        offenders: list[str] = []
        for path in _prose_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in MODEL_ID_ALLOWED:
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                for match in MODEL_ID_RE.finditer(line):
                    offenders.append(f"{rel}:{number} {match.group(0)}")

        assert not offenders, (
            "Model identifiers found in prose:\n  "
            + "\n  ".join(offenders)
            + "\n\nRoutes change whenever an eval says they should, so a name "
            "here drifts silently. Describe the tier instead and point at "
            "`uv run python main.py models`."
        )

    def test_the_guard_detects_a_planted_identifier(self) -> None:
        """A clean pass must mean the pattern works, not that it matches nothing."""
        assert MODEL_ID_RE.search("route it to anthropic/claude-opus-5 today")
        assert MODEL_ID_RE.search("`openai/gpt-5.6-luna`")
        assert MODEL_ID_RE.search("deepseek/deepseek-v4-flash")
        assert not MODEL_ID_RE.search("https://github.com/BerriAI/litellm")
        assert not MODEL_ID_RE.search("use the flash tier")


class TestEnvExampleStaysCurrent:
    def test_documented_env_vars_exist_in_code(self) -> None:
        """An env var in prose that nothing reads is a promise the code broke."""
        declared: set[str] = set()
        for path in (REPO_ROOT / "src", REPO_ROOT / "evals"):
            for source in path.rglob("*.py"):
                declared.update(re.findall(r"ZDROWSKIT_[A-Z0-9_]+", source.read_text()))

        offenders: list[str] = []
        for path in _prose_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            for name in re.findall(r"ZDROWSKIT_[A-Z0-9_]+", path.read_text()):
                # Written as a placeholder for the per-feature family.
                if name == "ZDROWSKIT_":
                    continue
                if name not in declared:
                    offenders.append(f"{rel}: {name}")

        assert not offenders, (
            "Environment variables named in prose but read nowhere in the "
            "code:\n  " + "\n  ".join(sorted(set(offenders)))
        )
