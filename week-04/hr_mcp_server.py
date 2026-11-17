"""MCP server with Hartwell Foods HR tools: policy search and leave balances.

Tools
    search_policy(query)       the 4 HR document sections closest to the query
    get_leave_balance(email)   an employee's vacation, sick and personal days

The server speaks MCP over stdio, so an AI app (Claude Code, or an agent)
starts it as a subprocess. Two modes:

    HR staff (default): get_leave_balance takes any employee's work email.
        .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py

    One employee: get_leave_balance takes no inputs and returns only that
    employee's balance.
        .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py --as-employee dana.whitfield@hartwellfoods.example

The HR documents are indexed in a local Chroma database (week-04/chroma_db,
git-ignored). It is rebuilt automatically when a document changes; to force it:
        .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py --rebuild

The chunking and search follow week-03/ask.py: one chunk per document section,
Chroma's default embedding model, cosine similarity.

stdout carries the MCP protocol, so every status message goes to stderr.
"""

import argparse
import csv
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

import chromadb
from bs4 import BeautifulSoup
from chromadb.config import Settings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "hartwell-hr"
DOCS_DIR = DATA_DIR / "hr-docs"
EMPLOYEES_CSV = DATA_DIR / "employees.csv"
DB_DIR = HERE / "chroma_db"
COLLECTION = "hr_docs"

TOP_K = 4
MAX_CHUNK_CHARS = 1500  # longer sections are split into parts
CHUNKER_VERSION = "1"  # bump when the chunking logic changes, to force a rebuild

NO_RECORD = ("No employee record matches {who}. Do not state, estimate, or guess a "
             "leave balance. Say that the balance could not be found and that a person "
             "from HR will follow up.")


def log(message):
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Loading and chunking the HR documents (adapted from week-03/ask.py)
# ---------------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", text).strip()


HTML_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre",
                   "blockquote", "tr", "dt", "dd"]


def load_html(path):
    """Return the page's blocks, ("heading", level, text) or ("text", 0, text), and its title."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    fallback_title = soup.title.get_text().split("|")[0].strip() if soup.title else ""
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup

    blocks = []
    for el in root.find_all(HTML_BLOCK_TAGS):
        if el.find_parent(HTML_BLOCK_TAGS):
            continue  # already included in its parent's text
        if el.name == "tr":
            text = " | ".join(clean(c.get_text(" ")) for c in el.find_all(["th", "td"]))
        else:
            text = clean(el.get_text(" "))
        if not text:
            continue
        if el.name[0] == "h" and el.name[1:].isdigit():
            blocks.append(("heading", int(el.name[1]), text))
        else:
            blocks.append(("text", 0, "- " + text if el.name == "li" else text))
    return blocks, fallback_title


def split_long(paragraphs):
    """Group paragraphs into parts no longer than MAX_CHUNK_CHARS."""
    pieces = []
    for p in paragraphs:
        if len(p) <= MAX_CHUNK_CHARS:
            pieces.append(p)
        else:  # a single very long paragraph: split between sentences
            pieces.extend(re.split(r"(?<=[.!?])\s+", p))
    parts, current = [], []
    for piece in pieces:
        if current and len("\n".join(current + [piece])) > MAX_CHUNK_CHARS:
            parts.append(current)
            current = []
        current.append(piece)
    if current:
        parts.append(current)
    return parts


def make_chunks(file_name, blocks, fallback_title):
    """One chunk per section. Sections are the most common heading level; a
    bigger heading before them is the title, and a short preamble (like
    "Version 6.1, current.") is repeated on every chunk."""
    levels = Counter(level for kind, level, _ in blocks if kind == "heading")
    section_level = max(levels, key=lambda lv: (levels[lv], lv)) if levels else None

    title, group, preamble, sections, current = None, None, [], [], None
    for kind, level, text in blocks:
        if kind == "heading" and level < section_level:
            if not sections:
                title = text
            else:
                group, current = text, None
        elif kind == "heading" and level == section_level:
            current = (f"{group} > {text}" if group else text, [])
            sections.append(current)
        else:
            if kind == "heading":  # a sub-heading inside a section
                text += ":"
            (current[1] if current else preamble).append(text)

    title = title or fallback_title or Path(file_name).stem.replace("-", " ").title()

    doc_info = ""
    if preamble and len(" ".join(preamble)) <= 400:
        doc_info = " ".join(p if p.endswith((".", "!", "?", ":")) else p + "." for p in preamble)
    elif preamble:
        sections.insert(0, ("Introduction", preamble))

    chunks = []
    for heading, paragraphs in sections:
        if not paragraphs:
            continue
        parts = split_long(paragraphs)
        for i, part in enumerate(parts, start=1):
            section = heading if len(parts) == 1 else f"{heading} (part {i} of {len(parts)})"
            lines = [f"{title} > {section}"]
            if doc_info:
                lines.append(f"About this document: {doc_info}")
            lines += part
            chunks.append({"text": "\n".join(lines), "source": file_name,
                           "title": title, "section": section})
    return chunks


def find_documents():
    return sorted(p for p in DOCS_DIR.glob("*") if p.suffix.lower() in (".html", ".htm"))


# ---------------------------------------------------------------------------
# Chroma database
# ---------------------------------------------------------------------------

def fingerprint(files):
    """Changes whenever a document or the chunker changes."""
    digest = hashlib.sha256(CHUNKER_VERSION.encode())
    for path in files:
        stat = path.stat()
        digest.update(f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def build_index(db, files, fp):
    if COLLECTION in [c.name for c in db.list_collections()]:
        db.delete_collection(COLLECTION)
    collection = db.create_collection(
        COLLECTION,
        configuration={"hnsw": {"space": "cosine"}},  # similarity = 1 - distance
        metadata={"fingerprint": fp},
    )
    chunks = []
    for path in files:
        try:
            blocks, fallback_title = load_html(path)
        except Exception as exc:
            log(f"  Skipped {path.name}: could not read it ({exc})")
            continue
        file_chunks = make_chunks(path.name, blocks, fallback_title)
        log(f"  {path.name}: {len(file_chunks)} chunks")
        chunks += file_chunks
    if chunks:
        collection.add(
            ids=[f"{c['source']}::{i}" for i, c in enumerate(chunks)],
            documents=[c["text"] for c in chunks],
            metadatas=[{k: c[k] for k in ("source", "title", "section")} for c in chunks],
        )
    log(f"Database ready: {len(chunks)} chunks from {len(files)} documents.")
    return collection


def open_index(rebuild=False):
    files = find_documents()
    if not files:
        raise SystemExit(f"No HTML documents found in {DOCS_DIR}")
    fp = fingerprint(files)
    db = chromadb.PersistentClient(path=str(DB_DIR), settings=Settings(anonymized_telemetry=False))

    reason = "rebuild requested" if rebuild else None
    if not reason:
        if COLLECTION not in [c.name for c in db.list_collections()]:
            reason = "no database yet"
        elif (db.get_collection(COLLECTION).metadata or {}).get("fingerprint") != fp:
            reason = "documents changed"
    if reason:
        log(f"Building the HR document database ({reason})...")
        return build_index(db, files, fp)
    return db.get_collection(COLLECTION)


# ---------------------------------------------------------------------------
# Employee records
# ---------------------------------------------------------------------------

def find_employee(email):
    """The employees.csv row for this email (case-insensitive), or None.
    The file is re-read on every call so edits show up right away."""
    wanted = email.strip().lower()
    with EMPLOYEES_CSV.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["email"].strip().lower() == wanted:
                return row
    return None


def balance_for(email, who=None):
    """The balance as a dict, or a ToolError telling the model not to state one."""
    row = find_employee(email) if email.strip() else None
    if row is None:
        raise ToolError(NO_RECORD.format(who=who or f"'{email}'"))
    return {
        "email": row["email"],
        "name": row["name"],
        "vacation_days_left": int(row["vacation_days_left"]),
        "sick_days_left": int(row["sick_days_left"]),
        "personal_days_left": int(row["personal_days_left"]),
    }


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

class EmployeeServer(MCPServer):
    """In --as-employee mode, refuse get_leave_balance calls that pass any input.

    The SDK would otherwise ignore an unexpected argument such as another
    employee's email and return the session employee's balance, which the model
    could then present as the other person's."""

    async def call_tool(self, name, arguments, context=None):
        if name == "get_leave_balance" and arguments:
            raise ToolError("get_leave_balance takes no inputs and returns only the balance of "
                            "the employee this session is for. It cannot look up anyone else. "
                            "Do not state another employee's balance.")
        return await super().call_tool(name, arguments, context)


def create_server(collection, as_employee=None):
    server = (EmployeeServer if as_employee else MCPServer)(
        "hartwell-hr",
        instructions="Hartwell Foods HR tools: search the HR policy documents and look up "
                     "leave balances. Cite the source file of each policy fact.",
    )

    @server.tool()
    def search_policy(query: str) -> dict:
        """Search Hartwell Foods' HR policy documents (leave, vacation, sick days, jury duty,
        bereavement, parental and family leave, remote work, benefits enrollment, reporting
        workplace concerns). Returns the 4 closest document sections, each with its source
        file, section heading and similarity (0 to 1, higher is closer). Use only what the
        returned text says, and cite the source file for each fact."""
        if not query.strip():
            raise ToolError("The search query is empty. Pass a few words describing the policy question.")
        result = collection.query(
            query_texts=[query],
            n_results=min(TOP_K, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        return {"results": [
            {"rank": rank, "source": meta["source"], "section": meta["section"],
             "similarity": round(1 - distance, 3), "text": text}
            for rank, (text, meta, distance) in enumerate(
                zip(result["documents"][0], result["metadatas"][0], result["distances"][0]), start=1)
        ]}

    if as_employee:
        @server.tool()
        def get_leave_balance() -> dict:
            """Get the leave balance (vacation, sick and personal days left) of the employee
            this session is for. It takes no inputs and cannot look up any other employee."""
            return balance_for(as_employee, who="the employee this session is for")
    else:
        @server.tool()
        def get_leave_balance(email: str) -> dict:
            """Get an employee's leave balance (vacation, sick and personal days left) by their
            work email address, for example dana.whitfield@hartwellfoods.example."""
            return balance_for(email)

    return server


def main():
    parser = argparse.ArgumentParser(description="Hartwell Foods HR MCP server (stdio).")
    parser.add_argument("--as-employee", metavar="EMAIL",
                        help="serve one employee: get_leave_balance takes no inputs and "
                             "returns only this employee's balance")
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the document database and exit")
    args = parser.parse_args()

    if args.rebuild:
        open_index(rebuild=True)
        return

    collection = open_index()  # built before serving, so the first search isn't slow
    if args.as_employee:
        if find_employee(args.as_employee) is None:
            log(f"Warning: no employee record matches {args.as_employee}; "
                "get_leave_balance will return an error.")
        log(f"Serving as employee {args.as_employee}.")
    create_server(collection, args.as_employee).run("stdio")


if __name__ == "__main__":
    main()
