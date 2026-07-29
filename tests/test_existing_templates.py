"""Regression guard for the existing low-level YAML template library."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from test_manager import TestStep


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ExistingTemplateCompatibilityTests(unittest.TestCase):
    def test_every_existing_template_step_still_matches_runtime_schema(self):
        template_paths = sorted((PROJECT_ROOT / "templates").glob("*.yaml"))
        self.assertTrue(template_paths)

        step_count = 0
        for path in template_paths:
            with self.subTest(template=path.name):
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                steps = payload.get("steps") or []
                self.assertIsInstance(steps, list)
                pending = list(enumerate(steps, start=1))
                while pending:
                    index, step = pending.pop(0)
                    with self.subTest(template=path.name, step=index):
                        parsed = TestStep.model_validate(step)
                        step_count += 1
                    nested = (parsed.params or {}).get("steps")
                    if isinstance(nested, list):
                        pending.extend(
                            (f"{index}.{nested_index}", nested_step)
                            for nested_index, nested_step in enumerate(
                                nested, start=1
                            )
                        )

        self.assertGreater(step_count, 0)


if __name__ == "__main__":
    unittest.main()
