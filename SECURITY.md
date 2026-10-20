<div align="center">

<img src="docs/assets/logo.svg" width="72" alt="">

# 🔐 Security policy

**Alfred reads your mail, runs commands and holds your keys, so security reports get priority. Here's how to send one.**

[🏠 Home](README.md) · [🏗️ Architecture](docs/architecture.md#-known-limitations) · [🤝 Contributing](CONTRIBUTING.md)

</div>

---

> [!CAUTION]
> **Please don't open a public issue for a vulnerability.** Report it privately so it can be fixed before it's known.

## 📮 Reporting a vulnerability

Use **[Report a vulnerability](https://github.com/anilkumarmeena/alfred/security/advisories/new)** (GitHub's private reporting). Include:

| | What | Why |
|:-:|---|---|
| 🎯 | **What an attacker gets** | Sending mail, running a command, reading secrets, reaching the panel… |
| 🪜 | **Steps to reproduce** | The input that does it: a message, an email, a web page, a tool result |
| 🖥️ | **Platform and commit** | macOS, Linux or WSL2, and `git rev-parse --short HEAD` |
| 📋 | **Relevant log lines** | From `logs/`, with personal details removed |

## ⏱️ What happens next

| Step | Within |
|---|---|
| Acknowledgement | **3 days** |
| First assessment | **7 days** |
| Fix or mitigation for a confirmed issue | As soon as it's ready, credited in the advisory unless you'd rather not be named |

## 🎯 In scope

- A way past a **guard that is enforced in code**: the spoken yes, approvals, the task sandbox (Seatbelt or bubblewrap), gated MCP tools, the encrypted vault, injection checks, or the passcode on the web app
- **Prompt injection** that makes Alfred act (send, delete, run, leak) without the user agreeing
- Secrets from `.env`, `data/` or `secrets/` reaching a log, a prompt, a task or the network

Only the **latest commit on `master`** is supported. Limits that are already documented, in **[Architecture › Known limitations](docs/architecture.md#-known-limitations)**, are out of scope unless you find a worse way through them.

---

<div align="center">

[🏠 Home](README.md) &nbsp;·&nbsp; [🤝 Contributing →](CONTRIBUTING.md)

</div>
