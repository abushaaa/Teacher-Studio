"""Data models for the educational video pipeline.

The LLM never writes HTML/SVG directly. It fills in these small, validated
structures and our renderer draws them. That keeps the visual style
consistent, makes the output safe to render, and means a malformed answer is
caught here (with a readable error the LLM can use to self-correct) instead
of producing a broken video.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

LAYOUTS = ("title", "flow", "cycle", "in_process_out", "compare", "steps", "equation", "quiz")
Layout = Literal["title", "flow", "cycle", "in_process_out", "compare", "steps", "equation", "quiz"]


class LessonRequest(BaseModel):
    """What the teacher types into the platform."""

    topic: str = Field(min_length=2, max_length=300)
    grade: str = Field(default="Grade 5", max_length=40)
    subject: str = Field(default="Science", max_length=60)
    language: Literal["en", "ar"] = "en"
    target_seconds: int = Field(default=90, ge=30, le=300)
    teacher_notes: str = Field(default="", max_length=1500)


class Item(BaseModel):
    """One labelled thing on screen (a node in a diagram, a step, an option)."""

    label: str = Field(min_length=1, max_length=60)
    icon: str = Field(default="", max_length=8)  # a single emoji, e.g. "☀️"
    caption: str = Field(default="", max_length=90)
    # A short phrase copied from the narration. The item appears on screen when
    # the narrator reaches it. Optional; items are spread evenly if omitted.
    cue: str = Field(default="", max_length=80)


class EquationPart(BaseModel):
    text: str = Field(min_length=1, max_length=40)
    icon: str = Field(default="", max_length=8)
    cue: str = Field(default="", max_length=80)


class Visual(BaseModel):
    layout: Layout
    heading: str = Field(default="", max_length=90)
    subheading: str = Field(default="", max_length=140)

    # flow / cycle / steps: `items`; in_process_out: inputs + process + outputs;
    # compare: left + right; equation: reactants + products; quiz: options.
    items: list[Item] = Field(default_factory=list, max_length=7)
    inputs: list[Item] = Field(default_factory=list, max_length=4)
    process: Optional[Item] = None
    outputs: list[Item] = Field(default_factory=list, max_length=4)
    left_title: str = Field(default="", max_length=40)
    left: list[Item] = Field(default_factory=list, max_length=5)
    right_title: str = Field(default="", max_length=40)
    right: list[Item] = Field(default_factory=list, max_length=5)
    reactants: list[EquationPart] = Field(default_factory=list, max_length=4)
    products: list[EquationPart] = Field(default_factory=list, max_length=4)
    over_arrow: str = Field(default="", max_length=40)
    options: list[str] = Field(default_factory=list, max_length=4)
    answer_index: int = Field(default=0, ge=0, le=3)

    @model_validator(mode="after")
    def _layout_has_what_it_needs(self) -> "Visual":
        n = len
        need = {
            "title": (n(self.heading) > 0, "title needs a heading"),
            "flow": (2 <= n(self.items) <= 6, "flow needs 2-6 items"),
            "cycle": (3 <= n(self.items) <= 6, "cycle needs 3-6 items"),
            "steps": (2 <= n(self.items) <= 6, "steps needs 2-6 items"),
            "in_process_out": (
                n(self.inputs) >= 1 and self.process is not None and n(self.outputs) >= 1,
                "in_process_out needs inputs, a process and outputs",
            ),
            "compare": (
                n(self.left) >= 1 and n(self.right) >= 1 and bool(self.left_title) and bool(self.right_title),
                "compare needs left_title/right_title and items on both sides",
            ),
            "equation": (
                n(self.reactants) >= 1 and n(self.products) >= 1,
                "equation needs reactants and products",
            ),
            "quiz": (
                2 <= n(self.options) <= 4 and self.answer_index < n(self.options) and n(self.heading) > 0,
                "quiz needs a question in heading, 2-4 options and a valid answer_index",
            ),
        }[self.layout]
        ok, message = need
        if not ok:
            raise ValueError(message)
        return self


class Scene(BaseModel):
    narration: str = Field(min_length=10, max_length=900)
    visual: Visual

    @field_validator("narration")
    @classmethod
    def _plain_text(cls, v: str) -> str:
        # The narration is read aloud: no markdown, no stage directions.
        cleaned = v.replace("*", "").replace("#", "").strip()
        if not cleaned:
            raise ValueError("narration is empty after cleaning")
        return cleaned


class LessonPlan(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    scenes: list[Scene] = Field(min_length=2, max_length=14)
    # Filled in by the optional reviewer pass: things a teacher should double
    # check (scientific accuracy, wording above the grade level, ...).
    review_notes: list[str] = Field(default_factory=list)
