# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Context

This is the course repository for **GBUS 562 (AI Language Models)**. The work here is software tools that use large language models, typically connecting to Claude via the Anthropic API.

## Repository

- Local path: `C:\Users\alesg\OneDrive\Documents\GBUS 562 Course\GBUS562`
- GitHub remote: https://github.com/AlesiaHarmon/UMW-GBUS-562-Harmon (branch `main`)
- Weekly work lives in `week-NN/` folders (e.g. `week-01/` holds the Week 1 Connect 4 game). Keep new assignments in a new week folder and add them to the "Weekly Material" section of `README.md`.

## Python environment

- Always use `.venv` (at the repo root) for running Python and installing packages, e.g. `.\.venv\Scripts\python.exe script.py` and `.\.venv\Scripts\python.exe -m pip install <package>`. Never install packages into the global Python.
- `.venv` uses Python 3.13 and is git-ignored.

## Anthropic API key

- The key is stored **outside** this repo at:
  `C:\Users\alesg\OneDrive\Documents\GBUS 562 Course\API Key.txt`
  (relative to the repo: `../API Key.txt`)
- Use this key whenever a project needs to call Claude.

### Security rules (always follow)

- **NEVER commit, push, or publish the API key** to GitHub or anywhere else, and never paste it into source code, notebooks, README files, artifacts, logs, or commit messages.
- Never copy `API Key.txt` into this repo.
- Load the key at runtime instead of hardcoding it, e.g. read it from `../API Key.txt` or from the `ANTHROPIC_API_KEY` environment variable (which the Anthropic SDK reads automatically).
- If a local `.env` file is used, it must stay git-ignored (it already is via `.gitignore`).
- Before every commit, check the staged diff (`git diff --cached`) to confirm no key (strings starting with `sk-ant-`) is included.
- Don't print the key's value in terminal output; only confirm it loaded.
