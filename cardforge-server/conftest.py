# ruff: noqa: E501
"""Folder 2: the shared core tests (`tests/`, synced from the core) run here unchanged, except
the few that check folder 1's own files (plugin manifests, installer, `build_dist.py`), which
this folder does not carry. They are deselected by node id, so a new core test runs here by
default. Server tests are in `tests_server/`.
"""

from __future__ import annotations

FOLDER_1_ONLY = {
    "tests/test_plugin.py::test_plugin_manifests_and_skill": "plugin/ манифесты папки 1",
    "tests/test_plugin.py::test_codex_and_claude_agent_adapters_are_in_sync": "plugin/ папки 1",
    "tests/test_plugin.py::test_build_dist_has_no_customer_data_and_is_rebuildable": "scripts/build_dist.py папки 1",
    "tests/test_plugin.py::test_installer_dry_run": "installer/ папки 1",
    "tests/test_plugin.py::test_double_click_install_finds_assets_and_agents": "installer/ папки 1",
    "tests/test_plugin.py::test_installer_finds_the_claude_desktop_app_cli": "installer/ папки 1",
    "tests/test_plugin.py::test_installer_stops_on_an_incomplete_folder": "installer/ папки 1",
}


def pytest_collection_modifyitems(config, items):
    keep, drop = [], []
    for item in items:
        (drop if item.nodeid in FOLDER_1_ONLY else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
