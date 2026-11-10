"""Whole-lesson pipeline: LessonPlan -> narrated explainer video (mp4).

per scene:  TTS -> measured duration -> deterministic slide render (renderer)
then:       concat clips (stream copy) -> optional background music mix.

CLI:
    python -m app.services.edu.pipeline --plan test/edu/fixtures/photosynthesis_g5.json \
        --out output/edu --tts espeak
    python -m app.services.edu.pipeline --topic "photosynthesis" --grade "Grade 5" \
        --subject Science --language en --seconds 90 --out output/edu
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger

from app.services.edu import renderer, tts_backend
from app.services.edu.schema import LessonPlan, LessonRequest

ProgressFn = Callable[[float, str], None]


@dataclass
class RenderOptions:
    tts_backend: str = "app"  # "app" (Edge/Azure/... via config.toml) | "espeak" (offline)
    voice_name: Optional[str] = None
    voice_rate: float = 0.85  # 1.0 = normal speed; lower = slower, better for children
    theme: str = "kids"  # "kids" (animated sky, bouncy) | "clean" (plain, calm)
    bgm: bool = False
    bgm_file: str = ""
    bgm_volume: float = 0.10


@dataclass
class LessonVideo:
    path: str
    duration: float
    scene_durations: list[float]


def _ff(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-600:]}")


def _mix_bgm(video: str, bgm: str, volume: float, out: str) -> None:
    _ff([
        "-i", video, "-stream_loop", "-1", "-i", bgm,
        "-filter_complex",
        f"[1:a]volume={volume}[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", out,
    ])


def render_lesson(
    plan: LessonPlan,
    language: str,
    output_dir: str,
    options: Optional[RenderOptions] = None,
    progress: Optional[ProgressFn] = None,
    basename: str = "lesson",
) -> LessonVideo:
    opts = options or RenderOptions()
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="edu_", dir=output_dir)
    n = len(plan.scenes)
    clips: list[renderer.SceneClip] = []

    def tick(i: float, msg: str) -> None:
        logger.info(f"[{int(i / n * 100):3d}%] {msg}")
        if progress:
            progress(min(i / n, 1.0) * 0.95, msg)

    for i, scene in enumerate(plan.scenes):
        tick(i, f"scene {i + 1}/{n}: voice")
        audio = os.path.join(work, f"s{i:02d}.mp3")
        speech = tts_backend.synthesize(
            scene.narration, language, audio,
            backend=opts.tts_backend, voice_name=opts.voice_name, rate=opts.voice_rate,
        )
        tick(i + 0.4, f"scene {i + 1}/{n}: slides ({scene.visual.layout})")
        clips.append(renderer.render_scene(
            scene, audio, speech, os.path.join(work, f"s{i:02d}.mp4"),
            language=language, work_dir=os.path.join(work, f"w{i:02d}"), theme=opts.theme,
        ))

    tick(n - 0.1, "joining scenes")
    lst = os.path.join(work, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '%s'\n" % os.path.abspath(c.path).replace("\\", "/"))
    joined = os.path.join(work, "joined.mp4")
    _ff(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])

    final = os.path.join(output_dir, f"{basename}.mp4")
    if opts.bgm:
        bgm = opts.bgm_file
        if not bgm:
            from app.services import video as video_service
            bgm = video_service.get_bgm_file("random")
        if bgm:
            _mix_bgm(joined, bgm, opts.bgm_volume, final)
        else:
            logger.warning("bgm requested but no music file found; continuing without")
            os.replace(joined, final)
    else:
        os.replace(joined, final)

    total = tts_backend.probe_duration(final)
    if progress:
        progress(1.0, "done")
    logger.success(f"lesson video: {final} ({total:.1f}s, {n} scenes)")
    return LessonVideo(final, total, [c.duration for c in clips])


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a teacher explainer video")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--plan", help="LessonPlan JSON file (skips the LLM)")
    src.add_argument("--topic", help="Lesson topic; plan is generated with the configured LLM")
    ap.add_argument("--grade", default="Grade 5")
    ap.add_argument("--subject", default="Science")
    ap.add_argument("--language", default="en", choices=["en", "ar"])
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--notes", default="")
    ap.add_argument("--out", default="output/edu")
    ap.add_argument("--tts", default="app", choices=["app", "espeak"])
    ap.add_argument("--voice", default=None)
    ap.add_argument("--rate", type=float, default=0.85, help="voice speed, 1.0 = normal (default 0.85)")
    ap.add_argument("--theme", default="kids", choices=["kids", "clean"])
    ap.add_argument("--bgm", action="store_true")
    a = ap.parse_args()

    if a.plan:
        plan = LessonPlan.model_validate(json.load(open(a.plan, encoding="utf-8")))
    else:
        from app.services.edu.planner import plan_lesson
        req = LessonRequest(topic=a.topic, grade=a.grade, subject=a.subject,
                            language=a.language, target_seconds=a.seconds, teacher_notes=a.notes)
        plan = plan_lesson(req)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "plan.json"), "w", encoding="utf-8") as f:
            f.write(plan.model_dump_json(indent=2))
    vid = render_lesson(plan, a.language, a.out,
                        RenderOptions(tts_backend=a.tts, voice_name=a.voice, voice_rate=a.rate,
                                      theme=a.theme, bgm=a.bgm))
    print(vid.path)


if __name__ == "__main__":
    main()
