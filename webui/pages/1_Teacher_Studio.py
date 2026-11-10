"""Teacher Studio - write one request, get a narrated explainer video.

Run:  streamlit run webui/pages/1_Teacher_Studio.py
"""
import os
import shutil
import sys
import time

import streamlit as st

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from pydantic import ValidationError

from app.services.edu import tts_backend
from app.services.edu.intake import round_seconds, to_request, understand_request
from app.services.edu.pipeline import RenderOptions, render_lesson
from app.services.edu.planner import plan_lesson, review_lesson
from app.services.edu.schema import LessonPlan

st.set_page_config(page_title="Teacher Studio", page_icon="🎓", layout="wide")
st.title("🎓 Teacher Studio")
st.caption("Tell us what you need in your own words. We make the narrated explainer video.")

ss = st.session_state
for key, default in dict(intake=None, prompt="", plan=None, request=None, review=[], video=None,
                         plan_id=0, work_dir=None).items():
    ss.setdefault(key, default)

THEMES = {"kids": "Kids - colourful, animated sky", "clean": "Clean - plain and calm"}
ENGINES = ["app"] + (["espeak"] if (shutil.which("espeak-ng") or shutil.which("espeak")) else [])
EXAMPLE = ("Make an explanatory video for Grade 5 about photosynthesis, around 2 minutes. "
           "Start with a question, mention chlorophyll, and finish with a short quiz.")


def _make_plan(req) -> None:
    """Plan the lesson, then save plan.json in this lesson's folder (the teacher never handles it)."""
    with st.spinner("Planning the lesson…"):
        try:
            plan = plan_lesson(req)
            review = review_lesson(plan, req)
        except Exception as e:  # shown to the teacher, never swallowed
            st.error(str(e))
            return
    ss.plan, ss.request, ss.review, ss.video = plan, req, review, None
    ss.plan_id += 1  # fresh widget keys, so old edits never leak into a new plan
    ss.work_dir = os.path.join(root_dir, "storage", "edu", str(int(time.time())))
    ss.intake = None
    _save_plan()
    st.rerun()  # redraw without the questions form


def _save_plan() -> None:
    os.makedirs(ss.work_dir, exist_ok=True)
    with open(os.path.join(ss.work_dir, "plan.json"), "w", encoding="utf-8") as f:
        f.write(ss.plan.model_dump_json(indent=2))


# ------------------------------------------------------------ 1. the request ----
st.subheader("What video do you need?")
prompt = st.text_area(
    "Your request", ss.prompt, height=130, label_visibility="collapsed",
    placeholder=EXAMPLE + "\n\n(English or العربية. Add any details you like: structure, examples, tone, length, grade.)",
)
if st.button("Continue", type="primary"):
    ss.prompt = prompt
    if len(prompt.strip()) < 5:
        st.warning("Please write a few words about the video you need.")
    else:
        with st.spinner("Reading your request…"):
            try:
                ss.intake = understand_request(prompt)
            except Exception as e:
                ss.intake = None
                st.error(str(e))
        intake = ss.intake
        if intake and not intake.missing and intake.length_seconds:
            _make_plan(to_request(intake))  # nothing important is missing: go straight on

# ------------------------------------------- 2. only ask about what is missing ----
intake = ss.intake
if intake is not None:
    for w in intake.warnings:
        st.info(w)
    if not intake.understood:
        st.warning("I could not fully read your request, so I will use your whole text as the topic.")
    with st.form("follow_up"):
        st.markdown("**A few quick questions** - everything else I understood from your text.")
        topic = st.text_input("Topic", intake.topic) if "topic" in intake.missing else None
        grade = st.text_input("Which grade are these students in?", placeholder="e.g. Grade 4") \
            if "grade" in intake.missing else None
        seconds = None
        if not intake.length_seconds:
            seconds = st.slider(f"You didn't mention the length. I suggest {intake.suggested_seconds} seconds.",
                                30, 300, intake.suggested_seconds, 15)
        if st.form_submit_button("Create lesson plan", type="primary"):
            if "grade" in intake.missing and not (grade or "").strip():
                st.error("Please tell me the grade, so the words and examples fit the students.")
            else:
                try:
                    _make_plan(to_request(intake, topic=topic, grade=grade,
                                          seconds=round_seconds(seconds) if seconds else None))
                except ValidationError as e:
                    st.error("Please check your answers: " + "; ".join(x["msg"] for x in e.errors()[:2]))

plan = ss.plan
if plan is None:
    st.stop()

# ------------------------------------------------------------- 3. plan review ----
req = ss.request
st.divider()
st.subheader(plan.title)
st.caption(f"Understood as: **{req.topic}** · {req.grade} · {req.subject} · "
           f"{'العربية' if req.language == 'ar' else 'English'} · about {req.target_seconds} seconds")
if ss.review:
    with st.expander(f"⚠️ Please check these ({len(ss.review)})", expanded=True):
        for note in ss.review:
            st.markdown(f"- {note}")
else:
    st.success("The automatic review found no accuracy or grade-level concerns.")

st.markdown("**Scenes** - you can change the words that are read aloud.")
edited = plan.model_dump()
for i, sc in enumerate(plan.scenes):
    with st.expander(f"Scene {i + 1} · {sc.visual.heading or sc.visual.layout}", expanded=False):
        edited["scenes"][i]["narration"] = st.text_area(
            "Narration (read aloud)", sc.narration, key=f"n{ss.plan_id}_{i}", height=110,
            label_visibility="collapsed")

try:
    ss.plan = plan = LessonPlan.model_validate(edited)
except ValidationError as e:
    st.error("Your edits are not valid yet: " + "; ".join(
        f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3]))
    st.stop()
_save_plan()

with st.expander("🎨 Look and voice (optional)"):
    theme = st.radio("Look", list(THEMES), format_func=THEMES.get, horizontal=True)
    speed = st.slider("Voice speed", 0.6, 1.1, 0.85, 0.05, help="1.0 is normal speed. Lower is slower.")
    bgm = st.checkbox("Background music", False)
    backend = st.selectbox(
        "Voice engine", ENGINES,
        format_func=lambda b: "Natural voices (Edge TTS, needs internet)" if b == "app"
        else "espeak (offline, robotic - testing only)")
    voice = st.text_input("Voice name", tts_backend.DEFAULT_VOICES[req.language],
                          key=f"voice_{req.language}") if backend == "app" else ""

# ------------------------------------------------------------------- render ----
st.caption("Rendering takes about twice as long as the video.")
if st.button("Create video", type="primary"):
    bar = st.progress(0.0, text="Starting…")
    try:
        result = render_lesson(
            plan, req.language, ss.work_dir,
            RenderOptions(tts_backend=backend, voice_name=voice or None, voice_rate=speed, theme=theme, bgm=bgm),
            progress=lambda p, m: bar.progress(min(max(p, 0.0), 1.0), text=m),
        )
        ss.video = result.path
        bar.progress(1.0, text="Done")
    except Exception as e:
        bar.empty()
        st.error(f"Rendering failed: {e}")

if ss.video and os.path.exists(ss.video):
    st.video(ss.video)
    with open(ss.video, "rb") as f:
        st.download_button("Download video (MP4)", f.read(), file_name="lesson.mp4", mime="video/mp4")
