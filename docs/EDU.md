# Teacher Studio - explainer videos for lessons

A teacher types e.g. *"Make an explainer video for Grade 5 about photosynthesis"* and gets a narrated
video built from **generated slides and animated diagrams** (not stock footage - stock clips can't
show a chloroplast accurately).

## Pipeline
```
request -> planner (LLM -> JSON, Pydantic-validated, repair loop) -> reviewer (2nd LLM pass: accuracy / level)
        -> teacher edits narration + visuals -> per scene: TTS -> timeline -> HTML renderer (Playwright frames)
        -> ffmpeg concat (+ optional BGM) -> mp4
```
* `app/services/edu/schema.py`   - the contract between LLM and renderer (layouts: title, flow, cycle,
  steps, in_process_out, compare, equation, quiz). Invalid output is rejected and sent back to the LLM with the error.
* `planner.py`   - prompt + repair loop + reviewer. Uses MoneyPrinterTurbo's existing LLM provider config.
* `timeline.py`  - maps measured speech duration onto the narration text so each element appears when it is
  spoken; works with any TTS provider (no word timestamps needed).
* `renderer.py`  - deterministic: same input -> same frames. All LLM text is HTML-escaped; the browser
  blocks every non-data URL.
* `tts_backend.py` - reuses the app's voices (Edge TTS is free); `espeak` backend for offline tests.
* `pipeline.py`  - whole-lesson render + CLI.

## Run
```
# UI
streamlit run webui/Main.py        # then open "Teacher Studio" in the sidebar
# CLI, no LLM needed
python -m app.services.edu.pipeline --plan test/edu/fixtures/photosynthesis_g5.json --out output/edu --tts espeak
# CLI with LLM + Edge TTS
python -m app.services.edu.pipeline --topic "photosynthesis" --grade "Grade 5" --subject Science --seconds 90
```
Extra requirement: `playwright install chromium`, ffmpeg, and fonts: `fonts-noto-color-emoji`
(icons) and `fonts-noto-core` (Arabic). In Docker add them to the image.

## Tests
`python -m pytest test/edu` (schema, escaping, JSON extraction, planner repair loop - no network).

## Honest limits
* Quality of the plan depends on the LLM; the reviewer reduces but does not remove factual errors - the
  teacher review step is part of the design.
* Emoji icons are the visual vocabulary; a fixed set of layouts keeps output reliable.
