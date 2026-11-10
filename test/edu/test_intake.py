import json
import pytest
from app.services.edu import intake as I
from app.services.edu.planner import LlmProviderError


def fake(d):
    return lambda prompt: json.dumps(d)


def test_full_request_has_nothing_missing():
    r = I.understand_request("Grade 5 science, photosynthesis, 2 minutes, start with a question",
        fake({"topic": "Photosynthesis", "grade": "Grade 5", "subject": "Science", "language": None,
              "length_seconds": 120, "suggested_length_seconds": 90, "notes": "Start with a question."}))
    assert r.missing == [] and r.length_seconds == 120 and r.language == "en" and not r.language_stated
    q = I.to_request(r)
    assert q.grade == "Grade 5" and q.target_seconds == 120 and q.teacher_notes == "Start with a question."


def test_missing_grade_is_flagged_and_length_suggested():
    r = I.understand_request("make a video about the water cycle",
        fake({"topic": "The water cycle", "grade": None, "subject": None, "language": None,
              "length_seconds": None, "suggested_length_seconds": 100, "notes": ""}))
    assert r.missing == ["grade"] and r.length_seconds is None and r.suggested_seconds == 105
    q = I.to_request(r, grade="Grade 4")
    assert q.grade == "Grade 4" and q.target_seconds == 105 and q.subject == I.DEFAULT_SUBJECT


def test_arabic_detected_when_not_stated():
    r = I.understand_request("اشرح التمثيل الضوئي لطلاب الصف الخامس",
        fake({"topic": "التمثيل الضوئي", "grade": "5", "language": None, "suggested_length_seconds": 90}))
    assert r.language == "ar" and r.grade == "Grade 5"


def test_unsupported_language_warns():
    r = I.understand_request("video in French about plants",
        fake({"topic": "plants", "grade": "Grade 3", "language": "fr"}))
    assert r.language == "en" and r.warnings


def test_seconds_rounding_and_clamp():
    assert I.round_seconds(10) == 30 and I.round_seconds(1000) == 300 and I.round_seconds(100) == 105
    assert I.round_seconds("x", 90) == 90


def test_unreadable_answer_falls_back_to_whole_text():
    r = I.understand_request("something about volcanoes", lambda p: "no json here")
    assert not r.understood and r.topic.startswith("something about volcanoes") and "grade" in r.missing


def test_provider_error_raises():
    with pytest.raises(LlmProviderError):
        I.understand_request("x", lambda p: "Error: Error code: 401 - invalid key")


def test_prompt_is_not_formatted_with_braces():
    seen = []
    I.understand_request("topic with {braces} and %s", lambda p: (seen.append(p), '{"topic":"t"}')[1])
    assert "{braces}" in seen[0]
