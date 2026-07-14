# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## File System Permissions

NEVER update, remove, or move any files or directories in system paths or the home directory (`~`) without explicit permission. You must receive the word **"Approved"** from the user before proceeding with any such operation. This applies even when an action seems obviously correct or is part of a larger requested task.

## Environment

Arch Linux with a Hyprland desktop (fish shell, foot terminal). Git credentials are managed via the GitHub CLI (`gh auth`).

## Projects

### helio (`~/Development/helio`)

Main active project — a dashboard builder with a React/Redux/TypeScript frontend and a Scala/Akka HTTP backend. Has its own `CLAUDE.md` with full commands, architecture, and workflow details. Start there for any helio work.

### caelestia (`~/caelestia`)

Dotfiles repo for the Hyprland desktop environment. Configs for hypr, fish, foot, btop, fastfetch, spicetify, vscode/vscodium, and zen browser. All configs are symlinked into `~/.config` by the install script — edits in `~/caelestia/*` take effect immediately.

```bash
./install.fish [--noconfirm] [--spotify] [--vscode=codium|code] [--discord] [--zen] [--aur-helper=yay|paru]
```

### job_tracker (`~/Development/DataScience/job_tracker`)

Python script that reads job-related emails from Gmail and writes classified results to Google Sheets. Uses the Claude API for classification. OAuth credentials are in `credentials.json` / `token.json` (gitignored).

```bash
# In ~/Development/DataScience/job_tracker/ with venv activated:
python track.py fetch --since [day|week|month]   # fetch emails, print JSON
python track.py write --payload '<json>'          # write to Google Sheets
python track.py write --payload '<json>' --dry-run
```

Dependencies: `pip install -r requirements.txt` (anthropic, google-auth, google-api-python-client).

### DataScience (`~/Development/DataScience`)

Jupyter notebooks and data science work. Python venv at `~/Development/.venv`.

### ai (`~/Development/ai`)

Stable Diffusion WebUI installation.

## News aggregator (this repo)

See `README.md` for the architecture and workflow: RSS → sequential gemma passes
(triage → planner → summarizer) → pluggable enrichers → helio via the MCP client
(`news/run.py`). Panel selection is dynamic per story ("alive" dashboards).
