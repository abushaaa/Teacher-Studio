import json, pathlib
import pytest
from pydantic import ValidationError
from app.services.edu.schema import LessonPlan, LessonRequest
from app.services.edu.planner import plan_lesson, _strip_json
from app.services.edu.timeline import Timeline
from app.services.edu import renderer

FIX = pathlib.Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIX.glob("*.json"))


@pytest.mark.parametrize("p", FIXTURES, ids=lambda p: p.stem)
def test_fixtures_validate_and_build(p):
    plan = LessonPlan.model_validate_json(p.read_text(encoding="utf-8"))
    lang = "ar" if "ar" in p.stem.split("_")[-1] else "en"
    for sc in plan.scenes:
        tl = Timeline(sc.narration, 10.0)
        html, animated, instant = renderer.build_page(sc, tl, lang)
        assert "<script>" in html and animated is not None


def test_html_escaping():
    plan = LessonPlan.model_validate_json((FIX / "photosynthesis_g5.json").read_text(encoding="utf-8"))
    sc = plan.scenes[0].model_copy(deep=True)
    sc.visual.heading = '<img src=x onerror=alert(1)>'
    html, _, _ = renderer.build_page(sc, Timeline(sc.narration, 8.0), "en")
    assert "<img src=x" not in html


def test_strip_json_variants():
    assert _strip_json('<think>x</think>```json\n{"a":1}\n```') == '{"a":1}'
    assert json.loads(_strip_json('Sure! {"a": {"b": 2}} done')) == {"a": {"b": 2}}


def _req():
    return LessonRequest(topic="Photosynthesis", grade="Grade 5", subject="Science", language="en", target_seconds=90)


def test_planner_repairs_invalid_output():
    good = (FIX / "photosynthesis_g5.json").read_text(encoding="utf-8")
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return "not json at all" if len(calls) == 1 else good

    plan = plan_lesson(_req(), llm_call=llm)
    assert len(calls) == 2 and len(plan.scenes) >= 2
    assert "not json" in calls[1] or "error" in calls[1].lower()


def test_planner_gives_up():
    with pytest.raises(Exception):
        plan_lesson(_req(), llm_call=lambda p: "{}", max_attempts=2)


def test_schema_rejects_bad_quiz():
    with pytest.raises(ValidationError):
        LessonPlan.model_validate({"title": "t", "scenes": [
            {"narration": "A fine narration sentence here.", "visual": {"layout": "quiz", "heading": "Q?", "options": [{"label": "a"}, {"label": "b"}], "answer_index": 5}},
            {"narration": "Another narration sentence here.", "visual": {"layout": "title", "heading": "x"}}]})
