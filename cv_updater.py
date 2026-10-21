#!/usr/bin/env python3
"""Update citation counts in an academic CV Markdown export."""

import argparse
from datetime import date
from difflib import SequenceMatcher
import json
import logging
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SECTIONS = {"JOURNAL PUBLICATIONS", "BOOKS", "BOOK CHAPTERS",
            "PUBLISHED CONFERENCE PROCEEDINGS"}
HEADING_RE = re.compile(r"(?m)^#{1,6}\s+\*\*(.*?)\*\*\s*$")
YEAR_RE = re.compile(r"\((?P<year>\d{4}|in\s+press)\)\s*\.?\s*", re.I)
CITATION_RE = re.compile(r"\(\s*times\s+cited\s*:\s*(?P<count>\d+)\s*\\?\)", re.I)


def choose_citation_count(old: int | None, new: int) -> int:
    """Choose the count to write when the CV and Scholar disagree."""
    # TODO(user): Decide whether a lower Scholar count should replace the old
    # CV count; Scholar sometimes splits or merges publication versions.
    return new


class CVPublicationUpdater:
    """Match CV entries to Scholar publications while preserving formatting."""

    def __init__(self, scholar_id: str, similarity_threshold: int = 88,
                 snapshot: Optional[str] = None):
        self.scholar_id = scholar_id
        self.similarity_threshold = similarity_threshold
        self.snapshot = Path(snapshot) if snapshot else None
        self.fetched = date.today()
        self.updated_entries: List[Dict] = []
        self.unmatched_entries: List[Dict] = []
        self.duplicate_matches: List[Dict] = []

    def fetch_scholar_publications(self) -> List[Dict]:
        """Read the snapshot, or lazily import scholarly for a live fetch."""
        if self.snapshot:
            data = json.loads(self.snapshot.read_text(encoding="utf-8"))
            self.fetched = date.fromisoformat(data["fetched"][:10])
            logger.info("Loaded %d publications from %s",
                        len(data["publications"]), self.snapshot)
            return data["publications"]
        try:
            from scholarly import scholarly
        except ImportError as exc:
            raise RuntimeError("scholarly is required without --snapshot") from exc
        author = scholarly.fill(scholarly.search_author_id(self.scholar_id),
                                sections=["publications"])
        publications = []
        for pub in author["publications"]:
            try:
                filled = scholarly.fill(pub)
                bib = filled.get("bib", {})
                if bib.get("title"):
                    publications.append({"title": bib["title"],
                                         "year": bib.get("pub_year"),
                                         "citations": filled.get("num_citations", 0)})
            except Exception as exc:
                logger.warning("Could not fetch a publication: %s", exc)
        return publications

    @staticmethod
    def clean_markdown(text: str) -> str:
        """Remove Markdown emphasis and Google Docs punctuation escapes."""
        text = re.sub(r"\\([\\*_.,!?()\-])", r"\1", text)
        return re.sub(r"\s+", " ", text.replace("*", "")).strip()

    @classmethod
    def extract_title_from_entry(cls, entry_text: str) -> str:
        """Read the APA title after the year or in-press marker."""
        marker = YEAR_RE.search(entry_text)
        if not marker:
            return ""
        remainder = entry_text[marker.end():]
        # A Google Docs export sometimes joins the venue as "title.Journal".
        period = re.search(r"\.(?=\s|$|[A-Z][a-z]+\s)", remainder)
        question = re.search(r"\?(?=\s|$)", remainder)
        ending = period or question
        if question and (not period or question.start() < period.start()):
            after_question = remainder[question.end():]
            next_comma = after_question.find(",")
            next_period = after_question.find(".")
            # "Why? A second sentence. Journal, ..." is one full title;
            # "Why? Journal, ..." ends at the question mark.
            if not (period and next_period >= 0 and
                    (next_comma < 0 or next_period < next_comma)):
                ending = question
        return cls.clean_markdown(remainder[:ending.start()]) if ending else ""

    @staticmethod
    def extract_year(entry_text: str) -> Optional[int]:
        """Return the CV year, with None for an in-press work."""
        marker = YEAR_RE.search(entry_text)
        if not marker or marker.group("year").lower().startswith("in"):
            return None
        return int(marker.group("year"))

    @classmethod
    def normalize_title(cls, title: str) -> str:
        """Compare titles without case, punctuation, or extra spacing."""
        return " ".join(re.findall(r"\w+", cls.clean_markdown(title).lower(), re.UNICODE))

    @staticmethod
    def similarity(first: str, second: str) -> int:
        """Use optional fuzzy matching or the standard library fallback."""
        try:
            from fuzzywuzzy import fuzz
        except ImportError:
            try:
                from rapidfuzz import fuzz
            except ImportError:
                return round(100 * SequenceMatcher(None, first, second).ratio())
        return round(fuzz.ratio(first, second))

    @classmethod
    def merge_duplicate_records(cls, publications: List[Dict]) -> List[Dict]:
        """Collapse Scholar's unmerged versions of one work into a single record.

        Scholar often keeps stub copies (truncated titles, chapter-number prefixes,
        off-by-one years, zero citations) beside the real record. Each group keeps
        the most-cited record's fields plus every year seen, for year matching.
        """
        def key(pub: Dict) -> str:
            return re.sub(r"^\d+\s+", "", cls.normalize_title(pub.get("title", "")))

        def same_work(first: str, second: str) -> bool:
            shorter, longer = sorted((first, second), key=len)
            if len(shorter.split()) >= 4 and longer.startswith(shorter):
                return True
            return cls.similarity(first, second) >= 95

        groups: List[List[Dict]] = []
        for pub in publications:
            for group in groups:
                if key(pub) and same_work(key(pub), key(group[0])):
                    group.append(pub)
                    break
            else:
                groups.append([pub])
        merged = []
        for group in groups:
            best = max(group, key=lambda pub: (int(pub.get("citations") or 0),
                                               len(pub.get("title", ""))))
            years = [pub.get("year") for pub in group]
            merged.append({**best, "years": years})
            if len(group) > 1:
                logger.debug("Merged %d Scholar records: %s", len(group), best.get("title"))
        return merged

    @staticmethod
    def year_is_plausible(cv_year: Optional[int], scholar_year: object,
                          max_gap: int = 1) -> bool:
        """Reject known dates more than max_gap years apart."""
        if cv_year is None or scholar_year in (None, "", "in press"):
            return True
        try:
            return abs(cv_year - int(scholar_year)) <= max_gap
        except (TypeError, ValueError):
            return True

    @staticmethod
    def extract_publication_entries(cv_text: str) -> List[Dict]:
        """Find numbered journal lines and blank-line-separated paragraphs."""
        headings = list(HEADING_RE.finditer(cv_text))
        entries = []
        for index, heading in enumerate(headings):
            section = re.sub(r"\s+", " ", heading.group(1).strip()).upper()
            if section not in SECTIONS:
                continue
            start = heading.end()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(cv_text)
            body = cv_text[start:end]
            if section == "JOURNAL PUBLICATIONS":
                spans = re.finditer(r"(?m)^\d+[.)]\s+[^\r\n]*", body)
            else:
                spans = re.finditer(r"(?ms)^\S[^\r\n]*(?:\r?\n(?!\s*$|#)[^\r\n]+)*", body)
            for match in spans:
                original = match.group()
                if YEAR_RE.search(original):
                    entries.append({"section": section, "original_text": original,
                                    "start_pos": start + match.start(),
                                    "end_pos": start + match.end()})
        logger.info("Extracted %d publication entries", len(entries))
        return entries

    @staticmethod
    def update_entry_with_citations(entry_text: str, citations: int) -> str:
        """Replace only count digits, or append before trailing line markup."""
        annotation = CITATION_RE.search(entry_text)
        if annotation:
            start, end = annotation.span("count")
            return entry_text[:start] + str(citations) + entry_text[end:]
        trailing = re.search(r"(?:[ \t]*\\+)?[ \t]*(?:\r?\n[ \t]*)*\Z", entry_text)
        assert trailing is not None
        body = entry_text[:trailing.start()]
        suffix = entry_text[trailing.start():]
        return body.rstrip(" \t") + f" (Times cited: {citations})" + suffix

    @staticmethod
    def update_footnote(cv_text: str, fetched: date) -> str:
        """Replace footnote date and source with the snapshot provenance."""
        day = fetched.day
        ordinal = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        formatted = f"{fetched.strftime('%B')} {day}{ordinal}, {fetched.year}"
        return re.sub(r"(?m)^\[\^1\]:[^\r\n]*",
                      f"[^1]:  Citation counts  updated on {formatted} from Google Scholar",
                      cv_text, count=1)

    def update_cv_publications(self, cv_text: str) -> str:
        """Assign plausible Scholar records, then update CV counts."""
        publications = self.merge_duplicate_records(self.fetch_scholar_publications())
        entries = self.extract_publication_entries(cv_text)
        self.updated_entries, self.unmatched_entries, self.duplicate_matches = [], [], []
        candidates = []
        for entry_index, entry in enumerate(entries):
            title = self.extract_title_from_entry(entry["original_text"])
            entry["title"] = title
            normalized = self.normalize_title(title)
            year = self.extract_year(entry["original_text"])
            if not normalized:
                continue
            for pub_index, pub in enumerate(publications):
                scholar_title = self.normalize_title(pub.get("title", ""))
                if scholar_title:
                    score = self.similarity(normalized, scholar_title)
                    # Near-identical titles tolerate an online-first vs. print year lag.
                    max_gap = 2 if score >= 98 else 1
                    year_ok = any(self.year_is_plausible(year, pub_year, max_gap)
                                  for pub_year in pub.get("years", [pub.get("year")]))
                    if year_ok and score >= self.similarity_threshold:
                        candidates.append((score, entry_index, pub_index))
        assignments: Dict[int, Tuple[int, int]] = {}
        scholar_owners: Dict[int, int] = {}
        for score, entry_index, pub_index in sorted(candidates, reverse=True):
            if entry_index in assignments:
                continue
            owner = scholar_owners.get(pub_index)
            if owner is not None:
                if self.normalize_title(entries[owner]["title"]) != self.normalize_title(entries[entry_index]["title"]):
                    continue
                warning = {"title": entries[entry_index]["title"],
                           "scholar_title": publications[pub_index]["title"]}
                self.duplicate_matches.append(warning)
                logger.warning("Duplicate CV entry shares Scholar match: %s", warning["title"])
            else:
                scholar_owners[pub_index] = entry_index
            assignments[entry_index] = (score, pub_index)
        replacements = []
        for entry_index, entry in enumerate(entries):
            if entry_index not in assignments:
                self.unmatched_entries.append(entry)
                logger.warning("No match: %s", entry["title"])
                continue
            score, pub_index = assignments[entry_index]
            pub = publications[pub_index]
            old_match = CITATION_RE.search(entry["original_text"])
            old = int(old_match.group("count")) if old_match else None
            new = choose_citation_count(old, int(pub["citations"]))
            replacements.append((entry["start_pos"], entry["end_pos"],
                                 self.update_entry_with_citations(entry["original_text"], new)))
            self.updated_entries.append({**entry, "old": old, "new": new,
                                         "score": score, "scholar_title": pub["title"]})
            logger.info("Updated: %s (%s -> %d; score %d)", entry["title"], old, new, score)
        for start, end, updated in reversed(replacements):
            cv_text = cv_text[:start] + updated + cv_text[end:]
        return self.update_footnote(cv_text, self.fetched)

    def write_report(self, report_file: str) -> None:
        """Write matched counts, scores, unmatched entries, and warnings."""
        changed = sum(entry["old"] != entry["new"] for entry in self.updated_entries)
        lines = ["# Citation match report", "",
                 f"Source date: {self.fetched.isoformat()} (Google Scholar)", "",
                 f"Matched entries: {len(self.updated_entries)}", "",
                 f"Counts changed or added: {changed}", "",
                 f"Counts already current: {len(self.updated_entries) - changed}", "",
                 f"Unmatched CV entries: {len(self.unmatched_entries)}", "",
                 f"Shared Scholar matches: {len(self.duplicate_matches)}", "",
                 "## Updated entries", "",
                 "| Section | CV title | Scholar title | Score | Count |",
                 "| --- | --- | --- | ---: | ---: |"]
        for entry in self.updated_entries:
            old = "none" if entry["old"] is None else str(entry["old"])
            cells = [entry["section"], entry["title"], entry["scholar_title"],
                     str(entry["score"]), f"{old} → {entry['new']}"]
            lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
        lines += ["", "## Unmatched CV entries", ""]
        for entry in self.unmatched_entries:
            lines.append(f"- **{entry['section']}**: {entry['title'] or entry['original_text'].strip()}")
        lines += ["", "## Shared Scholar matches", ""]
        for match in self.duplicate_matches:
            lines.append(f"- {match['title']} → {match['scholar_title']}")
        Path(report_file).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def process_cv_file(self, input_file: str, output_file: Optional[str] = None,
                        report_file: str = "citation_report.md") -> None:
        """Read the CV and write its updated copy and match report."""
        input_path = Path(input_file)
        updated = self.update_cv_publications(input_path.read_text(encoding="utf-8"))
        if output_file is None:
            output_file = str(input_path.with_name(input_path.stem + "_updated" + input_path.suffix))
        Path(output_file).write_text(updated, encoding="utf-8")
        self.write_report(report_file)
        logger.info("Saved %d updates and %d unmatched entries to %s and %s",
                    len(self.updated_entries), len(self.unmatched_entries), output_file, report_file)


def main() -> None:
    """Run the citation updater from the command line."""
    parser = argparse.ArgumentParser(description="Update CV publication citation counts")
    parser.add_argument("input_file", help="Markdown CV file")
    parser.add_argument("--output", "-o", help="Updated CV path")
    parser.add_argument("--report", default="citation_report.md", help="Match report path")
    parser.add_argument("--snapshot", help="Local Google Scholar JSON snapshot")
    parser.add_argument("--scholar-id", "-s", default="sH44LC4AAAAJ")
    parser.add_argument("--threshold", "-t", type=int, default=88)
    args = parser.parse_args()
    CVPublicationUpdater(args.scholar_id, args.threshold, args.snapshot).process_cv_file(
        args.input_file, args.output, args.report)


if __name__ == "__main__":
    main()
