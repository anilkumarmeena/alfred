"""docs/features.md is made from the code (jarvis.apps.features), so it must match what the code says now."""

from jarvis.apps import features


def test_the_features_page_is_up_to_date():
    assert features.OUT.read_text(encoding="utf-8") == features.render(), "Run make features to bring it up to date"


def test_the_web_apps_list_of_panel_actions_is_up_to_date():
    assert features.ACTIONS_OUT.read_text(encoding="utf-8") == features.render_actions(), "Run make features"
    from jarvis import panel

    assert features.panel_actions() == sorted(panel.ACTIONS)  # the same actions the server answers


def test_it_names_every_part_and_every_tool_module():
    page = features.render()
    for part in features._packages():
        assert f"`jarvis/{part}" in page, part
    for module in features._tools():
        assert f"tools/{module}.py" in page, module
