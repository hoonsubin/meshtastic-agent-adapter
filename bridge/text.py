"""
Text helpers shared by the bridge components.

LoRa text messages have a hard size budget, so every place that writes text to
the mesh truncates through the same byte-safe helper (truncating by characters
would overflow the budget for non-ASCII text). Truncation prefers a word
boundary and appends a marker, so a reader on a handset can tell that the
message was cut rather than assuming the sentence ended there.
"""

DEFAULT_MARKER = "..."

# Do not trim back to a word boundary if that would waste more than this share of
# the budget (a long unbroken token would otherwise lose a lot of useful bytes).
MIN_BUDGET_USE = 0.8


def truncate_bytes(text: str, limit: int, marker: str = DEFAULT_MARKER) -> str:
    """
    Truncate text to at most `limit` UTF-8 bytes, never splitting a character.

    When the text does not fit, it is cut at a word boundary where possible and
    `marker` is appended (the marker counts against the limit). Pass marker=""
    for a plain hard cut.

    Args:
        text: Text to truncate
        limit: Maximum size in bytes, marker included
        marker: Appended when truncation happens

    Returns:
        The original text when it fits, otherwise a byte-safe prefix plus marker
    """
    if limit <= 0 or not text:
        return ""

    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text

    marker_bytes = marker.encode("utf-8")
    budget = limit - len(marker_bytes)

    # Not even room for the marker: hard cut, no marker
    if budget <= 0:
        return encoded[:limit].decode("utf-8", errors="ignore")

    cut = encoded[:budget].decode("utf-8", errors="ignore")

    trimmed = cut.rstrip()
    if " " in trimmed:
        candidate = trimmed[: trimmed.rfind(" ")].rstrip()
        if len(candidate.encode("utf-8")) >= int(budget * MIN_BUDGET_USE):
            trimmed = candidate

    return trimmed + marker
