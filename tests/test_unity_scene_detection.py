"""Tests for scene detection from the Unity v2 debug API."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from unity_api_client import UnityAPIClient


class UnitySceneDetectionTests(unittest.TestCase):
    def test_combines_cheat_and_property_prefixes(self) -> None:
        client = UnityAPIClient(base_url="http://127.0.0.1:1")

        with (
            patch.object(
                client,
                "list_cheats_v2",
                return_value=[
                    {"Id": "debug.time.reset"},
                    {"Id": "common.stage.start"},
                ],
            ),
            patch.object(
                client,
                "list_properties_v2",
                return_value=[
                    {"Id": "debug.time_scale"},
                    {"Id": "ingame.player.invincible_state"},
                ],
            ),
        ):
            self.assertEqual(
                client.current_scene_prefixes_v2(),
                ["debug", "common", "ingame"],
            )

    def test_property_only_scene_signal_is_preserved(self) -> None:
        client = UnityAPIClient(base_url="http://127.0.0.1:1")

        with (
            patch.object(client, "list_cheats_v2", return_value=[]),
            patch.object(
                client,
                "list_properties_v2",
                return_value=[{"Id": "outgame.stage.current"}],
            ),
        ):
            self.assertEqual(client.current_scene_prefixes_v2(), ["outgame"])


if __name__ == "__main__":
    unittest.main()
