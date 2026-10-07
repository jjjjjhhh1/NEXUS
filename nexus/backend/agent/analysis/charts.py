"""The chart contract.

Every numeric picture the agent draws is one dict in one shape. That buys two
things the front end and this codebase both need: a single renderer instead of a
per-scene special case, and one auditable rule for when a number deserves a
picture instead of another sentence.

Two kinds, chosen by the shape of the data and never by which scene asked:

  ``trend``   four or more points on a time axis. A line shows direction.
  ``compare`` two or three points. A bar shows the gap. "Last month versus this
              month, did I come out ahead" is a comparison, and drawing it as a
              line would imply a path that was never measured.

This module decides how to draw verified numbers. It never decides what is true
— callers pass values that already came out of the database, and this module
refuses to draw a series too short or too ragged to mean anything.

Layer contract:
  owns      — chart shape, kind selection, downsampling, comparative value
  does NOT own — the numbers (callers), the wording (model), the pixels
                 (frontend/app.js)
"""
from __future__ import annotations

from typing import Any, Sequence

# Past this many points a line stops being readable at card width, so long
# ranges are sampled down rather than drawn as noise.
MAX_POINTS = 14

UNIT_MONEY = "¥"
UNIT_PERCENT = "%"
UNIT_COUNT = "笔"


def _sample_indices(count: int, limit: int = MAX_POINTS) -> list[int]:
    """Evenly spaced indices that always keep the first and last point."""
    if count <= limit or count == 0:
        return list(range(count))
    step = (count - 1) / (limit - 1)
    return sorted({round(index * step) for index in range(limit)})


def _clean(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # reject NaN, keep real zeros


def build(
    *,
    title: str,
    labels: Sequence[Any],
    series: Sequence[dict],
    unit: str = "",
    note: str = "",
    insight: str = "",
    partial_index: int | None = None,
) -> dict | None:
    """Assemble one chart, or ``None`` when the data cannot carry a picture.

    ``series`` items are ``{"name": str, "values": [...]}``. Returns ``None``
    rather than a near-empty frame so callers can pass whatever they have
    without branching first. ``partial_index`` names the last still-running
    period, in the caller's own coordinates — it is remapped onto the sampled
    axis here and then honoured by :func:`describe`.
    """
    labels = list(labels)
    if len(labels) < 2:
        return None

    cleaned: list[dict] = []
    for item in series:
        values = [_clean(value) for value in item.get("values", [])]
        if len(values) != len(labels):
            values = (values + [None] * len(labels))[: len(labels)]
        # A series that is mostly missing is noise on a shared axis.
        present = [value for value in values if value is not None]
        if len(present) < 2:
            continue
        cleaned.append({"name": str(item.get("name", "")), "values": values, "tone": _tone(present)})
    if not cleaned:
        return None

    keep = _sample_indices(len(labels))
    sampled_labels = [labels[index] for index in keep]
    sampled_series = [
        {"name": item["name"], "values": [item["values"][index] for index in keep], "tone": item["tone"]}
        for item in cleaned
    ]
    chart = {
        "kind": "trend" if len(sampled_labels) >= 4 else "compare",
        "title": title,
        "labels": [str(label) for label in sampled_labels],
        "series": sampled_series,
        "unit": unit,
        "note": note,
        "insight": insight,
        "source": "",
    }
    if partial_index is not None and partial_index in keep:
        chart["partial_index"] = keep.index(partial_index)
    return chart


def _tone(values: Sequence[float]) -> str:
    """Direction of a series, derived from its own numbers.

    Callers do not get to assert this. A caller that labelled spending "down"
    because spending is a bad thing would paint a rising spending line as a
    falling one, and the chart would then argue with the table above it.
    """
    scale = max(abs(values[0]), abs(values[-1]), 1e-9)
    change = (values[-1] - values[0]) / scale
    if change > 0.01:
        return "up"
    if change < -0.01:
        return "down"
    return "flat"


def comparative_value(chart: dict) -> float:
    """How strongly a chart answers "up or down, and by how much".

    Used to pick between charts that carry the same kind of number. A picture
    earns its place by showing *movement*; a flat line that only restates a
    total does not.
    """
    if not chart:
        return 0.0
    labels = chart.get("labels") or []
    if len(labels) < 2:
        return 0.0

    # Same rule as describe(): a still-running period must not be allowed to
    # inflate the apparent movement of the whole chart.
    cut = chart.get("partial_index")
    series = []
    for item in chart.get("series") or []:
        values = [value for value in item.get("values", []) if isinstance(value, (int, float))]
        if cut is not None and cut < len(values):
            values = values[:cut]
        if len(values) >= 2:
            series.append(values)
    if not series:
        return 0.0

    spread: list[float] = []
    crosses_zero = False
    for values in series:
        low, high = min(values), max(values)
        scale = max(abs(low), abs(high), 1e-9)
        spread.append((high - low) / scale)
        if low < 0 < high:
            crosses_zero = True

    breadth = min(len(labels), 8) / 8
    movement = min(sum(spread) / len(spread), 2.0) / 2
    direction = 1.0 if crosses_zero else 0.85
    unit = {UNIT_MONEY: 1.0, UNIT_PERCENT: 0.95, UNIT_COUNT: 0.5}.get(chart.get("unit", ""), 0.6)
    return round(breadth * movement * direction * unit, 4)


# Which kind of number a user is really asking to be shown. Amounts come first:
# the questions people ask here are "did I come out ahead, and by how much",
# and a percentage curve answers a different question than the one being asked.
# Within a rank, comparative_value decides.
UNIT_RANK = {UNIT_MONEY: 0, UNIT_PERCENT: 1}


def pick_chart(candidates: Sequence[dict | None]) -> dict | None:
    """The single picture an answer should lead with.

    Amounts first, because "did I come out ahead, and by how much" is the
    question behind most of these replies; then, among numbers of equal
    relevance, the one that actually shows movement. Returns ``None`` when
    nothing readable was offered, so a reply with no comparable data simply
    has no picture.
    """
    usable = [item for item in (candidates or []) if item and item.get("series")]
    if not usable:
        return None
    return min(
        usable,
        key=lambda item: (UNIT_RANK.get(item.get("unit", ""), 2), -comparative_value(item)),
    )


def describe(chart: dict) -> str:
    """One plain sentence naming the biggest mover, computed from the values.

    Deliberately not model-written: this line sits directly above a picture and
    must agree with it exactly, so it is derived from the same numbers.

    A trailing point the caller marked ``partial_index`` is excluded. A month
    that is three days old must not be allowed to declare itself the biggest
    mover of the period.
    """
    cut = chart.get("partial_index")
    series = []
    for item in chart.get("series", []):
        values = [value for value in item.get("values", []) if isinstance(value, (int, float))]
        if cut is not None and cut < len(values):
            values = values[:cut]
        if len(values) >= 2:
            series.append((item.get("name", ""), values, values[-1] - values[0]))
    if not series:
        return ""
    unit = chart.get("unit", "")
    suffix = f"{unit}" if unit in {UNIT_MONEY, UNIT_PERCENT} else (f" {unit}" if unit else "")
    name, values, span = max(series, key=lambda row: abs(row[2]))
    low, high = min(values), max(values)
    direction = "上涨" if span > 0 else "下降" if span < 0 else "持平"
    return f"{name} 区间{direction} {span:+.2f}{suffix}，最高 {high:,.2f}{suffix}，最低 {low:,.2f}{suffix}。"


__all__ = [
    "MAX_POINTS", "UNIT_MONEY", "UNIT_PERCENT", "UNIT_COUNT", "UNIT_RANK",
    "build", "comparative_value", "pick_chart", "describe",
]
