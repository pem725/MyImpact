"""Focused regression checks for the CV's APA export and citation markup."""

from datetime import date
from pathlib import Path
import unittest

from cv_updater import CVPublicationUpdater, choose_citation_count


CV = Path(__file__).resolve().parents[1] / "pemCV.md"


class CVUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.updater = CVPublicationUpdater("test")
        self.lines = CV.read_text(encoding="utf-8").splitlines()

    def test_extracts_apa_title_after_authors_and_year(self):
        title = self.updater.extract_title_from_entry(self.lines[156])
        self.assertEqual(title, "Willingness for treatment as a predictor of retention and outcomes")

    def test_extracts_question_title_and_in_press_title(self):
        question = self.updater.extract_title_from_entry(self.lines[163])
        in_press = self.updater.extract_title_from_entry(self.lines[239])
        self.assertEqual(question, "Low serum albumin levels, confusion and fecal incontinence: Are these risk factors for pressure ulcers in mobility-impaired hospitalized adults")
        self.assertEqual(in_press, "Past glories feel good but creative minorities push us forward")

    def test_internal_question_marks_and_joined_venue(self):
        joined = self.updater.extract_title_from_entry(self.lines[188])
        full_question_title = self.updater.extract_title_from_entry(self.lines[190])
        rumination = self.updater.extract_title_from_entry(self.lines[191])
        self.assertEqual(joined, "When curiosity breeds intimacy: Taking advantage of intimacy opportunities and transforming boring conversations")
        self.assertEqual(full_question_title, "One size fits all? A Monte-Carlo simulation on the relationship between repeated measures (M)ANOVA and latent curve modeling")
        self.assertEqual(rumination, "When is rumination an adaptive mood repair strategy? Day-to-day rhythms of life in combat veterans with and without post-traumatic stress disorder")

    def test_replaces_escaped_parenthesis_count_without_duplicate(self):
        line = self.lines[157]
        updated = self.updater.update_entry_with_citations(line, 222)
        self.assertIn(r"(Times cited:  222\)", updated)
        self.assertNotIn("174", updated)
        self.assertEqual(updated.count("Times cited"), 1)

    def test_preserves_footnote_marker_after_count(self):
        updated = self.updater.update_entry_with_citations(self.lines[156], 77)
        self.assertIn("(Times cited:  77)[^1]", updated)

    def test_appends_before_trailing_spaces_and_backslash(self):
        line = "Author (2020). A title. Journal.  \\\n"
        updated = self.updater.update_entry_with_citations(line, 4)
        self.assertEqual(updated, "Author (2020). A title. Journal. (Times cited: 4)  \\\n")

    def test_impact_annotation_is_untouched(self):
        line = self.lines[234]
        updated = self.updater.update_entry_with_citations(line, 19)
        self.assertIn("(Impact:  12.3)", updated)
        self.assertIn("(Times cited: 19)", updated)

    def test_typo_annotation_is_replaced(self):
        line = self.lines[259]
        updated = self.updater.update_entry_with_citations(line, 18)
        self.assertIn(r"(TImes  cited:  18\)", updated)
        self.assertEqual(updated.count("cited:"), 1)

    def test_footnote_uses_snapshot_date_and_google_scholar(self):
        original = self.lines[718]
        updated = self.updater.update_footnote(original, date(2026, 10, 21))
        self.assertEqual(updated, "[^1]:  Citation counts  updated on October 21st, 2026 from Google Scholar")

    def test_section_extraction_and_year_guard(self):
        entries = self.updater.extract_publication_entries("\n".join(self.lines))
        counts = {section: sum(entry["section"] == section for entry in entries)
                  for section in ("JOURNAL PUBLICATIONS", "BOOKS", "BOOK CHAPTERS",
                                  "PUBLISHED CONFERENCE PROCEEDINGS")}
        self.assertEqual(counts, {"JOURNAL PUBLICATIONS": 89, "BOOKS": 2,
                                  "BOOK CHAPTERS": 7, "PUBLISHED CONFERENCE PROCEEDINGS": 11})
        self.assertFalse(self.updater.year_is_plausible(2024, 2021))
        self.assertTrue(self.updater.year_is_plausible(2024, 2025))

    def test_policy_default_uses_new_count(self):
        self.assertEqual(choose_citation_count(100, 90), 90)

    def test_duplicate_cv_titles_can_share_one_scholar_record(self):
        updater = CVPublicationUpdater("test")
        updater.fetch_scholar_publications = lambda: [
            {"title": "Shared title", "year": 2020, "citations": 7},
            {"title": "Separate title", "year": 2017, "citations": 9},
        ]
        cv = ("#### **JOURNAL PUBLICATIONS**\n"
              "1. A (2020). Shared title. Journal.\n"
              "2. B (2020). Shared title. Journal.\n"
              "3. C (2020). Separate title. Journal.\n")
        updated = updater.update_cv_publications(cv)
        self.assertEqual(updated.count("(Times cited: 7)"), 2)
        self.assertEqual(len(updater.duplicate_matches), 1)
        self.assertEqual(len(updater.unmatched_entries), 1)

    def test_scholar_stub_records_do_not_zero_out_counts(self):
        updater = CVPublicationUpdater("test")
        updater.fetch_scholar_publications = lambda: [
            {"title": "Willingness for treatment as a predictor of retention and outcomes", "year": 1996, "citations": 80},
            {"title": "Willingness for Treatment as a Predictor", "year": 1995, "citations": 0},
            {"title": "10 Ability and Achievement Testing on the World", "year": 2002, "citations": 0},
            {"title": "11 Ability and achievement testing on the world wide web", "year": 2000, "citations": 41},
        ]
        cv = ("#### **JOURNAL PUBLICATIONS**\n"
              "1. E (1995). Willingness for treatment as a predictor of retention and outcomes. J. (Times cited:  62\\)\n"
              "#### **BOOK CHAPTERS**\n\n"
              "W (2002). Ability and achievement testing on the World Wide Web. In B (Eds.) Online. (TImes  cited:  14\\)\n")
        updated = updater.update_cv_publications(cv)
        self.assertIn("(Times cited:  80\\)", updated)
        self.assertIn("cited:  41\\)", updated)


if __name__ == "__main__":
    unittest.main()
