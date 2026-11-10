"""Turn a teacher's request into a validated lesson plan using the LLM that is
already configured for MoneyPrinterTurbo (any provider in config.toml).

Two calls, both optional to the pipeline's rendering step:
  * plan_lesson   - request -> LessonPlan (scenes: narration + visual spec)
  * review_lesson - LessonPlan -> list of things the teacher should double-check
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from loguru import logger
from pydantic import ValidationError

from app.services.edu.schema import LessonPlan, LessonRequest

LlmCall = Callable[[str], str]

# Spoken pace used to size the script. Slower than adult narration on purpose:
# children need time to look at the diagram while they listen (matches the
# slower default voice rate of 0.85).
_WORDS_PER_SECOND = {"en": 1.8, "ar": 1.5}

_LANGUAGE_NAME = {"en": "English", "ar": "Arabic (Modern Standard, simple wording)"}

_PLAN_PROMPT = """\
You are an experienced {subject} teacher and instructional designer. Design a
short explainer video for a teacher to show in class.

# Request
Topic: {topic}
Audience: {grade} students
Language of ALL narration and on-screen text: {language_name}
Target length: about {target_seconds} seconds (~{target_words} spoken words in total)
{notes_block}
# Teaching rules
1. Use vocabulary and sentence length suitable for {grade}. Short sentences.
   Explain any new term the first time you use it. Prefer one concrete analogy
   a child of that age knows over abstract wording.
2. One idea per scene. Order: a hook/question -> the key ideas, simplest first
   -> a one-sentence recap -> ONE multiple-choice check question (last scene).
   When the topic is a process that makes something (photosynthesis, digestion,
   ...), add ONE scene after it that shows what the result is USED for, drawn
   with "in_process_out": the living thing as the process (e.g. a tree), what it
   takes in as inputs (e.g. sugar) and what it gains as outputs (e.g. energy,
   growth). Keep it accurate: plants use sugar for energy and to grow, they do
   not "eat" like animals.
   Include the key vocabulary terms a {grade} curriculum expects for this topic
   (for example chlorophyll in a photosynthesis lesson), each explained simply.
3. Be scientifically accurate. If something is simplified for the age group,
   simplify without saying anything false. Never invent facts, numbers or names.
4. Each scene's narration is read aloud: plain sentences only. No markdown, no
   emoji, no stage directions, no "Scene 1", no "in this video".
5. Each scene has a visual that SHOWS what the narration says. EVERY scene except
   "title" and "quiz" must have a non-empty "heading" (2-6 words) shown as its
   slide title. Choose the layout that matches the idea:
   - "title": opening card (heading + subheading). ALSO give it exactly one item in
     "items": its "icon" is a big literal emoji for the topic (a seedling for
     plants, a water drop for water...) shown as the hero picture, its "label"
     is the topic name.
   - "in_process_out": things going in -> something happening -> things coming out
   - "flow": a left-to-right chain (2-6 items) where each item LEADS TO the next
     (a real sequence or cause and effect). Never use it for a list of
     ingredients or unrelated facts: use "in_process_out", "equation" or "steps".
   - "cycle": a loop that repeats (3-6 items), e.g. the water cycle
   - "steps": a numbered list (2-6 items)
   - "compare": two columns (left_title/right_title + items each side)
   - "equation": reactants -> products (use real formulas only when appropriate for the grade)
   - "quiz": heading = the question, options = exactly 3 answers, correct one at position {answer_position} (so answer_index = {answer_index}).
     The options on screen keep the same order as in the narration.
     The quiz narration must (a) read the question, (b) read the options in one
     short sentence, (c) say "Think about it for a moment.", and (d) END with a
     separate sentence "The answer is ...". The answer is highlighted on screen
     when that last sentence is spoken, so do not reveal it earlier.
6. Every item needs a single emoji in "icon" and a short "label" (1-4 words).
   "caption" is optional and at most one short line. Pick the most literal,
   instantly recognisable emoji (sun ☀️, water drop 💧, leaf 🌿, bubbles 🫧).
   Avoid abstract or ambiguous ones that children could misread (🍃 looks like
   wind, 🌫️ looks like a grey box).
7. "cue" (optional on items): copy 2-5 consecutive words EXACTLY from that
   scene's narration; the item appears when the narrator says them. Order the
   items in the order the narration mentions them.
8. Everything shown on screen must be said in the SAME scene's narration, and
   must appear in the order it is said. Never show an item (for example an
   output such as oxygen) that the narrator has not mentioned yet. Give every
   item a "cue" copied from the narration so it appears exactly when it is said.
   Describe gases accurately, e.g. carbon dioxide is a gas that is part of the
   air around us (animals breathe it out, and many other things make it too).
   Never use body-part emoji (lungs, nose) for a gas; use ☁️ for carbon dioxide
   and 🫧 for oxygen.
9. Number of scenes: {min_scenes}-{max_scenes}. Keep total narration near {target_words} words.

# Output
Return ONLY one JSON object, no code fences, no commentary, matching:
{{
  "title": "short video title",
  "scenes": [
    {{
      "narration": "text read aloud",
      "visual": {{
        "layout": "title | flow | cycle | in_process_out | compare | steps | equation | quiz",
        "heading": "...", "subheading": "...",
        "items": [{{"label": "...", "icon": "🌱", "caption": "...", "cue": "..."}}],
        "inputs": [], "process": {{"label": "...", "icon": "..."}}, "outputs": [],
        "left_title": "", "left": [], "right_title": "", "right": [],
        "reactants": [{{"text": "6CO2", "icon": "", "cue": ""}}],
        "products": [], "over_arrow": "light energy",
        "options": [], "answer_index": 0
      }}
    }}
  ]
}}
Only fill the fields the chosen layout uses; omit or leave the rest empty.
"""

_REPAIR_PROMPT = """\
Your previous answer could not be used.

Problem: {problem}

Return the corrected JSON object only (same rules and schema as before). Do not
apologise or explain.

Previous answer:
{previous}
"""

_REVIEW_PROMPT = """\
You are a careful {subject} teacher reviewing a draft explainer video before it
is shown to {grade} students. Language of the draft: {language_name}.

Check the narration and the on-screen text for:
- statements that are factually WRONG or would give students a false belief
  (a simplification that is correct and normal for {grade}, or a sentence that
  is true but incomplete, is NOT a problem)
- wording or concepts too hard (or too babyish) for {grade}
- a quiz whose marked answer is wrong or ambiguous
- on-screen items that do not match what is being said

Return ONLY a JSON array of short strings in English, one per real problem,
each naming the scene number (1-based). Report only problems a teacher would
really want fixed before class; at most 3. Return [] if you find nothing. Do not
praise or pad the list.

Draft:
{draft}
"""


def _strip_json(text: str) -> str:
    """Extract the JSON payload from an LLM answer (code fences, <think>, prose)."""
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("no JSON found in the model answer")
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    if end <= start:
        raise ValueError("JSON in the model answer is not closed")
    return text[start : end + 1]


def build_plan_prompt(request: LessonRequest) -> str:
    target_words = int(request.target_seconds * _WORDS_PER_SECOND[request.language])
    min_scenes = max(3, request.target_seconds // 25)
    max_scenes = max(min_scenes + 1, request.target_seconds // 12)
    notes = request.teacher_notes.strip()
    notes_block = f"Teacher's notes (follow them): {notes}\n" if notes else ""
    import hashlib

    pos = 1 + int(hashlib.sha256(request.topic.encode("utf-8")).hexdigest(), 16) % 3  # 1..3, varies by topic
    return _PLAN_PROMPT.format(
        answer_position=pos,
        answer_index=pos - 1,
        subject=request.subject,
        topic=request.topic,
        grade=request.grade,
        language_name=_LANGUAGE_NAME[request.language],
        target_seconds=request.target_seconds,
        target_words=target_words,
        notes_block=notes_block,
        min_scenes=min_scenes,
        max_scenes=min(max_scenes, 14),
    )


def _default_llm_call() -> LlmCall:
    from app.services import llm  # imported lazily: pulls in config + SDKs

    return llm._generate_response


class LlmProviderError(RuntimeError):
    """The AI provider itself failed (bad key, no credit, wrong model...)."""


def _raise_if_provider_error(raw: str) -> None:
    # MoneyPrinterTurbo's llm layer returns failures as text starting with "Error:".
    if raw.lstrip().startswith("Error:"):
        raise LlmProviderError(f"The AI provider returned an error - {raw.strip()[:500]}")


def shuffle_quiz_options(plan: LessonPlan) -> LessonPlan:
    """LLMs love putting the right answer first. Shuffle each quiz's options
    deterministically (same plan -> same order) and keep answer_index correct."""
    import hashlib
    import random

    for scene in plan.scenes:
        v = scene.visual
        if v.layout != "quiz" or len(v.options) < 2:
            continue
        seed = int(hashlib.sha256((v.heading + "|".join(v.options)).encode("utf-8")).hexdigest(), 16)
        order = list(range(len(v.options)))
        random.Random(seed).shuffle(order)
        correct = v.options[v.answer_index]
        v.options = [v.options[i] for i in order]
        v.answer_index = v.options.index(correct)
    return plan


def _parse_plan(raw: str) -> LessonPlan:
    _raise_if_provider_error(raw)
    data = json.loads(_strip_json(raw))
    return LessonPlan.model_validate(data)  # quiz order is fixed in the prompt, so narration and slide always agree


def _problem_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:6]:
            loc = ".".join(str(p) for p in err["loc"])
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)
    return str(exc)


def plan_lesson(
    request: LessonRequest,
    llm_call: Optional[LlmCall] = None,
    max_attempts: int = 3,
) -> LessonPlan:
    """Ask the LLM for a lesson plan, retrying with the validation error so the
    model can fix its own mistakes (a bad layout, a missing field, bad JSON)."""
    call = llm_call or _default_llm_call()
    prompt = build_plan_prompt(request)
    last_error: Exception | None = None
    raw = ""
    for attempt in range(1, max_attempts + 1):
        raw = call(prompt if attempt == 1 else _REPAIR_PROMPT.format(problem=_problem_text(last_error), previous=raw[:6000]))
        try:
            plan = _parse_plan(raw)
            logger.info(f"lesson plan ok on attempt {attempt}: {len(plan.scenes)} scenes")
            return plan
        except LlmProviderError:
            raise  # retrying cannot fix a bad key / no credit / wrong model
        except (ValueError, ValidationError) as exc:  # json.JSONDecodeError is a ValueError
            last_error = exc
            logger.warning(f"lesson plan attempt {attempt} rejected: {_problem_text(exc)}")
    raise ValueError(f"could not get a valid lesson plan after {max_attempts} attempts: {_problem_text(last_error)}")


def review_lesson(
    plan: LessonPlan,
    request: LessonRequest,
    llm_call: Optional[LlmCall] = None,
) -> list[str]:
    """Second-opinion pass. Never raises: a failed review must not block the
    teacher, so on any problem it returns a single note saying so."""
    call = llm_call or _default_llm_call()
    draft = json.dumps(
        {"title": plan.title, "scenes": [s.model_dump(exclude_defaults=True) for s in plan.scenes]},
        ensure_ascii=False,
        indent=1,
    )
    prompt = _REVIEW_PROMPT.format(
        subject=request.subject,
        grade=request.grade,
        language_name=_LANGUAGE_NAME[request.language],
        draft=draft,
    )
    try:
        reply = call(prompt)
        _raise_if_provider_error(reply)
        notes = json.loads(_strip_json(reply))
        return [str(n).strip() for n in notes if str(n).strip()] if isinstance(notes, list) else []
    except Exception as exc:  # noqa: BLE001 - best effort by design
        logger.warning(f"lesson review failed: {exc}")
        return ["The automatic accuracy review could not run - please check the content yourself."]
