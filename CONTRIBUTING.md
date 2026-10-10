<div align="center">

<img src="docs/assets/logo.svg" width="72" alt="">

# 🤝 Contributing to JARVIS

**Thanks for helping! This page gets you from a fresh clone to a pull request that's easy to merge.**

[🏠 Home](README.md) · [👩‍💻 Development](docs/development.md) · [🏗️ Architecture](docs/architecture.md)

</div>

---

> [!IMPORTANT]
> You need a **Mac on Apple silicon**, Homebrew, [uv](https://docs.astral.sh/uv/) and Node.js. The full requirements are in the **[README](README.md#-quickstart)**.

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
| 🧪 | `make test` | Pass: pytest, plus the web app's vitest tests with `web/node_modules` installed |
| 🔍 | `make check` | Pass: byte-compile, pyright (errors only), web type-check, imports |
| 🧱 | `make host-check` | Pass, if you touched `jarvis/safety/` or how tasks run commands |

## 🎨 Code style

- **Python 3.12**, 120-character lines (`[tool.ruff]` in `pyproject.toml`).
- **Comments are short and explain why**, not what: the reason, the constraint or the trap, in plain sentences. No history ("used to", "now") in comments; that belongs in the commit message.
- **Every module starts with a short docstring** saying what it's for and how it fits.
- **Tests describe behaviour.** Name them for what the user or caller sees (`test_a_prompt_is_filled_in_and_its_comment_left_out`), and add one for every bug fixed and every behaviour added.
- **Model verdicts are JSON**, read through `ask_json` against a schema, never keywords picked out of prose.
- **Safety stays in code.** Anything that sends, deletes, spends or reaches outside the Mac goes through the existing gates (`jarvis/safety/`); don't rely on the prompt to hold it back.
- **No personal data** in code, tests, prompts or docs: use placeholder names and `example.com`.
- **Settings** go in `jarvis/core/config.py` with a default, and in [Configuration](docs/configuration.md).

## 📬 Pull requests

A good pull request is easy to review and easy to trust:

| | What | How |
|:-:|---|---|
| 🎯 | **One change** | With a title that says what it does |
| 📝 | **A clear description** | What changed and why, how you tested it, anything you couldn't test (say, on an iPhone) |
| 📚 | **Docs updated** | The pages that describe the behaviour you changed |
| 🧹 | **Focused commits** | Rebased on `main` before asking for review |

## 🐛 Reporting bugs

Open an issue with **what you did**, **what you expected** and **what happened**, plus the relevant lines from `logs/` (with any personal details removed).

---

<div align="center">

By contributing, you agree that your contributions are licensed under the **[MIT License](LICENSE)**.

[🏠 Home](README.md) &nbsp;·&nbsp; [👩‍💻 Development →](docs/development.md)

</div>
