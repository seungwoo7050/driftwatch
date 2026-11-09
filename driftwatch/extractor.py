"""HTTP fetching and value extraction engine for driftwatch."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from jsonpath_ng.parser import parse as parse_jsonpath
from pydantic import TypeAdapter, ValidationError

_ANY_ADAPTER = TypeAdapter(Any)
_INT_ADAPTER = TypeAdapter(int)
_FLOAT_ADAPTER = TypeAdapter(float)


class HttpFetcher:
    """Asynchronous HTTP body fetcher with customizable transport for mocking."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.transport = transport
        self.timeout = timeout

    async def fetch(self, url: str) -> str:
        """Fetch remote URL contents as UTF-8 string."""
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text


def extract_value(raw_body: str, extractor_type: str, selector: str) -> str:
    """Extract a target fact value using JSON path or regex word boundary selector."""
    if extractor_type == "json_path":
        return _extract_json_path(raw_body, selector)
    elif extractor_type == "regex":
        return _extract_regex(raw_body, selector)
    else:
        raise ValueError(f"Unsupported extractor_type: '{extractor_type}'")


def _diagnose_missing_path(data: Any, selector: str) -> str:
    """Diagnose missing key or list index out of range for detailed error reporting."""
    tokens = [p.strip() for p in re.split(r"\.|\[(\d+)\]", selector) if p is not None and p.strip() and p != "$"]
    current = data
    for token in tokens:
        if isinstance(current, dict):
            if token in current:
                current = current[token]
            else:
                return f"Key '{token}' not found in JSON object"
        elif isinstance(current, list):
            try:
                idx = int(token)
            except ValueError:
                return f"Cannot index list with non-integer token '{token}'"
            if 0 <= idx < len(current):
                current = current[idx]
            else:
                return f"List index {idx} out of range (length {len(current)})"
        else:
            return f"Cannot traverse token '{token}' on scalar value of type {type(current).__name__}"
    return f"JSON path '{selector}' not found"


def _extract_json_path(raw_body: str, selector: str) -> str:
    """Extract value using jsonpath_ng expressions with dot or bracket syntax."""
    cleaned_selector = selector.strip()
    if not cleaned_selector:
        raise ValueError("Selector path cannot be empty")

    try:
        data: Any = _ANY_ADAPTER.validate_json(raw_body)
    except Exception as exc:
        raise ValueError(f"Malformed JSON body: {exc}") from exc

    normalized = re.sub(r"\.(\d+)(?=\.|$)", r"[\1]", cleaned_selector)
    if not normalized.startswith("$") and not normalized.startswith("["):
        normalized = "$." + normalized

    try:
        expr = parse_jsonpath(normalized)
    except Exception as exc:
        raise ValueError(f"Invalid JSON path expression '{selector}': {exc}") from exc

    matches = expr.find(data)
    if not matches:
        detail = _diagnose_missing_path(data, cleaned_selector)
        raise ValueError(detail)

    val = matches[0].value
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val, separators=(",", ":"))
    return str(val)


def _extract_regex(raw_body: str, selector: str) -> str:
    """Match regex pattern ensuring word/digit boundary safety and extracting capture group."""
    try:
        pattern = re.compile(selector)
    except re.error as exc:
        raise ValueError(f"Invalid regex selector pattern: {exc}") from exc

    match = pattern.search(raw_body)
    if not match:
        raise ValueError(f"Regex pattern '{selector}' did not match payload")

    groups = match.groups()
    if groups:
        for grp in groups:
            if grp is not None:
                return grp.strip()
    return match.group(0).strip()


def validate_extracted_value(
    raw_val: str,
    expected_type: str,
    expected_value: str | None,
    min_value: float | None,
    max_value: float | None,
) -> tuple[str, str | None]:
    """Validate and normalize value using TypeAdapter, returning (normalized_string, violation_detail | None)."""
    normalized_type = expected_type.strip().lower()
    cleaned = raw_val.strip()

    if normalized_type == "int":
        try:
            int_val = _INT_ADAPTER.validate_python(int(float(cleaned)))
        except (ValueError, ValidationError):
            return cleaned, f"Value '{cleaned}' cannot be converted to int"

        norm_str = str(int_val)
        if expected_value is not None:
            try:
                exp_int = _INT_ADAPTER.validate_python(int(float(expected_value.strip())))
                if int_val != exp_int:
                    return norm_str, f"Expected value {exp_int}, got {int_val}"
            except (ValueError, ValidationError):
                pass

        if min_value is not None and float(int_val) < min_value:
            return norm_str, f"Value {int_val} is below minimum allowed {min_value}"
        if max_value is not None and float(int_val) > max_value:
            return norm_str, f"Value {int_val} exceeds maximum allowed {max_value}"

        return norm_str, None

    elif normalized_type == "float":
        try:
            flt_val = _FLOAT_ADAPTER.validate_python(float(cleaned))
        except (ValueError, ValidationError):
            return cleaned, f"Value '{cleaned}' cannot be converted to float"

        norm_str = str(flt_val)
        if expected_value is not None:
            try:
                exp_flt = _FLOAT_ADAPTER.validate_python(float(expected_value.strip()))
                if abs(flt_val - exp_flt) > 1e-9:
                    return norm_str, f"Expected value {exp_flt}, got {flt_val}"
            except (ValueError, ValidationError):
                pass

        if min_value is not None and flt_val < min_value:
            return norm_str, f"Value {flt_val} is below minimum allowed {min_value}"
        if max_value is not None and flt_val > max_value:
            return norm_str, f"Value {flt_val} exceeds maximum allowed {max_value}"

        return norm_str, None

    elif normalized_type == "str":
        if expected_value is not None and cleaned != expected_value.strip():
            return cleaned, f"Expected value '{expected_value.strip()}', got '{cleaned}'"
        return cleaned, None

    else:
        raise ValueError(f"Unsupported expected_type: '{expected_type}'")
