"""Frozen external-label adapters; never repair offsets by searching predictions."""
from __future__ import annotations

from collections import Counter
import hashlib

from tests.support.masking_benchmark import LABEL_MAP

NEMOTRON_MAP = {
    **LABEL_MAP, "credit_debit_card": "ACCOUNT", "account_number": "ACCOUNT",
    "health_plan_beneficiary_number": "ACCOUNT", "biometric_identifier": "ACCOUNT",
    "medical_record_number": "ACCOUNT", "certificate_license_number": "ID",
    "vehicle_identifier": "ID", "license_plate": "ID", "tax_id": "ID", "national_id": "ID",
    "pin": "SECRET", "cvv": "SECRET", "http_cookie": "SECRET", "fax_number": "PHONE",
}
NEMOTRON_EXCLUDED = {
    **dict.fromkeys("date_of_birth date time date_time".split(), "date/time disabled; birth-date coverage gap"),
    **dict.fromkeys("county country city state postcode coordinate".split(), "coarse geography or coordinates outside current policy"),
    **dict.fromkeys("mac_address ipv4 ipv6 url".split(), "network identifiers/URLs disabled"),
    **dict.fromkeys("customer_id employee_id unique_id device_identifier".split(), "business identifiers preserved"),
    **dict.fromkeys("company_name swift_bic".split(), "organization/bank identifier, not personal account"),
    **dict.fromkeys("race_ethnicity age occupation employment_status education_level blood_type religious_belief sexuality language political_view gender".split(), "personal/sensitive attribute not covered by current identifier policy"),
}
ZH_MAP = {"person": "PERSON", "phone": "PHONE", "id_number": "ID", "address": "ADDRESS",
          "email": "EMAIL", "bank_card": "ACCOUNT", "passport": "ID", "license_plate": "ID"}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def validate_spans(text, spans, *, allow_case_only=False):
    """Keep original offsets; accept scalar rendering, optionally case-only differences.

    Empty/reversed/out-of-bounds or mismatched values quarantine the WHOLE document.
    A numeric JSON value can validate only when str(value) matches that exact slice.
    """
    normalized, quality = [], Counter()
    for span in spans:
        a, b, value = span["start"], span["end"], span["text"]
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(text):
            raise ValueError("invalid_offset")
        if type(value) not in {str, int, float}:
            raise ValueError("invalid_value_type")
        if not isinstance(value, str):
            quality["scalar_value_rendered"] += 1
        actual, expected = text[a:b], str(value)
        if actual != expected:
            if allow_case_only and actual.casefold() == expected.casefold():
                quality["case_only_value_difference"] += 1
            else:
                raise ValueError("offset_value_mismatch")
        normalized.append((a, b, span["label"]))
    return sorted(set(normalized)), dict(quality)


def select_rows(rows, prior_hashes, limit=0):
    """Select by a fixed salted text hash before checking labels or running inference."""
    seen, candidates, duplicates = set(prior_hashes), [], []
    for row in rows:
        key = digest(row["text"])
        if key in seen:
            duplicates.append(row["id"])
            continue
        seen.add(key)
        candidates.append({**row, "hash": key})
    candidates.sort(key=lambda r: digest("external-pii-20261114:" + r["hash"]))
    return candidates[:limit] if limit else candidates, duplicates, len(candidates)
