from collections.abc import Mapping
from urllib.parse import urlencode

BASE_URL = "https://djinni.co/jobs/"


def get_start_url(params: Mapping[str, object]) -> str:
    """Builds a Djinni jobs URL from structured, not-yet-encoded query values."""
    normalized_params = {
        key: _query_values(value)
        for key, value in params.items()
        if value is not None
    }
    query_string = urlencode(normalized_params, doseq=True)
    return f"{BASE_URL}?{query_string}"


def _query_values(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)
