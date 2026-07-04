"""FastAPI backend for the Google Docs sidebar.

One run at a time. The run is a single `asyncio.Task` on the app's event loop (not a thread),
and the approval gate is an `asyncio.Event` the endpoints set — the pattern from the SDK's
examples/deep_dives/async_chat.py + host_tool_hooks.py. The agent's approval hook calls
`Session.request_approval(...)`, which awaits that event; `/api/approve` resolves it.
"""

import os
import json
import asyncio
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.agent import run_session

@asynccontextmanager
async def lifespan(app: FastAPI):
    email = _service_account_email()
    print("\n" + "=" * 64)
    print(" Antigravity Docs Goal Agent Backend running on port 8123")
    if email:
        print(f" Loaded credentials.json: {email}")
        print(" SHARE YOUR GOOGLE DOCS WITH THIS EMAIL AS EDITOR!")
    else:
        print(" No credentials.json found — Google Docs access will fail until it is configured.")
    print("=" * 64 + "\n")
    yield


app = FastAPI(title="Antigravity Docs Goal Agent Backend", lifespan=lifespan)

# Allow the Google Docs sidebar frame to call this local server directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)


def _service_account_email():
    if os.path.exists("credentials.json"):
        try:
            with open("credentials.json") as f:
                return json.load(f).get("client_email")
        except Exception:
            return None
    return None


class Session:
    """Live state for the single active run. The sidebar polls /api/status for `log` +
    `pending`; the run task writes to them and awaits `request_approval`."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.log: List[str] = []
        self.pending: Optional[dict] = None
        self.status = "idle"  # idle | running | waiting_approval | success | failed
        self.error: Optional[str] = None
        self.doc_id: Optional[str] = None
        self.goal: Optional[str] = None
        self.auto_approve = False
        self.approval_history: List[dict] = []
        self.task: Optional[asyncio.Task] = None
        self.current_response = None
        self._event = asyncio.Event()
        self._decision: Optional[dict] = None

    def __getstate__(self):
        # The SDK deep-copies the agent config (reached via the approval hook -> this Session)
        # when it spawns the researcher subagent. Drop the process-local, uncopyable fields; the
        # copy is inert (the subagent only makes read/search calls, which the hook approves
        # without ever touching these).
        state = self.__dict__.copy()
        for field in ("task", "current_response", "_event", "_decision"):
            state.pop(field, None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._event = asyncio.Event()
        self._decision = None
        self.task = None
        self.current_response = None

    def start(self, doc_id: str, goal: str, auto_approve: bool):
        if self.log:
            self.log.append("[System] --- New Run Started ---")
        self.log.append(f"[User Goal] {goal}")
        self.pending = None
        self._decision = None
        self._event = asyncio.Event()
        self.status = "running"
        self.error = None
        self.doc_id = doc_id
        self.goal = goal
        self.auto_approve = auto_approve
        self.current_response = None

    # --- output the sidebar renders (log-prefix contract) ---
    def emit(self, kind: str, text: str):
        if kind == "thought":
            self._stream("[Thought] ", text)
        elif kind == "response":
            self._stream("[Agent Response]:\n", text)
        else:  # status
            self.log.append(f"[{text}]")
            print(f"[{text}]")

    def _stream(self, prefix: str, text: str):
        if self.log and self.log[-1].startswith(prefix):
            self.log[-1] += text
        else:
            self.log.append(prefix + text)
        print(text, end="", flush=True)

    # --- approval gate: called by the agent's ApprovalHook, resolved by /api/approve ---
    async def request_approval(self, summary: str, diff: str, original: str, new_text) -> dict:
        if self.auto_approve:
            self.approval_history.append({"summary": summary, "diff": "", "decision": "auto", "edited_text": None})
            self.log.append(f"[Change] {summary}")
            return {"kind": "accept", "text": None}

        self.pending = {
            "summary": summary, "diff": diff, "original_text": original,
            "new_text": new_text if new_text is not None else original,
        }
        self.status = "waiting_approval"
        self.log.append(f"[Approval Gate] Intercepted edits: {summary}")

        self._event.clear()
        await self._event.wait()

        d = self._decision or {"kind": "reject", "text": None}
        self._decision = None
        self.pending = None
        self.status = "running"
        self.approval_history.append({
            "summary": summary, "diff": diff, "decision": d["kind"],
            "edited_text": d["text"] if d["kind"] == "edit" else None,
        })
        if d["kind"] == "accept":
            self.log.append("[Approval Gate] User APPROVED.")
        elif d["kind"] == "edit":
            self.log.append("[Approval Gate] User EDITED — applying their text.")
        else:
            self.log.append(f"[Approval Gate] User REJECTED with feedback: {d['text'] or ''}")
        return d

    def resolve(self, kind: str, text: Optional[str] = None):
        self._decision = {"kind": kind, "text": text}
        self._event.set()


# Careful: a single module-level session will NOT scale beyond one user/run — deliberate
# for this sample. The deployment model is one sidebar panel talking to one localhost
# process, and keeping it global keeps the agent code (the interesting part) uncluttered.
# If this ever becomes a hosted multi-user service, replace with a dict of sessions keyed
# by a run_id that /api/approve and /api/cancel must echo back.
session = Session()


async def _run(doc_id: str, goal: str, model: str, use_goal_mode: bool):
    try:
        await run_session(session, doc_id, goal, model, use_goal_mode)
        if session.status != "failed":
            session.status = "success"
            session.log.append("[Finished]")
    except asyncio.CancelledError:
        session.status = "failed"
        session.error = "Cancelled by user"
    except Exception as e:
        session.status = "failed"
        session.error = str(e)
        session.log.append(f"[Fatal error: {e}]")


class RunRequest(BaseModel):
    docId: str
    goal: str
    model: Optional[str] = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    useGoalMode: Optional[bool] = True
    autoApprove: Optional[bool] = False


class ApproveRequest(BaseModel):
    decision: str  # "accept" | "reject" | "edit"
    editedText: Optional[str] = None


@app.post("/api/run")
async def start_run(req: RunRequest):
    if session.status in ("running", "waiting_approval"):
        raise HTTPException(status_code=400, detail="An agent run is already in progress.")
    session.start(req.docId, req.goal, bool(req.autoApprove))
    email = _service_account_email()
    if email:
        session.log.append(f"[Auth Info] Using Service Account: {email}")
        session.log.append("[Auth Info] Please share your Google Doc with this email as Editor!")
    else:
        session.log.append("[Auth Info] No credentials.json found — configure a service account and share the doc with it.")
    session.task = asyncio.create_task(_run(req.docId, req.goal, req.model, bool(req.useGoalMode)))
    return {"status": "started"}


@app.post("/api/approve")
async def approve_changes(req: ApproveRequest):
    if session.status != "waiting_approval":
        raise HTTPException(status_code=400, detail="No approval is pending.")
    if req.decision not in ("accept", "reject", "edit"):
        raise HTTPException(status_code=400, detail="Invalid decision.")
    session.resolve(req.decision, req.editedText)
    return {"status": "ok"}


@app.get("/api/status")
async def get_status():
    return {
        "status": session.status,
        "logs": session.log,
        "pendingApproval": session.pending,
        "approvalHistory": session.approval_history,
        "error": session.error,
        "docId": session.doc_id,
        "goal": session.goal,
    }


@app.post("/api/cancel")
async def cancel_run():
    if session.status in ("running", "waiting_approval"):
        session.status = "failed"
        session.error = "Cancelled by user"
        if session.task:
            session.task.cancel()
        return {"status": "cancelled"}
    return {"status": "idle"}


@app.post("/api/clear")
async def clear_history():
    if session.task and not session.task.done():
        session.task.cancel()
    session.reset()
    return {"status": "cleared"}
