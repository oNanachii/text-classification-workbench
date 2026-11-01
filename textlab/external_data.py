"""Prepare external SMS identities before prediction, without reading any default data source."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any

from textlab.data import normalize
from textlab.dataflow import (
    BLOCK_TOKEN_COUNT,
    CHAR_GRAM_SIZE,
    JACCARD_THRESHOLD,
    MAX_TEXT_CHARS,
    MIN_NEAR_DUPLICATE_CHARS,
    char_grams,
    template_key,
)

LABELS = frozenset({"ham", "spam"})
REFERENCE_SPLITS = frozenset({"train", "validation"})
ASCII_LETTER_RUN = re.compile(r"[a-zA-Z]{2}")
CSV_ENCODING = "utf-8-sig"
SHA256 = re.compile(r"[a-f0-9]{64}\Z")

JsonObject = dict[str, Any]
Record = Mapping[str, Any]


def _identity(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity_digest(identities: Sequence[str]) -> str:
    content = json.dumps(sorted(identities), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def eligibility_policy() -> JsonObject:
    """Expose the fixed pre-prediction eligibility rules so a caller can freeze them in its plan."""
    return {
        "normalization": "textlab.data.normalize: casefold + collapse whitespace + trim",
        "sample_id": "SHA-256 of normalized UTF-8 text",
        "label_mapping": "Caller-supplied exact source-label mapping; no strip or case guesses",
        "max_normalized_text_chars": MAX_TEXT_CHARS,
        "ascii_letter_guard": ASCII_LETTER_RUN.pattern,
        "language_scope": (
            "The existing lightweight ASCII-letter guard is not language detection. "
            "It accepts mixed-language strings containing an ASCII two-letter run; "
            "dataset English provenance must be checked separately."
        ),
        "internal_dedup": "Collapse normalized exact duplicates with the same mapped label only",
        "conflict_policy": (
            "Quarantine every eligible row in a normalized-text group with both labels"
        ),
        "exclusion_precedence": [
            "malformed_record",
            "unsupported_label",
            "empty_text",
            "overlong_text",
            "ascii_letter_guard",
            "conflicting_labels",
            "internal_exact_duplicate",
        ],
        "selection_policy": (
            "Freeze eligible sample IDs before predictions; never filter by model result"
        ),
    }


def clean_external_records(
    records: Sequence[Record],
    text_column: str,
    label_column: str,
    mapping: Mapping[str, str],
) -> JsonObject:
    """Normalize explicitly mapped records and audit exclusions before any model sees the cohort."""
    if (
        not isinstance(text_column, str)
        or not text_column
        or not isinstance(label_column, str)
        or not label_column
        or text_column == label_column
    ):
        raise ValueError("Distinct explicit text and label column names are required")
    if (
        not isinstance(mapping, Mapping)
        or not mapping
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or value not in LABELS
            for key, value in mapping.items()
        )
    ):
        raise ValueError("Provide a nonempty exact source-label mapping to ham/spam")
    raw_labels: Counter[str] = Counter()
    exclusions: list[JsonObject] = []
    groups: dict[str, list[JsonObject]] = defaultdict(list)
    invalid_counts: Counter[str] = Counter()
    eligible_count = 0
    for source_row, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            raise ValueError("External records must be mappings")
        raw_text = record.get(text_column)
        raw_label = record.get(label_column)
        if isinstance(raw_label, str):
            raw_labels[raw_label] += 1
        normalized = normalize(raw_text) if isinstance(raw_text, str) else None
        identity = _identity(normalized) if normalized is not None else None
        reason = None
        if None in record or not isinstance(raw_text, str) or not isinstance(raw_label, str):
            reason = "malformed_record"
        elif raw_label not in mapping:
            reason = "unsupported_label"
        elif not normalized:
            reason = "empty_text"
        elif len(normalized) > MAX_TEXT_CHARS:
            reason = "overlong_text"
        elif ASCII_LETTER_RUN.search(normalized) is None:
            reason = "ascii_letter_guard"
        if reason is not None:
            invalid_counts[reason] += 1
            exclusions.append(
                {"sample_id": identity, "reason": reason, "source_rows": [source_row]}
            )
            continue
        eligible_count += 1
        groups[identity].append(
            {
                "id": identity,
                "text": normalized,
                "label": mapping[raw_label],
                "source_row": source_row,
            }
        )
    accepted = []
    duplicate_rows = 0
    duplicate_groups = 0
    conflict_rows = 0
    conflict_groups = 0
    for identity in sorted(groups):
        members = groups[identity]
        sources = [member["source_row"] for member in members]
        if len({member["label"] for member in members}) > 1:
            conflict_groups += 1
            conflict_rows += len(members)
            exclusions.append(
                {"sample_id": identity, "reason": "conflicting_labels", "source_rows": sources}
            )
            continue
        first = members[0]
        accepted.append(
            {"id": identity, "text": first["text"], "label": first["label"], "source_rows": sources}
        )
        if len(members) > 1:
            duplicate_groups += 1
            duplicate_rows += len(members) - 1
            exclusions.append(
                {
                    "sample_id": identity,
                    "reason": "internal_exact_duplicate",
                    "source_rows": sources[1:],
                }
            )
    audit = {
        "original_rows": len(records),
        "raw_label_counts": dict(sorted(raw_labels.items())),
        "raw_label_missing_or_nonstring_rows": len(records) - sum(raw_labels.values()),
        "eligible_rows_before_internal_dedup": eligible_count,
        "malformed_rows_excluded": invalid_counts["malformed_record"],
        "unsupported_label_rows_excluded": invalid_counts["unsupported_label"],
        "empty_rows_excluded": invalid_counts["empty_text"],
        "overlong_rows_excluded": invalid_counts["overlong_text"],
        "ascii_letter_guard_rows_excluded": invalid_counts["ascii_letter_guard"],
        "internal_exact_duplicate_rows_collapsed": duplicate_rows,
        "internal_exact_duplicate_groups": duplicate_groups,
        "conflicting_label_groups_quarantined": conflict_groups,
        "conflicting_label_rows_quarantined": conflict_rows,
        "accepted_unique_rows": len(accepted),
        "accepted_class_counts": dict(sorted(Counter(row["label"] for row in accepted).items())),
        "accepted_id_sha256": _identity_digest([row["id"] for row in accepted]),
        "policy": eligibility_policy(),
    }
    assert len(records) == len(accepted) + duplicate_rows + conflict_rows + sum(
        invalid_counts.values()
    )
    return {"rows": accepted, "audit": audit, "exclusions": exclusions}


def load_external_csv(
    path: str | Path,
    text_column: str,
    label_column: str,
    mapping: Mapping[str, str],
) -> JsonObject:
    """Read an explicit UTF-8 CSV with source hashing and no column or label guesses."""
    content = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(content.decode(CSV_ENCODING), newline=""), strict=True)
    try:
        columns = reader.fieldnames
    except csv.Error as exc:
        raise ValueError("Malformed external CSV header") from exc
    if (
        columns is None
        or len(set(columns)) != len(columns)
        or text_column not in columns
        or label_column not in columns
    ):
        raise ValueError("CSV needs unique headers containing both explicitly selected columns")
    try:
        records = list(reader)
    except csv.Error as exc:
        raise ValueError("Malformed external CSV") from exc
    result = clean_external_records(records, text_column, label_column, mapping)
    result["audit"]["source_csv_sha256"] = hashlib.sha256(content).hexdigest()
    result["audit"]["csv_encoding"] = CSV_ENCODING
    result["audit"]["text_column"] = text_column
    result["audit"]["label_column"] = label_column
    result["audit"]["label_mapping"] = dict(mapping)
    result["audit"]["source_row_scope"] = (
        "One-based parsed data-record positions, excluding the header and blank CSV lines; "
        "quoted multiline cells remain one record"
    )
    return result


def overlap_policy() -> JsonObject:
    """Declare the existing v2 blocked lexical rule and its limits before cohort selection."""
    return {
        "reference_splits": sorted(REFERENCE_SPLITS),
        "reference_input": (
            "Explicit rows only; no default path, source, snapshot or held-out access"
        ),
        "matching_precedence": [
            "normalized_exact",
            "nonempty_template",
            "blocked_char_gram_jaccard",
        ],
        "template_function": "textlab.dataflow.template_key",
        "gram_function": "textlab.dataflow.char_grams",
        "char_gram_size": CHAR_GRAM_SIZE,
        "jaccard_threshold_inclusive": JACCARD_THRESHOLD,
        "gram_count_ratio_threshold_inclusive": JACCARD_THRESHOLD,
        "min_normalized_chars_both_texts": MIN_NEAR_DUPLICATE_CHARS,
        "candidate_blocking": (
            "A candidate and reference must share at least one token among the first two or "
            "last two tokens of their own template keys; same rule as DataStore.prepare"
        ),
        "block_token_count_each_end": BLOCK_TOKEN_COUNT,
        "witness_policy": (
            "One exclusion witness per candidate: exact ID, then equal nonempty template, "
            "then first qualifying reference in (split, id) order"
        ),
        "scope_limit": (
            "Direct cross-dataset matches only. No full all-pairs gram audit, no transitive "
            "external grouping, and no semantic paraphrase coverage. Near pairs with disjoint "
            "boundary-token blocks can remain. Internal external dedup is normalized-exact only."
        ),
        "selection_policy": (
            "Freeze final accepted IDs before predictions; never filter by model result"
        ),
    }


def _block_tokens(key: str) -> set[str]:
    tokens = key.split()
    return set(tokens[:BLOCK_TOKEN_COUNT]) | set(tokens[-BLOCK_TOKEN_COUNT:])


def _prepared_candidates(rows: Sequence[Record]) -> list[JsonObject]:
    result = []
    identities = set()
    for row in rows:
        if not isinstance(row.get("text"), str) or row.get("label") not in LABELS:
            raise ValueError("External candidates need text and a mapped ham/spam label")
        text = normalize(row["text"])
        identity = _identity(text)
        if row.get("id") != identity or identity in identities:
            raise ValueError("Candidate IDs must be unique hashes of normalized text")
        if not text or len(text) > MAX_TEXT_CHARS or ASCII_LETTER_RUN.search(text) is None:
            raise ValueError("External candidates must pass the frozen eligibility policy first")
        identities.add(identity)
        item = {"id": identity, "text": text, "label": row["label"]}
        if "source_rows" in row:
            item["source_rows"] = list(row["source_rows"])
        result.append(item)
    return sorted(result, key=lambda row: row["id"])


def _prepared_references(rows: Sequence[Record]) -> list[JsonObject]:
    result = []
    identities = set()
    for row in rows:
        # Reject a held-out split before accessing its text, even when supplied accidentally.
        split = row.get("split")
        if split not in REFERENCE_SPLITS:
            raise ValueError("References must explicitly belong to UCI train or validation only")
        if not isinstance(row.get("text"), str):
            raise ValueError("Reference text must be a string")
        text = normalize(row["text"])
        identity = _identity(text)
        if row.get("id") != identity or identity in identities:
            raise ValueError("Reference IDs must be unique hashes of normalized text")
        identities.add(identity)
        key = template_key(text)
        result.append(
            {
                "id": identity,
                "text": text,
                "split": split,
                "template": key,
                "grams": char_grams(text),
                "blocks": _block_tokens(key),
            }
        )
    return sorted(result, key=lambda row: (row["split"], row["id"]))


def exclude_uci_overlap(rows: Sequence[Record], reference_rows: Sequence[Record]) -> JsonObject:
    """Remove v2-rule overlap against explicit train/validation rows before predictions."""
    candidates = _prepared_candidates(rows)
    references = _prepared_references(reference_rows)
    exact = {row["id"]: index for index, row in enumerate(references)}
    templates = {}
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, reference in enumerate(references):
        if reference["template"]:
            templates.setdefault(reference["template"], index)
        for token in reference["blocks"]:
            buckets[token].append(index)
    accepted = []
    exclusions = []
    pairs_considered = 0
    jaccard_compared = 0
    for row in candidates:
        key = template_key(row["text"])
        witness = exact.get(row["id"])
        reason = "normalized_exact" if witness is not None else None
        jaccard = None
        gram_ratio = None
        if witness is None and key:
            witness = templates.get(key)
            if witness is not None:
                reason = "nonempty_template"
        if witness is None and len(row["text"]) >= MIN_NEAR_DUPLICATE_CHARS:
            grams = char_grams(row["text"])
            blocked = {index for token in _block_tokens(key) for index in buckets.get(token, ())}
            for index in sorted(blocked):
                reference = references[index]
                if len(reference["text"]) < MIN_NEAR_DUPLICATE_CHARS:
                    continue
                pairs_considered += 1
                other = reference["grams"]
                if not grams or not other:
                    continue
                ratio = min(len(grams), len(other)) / max(len(grams), len(other))
                if ratio < JACCARD_THRESHOLD:
                    continue
                jaccard_compared += 1
                score = len(grams & other) / len(grams | other)
                if score >= JACCARD_THRESHOLD:
                    witness = index
                    reason = "blocked_char_gram_jaccard"
                    jaccard = score
                    gram_ratio = ratio
                    break
        if witness is None:
            accepted.append(row)
            continue
        reference = references[witness]
        exclusion = {
            "sample_id": row["id"],
            "reason": reason,
            "reference_id": reference["id"],
            "reference_split": reference["split"],
        }
        if jaccard is not None:
            exclusion["jaccard"] = jaccard
            exclusion["gram_count_ratio"] = gram_ratio
        exclusions.append(exclusion)
    reference_identities = [{"id": row["id"], "split": row["split"]} for row in references]
    reference_identity_bytes = json.dumps(
        reference_identities, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "rows": accepted,
        "audit": {
            "input_unique_rows": len(candidates),
            "reference_rows": len(references),
            "reference_counts_by_split": dict(
                sorted(Counter(row["split"] for row in references).items())
            ),
            "reference_id_split_sha256": hashlib.sha256(reference_identity_bytes).hexdigest(),
            "uci_overlap_rows_excluded": len(exclusions),
            "overlap_counts_by_reason": dict(
                sorted(Counter(row["reason"] for row in exclusions).items())
            ),
            "blocked_long_pairs_considered": pairs_considered,
            "gram_count_ratio_qualified_pairs": jaccard_compared,
            "accepted_unique_rows": len(accepted),
            "accepted_class_counts": dict(
                sorted(Counter(row["label"] for row in accepted).items())
            ),
            "accepted_id_sha256": _identity_digest([row["id"] for row in accepted]),
            "policy": overlap_policy(),
        },
        "exclusions": exclusions,
    }


def exclude_reserved_identities(
    rows: Sequence[Record],
    ids: set[str] | frozenset[str],
    template_hashes: set[str] | frozenset[str],
) -> JsonObject:
    """Quarantine supplied ID/template hashes without opening any reserved SMS text."""
    for values in (ids, template_hashes):
        if not isinstance(values, (set, frozenset)) or any(
            not isinstance(value, str) or SHA256.fullmatch(value) is None for value in values
        ):
            raise ValueError("Reserved identities and template hashes must be SHA-256 sets")
    candidates = _prepared_candidates(rows)
    accepted = []
    exclusions = []
    for row in candidates:
        reason = None
        matched = None
        if row["id"] in ids:
            reason = "reserved_exact_identity"
            matched = row["id"]
        else:
            key = template_key(row["text"])
            template_hash = _identity(key)
            if key and template_hash in template_hashes:
                reason = "reserved_nonempty_template_hash"
                matched = template_hash
        if reason is None:
            accepted.append(row)
        else:
            exclusions.append({"sample_id": row["id"], "reason": reason, "matched_sha256": matched})
    return {
        "rows": accepted,
        "audit": {
            "input_unique_rows": len(candidates),
            "reserved_identity_count": len(ids),
            "reserved_template_hash_count": len(template_hashes),
            "reserved_identities_sha256": _identity_digest(list(ids)),
            "reserved_template_hashes_sha256": _identity_digest(list(template_hashes)),
            "reserved_overlap_rows_excluded": len(exclusions),
            "overlap_counts_by_reason": dict(
                sorted(Counter(row["reason"] for row in exclusions).items())
            ),
            "accepted_unique_rows": len(accepted),
            "accepted_class_counts": dict(
                sorted(Counter(row["label"] for row in accepted).items())
            ),
            "accepted_id_sha256": _identity_digest([row["id"] for row in accepted]),
            "policy": {
                "reference_input": (
                    "Caller-supplied hashes only; no reserved text lookup or file access"
                ),
                "matching_precedence": ["exact_normalized_id", "equal_nonempty_template_hash"],
                "template_function": "SHA-256 of textlab.dataflow.template_key as UTF-8",
                "scope_limit": "No reserved raw text is available for near-gram or semantic checks",
                "selection_policy": (
                    "Freeze accepted IDs before any prediction; never filter by results"
                ),
            },
        },
        "exclusions": exclusions,
    }
