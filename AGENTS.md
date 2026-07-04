# AGENTS.md — Build brief for `antigravity-sdk-sample-docs-goal`

## Completed Architecture & Scope

This project is a **Google Docs Goal-Driven Editing Agent** built with the Google Antigravity SDK. It runs as an **Interactive Google Docs Add-on**: a Google Doc Sidebar (Google Apps Script) talking to a local Python FastAPI backend, permitting diff inspections and user approvals inside the editor.

The agent runs in two selectable modes: **Direct** (a single `agent.chat()`) and **Goal** (a verify loop we drive on top of `chat()`, since the SDK has no native goal loop).

---

## 🛠️ Build & Commands Reference

### Install Dependencies
This is a pure Python project using the `uv` toolchain.
```bash
python3 -m uv sync
```

### Run Google Docs Sidebar Add-on Backend
Runs the local API server on `http://localhost:8123` (the port the Sidebar Add-on connects to):
```bash
python3 -m uv run uvicorn src.server:app --reload --port 8123
```

---

## 📂 Project Structure

```
antigravity-sdk-sample-docs-goal/
├── appsscript/
│   ├── Code.js          # Google Apps Script sidebar loader and menu hook
│   ├── Sidebar.html     # Front-end UI (logs terminal, diff renderer, approvals)
│   └── appsscript.json  # Apps Script manifest
├── src/
│   ├── server.py      # FastAPI server: run manager, web approval gate, status endpoints
│   ├── agent.py       # Agent config, researcher subagent, direct/goal run modes, write policies
│   ├── docs_tool.py   # Semantic Docs tools over the REST API (char -> UTF-16 index map)
│   └── research_tool.py # Custom Developer Knowledge REST tool used by the researcher subagent
├── prompts/           # Per-role system instructions (Markdown, loaded at runtime)
├── pyproject.toml     # Python dependencies
└── .env.example       # Template env parameters
```

---

## 🛡️ Guardrails & Policies

* **CORS Settings**: The FastAPI server allows requests from all domains (`*`) and explicitly accepts `https://docs.google.com` to allow the Google Doc browser frame to make local client-side API requests directly to `localhost:8123`.
* **Index Safety**: The agent's edit tools are text-anchored — the model only ever passes phrases, never character indices or raw Docs API request JSON. `docs_tool.py` resolves phrases to exact UTF-16 index ranges in code, so a malformed model request cannot corrupt the document.
* **Credentials**: Never commit `.env` or `credentials.json` to version control.
