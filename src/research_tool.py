"""Custom research tool — queries the Google Developer Knowledge API over REST.

Independent of the Antigravity SDK: a plain Python function the `researcher` subagent
calls to find official Google documentation. We use the REST API (rather than the
Developer Knowledge MCP server) because the SDK does not allow subagents to use
MCP-server tools — only builtins and custom Python callables. Auth is an API key
(DEV_KNOWLEDGE_API_KEY) or Application Default Credentials.
"""

import os
import json
import urllib.parse
import urllib.request

_SEARCH_URL = "https://developerknowledge.googleapis.com/v1/documents:searchDocumentChunks"


def _auth_headers() -> dict | None:
    """API-key header if DEV_KNOWLEDGE_API_KEY is set, else an ADC Bearer token, else None."""
    api_key = os.getenv("DEV_KNOWLEDGE_API_KEY")
    if api_key:
        return {"X-Goog-Api-Key": api_key}
    try:
        import google.auth
        from google.auth.transport.requests import Request

        # Match the Docs client: honor a local credentials.json if present.
        if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists("credentials.json"):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("credentials.json")

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        return {"Authorization": f"Bearer {creds.token}"}
    except Exception:
        return None


def search_developer_knowledge(topics: list[str]) -> str:
    """Looks up official Google developer documentation for a list of topics.

    For each topic (a Google technology, product, or API name), returns matching
    documentation pages (URL + snippet) from the Google Developer Knowledge corpus.
    """
    headers = _auth_headers()
    if headers is None:
        return "Developer Knowledge API is not authenticated (set DEV_KNOWLEDGE_API_KEY or configure ADC)."

    sections = []
    for topic in topics:
        url = f"{_SEARCH_URL}?{urllib.parse.urlencode({'query': topic, 'pageSize': 3})}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
                results = json.load(resp).get("results", [])
        except Exception as e:
            sections.append(f"{topic}: search failed ({e})")
            continue

        if not results:
            sections.append(f"{topic}: no documentation found")
            continue

        lines = []
        for result in results:
            # parent format: "documents/{uri_without_scheme}"
            uri = result.get("parent", "").split("documents/", 1)[-1]
            doc_url = f"https://{uri}" if uri else "(unknown url)"
            snippet = (result.get("content") or "").strip().replace("\n", " ")[:200]
            lines.append(f"  - {doc_url}\n    {snippet}")
        sections.append(f"{topic}:\n" + "\n".join(lines))

    return "\n\n".join(sections)
