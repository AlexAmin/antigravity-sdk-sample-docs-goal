# antigravity-sdk-sample-docs-goal

> Point an autonomous agent at a **Google Doc**, hand it a **single high-level goal**, and watch it edit the document in real time — from an **interactive Sidebar Add-on**, with a human approval gate on every write.

An open-source showcase of **autonomous, goal-driven document editing** built on the **Google Antigravity SDK** (Python). Give the agent a doc and a plain-English instruction — *"turn these notes into a structured PRD"*, *"tighten this draft to 150 words"*, *"link every product name to its official docs"* — and it reads, plans, and edits the document to satisfy the goal, pausing to show you a diff before anything is applied.

It runs as an **interactive Google Docs Add-on**: a real Workspace sidebar embedded inside Google Docs, talking to a local Python **FastAPI** backend. You trigger runs and approve edits without ever leaving the editor.

<a href="https://www.youtube.com/watch?v=7LlPGnHRZoo"><img src="https://img.youtube.com/vi/7LlPGnHRZoo/maxresdefault.jpg" alt="Demo video" width="480"></a>

> 📹 [Watch the demo on YouTube](https://www.youtube.com/watch?v=7LlPGnHRZoo)

---

## ✨ The interesting part: the agent never touches Docs indices

The Google Docs API is notoriously fiddly — every edit is a JSON request keyed by exact character `startIndex`/`endIndex` offsets, and one off-by-one corrupts the document. LLMs are terrible at that arithmetic.

So this agent **never computes an index or writes a raw Docs request**. It edits purely through **semantic tools**, and all the fragile index math is resolved in Python. On each read we walk every text run, record its real `startIndex`, and build a `char → Docs-API-index` map; to act on a phrase we string-match it in the plain text and translate the offset back to an exact API range — correct even when the phrase spans multiple runs. The tools the model sees:

| Tool | What it does |
| --- | --- |
| `read_document()` | Returns the plain text of the doc. |
| `replace_text(find, replace)` | Find-and-replace across the doc (deletions = replace with `""`). |
| `link_text(text, url)` | Turns text into a **real hyperlink** via `updateTextStyle` — never Markdown. |
| `insert_text(text, after=None)` | Inserts after an anchor phrase, or at the end of the doc. |
| `remove_links(text=None)` | Strips hyperlink formatting without changing the text. |

A few more things worth calling out:

- **A researcher subagent** (an SDK subagent) owns web search and the Google **Developer Knowledge** API. The main editor agent delegates *"find the official docs URL for X"* to it and gets back canonical links, keeping research off its own context. (Subagents in this SDK currently can't use MCP-server tools — only builtins and custom Python callables — so this sample demonstrates the pattern for that case: wrap the REST API as a plain Python tool and register it on both the subagent and the main agent. If the SDK adds subagent MCP support, `research_tool.py` collapses into an `mcp_servers` entry.)
- **Deny-by-default policies + a human approval gate.** Every write tool is intercepted and surfaced as a unified diff (or a plain-language summary for link-only edits). You click **Accept**, **Reject**, or **Edit** in the sidebar; nothing hits the doc until you approve.
- **Two run modes, toggled in the sidebar.** **Direct** is a single `agent.chat()` — the SDK's internal agentic loop runs read → plan → edit → delegate to completion in one turn. **Goal** drives a verify loop on top: edit → an independent auditor checks whether the goal is met → repeat until it's satisfied, the doc stops changing, or max turns is reached.
- **Auto-Approve toggle.** Skip the prompts and apply edits automatically, each logged as one compact line — handy for demos and low-stakes goals.

> 🤫 Oh, and it runs great on **`gemini-3.1-flash-lite`**. Yes — *flash-lite* is doing all of this.

---

## 🏗️ How it fits together

```
your goal ─▶ [Apps Script Sidebar] ──(CORS)──▶ [Local FastAPI Server]
                     ▲                                 │ (Antigravity SDK agent
                     │                                 │  + researcher subagent)
              [Approval UI] ◀── unified diff ◀── [Approval Gate] ◀── [Gemini]
             (Accept/Reject/Edit)                                        │
                     │                              semantic tools       ▼
                     └──────── apply edits ─────▶ [docs_tool → Google Docs REST API]
```

- **Brain**: Gemini (set via `GEMINI_MODEL`; happily runs on `gemini-3.1-flash-lite`).
- **Harness**: Google Antigravity SDK — main agent with deny-by-default policies, plus a research subagent.
- **Docs layer**: hand-written semantic tools over the Google Docs REST API.
- **Gate**: a custom approval handler that turns each pending write into a diff and blocks until you decide.

---

## 📋 Prerequisites

- **Python 3.13+** and the [`uv`](https://docs.astral.sh/uv/) toolchain.
- A **`GEMINI_API_KEY`** (the agent's model).
- For editing real docs: a **Google Cloud service account** with the **Google Docs API** enabled, its key saved as `credentials.json`, and the target doc **shared to the service-account email as Editor**.
- For the sidebar: [`clasp`](https://github.com/google/clasp) to push the Apps Script project (or paste the files into the Apps Script editor by hand).
- _Optional research:_ the **Developer Knowledge API** enabled on your GCP project (or a `DEV_KNOWLEDGE_API_KEY`). Without it, the researcher just falls back to web search.

---

## 🚀 Setup

### 1. Install and configure

```bash
git clone https://github.com/AlexAmin/antigravity-sdk-sample-docs-goal.git
cd antigravity-sdk-sample-docs-goal

uv sync                 # install dependencies
cp .env.example .env    # then add your GEMINI_API_KEY
```

Open `.env` and set `GEMINI_API_KEY` (and optionally `GEMINI_MODEL` / `DEV_KNOWLEDGE_API_KEY`).

### 2. Google Docs access (for real docs)

1. Create a Google Cloud project with the **Google Docs API** enabled.
2. Create a **service account**, download its JSON key, rename it to `credentials.json`, and drop it in the project root.
3. **Share your target Doc** with the service-account email as **Editor**.


### 3. (Optional) Enable documentation research

```bash
gcloud services enable developerknowledge.googleapis.com
```

Or set `DEV_KNOWLEDGE_API_KEY` in `.env`. Otherwise the researcher subagent falls back to plain web search.

---

## ▶️ Running it

### 1. Start the backend

```bash
uv run uvicorn src.server:app --reload --port 8123
```

The API server comes up on `http://localhost:8123` — the port the sidebar calls.

### 2. Deploy the sidebar Add-on

The Apps Script project lives in [`appsscript/`](appsscript/) (`Code.js` + `Sidebar.html` + `appsscript.json`). Copy `.clasp.json.example` to `.clasp.json`, set your doc's bound script ID, and push with `clasp` — or paste `Code.js` and `Sidebar.html` into **Extensions → Apps Script** by hand and save.

Then, in the Doc, open **Internal Tools → Open Company Knowledge Assistant** to launch the sidebar.

### 3. Run and approve edits

- Enter a high-level goal (e.g. *"Tighten this draft to about 150 words"*).
- Click **Run** — the sidebar streams the agent's live reasoning.
- Choose **Direct** for a single turn or **Goal** for the verify loop; flip **Auto-Approve** to apply edits without prompting.
- When the agent proposes an edit, the sidebar renders a color-coded diff. Click **Accept** to apply, **Reject** to block it, or **Edit** to tweak the proposed text before it lands.

---

## 📂 Project structure

```
antigravity-sdk-sample-docs-goal/
├── appsscript/
│   ├── Code.js          # Apps Script: sidebar loader + Internal Tools menu hook
│   ├── Sidebar.html     # Sidebar UI: live logs, diff renderer, approval controls
│   └── appsscript.json  # Apps Script manifest
├── src/
│   ├── server.py        # FastAPI backend: run manager, web approval gate, status endpoints
│   ├── agent.py         # Antigravity agent, researcher subagent, direct/goal modes, policies
│   ├── docs_tool.py     # Semantic Docs tools + char→index map
│   └── research_tool.py # Custom Developer Knowledge REST tool (used by the researcher)
├── prompts/
│   ├── agent_instructions.md       # Main editor agent system prompt
│   └── researcher_instructions.md  # Researcher subagent system prompt
├── pyproject.toml       # Python dependencies
└── .env.example         # Environment template
```

---

## 🛡️ Guardrails

- **Deny-by-default policies.** The main agent may only read the doc, delegate to the researcher, and *ask* before every write. Nothing else is permitted.
- **Human approval gate.** Each write is intercepted and shown as a diff (or summary) before it's applied — Accept / Reject / Edit.
- **API self-healing.** `docs_tool` resolves all Docs API indices in code, so the model never emits raw request JSON or character offsets that could corrupt the document.
- **Secrets stay local.** Never commit `.env` or `credentials.json`.

---

## License

MIT © 2026 Alexander Amin
