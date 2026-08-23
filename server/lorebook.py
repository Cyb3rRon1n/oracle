"""Keyword-triggered lorebook (docs/protocol.md "Protocol v2 additions -
World context -> lorebook"). Campaign context files become small entries;
each turn, only entries whose keys appear in the recent play window are
injected into the DM prompt, under a hard character budget with
deterministic eviction - the SillyTavern World Info pattern, chosen over
whole-file injection so a long campaign's lore scales without drowning the
prompt (and without trusting the model to recall what matters).

Entries must be self-contained: injected text never assumes its own heading
is visible, so every injected entry carries its title line back with it."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Character budget for everything one turn's lore injection may add - chars,
# not tokens: a stable proxy (roughly 4 chars/token) that needs no tokenizer
# dependency. Generous by design; the point is a ceiling, not rationing.
MAX_LORE_CHARS = 24_000

SUPPORTED_SUFFIXES = {".txt", ".md", ".json", ".csv"}

_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be", "it",
    "its", "this", "that", "these", "those",
}


class LoreEntry(BaseModel):
    """One injectable chunk of campaign context. `keys` are lowercase
    trigger phrases; `constant` entries ride along every turn regardless of
    hits (a campaign's premise, say); `priority` breaks budget ties, higher
    first."""

    title: str = ""
    keys: list[str] = Field(default_factory=list)
    content: str
    priority: int = 0
    constant: bool = False

    def injection_text(self) -> str:
        if self.title and self.title not in self.content:
            return f"[{self.title}]\n{self.content}"
        return self.content


class Lorebook:
    def __init__(self) -> None:
        self.entries: list[LoreEntry] = []

    # ---- parsing ---------------------------------------------------------

    @classmethod
    def from_files(cls, paths: list[Path]) -> "Lorebook":
        book = cls()
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("Lorebook skipping unreadable file %s: %s", path, exc)
                continue
            suffix = path.suffix.lower()
            stem = path.stem.replace("_", " ").replace("-", " ")
            parser = {
                ".json": cls._parse_json,
                ".csv": cls._parse_csv,
            }.get(suffix)
            if parser is not None:
                entries = parser(text, stem)
            else:
                entries = cls._parse_sections(text, stem)
            book.entries.extend(entries)
        return book

    @classmethod
    def _parse_sections(cls, text: str, fallback_title: str) -> list[LoreEntry]:
        """md/txt: split on markdown headings (#..####). A section's heading
        becomes its title and its significant words its keys; an explicit
        'Keywords:' line inside the section adds exact triggers."""
        sections: list[tuple[str, str]] = []
        current_title = ""
        lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("#"):
                if lines:
                    sections.append((current_title, "\n".join(lines).strip()))
                current_title = line.lstrip("#").strip()
                lines = []
            else:
                lines.append(line)
        if lines:
            sections.append((current_title, "\n".join(lines).strip()))

        entries: list[LoreEntry] = []
        for title, content in sections:
            if not content:
                continue
            explicit_keys, body = cls._split_keyword_line(content)
            keys = explicit_keys or _significant_words(title or fallback_title)
            entries.append(LoreEntry(title=title or fallback_title, keys=keys, content=body))
        if not entries and text.strip():
            entries.append(
                LoreEntry(title=fallback_title, keys=_significant_words(fallback_title), content=text.strip())
            )
        return entries

    @staticmethod
    def _split_keyword_line(content: str) -> tuple[list[str], str]:
        out_lines: list[str] = []
        keys: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("keywords:") and ":" in stripped:
                raw = stripped.split(":", 1)[1]
                keys = [k.strip().lower() for k in raw.split(",") if k.strip()]
            else:
                out_lines.append(line)
        return keys, "\n".join(out_lines).strip()

    @classmethod
    def _parse_json(cls, text: str, fallback_title: str) -> list[LoreEntry]:
        """json: either a list of entry objects ({keys/content/priority/
        constant/title}) or {"entries": [...]}. Anything malformed degrades
        to one whole-file entry rather than dropping the file silently."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Lorebook: invalid JSON file for %s", fallback_title)
            return [LoreEntry(title=fallback_title, keys=_significant_words(fallback_title), content=text.strip())]
        raw_entries = data.get("entries") if isinstance(data, dict) else data
        if not isinstance(raw_entries, list):
            raw_entries = [data]
        entries: list[LoreEntry] = []
        for item in raw_entries:
            if isinstance(item, dict) and item.get("content"):
                entries.append(LoreEntry.model_validate(item))
            elif isinstance(item, str) and item.strip():
                entries.append(
                    LoreEntry(title=fallback_title, keys=_significant_words(fallback_title), content=item.strip())
                )
        return entries

    @staticmethod
    def _parse_csv(text: str, fallback_title: str) -> list[LoreEntry]:
        """csv: header row with at least keys,content (comma-separated key
        list); priority/constant/title optional columns."""
        entries: list[LoreEntry] = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            content = (row.get("content") or "").strip()
            if not content:
                continue
            keys = [k.strip().lower() for k in (row.get("keys") or "").split(",") if k.strip()]
            priority = 0
            constant = False
            if row.get("priority"):
                try:
                    priority = int(row["priority"])
                except ValueError:
                    pass
            if row.get("constant"):
                constant = row["constant"].strip().lower() in ("1", "true", "yes")
            entries.append(
                LoreEntry(
                    title=(row.get("title") or fallback_title).strip(),
                    keys=keys,
                    content=content,
                    priority=priority,
                    constant=constant,
                )
            )
        return entries

    # ---- per-turn injection ---------------------------------------------

    def injection_block(self, window_text: str, budget_chars: int = MAX_LORE_CHARS) -> str:
        """The prompt block for this turn: constant entries plus entries whose
        keys appear in window_text (case-insensitive), ranked constant-first,
        then priority, then longest-content-last-for-tiebreak determinism,
        greedily under the budget. Empty string when nothing qualifies - the
        same don't-render-the-absent-default convention WorldState.
        narrator_context follows."""
        lowered = window_text.lower()
        hits = [
            e
            for e in self.entries
            if e.constant or any(key in lowered for key in e.keys if key)
        ]
        # Eviction order when over budget (docs/protocol.md): non-constants
        # that didn't actually hit go first, then lowest priority, then
        # longest. Sorting candidates by (constant desc, hit desc, priority
        # desc, length asc) fills exactly that order.
        hits.sort(
            key=lambda e: (
                e.constant,
                any(key in lowered for key in e.keys if key),
                e.priority,
                -len(e.content),
            ),
            reverse=True,
        )
        parts: list[str] = []
        remaining = budget_chars
        for entry in hits:
            text = entry.injection_text()
            if len(text) > remaining:
                continue
            parts.append(text)
            remaining -= len(text) + 2  # the "\n\n" join below
        if not parts:
            return ""
        return "Campaign lore:\n" + "\n\n".join(parts)


def _significant_words(title: str) -> list[str]:
    """Heading words as trigger keys, minus stopwords - 'The Warden of the
    Veil' triggers on 'warden'/'veil', not on 'the'/'of'."""
    return [w for w in title.lower().split() if w.isalnum() and w not in _STOPWORDS]
