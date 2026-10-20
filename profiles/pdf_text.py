"""Visible-text extraction for one PDF page.

pypdf's ``extract_text`` ignores clipping. Editors such as Acrobat's "edit text" tool
can rewrite a page as several form XObjects, each drawing the *whole* original page
but clipped (by the form's ``/BBox``) to one band of it, then paint corrected text on
top. A clip-unaware extractor reads that page as N full copies of the resume.

This module walks the same content stream with pypdf's visitor callbacks, tracks the
clip every form XObject imposes (its ``/BBox`` mapped through its ``/Matrix`` and the
current transformation matrix), and keeps only text whose baseline origin falls
inside every enclosing clip. Text painted on top of form content is then put back
where it appears on the page: appended to the form line that shares its baseline, or
inserted before the first form line below it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pypdf import PageObject
from pypdf.generic import DictionaryObject

Matrix = tuple[float, float, float, float, float, float]
Box = tuple[float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
# Baselines this close (in points) are the same line; origins this far outside a
# clip still count as inside, to absorb rounding in the editor's band edges.
_SAME_BASELINE = 1.0
_CLIP_TOLERANCE = 0.5
_NO_SPACE_BEFORE = ",.;:)]}%!?"


def multiply(first: Matrix, second: Matrix) -> Matrix:
    """Return the matrix that applies ``first`` and then ``second``."""
    a, b, c, d, e, f = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def transform_box(matrix: Matrix, box: Box) -> Box:
    """Return the axis-aligned bounds of ``box`` after applying ``matrix``."""
    a, b, c, d, e, f = matrix
    corners = [(x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    xs = [a * x + c * y + e for x, y in corners]
    ys = [b * x + d * y + f for x, y in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _intersect(first: Box | None, second: Box) -> Box:
    if first is None:
        return second
    return (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )


def _inside(clip: Box | None, x: float, y: float) -> bool:
    if clip is None:
        return True
    tolerance = _CLIP_TOLERANCE
    return (
        clip[0] - tolerance <= x <= clip[2] + tolerance
        and clip[1] - tolerance <= y <= clip[3] + tolerance
    )


def _as_matrix(values: Any) -> Matrix:  # noqa: ANN401 - pypdf passes untyped arrays
    numbers = [float(value) for value in values]
    if len(numbers) != 6:
        raise ValueError("a PDF matrix has six numbers")
    return (numbers[0], numbers[1], numbers[2], numbers[3], numbers[4], numbers[5])


def _as_box(values: Any) -> Box:  # noqa: ANN401 - pypdf passes untyped arrays
    numbers = [float(value) for value in values]
    if len(numbers) != 4:
        raise ValueError("a PDF rectangle has four numbers")
    return (
        min(numbers[0], numbers[2]),
        min(numbers[1], numbers[3]),
        max(numbers[0], numbers[2]),
        max(numbers[1], numbers[3]),
    )


@dataclass
class _Frame:
    """Drawing state for one level of form XObject nesting."""

    resources: DictionaryObject | None
    to_page: Matrix
    clip: Box | None
    depth: int


@dataclass(eq=False)
class _Line:
    parts: list[str] = field(default_factory=list)
    x: float | None = None
    y: float | None = None
    depth: int = 0

    @property
    def text(self) -> str:
        return "".join(self.parts)


class _VisibleTextCollector:
    """pypdf visitor callbacks that keep only text inside the active clip."""

    def __init__(self, page: PageObject) -> None:
        resources = page.get("/Resources")
        self._stack = [
            _Frame(
                resources=_dictionary(resources),
                to_page=IDENTITY,
                clip=None,
                depth=0,
            )
        ]
        # A Do operator's frame takes effect at the form's first operator: pypdf
        # flushes the caller's pending text *after* the "before Do" callback.
        self._pending: _Frame | None = None
        self.lines: list[_Line] = [_Line()]

    def before(self, operator: bytes, operands: Any, cm: Any, tm: Any) -> None:  # noqa: ANN401
        self._apply_pending()
        if operator != b"Do":
            return
        top = self._stack[-1]
        self._pending = self._form_frame(top, operands, cm) or _Frame(
            resources=top.resources, to_page=top.to_page, clip=top.clip, depth=top.depth
        )

    def after(self, operator: bytes, operands: Any, cm: Any, tm: Any) -> None:  # noqa: ANN401
        if operator != b"Do":
            return
        if self._pending is not None:
            self._pending = None  # the form drew nothing, so its frame never applied
        elif len(self._stack) > 1:
            self._stack.pop()

    def text(self, text: str, cm: Any, tm: Any, font: Any, size: Any) -> None:  # noqa: ANN401
        if not text:
            return
        top = self._stack[-1]
        position = multiply(multiply(_as_matrix(tm), _as_matrix(cm)), top.to_page)
        x, y = position[4], position[5]
        if not _inside(top.clip, x, y):
            # Hidden text still ends lines, so visible neighbours do not merge.
            for _ in range(text.count("\n")):
                self.lines.append(_Line())
            return
        pieces = text.split("\n")
        for index, piece in enumerate(pieces):
            if index:
                self.lines.append(_Line())
            if not piece:
                continue
            line = self.lines[-1]
            if line.y is None and piece.strip():
                line.x, line.y, line.depth = x, y, top.depth
            line.parts.append(piece)

    def _apply_pending(self) -> None:
        if self._pending is not None:
            self._stack.append(self._pending)
            self._pending = None

    def _form_frame(self, top: _Frame, operands: Any, cm: Any) -> _Frame | None:  # noqa: ANN401
        if not operands or top.resources is None:
            return None
        xobjects = _dictionary(top.resources.get("/XObject"))
        if xobjects is None or operands[0] not in xobjects:
            return None
        xobject = _dictionary(xobjects[operands[0]])
        if xobject is None or xobject.get("/Subtype") != "/Form":
            return None
        form_matrix = _as_matrix(xobject.get("/Matrix", IDENTITY))
        to_page = multiply(multiply(form_matrix, _as_matrix(cm)), top.to_page)
        clip = top.clip
        if "/BBox" in xobject:
            clip = _intersect(clip, transform_box(to_page, _as_box(xobject["/BBox"])))
        resources = _dictionary(xobject.get("/Resources")) or top.resources
        return _Frame(resources=resources, to_page=to_page, clip=clip, depth=top.depth + 1)


def _dictionary(value: Any) -> DictionaryObject | None:  # noqa: ANN401
    if value is None:
        return None
    resolved = value.get_object() if hasattr(value, "get_object") else value
    return resolved if isinstance(resolved, DictionaryObject) else None


def _join(left: str, right: str) -> str:
    if not left or left[-1].isspace() or not right or right[0].isspace():
        return left + right
    if right[0] in _NO_SPACE_BEFORE:
        return left + right
    return f"{left} {right}"


def _place_overlays(lines: list[_Line]) -> list[_Line]:
    """Move page-level text painted over form content to where it sits on the page.

    Only pages whose text mostly lives in forms are touched: there the page-level text
    is a correction layer. On ordinary pages, stream order is the best reading order.
    """
    form_lines = [line for line in lines if line.depth > 0 and line.y is not None]
    form_characters = sum(len(line.text.strip()) for line in form_lines)
    page_characters = sum(len(line.text.strip()) for line in lines if line.depth == 0)
    if not form_lines or form_characters < page_characters:
        return lines
    first_form = next(index for index, line in enumerate(lines) if line in form_lines)
    placed = list(lines)
    for line in lines[first_form:]:
        if line.depth > 0 or line.y is None or line.x is None:
            continue
        line_y, line_x = line.y, line.x
        same = next(
            (
                target
                for target in form_lines
                if target.y is not None
                and target.x is not None
                and abs(target.y - line_y) <= _SAME_BASELINE
                and line_x > target.x
            ),
            None,
        )
        if same is not None:
            same.parts = [_join(same.text, line.text.strip())]
            placed.remove(line)
            continue
        below = next(
            (
                target
                for target in form_lines
                if target.y is not None and target.y < line_y - _SAME_BASELINE
            ),
            None,
        )
        if below is not None:
            placed.remove(line)
            placed.insert(placed.index(below), line)
    return placed


def visible_page_text(page: PageObject) -> str:
    """Return the page's text, skipping text that form XObject clipping hides."""
    collector = _VisibleTextCollector(page)
    page.extract_text(
        visitor_operand_before=collector.before,
        visitor_operand_after=collector.after,
        visitor_text=collector.text,
    )
    return "\n".join(line.text for line in _place_overlays(collector.lines))


__all__ = ["multiply", "transform_box", "visible_page_text"]
