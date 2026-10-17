<div align="center">

<img src="docs/assets/logo.svg" width="72" alt="">

# 🤝 Contributing to JARVIS

**Thanks for helping! This page gets you from a fresh clone to a pull request that's easy to merge.**

[🏠 Home](README.md) · [👩‍💻 Development](docs/development.md) · [🏗️ Architecture](docs/architecture.md)

</div>

---

> [!IMPORTANT]
> You need **macOS on Apple silicon**, **Linux** with systemd, or **Windows through WSL2**, plus [uv](https://docs.astral.sh/uv/), Node.js 22+ and the platform's packages (Homebrew on macOS; bubblewrap, socat and friends on Linux). The full list per platform is in **[Setup](docs/setup.md#-pick-your-platform)**.

## 🚀 Your first pull request

**1. Set up** — tools, Python deps, models and the web app, then your local keys:

```sh
make install          # tools, Python deps, models, web app
make claude-login     # only if you'll run the voice agent or tasks for real
make setup            # .env and local keys
```

**2. Find your way around** — the code map and how to run each part are in **[Development](docs/development.md)**; how the parts fit together is in **[Architecture](docs/architecture.md)**.

**3. Make one change, with a test** — follow the [code style](#-code-style) below and add a test for the behaviour.

**4. Run the checks and open the PR** — the [checks](#-checks) must pass; then describe it as in [pull requests](#-pull-requests).

## ✅ Checks

| | Command | Must |
|:-:|---|---|
| 🧪 | `make test` | Pass, on macOS and on Linux: pytest, plus the web app's vitest tests with `web/node_modules` installed |
| 🔍 | `make check` | Pass: byte-compile, pyright (errors only), web type-check, imports |
| 🧱 | `make host-check` | Pass, if you touched `jarvis/safety/` or how tasks run commands (Seatbelt on macOS, bubblewrap on Linux) |

Tests that need one OS's programs (`sips`, `afconvert`, a working `bwrap`) skip themselves elsewhere, so a green run on one platform doesn't prove the other: say in the PR which one you ran.

## 🎨 Code style

- **Python 3.12**, 120-character lines (`[tool.ruff]` in `pyproject.toml`).
- **Comments are short and explain why**, not what: the reason, the constraint or the trap, in plain sentences. No history ("used to", "now") in comments; that belongs in the commit message.
- **Every module starts with a short docstring** saying what it's for and how it fits.
- **Tests describe behaviour.** Name them for what the user or caller sees (`test_a_prompt_is_filled_in_and_its_comment_left_out`), and add one for every bug fixed and every behaviour added.
- **Model verdicts are JSON**, read through `ask_json` against a schema, never keywords picked out of prose.
- **Both platforms stay working.** Code that differs by OS branches on `jarvis.core.platform` (`MAC`, `LINUX`, `WSL`) and keeps **both paths**; tests cover each by faking `platform.MAC` / `platform.LINUX` with `monkeypatch`. Say "the computer", not "the Mac", in prompts and messages.
- **Safety stays in code.** Anything that sends, deletes, spends or reaches outside the computer goes through the existing gates (`jarvis/safety/`); don't rely on the prompt to hold it back.
- **No personal data** in code, tests, prompts or docs: use placeholder names and `example.com`.
- **Settings** get their default in `config/jarvis.toml` (models: a role in `config/models.toml`; never a model id in code), are read in `jarvis/config/__init__.py`, and are listed in [Configuration](docs/configuration.md).

## 📬 Pull requests

A good pull request is easy to review and easy to trust:

| | What | How |
|:-:|---|---|
| 🎯 | **One change** | With a title that says what it does |
| 📝 | **A clear description** | What changed and why, how you tested it, anything you couldn't test (say, on a phone) |
| 📚 | **Docs updated** | The pages that describe the behaviour you changed |
| 🧹 | **Focused commits** | Rebased on `main` before asking for review |

## 🐛 Reporting bugs

Open an issue with **what you did**, **what you expected** and **what happened**, plus the relevant lines from `logs/` (with any personal details removed).

---

<div align="center">

By contributing, you agree that your contributions are licensed under the **[MIT License](LICENSE)**.

[🏠 Home](README.md) &nbsp;·&nbsp; [👩‍💻 Development →](docs/development.md)

</div>
