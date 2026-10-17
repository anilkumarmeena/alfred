"""Settings come from config/*.toml outside the package, laid over by the user's config.toml and the environment;
models are asked for by role."""

import pytest

from jarvis import config, setup_local
from jarvis.config import files
from jarvis.config.files import ConfigError
from jarvis.config.models import Models

TABLE = {
    "tiers": {"deep": "model-deep", "balanced": "model-balanced", "fast": "model-fast"},
    "roles": {"voice": "deep", "task": "deep", "goal": "task", "lookup": "fast", "announce": "custom-model-1",
              "voice_quick": ""},
    "choices": {"injection": ["fast", "balanced"]},
    "families": {"deep": {"match": "deep", "window": "seven_day_deep", "lighter": "balanced"},
                 "balanced": {"match": "balanced"}},
}


def test_a_role_names_a_tier_another_role_or_a_model():
    models = Models(TABLE, env={})
    assert models.role("voice") == "model-deep" and models.role("lookup") == "model-fast"
    assert models.role("goal") == "model-deep"  # goal follows task
    assert models.role("announce") == "custom-model-1" and models.role("voice_quick") == ""
    assert models.choices("injection") == ("model-fast", "model-balanced")


def test_a_roles_variable_wins_and_a_role_that_follows_it_follows_the_variable_too():
    models = Models(TABLE, env={"JARVIS_TASK_MODEL": "model-from-env"})
    assert models.role("task") == "model-from-env" and models.role("goal") == "model-from-env"
    assert models.role("voice") == "model-deep"


def test_a_family_with_its_own_window_falls_back_to_its_lighter_tier():
    models = Models(TABLE, env={})
    deep = models.family("model-deep")
    assert deep and deep.window == "seven_day_deep" and deep.lighter == "model-balanced" and deep.spoken == "Deep"
    assert models.family("model-balanced").lighter == "" and models.family("something-else") is None


def test_a_missing_tier_or_role_or_a_loop_is_a_clear_error():
    with pytest.raises(ConfigError, match="fast tier"):
        Models({"tiers": {"deep": "a", "balanced": "b"}}, env={})
    models = Models(TABLE | {"roles": {"a": "b", "b": "a"}}, env={})
    with pytest.raises(ConfigError, match="loop"):
        models.role("a")
    with pytest.raises(ConfigError, match="no role nobody"):
        models.role("nobody")


def test_the_shipped_defaults_give_every_role_the_code_asks_for():
    for role in ("voice", "lookup", "side", "consent", "injection", "task", "task_search", "task_helper", "goal",
                 "announce", "placement", "schedule", "verdict", "command_summary", "command_triage", "learning",
                 "todo_match", "skill_review", "sign_in_check"):
        config.MODELS.role(role)
    fast = config.MODELS.tier("fast")
    assert fast in config.INJECTION_MODELS and config.DEFAULTS["injection_model"] == fast


def test_the_users_file_lays_over_the_shipped_defaults_key_by_key(tmp_path):
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "jarvis.toml").write_text('[tasks]\nmax_turns = 150\neffort = "high"\n')
    (shipped / "models.toml").write_text('[tiers]\nfast = "a"\n')
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[tasks]\nmax_turns = 40\n[models.tiers]\nfast = "b"\n')
    settings = files.load(home, env={}, defaults_dir=shipped)
    assert settings["tasks"] == {"max_turns": 40, "effort": "high"} and settings["models"]["tiers"]["fast"] == "b"
    assert files.value(settings, "tasks.effort") == "high"
    with pytest.raises(ConfigError, match="tasks.nothing"):
        files.value(settings, "tasks.nothing")


def test_a_named_config_file_must_be_there(tmp_path):
    with pytest.raises(ConfigError, match="isn't there"):
        files.load(tmp_path, env={"JARVIS_CONFIG": str(tmp_path / "missing.toml")})
    (tmp_path / "bad.toml").write_text("[tasks\n")
    with pytest.raises(ConfigError, match="isn't valid TOML"):
        files.load(tmp_path, env={"JARVIS_CONFIG": str(tmp_path / "bad.toml")})


def test_home_is_the_variable_else_dot_jarvis_once_it_holds_an_install_else_the_checkout(tmp_path):
    assert files.home({"JARVIS_HOME": str(tmp_path / "mine")}, tmp_path) == tmp_path / "mine"
    assert files.home({}, tmp_path) == files.ROOT
    (tmp_path / ".jarvis").mkdir()
    (tmp_path / ".jarvis/config.toml").write_text("")
    assert files.home({}, tmp_path) == files.ROOT  # a lone config.toml mustn't swap in an empty install
    (tmp_path / ".jarvis/.env").write_text("")
    assert files.home({}, tmp_path) == tmp_path / ".jarvis"


def test_a_setting_is_the_variable_else_the_files_kept_in_range(monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_ROUNDS", "9")
    assert config.whole("goal.rounds", "JARVIS_GOAL_ROUNDS", 0, 5) == 5
    monkeypatch.setenv("JARVIS_GOAL_ROUNDS", "not a number")
    assert config.whole("goal.rounds", "JARVIS_GOAL_ROUNDS", 0, 5) == 2
    monkeypatch.setenv("JARVIS_GOAL_LOOP", "off")
    assert config.flag("goal.loop", "JARVIS_GOAL_LOOP") is False
    monkeypatch.setenv("JARVIS_GOAL_LOOP", "maybe")
    assert config.flag("goal.loop", "JARVIS_GOAL_LOOP") is True


def test_move_home_takes_the_installs_files_out_of_the_checkout(tmp_path):
    root, home = tmp_path / "checkout", tmp_path / "home"
    (root / "data").mkdir(parents=True)
    (root / "data/tasks.db").write_text("db")
    (root / ".env").write_text("KEY=1\n")
    (root / "jarvis").mkdir()
    assert setup_local.move_home(home, root) == [".env", "data"]
    assert (home / "data/tasks.db").read_text() == "db" and (home / ".env").read_text() == "KEY=1\n"
    assert (root / "data").is_symlink() and (root / "data/tasks.db").read_text() == "db"  # old paths still work
    assert (root / "jarvis").is_dir() and not (root / ".env").exists()
    assert setup_local.move_home(home, root) == []  # the link isn't moved again


def test_move_home_refuses_before_moving_anything_if_home_has_one_already(tmp_path):
    root, home = tmp_path / "checkout", tmp_path / "home"
    (root / "secrets").mkdir(parents=True)
    (root / ".env").write_text("")
    (home / "secrets").mkdir(parents=True)
    (home / "secrets/livekit.yaml").write_text("")
    with pytest.raises(SystemExit, match="already has secrets"):
        setup_local.move_home(home, root)
    assert (root / ".env").exists()


def test_move_home_moves_into_the_empty_folders_config_made(tmp_path):
    root, home = tmp_path / "checkout", tmp_path / "home"
    (root / "secrets").mkdir(parents=True)
    (root / "secrets/livekit.yaml").write_text("keys")
    (home / "secrets").mkdir(parents=True)
    (home / "data").mkdir()
    assert setup_local.move_home(home, root) == ["secrets"]
    assert (home / "secrets/livekit.yaml").read_text() == "keys"


def test_move_home_refuses_a_link_to_somewhere_else(tmp_path):
    root, home, disk = tmp_path / "checkout", tmp_path / "home", tmp_path / "disk"
    (disk / "data").mkdir(parents=True)
    root.mkdir()
    (root / "data").symlink_to(disk / "data")
    (root / ".env").write_text("")
    with pytest.raises(SystemExit, match="data in .* link elsewhere"):
        setup_local.move_home(home, root)
    assert (root / ".env").exists() and not home.exists()


def test_remember_home_links_the_default_home_once(tmp_path):
    user, there = tmp_path / "user", tmp_path / "there"
    user.mkdir()
    there.mkdir()
    assert setup_local.remember_home(there, user) is True
    assert (user / ".jarvis").resolve() == there.resolve()
    assert setup_local.remember_home(there, user) is False


def test_a_roles_variable_may_name_a_tier():
    assert Models(TABLE, env={"JARVIS_TASK_MODEL": "fast"}).role("goal") == "model-fast"


def test_a_quoted_off_in_config_toml_is_off(monkeypatch):
    monkeypatch.delenv("JARVIS_GOAL_LOOP", raising=False)
    for written, expected in (("off", False), ("false", False), ("on", True), (False, False), (True, True)):
        monkeypatch.setitem(config.SETTINGS["goal"], "loop", written)
        assert config.flag("goal.loop", "JARVIS_GOAL_LOOP") is expected, written


def test_on_off_preferences_fall_back_to_the_shipped_default(monkeypatch):
    from jarvis.state import preferences

    for var in ("JARVIS_BROWSER", "JARVIS_BROWSER_WINDOW", "JARVIS_INJECTION_CHECK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(preferences, "stored", lambda key, db_file=None: "")
    monkeypatch.setitem(config.DEFAULTS, "browser", "off")
    monkeypatch.setitem(config.DEFAULTS, "browser_window", "headless")
    monkeypatch.setitem(config.DEFAULTS, "injection_check", "off")
    assert (preferences.browser_on(), preferences.browser_headless(), preferences.injection_check_on()) == (
        False, True, False)


def test_a_worktree_belongs_to_the_checkout_its_git_file_names(tmp_path):
    main, tree = tmp_path / "code/jarvis", tmp_path / "home/data/worktrees/t1"
    (main / ".git/worktrees/t1").mkdir(parents=True)
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {main}/.git/worktrees/t1\n")
    assert config.main_checkout(tree) == main and config.main_checkout(main) == main
    assert config.main_checkout(tmp_path) == tmp_path  # no git at all
