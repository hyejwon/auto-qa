from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from planner_context import (
    build_template_library_text,
    select_template_contexts,
)


class PlannerContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.template_dir = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _write_template(
        self,
        filename: str,
        *,
        package: str = "",
        title: str,
        description: str = "",
    ) -> Path:
        path = self.template_dir / filename
        path.write_text(
            yaml.safe_dump(
                {
                    "title": title,
                    "description": description,
                    "package": package,
                    "steps": [
                        {
                            "action": "verify",
                            "target": title,
                        }
                    ],
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_exact_package_is_preferred_over_more_relevant_generic(self) -> None:
        exact = self._write_template(
            "exact.yaml",
            package="com.example.target",
            title="설정 화면 확인",
        )
        self._write_template(
            "generic.yaml",
            title="보스 웨이브 전투 시작",
        )

        selected = select_template_contexts(
            self.template_dir,
            package="com.example.target",
            scenario="보스 웨이브 전투 시작",
            limit=1,
        )

        self.assertEqual([item.path for item in selected], [exact])
        self.assertGreaterEqual(selected[0].score, 1000)

    def test_templates_for_unrelated_packages_are_excluded(self) -> None:
        matching = self._write_template(
            "matching.yaml",
            package="com.example.target",
            title="스테이지 진입",
        )
        self._write_template(
            "unrelated.yaml",
            package="com.example.other",
            title="스테이지 진입",
        )
        generic = self._write_template(
            "generic.yaml",
            title="스테이지 진입 공통 처리",
        )

        selected = select_template_contexts(
            self.template_dir,
            package="com.example.target",
            scenario="스테이지 진입",
            limit=5,
        )

        selected_paths = {item.path for item in selected}
        self.assertIn(matching, selected_paths)
        self.assertIn(generic, selected_paths)
        self.assertNotIn(self.template_dir / "unrelated.yaml", selected_paths)

    def test_selection_respects_limit(self) -> None:
        for index in range(5):
            self._write_template(
                f"exact_{index}.yaml",
                package="com.example.target",
                title=f"검증 흐름 {index}",
            )

        selected = select_template_contexts(
            self.template_dir,
            package="com.example.target",
            scenario="검증",
            limit=2,
        )

        self.assertEqual(len(selected), 2)

    def test_only_relevant_generic_templates_are_selected_when_available(
        self,
    ) -> None:
        login = self._write_template(
            "login.yaml",
            title="게스트 로그인 성공 검증",
        )
        self._write_template(
            "purchase.yaml",
            title="상점 상품 결제 검증",
        )

        selected = select_template_contexts(
            self.template_dir,
            package="com.example.unknown",
            scenario="게스트 로그인",
            limit=5,
        )

        self.assertEqual([item.path for item in selected], [login])
        self.assertGreater(selected[0].score, 0)

    def test_generic_templates_are_fallback_when_none_are_relevant(self) -> None:
        first = self._write_template(
            "a_first.yaml",
            title="상점 상품 결제",
        )
        self._write_template(
            "b_second.yaml",
            title="게스트 로그인",
        )

        selected = select_template_contexts(
            self.template_dir,
            package="com.example.unknown",
            scenario="은하 항법 장치",
            limit=1,
        )

        self.assertEqual([item.path for item in selected], [first])
        self.assertEqual(selected[0].score, 0)

    def test_game_note_is_rendered_even_without_templates(self) -> None:
        output = build_template_library_text(
            self.template_dir,
            package="com.example.target",
            scenario="스테이지 진입",
            game_note="  하단 전투 탭은 세 번째입니다.  ",
        )

        self.assertEqual(
            output,
            "### 게임 UI 참고 정보 (반드시 준수)\n"
            "하단 전투 탭은 세 번째입니다.",
        )


if __name__ == "__main__":
    unittest.main()
