"""Synthetic records exercise deterministic cohort cleaning without real SMS or held-out access."""

from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from textlab.data import normalize
from textlab.dataflow import char_grams, template_key
from textlab.external_data import (
    clean_external_records,
    eligibility_policy,
    exclude_uci_overlap,
    exclude_reserved_identities,
    load_external_csv,
    overlap_policy,
)


def candidate(text: str, label: str = "ham") -> dict[str, Any]:
    """Create one normalized synthetic external row with its exact content identity."""
    text = normalize(text)
    return {"id": hashlib.sha256(text.encode()).hexdigest(), "text": text, "label": label}


def reference(text: str, split: str = "train") -> dict[str, Any]:
    """Create an explicit synthetic UCI reference tagged with an allowed split."""
    return {**candidate(text), "split": split}


class ExternalCleaningTests(unittest.TestCase):
    def test_duplicates_conflicts_and_exclusions_reconcile_every_source_row(self) -> None:
        """Collapse exact repeats and quarantine contradictory labels before cohort selection."""
        records = [
            {"message": " Hello   Friend ", "kind": "normal"},
            {"message": "hello friend", "kind": "normal"},
            {"message": " Special offer ", "kind": "junk"},
            {"message": "SPECIAL OFFER", "kind": "normal"},
            {"message": "  \t ", "kind": "normal"},
            {"message": "你好朋友", "kind": "normal"},
            {"message": "x" * 2001, "kind": "normal"},
            {"message": "unmapped label example", "kind": "other"},
        ]
        before = deepcopy(records)
        result = clean_external_records(
            records, "message", "kind", {"normal": "ham", "junk": "spam"}
        )
        audit = result["audit"]
        self.assertEqual(records, before)
        self.assertEqual(audit["original_rows"], 8)
        self.assertEqual(audit["raw_label_counts"], {"normal": 6, "junk": 1, "other": 1})
        self.assertEqual(audit["eligible_rows_before_internal_dedup"], 4)
        self.assertEqual(audit["internal_exact_duplicate_rows_collapsed"], 1)
        self.assertEqual(audit["internal_exact_duplicate_groups"], 1)
        self.assertEqual(audit["conflicting_label_groups_quarantined"], 1)
        self.assertEqual(audit["conflicting_label_rows_quarantined"], 2)
        self.assertEqual(audit["empty_rows_excluded"], 1)
        self.assertEqual(audit["overlong_rows_excluded"], 1)
        self.assertEqual(audit["ascii_letter_guard_rows_excluded"], 1)
        self.assertEqual(audit["unsupported_label_rows_excluded"], 1)
        self.assertEqual(audit["accepted_unique_rows"], 1)
        self.assertEqual(result["rows"][0]["source_rows"], [1, 2])
        self.assertEqual(result["rows"][0]["id"], candidate("hello friend")["id"])
        self.assertTrue(all("text" not in row for row in result["exclusions"]))

    def test_explicit_csv_columns_mapping_and_source_hash(self) -> None:
        """Read a temporary authored CSV and preserve its bytes without schema guesses."""
        content = (
            b"\xef\xbb\xbfbody,type,unused\r\nHello colleague,OK,one\r\n"
            b"Cash prize alert,JUNK,two\r\n"
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "authored.csv"
            path.write_bytes(content)
            result = load_external_csv(path, "body", "type", {"OK": "ham", "JUNK": "spam"})
            with self.assertRaises(ValueError):
                load_external_csv(path, "text", "label", {"OK": "ham"})
        self.assertEqual(result["audit"]["source_csv_sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(result["audit"]["original_rows"], 2)
        self.assertEqual(result["audit"]["accepted_class_counts"], {"ham": 1, "spam": 1})

    def test_labels_are_not_silently_casefolded_or_stripped(self) -> None:
        """Only exact caller-specified source labels can enter the frozen cohort."""
        result = clean_external_records(
            [
                {"body": "First authored message", "tag": "ham"},
                {"body": "Second authored message", "tag": "HAM"},
                {"body": "Third authored message", "tag": " ham "},
            ],
            "body",
            "tag",
            {"ham": "ham"},
        )
        self.assertEqual(result["audit"]["accepted_unique_rows"], 1)
        self.assertEqual(result["audit"]["unsupported_label_rows_excluded"], 2)

    def test_eligibility_guard_is_explicitly_not_a_language_detector(self) -> None:
        """Match the fixed lightweight guard while retaining its mixed-language limitation."""
        result = clean_external_records(
            [
                {"text": "你好", "label": "ham"},
                {"text": "123456", "label": "ham"},
                {"text": "x", "label": "ham"},
                {"text": "Hello 你好", "label": "ham"},
                {"text": "  hello" + " " * 2001, "label": "ham"},
            ],
            "text",
            "label",
            {"ham": "ham"},
        )
        self.assertEqual(result["audit"]["ascii_letter_guard_rows_excluded"], 3)
        self.assertEqual(result["audit"]["accepted_unique_rows"], 2)
        self.assertIn("not language detection", eligibility_policy()["language_scope"])
        self.assertEqual(eligibility_policy()["max_normalized_text_chars"], 2000)

    def test_malformed_rows_and_duplicate_csv_headers_are_rejected(self) -> None:
        """Report unusable records and stop on an ambiguous explicit-column schema."""
        result = clean_external_records(
            [{"text": None, "label": "ham"}, {"text": "Hello friend", "label": None}],
            "text",
            "label",
            {"ham": "ham"},
        )
        self.assertEqual(result["audit"]["malformed_rows_excluded"], 2)
        self.assertEqual(result["audit"]["raw_label_missing_or_nonstring_rows"], 1)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "ambiguous.csv"
            path.write_text("text,text,label\nHello,friend,ham\n")
            with self.assertRaises(ValueError):
                load_external_csv(path, "text", "label", {"ham": "ham"})


class UciOverlapTests(unittest.TestCase):
    def test_exact_and_url_number_templates_leave_id_only_witnesses(self) -> None:
        """Exclude exact IDs regardless of label and template variants from explicit UCI splits."""
        refs = [
            reference("Please bring the notebook tomorrow", "train"),
            reference("Your code 1234 is ready at https://old.invalid/path", "validation"),
        ]
        rows = [
            candidate("  PLEASE bring the notebook TOMORROW ", "spam"),
            candidate("Your code 9999 is ready at http://new.invalid/different"),
            candidate("Fresh independent garden appointment reminder"),
        ]
        original_rows = deepcopy(rows)
        original_refs = deepcopy(refs)
        result = exclude_uci_overlap(rows, refs)
        self.assertEqual(rows, original_rows)
        self.assertEqual(refs, original_refs)
        self.assertEqual(result["audit"]["uci_overlap_rows_excluded"], 2)
        self.assertEqual(
            result["audit"]["overlap_counts_by_reason"],
            {"normalized_exact": 1, "nonempty_template": 1},
        )
        self.assertEqual(len(result["rows"]), 1)
        for exclusion in result["exclusions"]:
            self.assertEqual(
                set(exclusion), {"sample_id", "reason", "reference_id", "reference_split"}
            )
            self.assertIn(exclusion["reference_split"], {"train", "validation"})

    def test_exact_threshold_is_inclusive_and_below_threshold_stays(self) -> None:
        """Hand-built grams isolate the 17/20 Jaccard and count-ratio boundary at 0.85."""
        ref = reference("start shared " + "a" * 40 + " ending marker")
        match = candidate("start shared " + "b" * 40 + " ending marker")
        miss = candidate("start shared " + "c" * 40 + " ending marker")
        grams = {f"{index:05d}" for index in range(20)}
        subsets = {
            ref["text"]: grams,
            match["text"]: set(sorted(grams)[:17]),
            miss["text"]: set(sorted(grams)[:16]),
        }
        with patch("textlab.external_data.char_grams", side_effect=lambda text: subsets[text]):
            result = exclude_uci_overlap([match, miss], [ref])
        self.assertEqual([row["id"] for row in result["rows"]], [miss["id"]])
        exclusion = result["exclusions"][0]
        self.assertEqual(exclusion["reason"], "blocked_char_gram_jaccard")
        self.assertEqual(exclusion["jaccard"], 0.85)
        self.assertEqual(exclusion["gram_count_ratio"], 0.85)

    def test_jaccard_gate_and_both_minimum_lengths_are_required(self) -> None:
        """Require sufficient Jaccard and at least 35 characters in both compared texts."""
        ref = reference("start share " + "a" * 14 + " end mark")
        long_row = candidate("start share " + "b" * 14 + " end mark")
        short_row = candidate("start share " + "c" * 13 + " end mark")
        self.assertEqual(len(ref["text"]), 35)
        self.assertEqual(len(short_row["text"]), 34)
        grams = {f"{index:05d}" for index in range(20)}
        with patch("textlab.external_data.char_grams", return_value=grams):
            result = exclude_uci_overlap([long_row, short_row], [ref])
        self.assertEqual([row["id"] for row in result["rows"]], [short_row["id"]])
        with patch("textlab.external_data.char_grams", return_value=grams):
            result = exclude_uci_overlap([long_row], [{**short_row, "split": "train"}])
        self.assertEqual(len(result["rows"]), 1)
        partly_different = set(sorted(grams)[:18]) | {"other", "grams"}
        with patch(
            "textlab.external_data.char_grams",
            side_effect=lambda text: grams if text == ref["text"] else partly_different,
        ):
            result = exclude_uci_overlap([long_row], [ref])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["audit"]["gram_count_ratio_qualified_pairs"], 1)

    def test_disjoint_boundary_blocks_remain_an_explicit_coverage_limit(self) -> None:
        """A real high-Jaccard pair can remain when the original v2 candidate blocker misses it."""
        core = " ".join(
            "middle" + chr(97 + index // 26) + chr(97 + index % 26) for index in range(200)
        )
        ref = reference("alpha beta " + core + " gamma delta")
        row = candidate("omega sigma " + core + " theta kappa")
        a, b = char_grams(ref["text"]), char_grams(row["text"])
        self.assertGreaterEqual(len(a & b) / len(a | b), 0.85)
        result = exclude_uci_overlap([row], [ref])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["audit"]["blocked_long_pairs_considered"], 0)
        self.assertIn("disjoint", overlap_policy()["scope_limit"])

    def test_order_does_not_change_selection_or_reference_witness(self) -> None:
        """Freeze identities deterministically regardless of candidate and reference input order."""
        rows = [
            candidate("Ticket 12 collection is ready"),
            candidate("New authored library reminder"),
        ]
        refs = [
            reference("Ticket 99 collection is ready", "validation"),
            reference("Ticket 33 collection is ready", "train"),
        ]
        first = exclude_uci_overlap(rows, refs)
        second = exclude_uci_overlap(list(reversed(rows)), list(reversed(refs)))
        self.assertEqual(first, second)
        self.assertEqual(first["exclusions"][0]["reference_split"], "train")

    def test_test_or_missing_reference_split_and_bad_id_fail_closed(self) -> None:
        """Never silently accept held-out references or identities detached from normalized text."""
        rows = [candidate("Authored independent reminder")]
        for invalid in (
            reference("Do not use this reference", "test"),
            candidate("No declared split is unsafe"),
            {**reference("Incorrect identity example"), "id": "incorrect"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    exclude_uci_overlap(rows, [invalid])
        with self.assertRaises(ValueError):
            exclude_uci_overlap(rows + rows, [])


class ReservedIdentityTests(unittest.TestCase):
    def test_reserved_hashes_exclude_exact_or_nonempty_templates_without_text_lookup(self) -> None:
        """Use only supplied metadata to quarantine matches before making predictions."""
        exact = candidate("Unique authored reminder for tomorrow")
        template = candidate("Your parcel code 9999 is ready at https://new.invalid/path")
        independent = candidate("Completely different authored garden reminder")
        reserved_template = template_key(
            "Your parcel code 1234 is ready at http://old.invalid/path"
        )
        digest = hashlib.sha256(reserved_template.encode()).hexdigest()
        result = exclude_reserved_identities(
            [exact, template, independent], {exact["id"]}, {digest}
        )
        self.assertEqual([row["id"] for row in result["rows"]], [independent["id"]])
        self.assertEqual(result["audit"]["reserved_overlap_rows_excluded"], 2)
        self.assertEqual(
            result["audit"]["overlap_counts_by_reason"],
            {"reserved_exact_identity": 1, "reserved_nonempty_template_hash": 1},
        )
        for row in result["exclusions"]:
            self.assertEqual(set(row), {"sample_id", "reason", "matched_sha256"})
        reordered = exclude_reserved_identities(
            [independent, template, exact], {exact["id"]}, {digest}
        )
        self.assertEqual(result, reordered)

    def test_empty_template_hash_is_not_a_match_and_bad_hashes_fail(self) -> None:
        """Honor the existing nonempty-template rule rather than rejecting all URL-only texts."""
        row = candidate("https://example.invalid/path")
        self.assertEqual(template_key(row["text"]), "")
        empty_hash = hashlib.sha256(b"").hexdigest()
        result = exclude_reserved_identities([row], set(), {empty_hash})
        self.assertEqual(len(result["rows"]), 1)
        with self.assertRaises(ValueError):
            exclude_reserved_identities([row], {"not-a-sha256"}, set())


if __name__ == "__main__":
    unittest.main()
