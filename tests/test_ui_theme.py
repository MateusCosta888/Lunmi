from app.ui import ui_preferences as prefs


def test_theme_round_trip_and_legacy_defaults(tmp_path):
    path = prefs.path_for(tmp_path)
    selected = prefs.parse({"theme": "soft-light", "window": [40, 50, 900, 700]})
    assert prefs.save(path, selected)
    assert prefs.load(path) == selected
    assert prefs.load(path).theme == "soft-light"
    assert prefs.parse({"scale_pct": 100}).theme == "celestial"
    assert list(path.parent.iterdir()) == [path]


def test_theme_id_is_inert_bounded_metadata():
    for value in (None, 3, True, [], "../file", "x" * 49, "var(--x)"):
        assert prefs.parse({"theme": value}).theme == "celestial"
    # Future registered themes can preserve their identifier through old preference readers.
    assert prefs.parse({"theme": "lunar-paper"}).theme == "lunar-paper"
