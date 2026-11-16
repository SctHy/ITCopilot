"""Strict, source-offset scoring for masking benchmarks (no model tuning)."""
from __future__ import annotations

import re

# Frozen before inference. Missing recognizers remain in the denominator.
LABEL_MAP = {
    "name": "PERSON", "first_name": "PERSON", "last_name": "PERSON",
    "street_address": "ADDRESS", "email": "EMAIL", "phone_number": "PHONE",
    "password": "SECRET", "api_key": "SECRET", "account_pin": "SECRET",
    "credit_card_security_code": "SECRET", "credit_card_number": "ACCOUNT",
    "iban": "ACCOUNT", "bban": "ACCOUNT", "bank_routing_number": "ACCOUNT",
    "user_name": "ACCOUNT", "ssn": "ID", "passport_number": "ID",
    "driver_license_number": "ID",
}
EXCLUDED = {
    "company": "organization, outside current personal-data policy",
    "date": "date masking disabled in current product policy",
    "date_time": "date masking disabled in current product policy",
    "date_of_birth": "date masking disabled; explicit coverage gap",
    "time": "time masking disabled",
    "ipv4": "IP masking disabled", "ipv6": "IP masking disabled",
    "employee_id": "business identifier preserved by current policy",
    "customer_id": "business identifier preserved by current policy",
    "swift_bic_code": "bank identifier, not an individual account",
    "local_latlng": "coordinates unsupported; explicit coverage gap",
}
TOKEN = re.compile(r"\{\{([A-Z_]+)_[a-f0-9]{24}\}\}")
KIND_MAP = {"CARD": "ACCOUNT", "PASSWORD": "SECRET", "TOKEN": "SECRET",
            "API_KEY": "SECRET", "PRIVATE_KEY": "SECRET"}


def emitted_spans(original, masked, values):
    """Recover only spans actually replaced on the wire; reject any misalignment."""
    cursor, previous, rebuilt, spans = 0, 0, [], []
    for match in TOKEN.finditer(masked):
        literal = masked[previous:match.start()]
        rebuilt.append(literal)
        cursor += len(literal)
        raw = values[match.group()]
        spans.append((cursor, cursor + len(raw), KIND_MAP.get(match[1], match[1])))
        rebuilt.append(raw)
        cursor += len(raw)
        previous = match.end()
    rebuilt.append(masked[previous:])
    if "".join(rebuilt) != original:
        raise ValueError("wire text does not round-trip to original offsets")
    return spans


def score(text, gold, predicted):
    """Strict typed exact match and full-character coverage are separate metrics."""
    truth, guesses = set(gold), set(predicted)
    covered = bytearray(len(text))
    expected = bytearray(len(text))
    for a, b, _ in guesses:
        covered[a:b] = b"\1" * (b - a)
    for a, b, _ in truth:
        expected[a:b] = b"\1" * (b - a)
    full = [all(covered[a:b]) for a, b, _ in gold]
    tp = len(truth & guesses)
    return {
        "gold": len(truth), "predicted": len(guesses), "tp": tp,
        "fp": len(guesses) - tp, "fn": len(truth) - tp,
        "untyped_tp": len({(a, b) for a, b, _ in truth} & {(a, b) for a, b, _ in guesses}),
        "fully_covered": sum(full), "full_flags": full,
        "gold_chars": sum(expected), "covered_gold_chars": sum(a and b for a, b in zip(expected, covered)),
        "masked_non_gold_chars": sum(b and not a for a, b in zip(expected, covered)),
        "leaking_document": bool(truth) and not all(full),
        "negative_document": not truth, "negative_changed": not truth and bool(guesses),
    }


def rates(counts):
    def ratio(n, d):
        return n / d if d else None
    return {
        **counts,
        "precision": ratio(counts["tp"], counts["predicted"]),
        "recall": ratio(counts["tp"], counts["gold"]),
        "f1": ratio(2 * counts["tp"], counts["gold"] + counts["predicted"]),
        "full_coverage_rate": ratio(counts["fully_covered"], counts["gold"]),
        "character_coverage_rate": ratio(counts["covered_gold_chars"], counts["gold_chars"]),
    }
