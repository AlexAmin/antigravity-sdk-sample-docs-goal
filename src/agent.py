"""Agent setup and the run loop, built on the Google Antigravity SDK.

The design follows the SDK's own deep-dive examples
(https://github.com/google-antigravity/antigravity-sdk-python/tree/main/examples/deep_dives):

  * host_tool_hooks.py / docstring_maintenance_agent.py — human approval is an *async*
    `PreToolCallDecideHook`. It `await`s the sidebar's decision and returns
    `HookResult(allow, message)`. A rejection carries the user's feedback back to the model
    via `message`, so the model revises IN THE SAME TURN — no feedback re-run machinery.
  * async_chat.py — the run is a single `asyncio.Task` awaiting the agent; no threads/polling.
  * docstring_maintenance_agent.py — a single `agent.chat()` drives all edits to completion;
    we only add a thin verify loop on top for Goal mode.
"""

import os
import asyncio
import difflib
from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig
from google.antigravity.hooks.policy import deny, allow
from google.antigravity.hooks.hooks import PreToolCallDecideHook
from google.antigravity.types import (
    BuiltinTools, Thought, Text, HookResult, SubagentConfig, SubagentCapabilities,
)
from src.docs_tool import (
    read_document, replace_text, link_text, insert_text, remove_links,
    preview_edit, set_document_text,
)
from src.research_tool import search_developer_knowledge

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_prompt(name: str) -> str:
    """Loads a role's system prompt from prompts/<name>.md (kept out of Python)."""
    with open(os.path.join(_PROJECT_ROOT, "prompts", f"{name}.md"), encoding="utf-8") as f:
        return f.read().strip()


AGENT_INSTRUCTION = _load_prompt("agent_instructions")
RESEARCHER_INSTRUCTION = _load_prompt("researcher_instructions")

# Per-role tools. The write tools resolve all Docs indices in code, so the model only ever
# passes text — never request JSON or offsets.
WRITE_TOOLS = [replace_text, link_text, insert_text, remove_links]
WRITE_TOOL_NAMES = {tool.__name__ for tool in WRITE_TOOLS}
ORCHESTRATOR_TOOLS = [read_document] + WRITE_TOOLS
RESEARCHER_TOOLS = [search_developer_knowledge]  # + the SEARCH_WEB builtin
# The SDK requires a subagent's custom tools to also be registered on the main agent.
AGENT_TOOLS = ORCHESTRATOR_TOOLS + RESEARCHER_TOOLS
_ALLOWED_TOOL_NAMES = [tool.__name__ for tool in AGENT_TOOLS] + ["search_web", "start_subagent"]


# The research subagent owns web search + the Developer Knowledge tool; the orchestrator
# delegates "find the official docs URL for X" to it.
RESEARCHER = SubagentConfig(
    name="researcher",
    description=(
        "Finds the official Google documentation URLs for a list of technologies, products, or "
        "APIs. Pass the topics you found in the document and get back their canonical docs links."
    ),
    system_instructions=RESEARCHER_INSTRUCTION,
    capabilities=SubagentCapabilities(enabled_tools=[BuiltinTools.SEARCH_WEB]),
    tools=RESEARCHER_TOOLS,
)


def build_config(doc_id: str, goal: str, model_name: str, hooks: list) -> LocalAgentConfig:
    """Builds the agent config. Human approval is a hook (passed in), not an ask_user policy —
    see build_approval_hook / examples/deep_dives/host_tool_hooks.py.
    """
    # Deny by default; allow the tools the agent (and its subagent) uses. The approval HOOK
    # gates the write tools; the policy just controls availability.
    policies = [deny("*")] + [allow(name) for name in _ALLOWED_TOOL_NAMES]
    return LocalAgentConfig(
        system_instructions=f"{AGENT_INSTRUCTION}\n\nGoal: {goal}\nDocument ID: {doc_id}",
        tools=AGENT_TOOLS,
        policies=policies,
        hooks=hooks,
        model=model_name,
        capabilities=CapabilitiesConfig(enabled_tools=[BuiltinTools.START_SUBAGENT], enable_subagents=True),
        subagents=[RESEARCHER],
    )


def _initial_prompt(doc_id: str, goal: str) -> str:
    return (
        f"Request: {goal}\n"
        f"Document ID: {doc_id}\n\n"
        "First read the document with read_document. If the request is a question or a check, "
        "answer it directly in your response and do NOT edit the document. Only if the request "
        "asks you to change the document should you edit it (replace_text / link_text / "
        "insert_text / remove_links), explaining your reasoning before each edit."
    )


async def goal_met(doc_text: str, goal: str, model_name: str) -> bool:
    """Independent auditor for Goal mode: does the document satisfy the goal? Uses the SDK's
    native response_schema for a structured boolean."""
    config = LocalAgentConfig(
        system_instructions="You are an objective auditor. Decide whether the document fully satisfies the goal.",
        model=model_name,
        response_schema={"type": "object", "properties": {"goal_met": {"type": "boolean"}}, "required": ["goal_met"]},
    )
    try:
        async with Agent(config) as auditor:
            response = await auditor.chat(f"Goal:\n{goal}\n\nDocument:\n---\n{doc_text}\n---")
            result = await response.structured_output()
            return bool(result and result.get("goal_met"))
    except Exception as e:
        print(f"[Auditor] error: {e}; assuming goal not met.")
        return False


class ApprovalHook(PreToolCallDecideHook):
    """Gates the write tools through the sidebar. Pattern from examples/deep_dives/host_tool_hooks.py:
    an async decide hook that returns HookResult(allow, message). It `await`s the sidebar's
    decision (an asyncio.Event inside `ui`) — no threads, no polling. On rejection it hands the
    user's feedback to the model via HookResult(message=...), so the model revises this turn.
    """

    def __init__(self, ui, doc_id: str):
        self.ui = ui
        self.doc_id = doc_id

    async def run(self, context, data) -> HookResult:
        if data.name not in WRITE_TOOL_NAMES:
            return HookResult(allow=True)  # only writes are gated

        original = await asyncio.to_thread(_safe_read, self.doc_id)
        summary, new_text = preview_edit(data.name, data.args, original)
        diff = ""
        if new_text is not None:
            diff = "\n".join(difflib.unified_diff(
                original.splitlines(), new_text.splitlines(),
                fromfile="Original Doc", tofile="Proposed Change", lineterm=""))

        decision = await self.ui.request_approval(summary, diff, original, new_text)
        kind = decision["kind"]
        if kind == "accept":
            return HookResult(allow=True)
        if kind == "edit":
            await asyncio.to_thread(set_document_text, self.doc_id, decision["text"] or original)
            return HookResult(allow=False, message="The user applied their own manual edit; do not re-apply this one.")
        # reject, optionally with feedback — the model revises in-turn from `message`.
        return HookResult(allow=False, message=decision["text"] or "The user rejected this edit.")


def _safe_read(doc_id: str) -> str:
    try:
        return read_document(doc_id)
    except Exception:
        return ""


async def run_session(ui, doc_id: str, goal: str, model_name: str, use_goal_mode: bool, max_turns: int = 6) -> None:
    """Drives one run as a single async task (see examples/deep_dives/async_chat.py).

    A single `agent.chat()` runs the SDK's internal agentic loop to completion. Direct mode is
    one turn; Goal mode wraps it in a verify loop until an auditor is satisfied, the doc stops
    changing, or max_turns. All human interaction (approve / reject-with-feedback / edit) happens
    inside ApprovalHook, so there is no feedback/stop/interrupt plumbing here.
    """
    config = build_config(doc_id, goal, model_name, hooks=[ApprovalHook(ui, doc_id)])
    previous = await asyncio.to_thread(_safe_read, doc_id)
    async with Agent(config) as agent:
        ui.emit("status", "Goal mode" if use_goal_mode else "Direct mode")
        prompt = _initial_prompt(doc_id, goal)
        for turn in range(1, (max_turns if use_goal_mode else 1) + 1):
            response = await agent.chat(prompt)
            ui.current_response = response
            async for chunk in response.chunks:
                if isinstance(chunk, Thought):
                    ui.emit("thought", chunk.text)
                elif isinstance(chunk, Text):
                    ui.emit("response", chunk.text)
            if not use_goal_mode:
                return
            doc = await asyncio.to_thread(_safe_read, doc_id)
            if doc == previous:
                ui.emit("status", "No change this turn — stopping.")
                return
            previous = doc
            if await goal_met(doc, goal, model_name):
                ui.emit("status", f"Goal achieved in {turn} turn(s).")
                return
            prompt = "The goal is not yet fully met. Continue editing to satisfy it."
