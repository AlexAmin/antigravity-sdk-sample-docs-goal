"""Custom Google Docs integration — the hand-written half of this project.

Independent of the Antigravity SDK (no `google.antigravity` imports). It exposes a few
SEMANTIC tools the agent calls — `read_document`, `replace_text`, `link_text`,
`insert_text` — and does all the Google Docs index arithmetic in code, so the model never
handles raw request JSON or character indices.

The trick for indices: `documents.get` returns every text run with its real `startIndex`,
so we build a char->API-index map once per read. To act on a phrase we string-match it in
the plain text (which gives an offset) and translate the offset to the real range via the
map. This is exact even when a phrase spans multiple runs. Note the API counts indices in
UTF-16 code units, so the map tracks each character's [start, end) range.
"""

import os
import google.auth
from googleapiclient.discovery import build


def get_docs_service():
    """Authenticated Google Docs client (honours a local credentials.json)."""
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists("credentials.json"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("credentials.json")
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/documents"])
    return build("docs", "v1", credentials=creds)


# --- reading + char -> API-index map -----------------------------------------

def _collect_runs(elements, chars, starts, ends):
    """Walk structural elements in order, recording each character's Docs API index range.

    Docs API indices count UTF-16 code units, not code points, so an astral char (emoji
    etc.) occupies TWO indices; we track per-char [start, end) so ranges stay exact."""
    for element in elements:
        if "paragraph" in element:
            for paragraph_element in element["paragraph"].get("elements", []):
                run = paragraph_element.get("textRun")
                if not run:
                    continue
                paragraph_element_index = paragraph_element.get("startIndex", 0)
                for char in run.get("content", ""):
                    width = len(char.encode("utf-16-le")) // 2
                    # Skip Unicode Private Use Area chars (Google Docs uses them for smart
                    # chips, list-bullet/checkbox glyphs, etc.). They render as boxes and
                    # confuse the model. We drop them from the text but still advance the
                    # index, so the remaining characters keep their correct API indices.
                    if not 0xE000 <= ord(char) <= 0xF8FF:  # Private Use Area (chips/bullets/icons)
                        chars.append(char)
                        starts.append(paragraph_element_index)
                        ends.append(paragraph_element_index + width)
                    paragraph_element_index += width
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _collect_runs(cell.get("content", []), chars, starts, ends)


def _read_live(doc_id):
    """Returns (plain_text, starts, ends, doc) for a Google Doc.

    starts[i]/ends[i] are the Docs API [start, end) indices of plain_text[i].
    """
    doc = get_docs_service().documents().get(documentId=doc_id).execute()
    chars, starts, ends = [], [], []
    _collect_runs(doc.get("body", {}).get("content", []), chars, starts, ends)
    return "".join(chars), starts, ends, doc


def _batch(doc_id, requests):
    return get_docs_service().documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()


def read_document(doc_id: str) -> str:
    """Returns the plain text of the Google Doc."""
    text, _, _, _ = _read_live(doc_id)
    return text


# --- semantic edit tools (what the agent calls) ------------------------------

_NO_MATCH = ("No occurrence of '{find}' found — nothing changed. The match is exact and "
             "case-sensitive; re-check quotes, dashes, and spacing against the document text.")


def replace_text(doc_id: str, find: str, replace: str) -> str:
    """Replaces every occurrence of `find` with `replace`. Reports the real match count."""
    if not find:
        return "'find' must not be empty — nothing changed."
    response = _batch(doc_id, [{"replaceAllText": {
        "containsText": {"text": find, "matchCase": True}, "replaceText": replace}}])
    count = response.get("replies", [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
    if not count:
        return _NO_MATCH.format(find=find)
    return f"Replaced {count} occurrence(s) of '{find}' with '{replace}'."


def link_text(doc_id: str, text: str, url: str) -> str:
    """Turns every occurrence of `text` into a hyperlink to `url`.

    Resolves the exact index range of each occurrence in code — the model never
    computes indices.
    """
    if not text:
        return "'text' must not be empty — nothing linked."
    plain, starts, ends, _ = _read_live(doc_id)
    requests, i = [], plain.find(text)
    while i >= 0:
        requests.append({"updateTextStyle": {
            "range": {"startIndex": starts[i], "endIndex": ends[i + len(text) - 1]},
            "textStyle": {"link": {"url": url}},
            "fields": "link",
        }})
        i = plain.find(text, i + len(text))
    if not requests:
        return f"'{text}' not found in the document; nothing linked."
    _batch(doc_id, requests)
    return f"Linked {len(requests)} occurrence(s) of '{text}' -> {url}."


def insert_text(doc_id: str, text: str, after: str | None = None) -> str:
    """Inserts `text` after the first occurrence of `after` (or at the end if `after` is None)."""
    plain, starts, ends, doc = _read_live(doc_id)
    if after:
        i = plain.find(after)
        if i < 0:
            return f"Anchor '{after}' not found; nothing inserted."
        index = ends[i + len(after) - 1]
    else:
        index = max(1, doc["body"]["content"][-1].get("endIndex", 2) - 1)
    _batch(doc_id, [{"insertText": {"location": {"index": index}, "text": text}}])
    return "Inserted text."


def remove_links(doc_id: str, text: str | None = None) -> str:
    """Removes hyperlink formatting while keeping the text. If `text` is given, only unlinks
    that phrase; otherwise removes every link in the document."""
    doc = get_docs_service().documents().get(documentId=doc_id).execute()
    requests = []

    def walk(elements):
        for el in elements:
            if "paragraph" in el:
                for paragraph_element in el["paragraph"].get("elements", []):
                    run = paragraph_element.get("textRun")
                    if not run or not run.get("textStyle", {}).get("link"):
                        continue
                    if text and text not in run.get("content", ""):
                        continue
                    # Empty textStyle + fields="link" clears ONLY the link (text/other styles kept).
                    requests.append({"updateTextStyle": {
                        "range": {"startIndex": paragraph_element["startIndex"],
                                  "endIndex": paragraph_element["endIndex"]},
                        "textStyle": {}, "fields": "link"}})
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))

    walk(doc.get("body", {}).get("content", []))
    if not requests:
        return "No matching links found to remove."
    _batch(doc_id, requests)
    return f"Removed {len(requests)} link(s)."


def set_document_text(doc_id: str, new_text: str) -> None:
    """Replaces the entire document body with new_text. Used only by the human 'Edit' override."""
    doc = get_docs_service().documents().get(documentId=doc_id).execute()
    end = doc["body"]["content"][-1].get("endIndex", 2)
    requests = []
    if end > 2:  # can't delete the doc's final newline
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1}}})
    requests.append({"insertText": {"location": {"index": 1}, "text": new_text}})
    _batch(doc_id, requests)


# --- approval preview ---------------------------------------------------------

def preview_edit(tool_name: str, args: dict, current_text: str):
    """Returns (summary, new_text_or_None) describing a pending edit for the approval gate.

    new_text is None for styling-only edits (links), which have no visible text diff.
    """
    if tool_name == "replace_text":
        find, replacement = args.get("find", ""), args.get("replace", "")
        if not find or find not in current_text:
            return f"Replace '{find}' -> '{replacement}' (no match — nothing will change)", current_text
        return f"Replace '{find}' -> '{replacement}'", current_text.replace(find, replacement)
    if tool_name == "insert_text":
        text, after = args.get("text", ""), args.get("after")
        if after:
            if after not in current_text:
                # Mirror the live tool, which refuses when the anchor is missing.
                return f"Insert text after '{after}' (anchor not found — nothing will change)", current_text
            pos = current_text.find(after) + len(after)
            return f"Insert text after '{after}'", current_text[:pos] + text + current_text[pos:]
        return "Insert text at end", current_text + text
    if tool_name == "link_text":
        text, url = args.get("text", ""), args.get("url", "")
        return f"Link {current_text.count(text)} occurrence(s) of '{text}' -> {url}", None
    if tool_name == "remove_links":
        text = args.get("text")
        return (f"Remove links from '{text}'" if text else "Remove all hyperlinks (text unchanged)"), None
    return f"Apply {tool_name}", None
