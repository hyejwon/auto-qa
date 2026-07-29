"""Small, deterministic context retrieval helpers for the legacy planner."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


@dataclass(frozen=True)
class TemplateContext:
    path: Path
    package: str
    score: int
    body: str


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(value or "")}


def select_template_contexts(
    template_dir: Path,
    *,
    package: str = "",
    scenario: str = "",
    limit: int = 5,
) -> list[TemplateContext]:
    """Select only package-compatible, scenario-relevant templates.

    Exact-package templates are preferred. Generic templates (empty package)
    are considered only when their title/description/step text overlaps the
    scenario, or when no exact-package template exists.
    """

    wanted_package = package.strip()
    scenario_tokens = _tokens(scenario)
    exact: list[TemplateContext] = []
    generic: list[TemplateContext] = []

    for path in sorted(template_dir.glob("*.yaml")):
        try:
            body = path.read_text(encoding="utf-8").strip()
            data = yaml.safe_load(body) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue

        item_package = str(data.get("package") or "").strip()
        if item_package and item_package != wanted_package:
            continue

        searchable = " ".join(
            [
                path.stem,
                str(data.get("title") or ""),
                str(data.get("description") or ""),
                body,
            ]
        ).lower()
        overlap = sum(
            max(1, min(len(token), 12))
            for token in scenario_tokens
            if token in searchable
        )
        if item_package == wanted_package and wanted_package:
            exact.append(
                TemplateContext(
                    path=path,
                    package=item_package,
                    score=1000 + overlap,
                    body=body,
                )
            )
        elif not item_package:
            generic.append(
                TemplateContext(
                    path=path,
                    package="",
                    score=overlap,
                    body=body,
                )
            )

    exact.sort(key=lambda item: (-item.score, item.path.name))
    generic.sort(key=lambda item: (-item.score, item.path.name))

    selected = list(exact[:limit])
    remaining = max(0, limit - len(selected))
    if remaining:
        relevant_generic = [item for item in generic if item.score > 0]
        if not exact and not relevant_generic:
            relevant_generic = generic
        selected.extend(relevant_generic[:remaining])
    return selected


def build_template_library_text(
    template_dir: Path,
    *,
    package: str = "",
    scenario: str = "",
    limit: int = 5,
    game_note: str = "",
) -> str:
    parts: list[str] = []
    if game_note.strip():
        parts.append(f"### 게임 UI 참고 정보 (반드시 준수)\n{game_note.strip()}")
    for item in select_template_contexts(
        template_dir,
        package=package,
        scenario=scenario,
        limit=limit,
    ):
        parts.append(f"### {item.path.stem}\n```yaml\n{item.body}\n```")
    return "\n\n".join(parts)
