"""Answer questions about Copperline Manufacturing's finance documents with RAG.

The documents in copperline-docs/ are split into self-contained chunks (one per
section), stored in a local Chroma database, and the most relevant chunks are
sent to Claude along with the question.

Usage, from the top of the repository:
    .venv\\Scripts\\python.exe week-03\\ask.py "Can I expense a $70 dinner in Chicago?"
    .venv\\Scripts\\python.exe week-03\\ask.py                 (ask several questions)
    .venv\\Scripts\\python.exe week-03\\ask.py --rebuild       (re-read the documents)
    .venv\\Scripts\\python.exe week-03\\ask.py --list-chunks   (show every chunk)

The database is rebuilt automatically when documents are added, removed or
changed; --rebuild forces it.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import anthropic
import chromadb
import docx
import pymupdf
from bs4 import BeautifulSoup
from chromadb.config import Settings
from docx.table import Table
from docx.text.paragraph import Paragraph

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DOCS_DIR = HERE / "copperline-docs"
DB_DIR = HERE / "chroma_db"
COLLECTION = "copperline_docs"

# The key lives next to the repository, never inside it.
KEY_FILES = [REPO_ROOT.parent / "API-Key.txt", REPO_ROOT.parent / "API Key.txt"]

MODEL = "claude-sonnet-5"
TOP_K = 4
PREVIEW_CHARS = 150
MAX_CHUNK_CHARS = 1500  # longer sections are split into parts
CHUNKER_VERSION = "2"  # bump when the chunking logic changes, to force a rebuild

# Facts a section relies on but states elsewhere in its document. Each note is
# added to every chunk of that file whose text contains the trigger phrase.
CONTEXT_NOTES = [
    {
        "file": "month-end-close-calendar.html",
        "if_contains": "business day",
        "note": "Note: business day 1 is the first business day of the new month; "
                "business day minus 1 is the last business day of the month being closed.",
    },
    {
        "file": "travel-expense-policy.html",
        "if_contains": "described below",
        "note": "Note: the client entertainment rules are in this policy's "
                "'Client entertainment' section.",
    },
]

SYSTEM_PROMPT = """You answer questions from Copperline Manufacturing employees about company finance policies.

Answer using only the sources in the user's message:
- State only facts that appear in the sources. Do not use outside knowledge or assume what a policy probably says.
- After each fact, cite the source file it came from in square brackets, for example [travel-expense-policy.html].
- If the sources do not contain the answer, say that the provided documents don't answer the question, and don't guess. If they answer only part of it, answer that part and say what is missing.
- Keep the answer short and direct."""


# ---------------------------------------------------------------------------
# Loading documents
#
# Each loader turns a file into a list of blocks: ("heading", level, text) or
# ("text", 0, text), plus a fallback title. make_chunks() then works the same
# way for every file type.
# ---------------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", text).strip()


HTML_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre",
                   "blockquote", "tr", "dt", "dd"]


def load_html(path):
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


def load_pdf(path):
    pdf = pymupdf.open(path)
    fallback_title = (pdf.metadata or {}).get("title", "").split("|")[0].strip()

    # Collect text lines with their font size and weight, skipping page
    # headers/footers.
    lines = []
    for page_no, page in enumerate(pdf):
        height = page.rect.height
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [s for s in line["spans"] if s["text"].strip()]
                text = clean("".join(s["text"] for s in line["spans"]))
                if not spans or not text:
                    continue
                top, bottom = line["bbox"][1], line["bbox"][3]
                if bottom < height * 0.05 or top > height * 0.94:
                    continue
                if re.search(r"\bpage \d+( of \d+)?$", text, re.IGNORECASE):
                    continue
                lines.append({
                    "page": page_no,
                    "block": block["number"],
                    "text": text,
                    "size": round(max(s["size"] for s in spans), 1),
                    "bold": all(s["flags"] & 16 or "bold" in s["font"].lower() for s in spans),
                })

    # Drop running headers that repeat on most pages of a long document.
    if len(pdf) >= 3:
        pages_with = Counter(text for _, text in {(l["page"], l["text"]) for l in lines})
        lines = [l for l in lines if pages_with[l["text"]] <= len(pdf) / 2]

    # Body text is the most common font size; headings are bigger or bold
    # short lines that don't read like a sentence.
    size_counts = Counter()
    for l in lines:
        size_counts[l["size"]] += len(l["text"])
    body_size = size_counts.most_common(1)[0][0] if size_counts else 0
    for l in lines:
        l["heading"] = ((l["size"] > body_size + 0.5 or l["bold"])
                        and len(l["text"]) <= 120
                        and not l["text"].endswith((".", ",", ";")))
    heading_sizes = sorted({l["size"] for l in lines if l["heading"]}, reverse=True)

    # Join wrapped lines back into headings and paragraphs.
    blocks, last_key = [], None
    for l in lines:
        key = (l["page"], l["block"], l["heading"], l["size"] if l["heading"] else None)
        if key == last_key:
            kind, level, text = blocks[-1]
            joined = text[:-1] + l["text"] if text.endswith("-") else text + " " + l["text"]
            blocks[-1] = (kind, level, joined)
        elif l["heading"]:
            blocks.append(("heading", heading_sizes.index(l["size"]) + 1, l["text"]))
        else:
            blocks.append(("text", 0, l["text"]))
        last_key = key
    return blocks, fallback_title


def load_docx(path):
    document = docx.Document(path)
    blocks = []
    for el in document.element.body.iterchildren():
        tag = el.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(el, document)
            text = clean(para.text)
            style = para.style.name if para.style is not None else ""
            match = re.match(r"Heading (\d)", style)
            if not text:
                continue
            if style == "Title":
                blocks.append(("heading", 0, text))
            elif match:
                blocks.append(("heading", int(match.group(1)), text))
            else:
                blocks.append(("text", 0, "- " + text if "List" in style else text))
        elif tag == "tbl":
            for row in Table(el, document).rows:
                blocks.append(("text", 0, " | ".join(clean(c.text) for c in row.cells)))
    return blocks, document.core_properties.title or ""


def load_markdown(path):
    blocks, paragraph = [], []

    def flush():
        if paragraph:
            blocks.append(("text", 0, clean(" ".join(paragraph))))
            paragraph.clear()

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        heading = re.match(r"(#{1,6})\s+(.*)", line)
        item = re.match(r"\s*(?:[-*+]|\d+\.)\s+(.*)", line)
        if heading:
            flush()
            blocks.append(("heading", len(heading.group(1)), clean(heading.group(2))))
        elif item:
            flush()
            blocks.append(("text", 0, "- " + clean(item.group(1))))
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
    flush()
    return blocks, ""


def load_text(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return [("text", 0, clean(p)) for p in re.split(r"\n\s*\n", text) if p.strip()], ""


LOADERS = {
    ".html": load_html,
    ".htm": load_html,
    ".pdf": load_pdf,
    ".docx": load_docx,
    ".md": load_markdown,
    ".txt": load_text,
}


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

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


def make_chunks(rel_path, blocks, fallback_title):
    """Turn a document's blocks into chunks, one per section.

    The section level is the most common heading level (the deeper one on a
    tie). The last bigger heading before the first section is the document
    title; bigger headings after that group the sections that follow.
    """
    levels = Counter(level for kind, level, _ in blocks if kind == "heading")
    section_level = (max(levels, key=lambda lv: (levels[lv], lv)) if levels else None)

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

    title = title or fallback_title or Path(rel_path).stem.replace("-", " ").title()

    # A short preamble (like "Version 4.0 (current)...") describes the whole
    # document, so it goes on every chunk. A long one is its own section.
    doc_info = ""
    if preamble and len(" ".join(preamble)) <= 400:
        doc_info = " ".join(p if p.endswith((".", "!", "?", ":")) else p + "." for p in preamble)
    elif preamble:
        sections.insert(0, ("Introduction", preamble))

    file_name = Path(rel_path).name
    chunks = []
    for heading, paragraphs in sections:
        if not paragraphs:
            continue
        body = "\n".join(paragraphs)
        notes = [n["note"] for n in CONTEXT_NOTES
                 if n["file"] == file_name and n["if_contains"].lower() in body.lower()]
        parts = split_long(paragraphs)
        for i, part in enumerate(parts, start=1):
            section = heading if len(parts) == 1 else f"{heading} (part {i} of {len(parts)})"
            lines = [f"{title} > {section}"]
            if doc_info:
                lines.append(f"About this document: {doc_info}")
            lines += notes + part
            chunks.append({
                "text": "\n".join(lines),
                "source": rel_path,
                "title": title,
                "section": section,
                "doc_info": doc_info,
            })
    return chunks


def find_documents():
    return sorted(p for p in DOCS_DIR.rglob("*")
                  if p.is_file() and p.suffix.lower() in LOADERS)


def chunk_all(files):
    chunks = []
    for path in files:
        rel_path = path.relative_to(DOCS_DIR).as_posix()
        try:
            blocks, fallback_title = LOADERS[path.suffix.lower()](path)
        except Exception as exc:
            print(f"  Skipped {rel_path}: could not read it ({exc})")
            continue
        file_chunks = make_chunks(rel_path, blocks, fallback_title)
        print(f"  {rel_path}: {len(file_chunks)} chunks")
        chunks += file_chunks
    return chunks


# ---------------------------------------------------------------------------
# Chroma database
# ---------------------------------------------------------------------------

def fingerprint(files):
    """Changes whenever a document, the notes or the chunker changes."""
    digest = hashlib.sha256(CHUNKER_VERSION.encode())
    digest.update(json.dumps(CONTEXT_NOTES, sort_keys=True).encode())
    for path in files:
        stat = path.stat()
        digest.update(f"{path.relative_to(DOCS_DIR).as_posix()}|{stat.st_size}|{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def build_index(db, files, fp):
    if COLLECTION in [c.name for c in db.list_collections()]:
        db.delete_collection(COLLECTION)
    # Chroma's default embedding model (all-MiniLM-L6-v2) is used because no
    # embedding function is given. Cosine distance makes similarity = 1 - distance.
    collection = db.create_collection(
        COLLECTION,
        configuration={"hnsw": {"space": "cosine"}},
        metadata={"fingerprint": fp},
    )
    chunks = chunk_all(files)
    if chunks:
        collection.add(
            ids=[f"{c['source']}::{i}" for i, c in enumerate(chunks)],
            documents=[c["text"] for c in chunks],
            metadatas=[{k: c[k] for k in ("source", "title", "section", "doc_info")} for c in chunks],
        )
    print(f"Database ready: {len(chunks)} chunks from {len(files)} documents.\n")
    return collection


def open_index(rebuild=False):
    files = find_documents()
    if not files:
        sys.exit(f"No supported documents found in {DOCS_DIR}")
    fp = fingerprint(files)
    db = chromadb.PersistentClient(path=str(DB_DIR), settings=Settings(anonymized_telemetry=False))

    reason = "rebuild requested" if rebuild else None
    if not reason:
        if COLLECTION not in [c.name for c in db.list_collections()]:
            reason = "no database yet"
        else:
            collection = db.get_collection(COLLECTION)
            if (collection.metadata or {}).get("fingerprint") != fp:
                reason = "documents changed"
    if reason:
        print(f"Building the document database ({reason})...")
        return build_index(db, files, fp)
    return collection


# ---------------------------------------------------------------------------
# Retrieval and answering
# ---------------------------------------------------------------------------

def retrieve(collection, question):
    result = collection.query(
        query_texts=[question],
        n_results=min(TOP_K, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    return [
        {"rank": rank, "similarity": 1 - distance, "source": meta["source"], "text": text}
        for rank, (text, meta, distance) in enumerate(
            zip(result["documents"][0], result["metadatas"][0], result["distances"][0]), start=1)
    ]


def print_sources(hits):
    print(f"Top {len(hits)} sources:")
    for hit in hits:
        preview = clean(hit["text"])[:PREVIEW_CHARS]
        print(f"  {hit['rank']}. similarity {hit['similarity']:.3f}  {hit['source']}")
        print(f"     {preview}...")
    print()


def ask_claude(client, question, hits):
    sources = "\n\n".join(
        f'<source rank="{h["rank"]}" file="{h["source"]}">\n{h["text"]}\n</source>' for h in hits)
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"<sources>\n{sources}\n</sources>\n\nQuestion: {question}",
        }],
    )
    if response.stop_reason == "refusal":
        return "Claude declined to answer this question."
    return "".join(block.text for block in response.content if block.type == "text").strip()


def answer(collection, client, question):
    hits = retrieve(collection, question)
    print_sources(hits)
    try:
        reply = ask_claude(client, question, hits)
    except anthropic.AuthenticationError:
        sys.exit("Claude rejected the API key. Check the key file next to the repository.")
    except anthropic.RateLimitError:
        print("Rate limit reached. Wait a moment and ask again.\n")
        return
    except anthropic.APIStatusError as exc:
        print(f"Claude API error {exc.status_code}: {exc.message}\n")
        return
    except anthropic.APIConnectionError:
        print("Could not reach the Claude API. Check your internet connection.\n")
        return
    print("Answer:")
    print(reply)
    print()


def load_api_key():
    for path in KEY_FILES:
        if path.is_file():
            key = path.read_text(encoding="utf-8-sig").strip()
            if key:
                return key
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    sys.exit(f"No API key found. Save it in {KEY_FILES[0]} (outside the repository).")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Answer questions about Copperline Manufacturing's finance documents.")
    parser.add_argument("question", nargs="*", help="the question to ask (omit to ask several)")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-read every document and rebuild the database")
    parser.add_argument("--list-chunks", action="store_true",
                        help="print every chunk in the database and exit")
    args = parser.parse_args()

    collection = open_index(rebuild=args.rebuild)

    if args.list_chunks:
        stored = collection.get(include=["documents", "metadatas"])
        for chunk_id, text in sorted(zip(stored["ids"], stored["documents"]),
                                     key=lambda pair: (pair[0].split("::")[0], int(pair[0].split("::")[1]))):
            print(f"=== {chunk_id}\n{text}\n")
        return

    question = " ".join(args.question).strip()
    if args.rebuild and not question:
        return

    client = anthropic.Anthropic(api_key=load_api_key())
    if question:
        answer(collection, client, question)
        return

    print("Ask a question about Copperline's finance documents (press Enter on a blank line to quit).")
    while True:
        try:
            question = input("\nQuestion: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            break
        print()
        answer(collection, client, question)


if __name__ == "__main__":
    main()
