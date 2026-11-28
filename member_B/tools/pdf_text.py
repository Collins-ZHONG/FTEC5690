"""Offline prospectus text extractor (standard library only).

Why this exists: the project's PDF requirements list ``pypdf``, but this
workspace has no network access, so ``pip install -r member_A/requirements.txt``
cannot be satisfied. The bundled interpreter therefore cannot import pypdf and
``member_A/extract_prospectus.py`` cannot be re-run.

This module recovers the same text from ``member_A/raw/prospectus.pdf`` with
``re`` + ``zlib`` only: it inflates every FlateDecode stream and concatenates the
literal strings of the text-showing operators. Layout and glyph spacing are
lossy (individual glyphs are emitted as separate strings), so keyword lookup is
done on a whitespace-stripped copy of the text.

Commands
--------
    python member_B/tools/pdf_text.py dump
    python member_B/tools/pdf_text.py find "note value" "8.85" "prepayment amount"

``dump`` writes ``member_B/raw/prospectus_text.txt`` (spaced form, cached) plus
``prospectus_text_nospace.txt`` (search form). ``find`` prints a context window
around each hit of each keyword in the search form.

Verified against the printed pages used by member A: "Original Scheduled
Payments" (printed p59) is recovered with all 73 monthly totals, which is the
same table you get from ``OriginalSchedule.csv``.
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "member_A" / "raw" / "prospectus.pdf"
OUT_DIR = ROOT / "member_B" / "raw"
SPACED = OUT_DIR / "prospectus_text.txt"
NOSPACE = OUT_DIR / "prospectus_text_nospace.txt"


def _inflate() -> list[bytes]:
    data = PDF.read_bytes()
    if not data.startswith(b"%PDF"):
        raise SystemExit(f"not a PDF: {PDF}")
    streams = re.findall(rb"stream\r?\n(.*?)endstream", data, re.S)
    out = []
    for raw in streams:
        try:
            out.append(zlib.decompress(raw))
        except zlib.error:
            continue
    return out


def _strings(content: bytes) -> str:
    """Concatenate the literal strings of a content stream, in drawing order."""
    parts = []
    for match in re.finditer(rb"\((?:\\.|[^\\()])*\)", content):
        chunk = match.group()[1:-1]
        chunk = chunk.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
        parts.append(chunk)
    return " ".join(p.decode("latin-1") for p in parts)


def build(force: bool = False) -> str:
    if SPACED.exists() and NOSPACE.exists() and not force:
        return NOSPACE.read_text(encoding="utf-8")
    if not PDF.exists():
        raise SystemExit(f"missing source PDF: {PDF}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = "\n".join(_strings(s) for s in _inflate())
    SPACED.write_text(text, encoding="utf-8")
    nospace = re.sub(r"\s+", "", text)
    NOSPACE.write_text(nospace, encoding="utf-8")
    return nospace


def find(*keywords: str, window: int = 320) -> list[str]:
    text = build()
    lines = []
    for kw in keywords:
        key = re.sub(r"\s+", "", kw)
        hits = list(re.finditer(re.escape(key), text, re.I))
        lines.append(f"===== {kw} ({len(hits)} hits)")
        for hit in hits[:6]:
            start = max(0, hit.start() - window)
            lines.append(text[start: hit.start() + window + 160])
            lines.append("-" * 40)
    return lines


def main(argv: list[str]) -> int:
    # The prospectus text carries cp1252 punctuation; the console here is GBK.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "dump":
        text = build(force=True)
        print(f"wrote {SPACED} and {NOSPACE} ({len(text)} chars)")
        return 0
    if cmd == "find":
        if not rest:
            print("give at least one keyword")
            return 2
        for line in find(*rest):
            print(line)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
