from __future__ import annotations

import re
from collections.abc import Sequence

from src.domain.schemas import ColumnSpec

_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
_NON_DIGIT_RE = re.compile(r"\D+")
_PHONE_MIN_DIGITS = 7
_PHONE_MAX_DIGITS = 18
_PHONE_FORMAT_CHARS = set("+ -()")
_PHONE_NAME_ALLOW = ("电话", "手机", "联系电话", "phone", "mobile", "tel")
_PHONE_NAME_DENY = ("税号", "纳税人识别号", "发票", "订单", "金额", "编号", "代码")
_COMPANY_NAME_HINTS = ("公司", "单位", "机构", "企业", "organization", "company", "employer")
_COMPANY_STRONG_SUFFIX = (
    "有限公司",
    "有限责任公司",
    "股份有限公司",
    "集团",
    "分公司",
    "研究院",
    "事务所",
    "Co., Ltd",
    "Inc.",
    "LLC",
    "Ltd.",
)


def infer_column_type(column_name: str, values: Sequence[object]) -> str:
    samples = [str(value).strip() for value in values if str(value).strip()]
    if not samples:
        return "string"

    name = str(column_name).strip()
    name_lower = name.lower()
    if _looks_email(samples):
        return "email"
    if _has_phone_deny_name(name_lower):
        return "string"
    if _has_phone_allow_name(name_lower) and _looks_phone(name_lower, samples):
        return "phone"
    if _looks_company(name_lower, samples):
        return "company"
    if _looks_phone(name_lower, samples):
        return "phone"
    return "string"


def infer_template_columns(
    examples: Sequence[Sequence[object]],
    existing_columns: Sequence[ColumnSpec],
) -> tuple[ColumnSpec, ...]:
    columns = list(existing_columns)
    if not examples:
        return tuple(columns)

    header = examples[0]
    for index, raw_name in enumerate(header):
        if index < len(existing_columns):
            continue
        name = str(raw_name)
        values = [row[index] for row in examples[1:] if index < len(row)]
        columns.append(ColumnSpec(name=name, type=infer_column_type(name, values)))
    return tuple(columns)


def _looks_email(samples: Sequence[str]) -> bool:
    return all(_EMAIL_RE.search(sample) for sample in samples)


def _looks_company(name_lower: str, samples: Sequence[str]) -> bool:
    if any(hint.lower() in name_lower for hint in _COMPANY_NAME_HINTS):
        return True

    return any(_has_company_strong_suffix(sample) for sample in samples)


def _has_company_strong_suffix(sample: str) -> bool:
    sample_lower = sample.lower()
    return any(suffix in sample or suffix.lower() in sample_lower for suffix in _COMPANY_STRONG_SUFFIX)


def _looks_phone(name_lower: str, samples: Sequence[str]) -> bool:
    if _has_phone_deny_name(name_lower):
        return False

    allow_pure_digits = _has_phone_allow_name(name_lower)
    return all(_sample_looks_phone(sample, allow_pure_digits) for sample in samples)


def _has_phone_allow_name(name_lower: str) -> bool:
    return any(allow.lower() in name_lower for allow in _PHONE_NAME_ALLOW)


def _has_phone_deny_name(name_lower: str) -> bool:
    return any(deny.lower() in name_lower for deny in _PHONE_NAME_DENY)


def _sample_looks_phone(sample: str, allow_pure_digits: bool) -> bool:
    digits = _NON_DIGIT_RE.sub("", sample)
    if not _PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS:
        return False
    if allow_pure_digits:
        return True
    return not sample.isdigit() and any(char in _PHONE_FORMAT_CHARS for char in sample)
