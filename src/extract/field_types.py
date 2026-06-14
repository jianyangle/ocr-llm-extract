from __future__ import annotations

import re

from .grounding import normalize_value_for_dedupe

_NON_DIGIT_RE = re.compile(r"\D+")
_PHONE_MIN_DIGITS = 7
_PHONE_MAX_DIGITS = 18


def normalize_phone(value: str) -> str:
    text = str(value).strip()
    plus = "+" if text.startswith("+") else ""
    digits = _NON_DIGIT_RE.sub("", text)
    return f"{plus}{digits}"


def is_valid_phone(value: str) -> bool:
    digits = _NON_DIGIT_RE.sub("", str(value))
    return _PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS


def normalize_email(value: str) -> str:
    return str(value).strip().lower()


def is_valid_email(value: str) -> bool:
    text = normalize_email(value)
    if any(ch.isspace() for ch in text) or text.count("@") != 1:
        return False
    local, domain = text.split("@", 1)
    if not local or not domain or "." not in domain:
        return False
    return all(part for part in domain.split("."))


def normalize_company(value: str) -> str:
    return str(value).strip()


def phone_equivalent(left: str, right: str) -> bool:
    left_digits = _NON_DIGIT_RE.sub("", str(left))
    right_digits = _NON_DIGIT_RE.sub("", str(right))
    if not left_digits or not right_digits:
        return False
    if left_digits == right_digits:
        return True
    return _edit_distance_at_most_one(left_digits, right_digits)


def email_equivalent(left: str, right: str) -> bool:
    left_norm = normalize_value_for_dedupe(str(left)).strip()
    right_norm = normalize_value_for_dedupe(str(right)).strip()
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True

    if len(left) > len(right):
        left, right = right, left

    index_left = 0
    index_right = 0
    mismatches = 0
    while index_left < len(left) and index_right < len(right):
        if left[index_left] == right[index_right]:
            index_left += 1
            index_right += 1
            continue
        mismatches += 1
        if mismatches > 1:
            return False
        if len(left) == len(right):
            index_left += 1
        index_right += 1

    if index_left < len(left) or index_right < len(right):
        mismatches += 1
    return mismatches <= 1
