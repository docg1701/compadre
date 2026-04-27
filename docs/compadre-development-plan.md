# Implementation Plan — Compadre (Speech-to-Text SaaS)

## Goal

Transform a fork of **Comadre** (the project owner's validated, security-hardened TTS SaaS) into **Compadre**, a self-hosted, CPU-only STT SaaS that transcribes WhatsApp voice messages from Brazilian Portuguese. The Comadre codebase is the base — only the engine is swapped. All subsystems listed in §2 of the brief must be inherited verbatim with branding renames. Nothing is rewritten to "improve."

---

## Gate (CRITICAL — read first)

**Do not begin ANY task in this plan until the project owner:**
1. Confirms Comadre is at its 100%-validated state
2. Provides the Comadre repo path (expected `~/dev/comadre/`)
3. Verifies that the Comadre commit is the one to fork

All tasks below are described assuming `~/dev/comadre/` as the source. If a different path is given, adjust accordingly.

---

## Phase 0 — Prerequisites & Read-Only Study

**Prerequisites:** Project owner confirms Comadre is at 100%-validated state and provides the path.

**Tasks:**

0.1 **Clone / fork Comadre as the Compadre starting point**
   - Action: `cp -r ~/dev/comadre ~/dev/compadre-src` or `git clone` the validated Comadre repo into a new Compadre repo.
   - Verify the full Comadre tree is present: `src/comadre/`, `deploy/`, `compose.yaml`, `Dockerfile`, `pyproject.toml`, `CLAUDE.md`, `README.md`, `docs/`, `scripts/`, `tests/`, `voice/`, `noise/`, `model/`, `models/`, `data/`.
   - Acceptance: `find . -name '*.py' | head -20` shows the Comadre source tree.

0.2 **Read Comadre source thoroughly (mandatory per §10)**
   - Read every file in `src/comadre/`, `deploy/`, `compose.yaml`, `Dockerfile`, `pyproject.toml`, `CLAUDE.md`, `README.md`, and the entire `docs/` directory.
   - Pay special attention to `CLAUDE.md`'s "Non-Obvious Gotchas" section — these apply verbatim to Compadre except where they reference TTS-specific files.
   - Acceptance: You understand the following contracts: key registry schema, auth flow, rate-limit bucket identity, counters snapshot aggregation, billing audit replay, health endpoint contract, Caddy handle-block ordering, ansible playbook execution model.

0.3 **Read the youtube-guru reference files (mandatory per §10)**
   - Only these three files:
     - `~/dev/youtube-guru/youtube_pipeline/ingest.py` — functions `extract_audio` (lines ~291–340) and `transcribe_audio` (lines ~370–550)
     - `~/dev/youtube-guru/youtube_pipeline/config.py` — Whisper constants only (lines 1–150)
     - `~/dev/youtube-guru/youtube_pipeline/srt_utils.py` — `format_srt_timestamp` function
   - Ignore everything else in youtube-guru (medical dictionary, VAD, checkpointing, Rich live tables, retry decorator).
   - Acceptance: You can recite the exact `model.transcribe(...)` call signature and its parameter values.

0.4 **Verify the reference `model.transcribe(...)` call in youtube-guru**
   - Confirm the call: `model.transcribe(str(audio_path), beam_size=5, word_timestamps=True, language="en", condition_on_previous_text=False)`
   - Note differences for Compadre: `language="pt"` (default, overridable), compute_type `"int8"` (explicit), cpu_threads default 4.
   - Acceptance: You know the exact parameter mapping.

---

## Phase 1 — Clean Deletion of TTS-Specific Code

**Prerequisites:** Phase 0 complete.

Per §5 of the brief, remove everything TTS-specific. Do NOT leave dead code.

1.1 **Remove source files**
   - Delete `src/comadre/preprocessing.py` (TTS text normalization, chunking, sigla expansion)
   - Delete `src/comadre/service.py` (KokoroEngine, QwenEngine, EngineRouter — the entire TTS engine layer)
   - Acceptance: `ls src/comadre/preprocessing.py` returns "No such file", same for `service.py`.

1.2 **Remove model/voice directories**
   - Delete `model/` directory (Kokoro model references)
   - Delete `models/` directory (downloaded model files)
   - Delete `voice/` directory
   - Delete `voices/` directory (entire voice discovery system)
   - Delete `noise/` directory (room/hiss noise WAV files for post-processing)
   - Delete `deploy/espeak-ng/` directory (custom espeak-ng dictionary system)
   - Delete `deploy/bin/qwen3-tts-cli` (pre-built Qwen3 C++ binary)
   - Acceptance: `find . -name 'model' -o -name 'models' -o -name 'voice' -o -name 'voices' -o -name 'noise' | wc -l` returns 0 for these top-level dirs.

1.3 **Remove TTS-specific docs**
   - Delete: `docs/cost-comparison-report.md`, `docs/relatorio-comparativo-custos.md`, `docs/replica-scaling-report.md`, `docs/feasibility-report-tts-engine.md`, `docs/voice-management.md`, `docs/espeak-ng-custom-dictionary.md`, `docs/building-qwen3-tts-cli.md`, `docs/qwen3-tts-internals.md`, `docs/quantizing-qwen3-models.md`
   - Keep: any docs that are not TTS-engine-specific (e.g., `docs/monetization.md`, `docs/rate-limiting.md`, ansible docs if any).
   - Acceptance: The deleted docs are gone; remaining docs are generic operational docs.

1.4 **Trim pyproject.toml dependencies**
   - Remove: `kokoro-onnx`, `num2words`, `libsonic-dev` reference (from Dockerfile), espeak-ng build dependencies from Dockerfile
   - Add: `faster-whisper` (pulls `ctranslate2`, `tokenizers`, `huggingface_hub` automatically)
   - Update `[project.scripts]` entry: `compadre-keys = "compadre.keys.cli.app:main"`
   - Update `[project]` metadata: `name = "compadre"`, update description, version to `0.1.0`.
   - Update `[[tool.mypy.overrides]]` to ignore `faster_whisper.*` (similar to how kokoro-onnx was ignored).
   - Acceptance: `uv sync` succeeds with only the new dependency set.

1.5 **Trim Dockerfile**
   - Remove: espeak-ng build from source (the entire `git init /tmp/espeak-ng` block)
   - Remove: `libsndfile1`, `git`, `cmake`, `pkg-config`, `gcc`, `g++`, `make`, `libsonic-dev` from apt installs (only keep `ffmpeg`, `openssl`, `sqlite3`)
   - Remove: the espeak-ng data copy block (misaki/espeakng_loader bundled path)
   - Remove: the `COPY deploy/bin/qwen3-tts-cli` block
   - Remove: `COPY noise/` block
   - Remove: the `apt-mark manual libstdc++6` and `apt-get purge` cleanup block (no longer needed without espeak-ng build deps)
   - Remove: `/models`, `/audio`, `/voices` mkdir lines (simplify to just `/var/lib/compadre`)
   - Keep: `ffmpeg` (still needed for audio preprocessing), `openssl` (backup encryption), `sqlite3` (operator debugging), `uv` installation, non-root user creation.
   - Add: HuggingFace cache dir env or volume for model auto-download.
   - Acceptance: Dockerfile builds successfully and produces an image with `ffmpeg` available.

1.6 **Trim compose.yaml volumes and environment**
   - Remove volumes: `compadre-models`, `compadre-voices`, `compadre-audio`
   - Keep: `compadre-registry` (key registry), `caddy-data`, `caddy-config`
   - Add: `compadre-models` volume for downloaded faster-whisper model
   - Remove all TTS-specific env vars: `ESPEAK_DATA_PATH`, `NOISE_DIR`, `VOICES_DIR`, `KOKORO_MODEL_PATH`, `KOKORO_VOICES_PATH`, `QWEN3_*`, `DEFAULT_VOICE`
   - Add STT env vars: `WHISPER_MODEL_SIZE` (default `large-v3-turbo`), `WHISPER_CPU_THREADS` (default `4`), `WHISPER_COMPUTE_TYPE` (default `int8`), `WHISPER_LANGUAGE` (default `pt`), `HF_HOME` for HuggingFace cache
   - Rename service: `compadre-stt` → `compadre-stt`
   - Update healthcheck: same pattern (use `/health` endpoint)
   - Keep `deploy.replicas` and resource limits
   - Update volume names with `compadre-` prefix
   - Acceptance: `docker compose config` validates without errors.

1.7 **Update the Caddyfile references**
   - Rename upstream service name: `compadre-stt` → `compadre-stt` (in the `dynamic a` block)
   - Update error hint strings: change "Comadre" → "Compadre" in the Caddyfile error response bodies
   - Update `_not_found_handler` hint in `main.py` (later phase) to say `/v1/audio/transcriptions` instead of `/v1/audio/speech`
   - Acceptance: `docker compose up` resolves the Caddy upstream correctly (after app is ready).

---

## Phase 2 — Brand Renames (Comadre → Compadre)

**Prerequisites:** Phase 1 complete.

2.1 **Rename Python package**
   - Rename `src/comadre/` → `src/compadre/`
   - In every Python file, replace `from comadre.` → `from compadre.`
   - In every Python file, replace `import comadre.` → `import compadre.`
   - In `pyproject.toml`, update `[project.scripts]` to `compadre-keys = "compadre.keys.cli.app:main"`
   - In `Dockerfile`, update the `compadre-keys-backup` script path if it references the package.
   - In `scripts/entrypoint.sh`, update `comadre.main:create_app` → `compadre.main:create_app`
   - Acceptance: `uv run python -c "from compadre.config import Settings; print('OK')"` succeeds.

2.2 **Rename the exception hierarchy**
   - Rename `ComadreError` → `CompadreError` in `src/compadre/exceptions.py`
   - Update all subclasses that reference `ComadreError` in their `super().__init__`
   - Grep for `ComadreError` across the entire codebase and replace all occurrences
   - Update the exception handler registration in `main.py`
   - Acceptance: `rg "ComadreError" src/` returns no hits. `rg "CompadreError" src/` shows the correct base class.

2.3 **Rename env var prefix**
   - Replace all `COMADRE_` prefixed env vars in `config.py`, `compose.yaml`, `Dockerfile`, ansible playbooks with `COMPADRE_`
   - Keep the env var name `KEY_REGISTRY_DB_PATH` as-is (per brief §11 — same registry contract)
   - Acceptance: `rg "COMADRE_" compose.yaml Dockerfile deploy/` returns no hits (except doc/comments that explicitly reference Comadre for provenance).

2.4 **Rename CLI binary and references**
   - The CLI Python code renames naturally with package rename. The pyproject.toml entry becomes `compadre-keys`.
   - Update all ansible playbooks, scripts, and docs that reference `compadre-keys` → `compadre-keys`
   - Update the backup script baked into the Dockerfile from `compadre-keys-backup` → `compadre-keys-backup`
   - Update the health check command if it references the CLI name
   - Acceptance: `rg "comadre-keys" deploy/ scripts/ Dockerfile` returns no hits.

2.5 **Rename key prefix**
   - In `src/compadre/keys/tokens.py`, change the key prefix from `cmdr_` → `cmpd_` (live) and `cmdr_test_` → `cmpd_test_` (if Comadre uses a test prefix)
   - Update any documentation/help strings that reference key prefixes
   - Acceptance: `rg "cmdr_" src/` returns no hits; `rg "cmpd_" src/` shows the correct prefixes.

2.6 **Update FastAPI app metadata**
   - In `src/compadre/main.py`, update `title="Comadre"` → `title="Compadre"`
   - Update error handler hint strings that mention "Comadre" → "Compadre"
   - Update `_method_not_allowed_handler` hint to say `/v1/audio/transcriptions`
   - Acceptance: Visiting `/docs` shows "Compadre" as the API title (once re-enabled or in dev).

2.7 **Rename volume and path references**
   - All Docker named volumes: `comadre-*` → `compadre-*`
   - In-container mount paths: `/var/lib/compadre` → `/var/lib/compadre` (for key registry)
   - Update `compose.yaml`, `Dockerfile`, ansible playbooks, and cron scripts
   - Acceptance: `rg "/var/lib/compadre"` returns no hits; `rg "/var/lib/compadre"` shows the new path.

2.8 **Update the Caddyfile `handle_errors` body strings**
   - The Caddyfile hardcodes "Comadre" in error response bodies — grep for "Comadre" in `deploy/caddy/Caddyfile` and replace with "Compadre"
   - Acceptance: `rg -i "comadre" deploy/caddy/Caddyfile` returns no hits.

---

## Phase 3 — Build the STT Engine

**Prerequisites:** Phase 2 complete.

3.1 **Create `src/compadre/download.py` — model auto-download**
   - Pattern: mirror Comadre's `download.py` exactly (same `_download_file`, `_ensure_file`, same `ssl.SSLCertVerificationError` handling).
   - Create `ensure_whisper_model(model_path: str)`: download `large-v3-turbo` from HuggingFace via `faster-whisper`'s built-in download mechanism or manual HuggingFace Hub download.
   - The `faster-whisper` library auto-downloads on first `WhisperModel(...)` call when the model is not cached. Consider using `huggingface_hub.snapshot_download` for explicit control, matching the Comadre pattern of explicit download-before-load.
   - Use `HF_HOME` env var for cache directory.
   - Acceptance: calling `ensure_whisper_model` creates the model cache directory at `HF_HOME/models--Systran--faster-whisper-large-v3-turbo`.

3.2 **Create `src/compadre/stt.py` — STT engine service**
   - File: `src/compadre/stt.py`
   - This is the replacement for `src/compadre/service.py` (which was deleted in Phase 1).
   - Contains a single class: `WhisperEngine`

   **`WhisperEngine` class specification:**

   ```python
   class WhisperEngine:
       """Wraps faster-whisper for STT transcription."""

       def __init__(self) -> None:
           self._model: WhisperModel | None = None

       @property
       def is_loaded(self) -> bool:
           """Return True if the Whisper model is loaded."""
           return self._model is not None

       def load_model(
           self,
           model_size: str = "large-v3-turbo",
           device: str = "cpu",
           compute_type: str = "int8",
           cpu_threads: int = 4,
       ) -> None:
           """Load the faster-whisper model into memory.

           On first call, faster-whisper auto-downloads from HuggingFace Hub.
           Subsequent calls load from cache (HF_HOME).
           """
           from faster_whisper import WhisperModel

           self._model = WhisperModel(
               model_size,
               device=device,
               compute_type=compute_type,
               cpu_threads=cpu_threads,
           )

       def warm_up(self) -> None:
           """Run a minimal transcription to preload model internals."""
           if self._model is None:
               raise CompadreError(...)
           # Transcribe a tiny silent WAV (or short built-in audio)
           # Use a tempfile with a short silence.
           ...

       def transcribe(
           self,
           audio_path: Path,
           *,
           language: str | None = "pt",
           beam_size: int = 5,
           word_timestamps: bool = True,
           condition_on_previous_text: bool = False,
       ) -> tuple[list[Segment], TranscriptionInfo]:
           """Run transcription. Returns segments and info object."""
           segments, info = self._model.transcribe(
               str(audio_path),
               beam_size=beam_size,
               word_timestamps=word_timestamps,
               language=language,
               condition_on_previous_text=condition_on_previous_text,
           )
           return list(segments), info
   ```

   - The `transcribe` method MUST use the exact parameter values from §3 of the brief:
     - `beam_size=5`
     - `word_timestamps=True`
     - `condition_on_previous_text=False` (non-negotiable per brief)
     - `language="pt"` as default, allowing `None` for auto-detect
   - The model is loaded ONCE at lifespan startup (same pattern Comadre uses).
   - Acceptance: `WhisperEngine().load_model()` completes without error; `is_loaded` returns `True`.

3.3 **Create `src/compadre/preprocessing.py` — audio decoding**
   - File: `src/compadre/preprocessing.py` (replaces the deleted TTS `preprocessing.py`)
   - Functions to create:

   ```python
   def decode_audio_to_wav(
       input_path: Path,
       output_dir: Path,
       *,
       sample_rate: int = 16000,
       channels: int = 1,
   ) -> Path:
       """Decode any audio format to 16 kHz mono PCM s16le WAV via ffmpeg.

       Args:
           input_path: Path to the uploaded audio file (OPUS, MP3, M4A, WAV, etc.)
           output_dir: Directory for the output temp WAV

       Returns:
           Path to the decoded WAV file

       Raises:
           CompadreError subclass on failure
       """
       # Implementation mirrors youtube-guru's extract_audio():
       # ffmpeg -y -i input_path -vn -acodec pcm_s16le -ar 16000 -ac 1 output.wav
       ...

   def get_audio_duration(audio_path: Path) -> float:
       """Return audio duration in seconds via ffprobe.

       Mirrors youtube-guru config.py::get_video_duration_ffprobe.
       """
       ...
   ```

   - Limits (enforced in the route handler, not here):
     - Max upload 25 MB
     - Max duration 10 minutes (600 seconds)
   - Acceptance: `decode_audio_to_wav(Path("test.opus"), tmpdir)` produces a valid 16kHz mono WAV.

3.4 **Create `src/compadre/formatters.py` — output formatting**
   - File: `src/compadre/formatters.py`
   - Functions to create:

   ```python
   def format_text(segments: list[Segment]) -> str:
       """Plain text: concatenate all segment texts."""
       return " ".join(s.text.strip() for s in segments)

   def format_json(text: str) -> dict:
       """JSON mode: {"text": "..."}"""
       return {"text": text}

   def format_verbose_json(
       segments: list[Segment],
       language: str,
       duration: float,
   ) -> dict:
       """OpenAI verbose_json format with segments and words."""
       return {
           "text": format_text(segments),
           "language": language,
           "duration": duration,
           "segments": [
               {
                   "start": s.start,
                   "end": s.end,
                   "text": s.text.strip(),
                   "words": [
                       {"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                       for w in (s.words or [])
                   ],
               }
               for s in segments
           ],
       }

   def format_srt(segments: list[Segment]) -> str:
       """SRT subtitle format.
       
       Reuses format_srt_timestamp from youtube-guru's srt_utils.py.
       """
       # Copy the format_srt_timestamp function verbatim from
       # ~/dev/youtube-guru/youtube_pipeline/srt_utils.py

   def format_vtt(segments: list[Segment]) -> str:
       """WebVTT subtitle format.
       
       Like SRT but with WebVTT header and period as millisecond separator.
       """
       ...
   ```

   - The `format_srt_timestamp` function MUST be copied verbatim from youtube-guru's `srt_utils.py` (single function, no further dependencies).
   - Acceptance: Each formatter produces correct output for a known segment list.

3.5 **Create `src/compadre/schemas.py` — request/response schemas**
   - File: `src/compadre/schemas.py` (replaces the deleted TTS schemas.py)
   - Pydantic models:

   ```python
   class ResponseFormat(StrEnum):
       text = "text"
       json = "json"
       verbose_json = "verbose_json"
       srt = "srt"
       vtt = "vtt"

   # No pydantic model for the request body — it's multipart/form-data.
   # We use FastAPI's UploadFile + Form fields.
   ```

   - Keep the file simple. The brief says the endpoint is `multipart/form-data`, not JSON.
   - Acceptance: `from compadre.schemas import ResponseFormat` works.

---

## Phase 4 — Build the API Endpoint

**Prerequisites:** Phase 3 complete.

4.1 **Create `src/compadre/api.py` — transcription route**
   - File: `src/compadre/api.py` (replaces the deleted TTS `api.py`)
   - Structure: mirror Comadre's `api.py` — same dependency chain, different middle three steps.

   **Route specification:**

   ```python
   @api_router.post(
       "/v1/audio/transcriptions",
       dependencies=[
           Depends(verify_ready),
           Depends(check_speech_per_ip_rate_limit),  # reuse existing per-IP gate
           Depends(verify_api_key),
           Depends(check_rate_limit),
       ],
   )
   async def transcribe_audio(
       request: Request,
       file: UploadFile = File(...),
       model: str = Form("whisper-large-v3-turbo"),
       language: str | None = Form(None),
       response_format: str = Form("text"),
       temperature: float = Form(0.0),
   ) -> Response:
       """Transcribe audio to text. OpenAI Whisper API compatible."""
       ...
   ```

   **Handler flow (mirrors Comadre's `generate_speech` lifecycle exactly):**

   1. Validate file size ≤ 25 MB (reject with `CompadreError` subclass if exceeded)
   2. Save uploaded file to a tempfile
   3. Validate duration via `get_audio_duration` — reject if > 600 seconds
   4. Decode audio via `decode_audio_to_wav` → 16 kHz mono WAV tempfile
   5. `request.app.state.queue_depth += 1`
   6. Acquire FIFO lock: `async with request.app.state.queue_lock:`
   7. Inside lock, run transcription via `asyncio.to_thread(request.app.state.engine.transcribe, ...)` with a timeout (e.g., 120s)
   8. `request.app.state.queue_depth -= 1` (in finally block)
   9. Format output per `response_format`
   10. Return appropriate `Response` (plain text, JSON, or SRT/VTT)

   **Audio limits enforcement:**
   - Max upload 25 MB → check `file.size` or read into a `SpooledTemporaryFile` with `max_size=25*1024*1024`
   - Max duration 10 minutes → validate with `ffprobe` before invoking engine
   - Reject with `CompadreError` subclass on violation (e.g., `InvalidRequestError`)
   - Also validate with `ffprobe` that the file is actually decodable audio

   **Dependencies reuse:**
   - `verify_ready` — same as Comadre (checks `app.state.ready`)
   - `check_speech_per_ip_rate_limit` — rename to `check_transcription_per_ip_rate_limit` but keep the same implementation (the per-IP gate)
   - `verify_api_key` — unchanged from Comadre
   - `check_rate_limit` — unchanged from Comadre (buckets on `key:<key_id>`)

   **Response formats:**
   - `text` → `Response(content=plain_text, media_type="text/plain")`
   - `json` → `JSONResponse(content={"text": plain_text})`
   - `verbose_json` → `JSONResponse(content={...})`
   - `srt` → `Response(content=srt_string, media_type="text/plain")`
   - `vtt` → `Response(content=vtt_string, media_type="text/plain")`

   - Acceptance: `POST /v1/audio/transcriptions` with a valid `.opus` file and API key returns transcribed text.

4.2 **Update `src/compadre/main.py` — lifespan and app**
   - Replace engine-related startup phases with STT equivalents:

   **In `lifespan`:**
   - Remove: kokoro model download, qwen3 model download, espeak-ng data verification, noise directory verification, voices directory verification, kokoro engine load, qwen3 engine load, engine warm-up (that specific shape)
   - Add:
     ```python
     # Phase 5: Download/verify whisper model
     logger.info("Checking Whisper model files...")
     await asyncio.to_thread(ensure_whisper_model, ...)

     # Phase 6: Load Whisper engine
     logger.info("Loading Whisper STT engine...")
     engine = WhisperEngine()
     engine.load_model(
         model_size=settings.whisper_model_size,
         compute_type=settings.whisper_compute_type,
         cpu_threads=settings.whisper_cpu_threads,
     )

     # Phase 7: Warm up
     logger.info("Warming up Whisper engine...")
     engine.warm_up()

     # Phase 8: Ready
     app.state.engine = engine
     app.state.ready = True
     ```

   - Keep ALL Comadre subsystems (Phase 1: config, logging, Phase 2: key registry, auth cache, rate limiter, counters, sentinel probe). These are verbatim — just namespace-renamed.
   - Remove the `EngineRouter`/`router` references; replace with single `engine` state.
   - Update `_not_found_handler` hint to say `/v1/audio/transcriptions`
   - Update `_method_not_allowed_handler` hint accordingly
   - Acceptance: App starts, model loads, `/health` returns `ready=true` after warm-up.

4.3 **Update `src/compadre/health.py` — health response**
   - Replace `router.available_voices()` and `router.engine_status()` with STT equivalents:
   - `available_models()` → returns `["large-v3-turbo"]` (single model)
   - `engine_status()` → returns `{"whisper": {"loaded": True, "model": "large-v3-turbo"}}`
   - Keep the same status/uptime/memory/version fields
   - Keep `build_minimal_health_response` and `build_health_response` signatures/modes
   - Acceptance: `GET /health` (unauthenticated) returns `{"status": "healthy", "models": ["large-v3-turbo"]}`.

4.4 **Update `src/compadre/config.py` — settings**
   - Remove all TTS-specific settings fields (kokoro paths, qwen3 paths, voices_dir, noise_dir, espeak_data_path, default_voice, etc.)
   - Keep `RegistrySettings` as-is (key registry, billing, crypto) — this is the critical inherited surface
   - Keep `Settings(RegistrySettings)` pattern
   - Add:
     ```python
     whisper_model_size: str = "large-v3-turbo"
     whisper_compute_type: str = "int8"
     whisper_cpu_threads: int = Field(default=4, ge=1)
     whisper_language: str | None = "pt"
     max_upload_size_mb: int = 25
     max_audio_duration_seconds: int = 600
     hf_home: str = Field(default="./data/cache/huggingface", ...)
     ```
   - Keep `get_settings()` and `get_registry_settings()` as LRU-cached factory functions
   - Acceptance: `Settings()` loads with new fields, old TTS fields are gone.

---

## Phase 5 — Build the Browser Landing Page

**Prerequisites:** Phase 4 complete. The API endpoint must work for the landing page to test against.

5.1 **Create `deploy/landing/index.html` — upload-to-transcribe UI**
   - File: `deploy/landing/index.html`
   - **Architectural pattern:** same as Comadre's landing page — static HTML/JS, served by Caddy at `/`, API calls go to `/v1/audio/transcriptions`. No Node.js, no framework.
   - **UI elements (Brazilian Portuguese strings):**
     - Logo (placeholder or same logo with "Compadre" branding)
     - File input area (drag-and-drop or click-to-upload, accept audio/*)
     - API key field (bring-your-own, same auth model as Comadre)
     - Language selector (auto-detect default, `pt` option)
     - Response format selector: `text`, `json`, `verbose_json`, `srt`, `vtt`
     - "Transcrever" button
     - Result text area below (read-only, monospace)
     - "Copiar" (copy-to-clipboard) button
     - Loading spinner during transcription
     - Error display area with error type/message/hint
   - **JS behavior:**
     - On submit: POST multipart form to `/v1/audio/transcriptions` with Authorization header
     - Display result or error
     - Copy-to-clipboard for text result
   - **Styling:** dark theme, matching Comadre's landing page aesthetic (CSS custom properties). Brand colors can shift from Comadre's orange to a complementary color (e.g., teal/blue for STT).
   - **Tagline (recursive acronym):** "Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita"
   - Acceptance: Opening `/` in a browser, uploading an `.opus` file with a valid API key, clicking "Transcrever" shows the transcribed text.

5.2 **Create `deploy/landing/logo-compadre.webp`**
   - File: `deploy/landing/logo-compadre.webp`
   - Create a placeholder logo or use a text-based logo. If the owner provides a logo later, replace.
   - Acceptance: The landing page shows the logo image.

5.3 **Ensure Caddy serves the landing page**
   - The Caddyfile pattern is already correct from Comadre (Phase 2 inherit): `handle { root * /srv/www; file_server }` for non-API paths.
   - No changes needed if `deploy/landing/` is bind-mounted at `/srv/www:ro` in `compose.yaml`.
   - Verify: `docker compose up`, then `curl http://localhost/` serves `index.html`.
   - Acceptance: Landing page loads at `/`.

---

## Phase 6 — Test Suite

**Prerequisites:** Phase 4 complete (API endpoint working).

6.1 **Update test infrastructure**
   - File: `tests/conftest.py` — update imports from `comadre.` → `compadre.`
   - Update `mock_router` to `mock_engine` (mock WhisperEngine instead of EngineRouter)
   - Update `app` fixture to use `compadre.main.create_app`
   - Remove TTS-specific fixtures (voice lists, etc.)
   - Update `auth_headers` fixture (should still work — key registry is unchanged)
   - Acceptance: `uv run pytest` discovers tests (even if many fail at first due to missing updates in individual test files).

6.2 **Update existing tests**
   - File by file:
     - `tests/test_config.py` — update to test new STT config fields, remove TTS field tests
     - `tests/test_exceptions.py` — rename `ComadreError` → `CompadreError`, update any TTS-specific error tests
     - `tests/test_health.py` — update to expect `models` instead of `voices`
     - `tests/test_rate_limit.py` — minimal changes (namespace rename)
     - `tests/test_counters.py` — minimal changes
     - `tests/test_logging.py` — minimal changes
     - `tests/test_main_lifespan.py` — update for STT engine
     - `tests/test_api.py` — rewrite for `/v1/audio/transcriptions` instead of `/v1/audio/speech`
     - `tests/test_schemas.py` — update for new schemas
     - `tests/test_routing.py` — update
     - ALL `tests/test_keys_*.py` files — namespace rename only
     - Delete: `tests/test_preprocessing.py` (was TTS preprocessing), `tests/test_service.py` (was TTS engine)
     - Delete: `tests/test_feasibility.py` (TTS-specific)
     - Acceptance: All non-slow tests pass.

6.3 **Write new tests**
   - `tests/test_stt.py` — unit tests for WhisperEngine (mocked `faster_whisper.WhisperModel`)
   - `tests/test_preprocessing_audio.py` — unit tests for `decode_audio_to_wav` and `get_audio_duration`
   - `tests/test_formatters.py` — tests for all five output formatters
   - `tests/test_api_transcriptions.py` — integration tests for the new endpoint:
     - Test with a small valid audio file → successful response in each format
     - Test file too large → 413 or 400
     - Test duration too long → 400
     - Test missing API key → 401
     - Test rate-limited key → 429
     - Test unsupported file format → ffprobe validation failure
     - Test `language` parameter override
     - Test `response_format` parameter for all five values
     - Test CORS headers if applicable
   - Mark slow tests (real model) with `@pytest.mark.slow`
   - Acceptance: `uv run pytest -m "not slow"` all pass; `uv run pytest -m slow` pass when model is available.

6.4 **Quality checks**
   - Ensure `ruff check .` clean
   - Ensure `mypy src/` strict mode clean
   - Ensure `pre-commit run --all-files` passes
   - Update `.pre-commit-config.yaml` if needed for namespace changes
   - Acceptance: All three commands exit 0.

---

## Phase 7 — Deploy Artifacts Finalization

**Prerequisites:** Phase 6 complete (all tests pass).

7.1 **Update Dockerfile**
   - Ensure it builds with `docker compose build`
   - Verify `ffmpeg` is in the image: `docker compose run compadre-stt ffmpeg -version`
   - Verify the model auto-download works on first startup (or is pre-baked via volume)
   - Acceptance: `docker compose up` starts the service and model loads.

7.2 **Update ansible playbooks**
   - Files in `deploy/ansible/playbooks/`:
     - `bootstrap.yml` — rename references: `compadre-keys` → `compadre-keys`, `/var/lib/compadre` → `/var/lib/compadre`, service names, volume names
     - `update.yml` — same renames
     - `health.yml` — update assertion checks for new service name, new CLI name, new paths
     - `cleanup.yml` — same renames
     - `backup-keys.yml` — rename script reference: `compadre-keys-backup` → `compadre-keys-backup`
     - `restore-keys.yml` — rename references
     - `deploy-models.yml` — if it exists, adapt for whisper model instead of TTS models
     - `deploy-voices.yml` — DELETE (no voices concept in Compadre)
   - Update `deploy/ansible/scripts/backup-keys.sh` if it references comadre
   - Acceptance: Each playbook can be run with `ansible-playbook --check` without syntax errors.

7.3 **Update scripts**
   - `scripts/loadtest.py` — rewrite for STT: upload audio files instead of generating speech, measure transcription latency, concurrency behavior
   - `scripts/capacity_test.py` — rewrite for STT: measure throughput with typical WhatsApp audio sizes (30s opus files)
   - `scripts/entrypoint.sh` — update to `compadre.main:create_app`
   - Acceptance: `scripts/entrypoint.sh` starts the app.

7.4 **Update compose.yaml healthcheck**
   - Healthcheck should query `/health` endpoint (unchanged pattern)
   - Update `start_period` to account for model download + load time (e.g., 180s)
   - Acceptance: `docker compose up` → service becomes healthy within start_period.

---

## Phase 8 — Documentation

**Prerequisites:** Phase 7 complete.

8.1 **Write `README.md`**
   - Reuse Comadre's README structure:
     - Cost comparison table (Compadre vs OpenAI Whisper API vs ElevenLabs Scribe pricing)
     - Architecture diagram (as Mermaid or text diagram)
     - Deployment block (compose up, ansible bootstrap)
     - Docs index
   - Brand: Compadre, tagline: "Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita"
   - Post-MVP section (§8 of brief): mention planned multi-speaker diarization feature
   - Acceptance: README renders correctly on GitHub/Gitea.

8.2 **Write `CLAUDE.md`**
   - Structure mirroring Comadre's `CLAUDE.md`:
     - Project overview (STT instead of TTS)
     - Language policy (unchanged)
     - Build & Test commands
     - Architecture section — update for STT engine
     - Code conventions (unchanged)
     - Key file locations (update)
     - Non-Obvious Gotchas (carry over applicable ones; add STT-specific)
   - Keep all Comadre gotchas that apply to the shared subsystems (key registry, billing, backup, ansible)
   - Acceptance: A developer can read CLAUDE.md and understand how to work on the project.

8.3 **Remove TTS-specific docs (already done in Phase 1.3)**
   - Verify no TTS docs remain
   - Create stub docs for STT-specific topics if needed (e.g., `docs/model-download.md`)

---

## Phase 9 — MVP Acceptance Checklist

**Prerequisites:** Phases 0-8 complete.

Execute the definition of done from §7 of the brief:

9.1 **API endpoint verification**
   - [ ] `POST /v1/audio/transcriptions` accepts WhatsApp `.opus` files and returns text in pt-BR
   - [ ] All five `response_format` values supported (`text`, `json`, `verbose_json`, `srt`, `vtt`)
   - Acceptance: Manual curl test with each format.

9.2 **CLI verification**
   - [ ] `compadre-keys` CLI feature-complete with all 21 verbs (same verb set as Comadre)
   - [ ] `compadre-keys create-customer`, `issue-key`, `mark-paid`, `suspend`, `reactivate`, `revoke-key`, `at-risk`, `revenue`, `suspend-sweep`, `health`, `cache-status`, `export-customer`, `delete-customer --hard`, `rotate-encryption-key`, `rotate-key`, `list-customers`, `list-keys`, `show`, `update-customer`, `update-key`, `correct-tax-id`
   - Acceptance: Run each verb; all produce expected output.

9.3 **Rate limiting verification**
   - [ ] Per-key rate limiting via existing `rate_limit.py` — buckets on `key_id`, fed by `rate_limit_rpm` from registry
   - [ ] Per-IP gate on `/v1/audio/transcriptions` (600/min/IP)
   - [ ] Per-IP gate on `/health` (60/min/IP)
   - Acceptance: Send > rpm requests within 1 minute, verify 429 with correct headers.

9.4 **Counters verification**
   - [ ] Counters snapshot every 15s
   - [ ] Multi-replica aggregation works (`compadre-keys cache-status` shows combined data)
   - [ ] `compadre-keys show <key-prefix> --audit-tail N` works
   - Acceptance: Run two replicas, send requests to both, verify aggregated counters.

9.5 **Landing page verification**
   - [ ] Browser landing page at `/` works: upload → transcribe → show text
   - [ ] API key field is bring-your-own (no stored keys)
   - [ ] Copy-to-clipboard button works
   - [ ] All UI strings are Brazilian Portuguese
   - Acceptance: Open in browser, perform a transcription, verify result displays.

9.6 **Caddy + multi-replica verification**
   - [ ] Caddy HTTPS termination active
   - [ ] `least_conn` LB across `deploy.replicas ≥ 2` verified
   - [ ] `dynamic a` upstream resolver re-resolves Docker DNS every 5s
   - [ ] Bounded-payload policy (Content-Length enforcement) active on API paths
   - Acceptance: Run two replicas, send traffic, verify both receive requests (check counters per replica).

9.7 **Ansible playbooks verification**
   - [ ] `bootstrap.yml` — runs without errors, creates cron jobs
   - [ ] `update.yml` — pulls new image, restarts service
   - [ ] `health.yml` — assertion chain passes all checks
   - [ ] `cleanup.yml` — runs without errors
   - [ ] `backup-keys.yml` — produces encrypted backup tarball
   - [ ] `restore-keys.yml` — restores from backup tarball
   - Acceptance: Run each playbook on a test VPS; all exit 0.

9.8 **Health check verification**
   - [ ] `compadre-keys health --format json` returns `is_healthy=true` with all checks green
   - [ ] Health checks include: DB mode 0600, parent dir 0700, cron markers, backup age, UFW rules (if applicable)
   - Acceptance: Command exits 0 in table mode; JSON output has `is_healthy: true`.

9.9 **Quality verification**
   - [ ] `ruff check .` clean
   - [ ] `mypy src/` strict mode clean
   - [ ] `pre-commit run --all-files` passes
   - [ ] All tests pass: `uv run pytest -m "not slow"` and `uv run pytest -m slow`
   - Acceptance: All commands exit 0.

9.10 **Load test verification**
   - [ ] Load test analogous to Comadre's `scripts/loadtest.py` works:
     - Upload 30s WhatsApp `.opus` files
     - Measure latency (target ≤ 10s for 30s audio)
     - Measure throughput under concurrency
   - [ ] Capacity test analogous to Comadre's `scripts/capacity_test.py` works:
     - Measure realtime factor (target ≤ 0.3 on 4 vCPU)
   - Acceptance: Run load test against at least 2 replicas; verify latency and throughput.

---

## Files to Modify

### Phase 1 — Deletions
- `src/comadre/preprocessing.py` — DELETE (TTS-specific)
- `src/comadre/service.py` — DELETE (TTS engine)
- `model/`, `models/`, `voice/`, `voices/`, `noise/` — DELETE dirs
- `deploy/espeak-ng/` — DELETE dir
- `deploy/bin/qwen3-tts-cli` — DELETE
- Multiple TTS docs in `docs/` — DELETE
- `deploy/ansible/playbooks/deploy-voices.yml` — DELETE

### Phase 1 — Edited
- `pyproject.toml` — dependency changes
- `Dockerfile` — remove TTS build deps, simplify
- `compose.yaml` — remove TTS volumes, rename service
- `deploy/caddy/Caddyfile` — rename upstream service, update error strings

### Phase 2 — Edited (namespace renames)
- `src/comadre/` → `src/compadre/` (all files — rename package)
- All `*.py` files under `src/compadre/` — update imports
- `src/compadre/exceptions.py` — `ComadreError` → `CompadreError`
- `src/compadre/config.py` — env var prefix `COMADRE_` → `COMPADRE_`
- `src/compadre/keys/tokens.py` — key prefix `cmdr_` → `cmpd_`
- `src/compadre/main.py` — app title, error hints
- `src/compadre/keys/cli/app.py` — CLI name in help text
- All ansible playbooks — rename references
- `scripts/entrypoint.sh` — module reference
- `Dockerfile` — path references, backup script name
- `compose.yaml` — volume names, service names, paths

### Phase 3 — New files
- `src/compadre/download.py` — whisper model auto-download
- `src/compadre/stt.py` — WhisperEngine class
- `src/compadre/preprocessing.py` — audio decode (replaces old TTS preprocessing.py)
- `src/compadre/formatters.py` — output formatting (text, json, verbose_json, srt, vtt)
- `src/compadre/schemas.py` — new schemas (replaces old TTS schemas.py)

### Phase 4 — Edited
- `src/compadre/api.py` — new transcription route (replaces old speech route)
- `src/compadre/main.py` — update lifespan for STT engine
- `src/compadre/health.py` — update for STT (models instead of voices)
- `src/compadre/config.py` — add STT settings, remove TTS settings

### Phase 5 — New/modified files
- `deploy/landing/index.html` — new landing page (replaces Comadre's)
- `deploy/landing/logo-compadre.webp` — new logo (replaces `logo-comadre.webp`)

### Phase 6 — Test files
- `tests/conftest.py` — update imports, fixtures
- `tests/test_config.py` — update for new settings
- `tests/test_exceptions.py` — update error class names
- `tests/test_health.py` — update for STT health shape
- `tests/test_api.py` — rewrite for transcription endpoint
- `tests/test_schemas.py` — update for new schemas
- `tests/test_routing.py` — update paths
- `tests/test_main_lifespan.py` — update for STT
- `tests/test_preprocessing.py` — DELETE old TTS version; write new audio preprocessing tests
- `tests/test_service.py` — DELETE; write `tests/test_stt.py` instead
- `tests/test_feasibility.py` — DELETE
- `tests/test_stt.py` — NEW (WhisperEngine unit tests)
- `tests/test_preprocessing_audio.py` — NEW (audio decode tests)
- `tests/test_formatters.py` — NEW (formatter tests)
- `tests/test_api_transcriptions.py` — NEW (transcription endpoint integration tests)
- All `tests/test_keys_*.py` — namespace rename only

### Phase 7 — Deploy artifacts
- `Dockerfile` — final verification and fixes
- `compose.yaml` — final verification
- `deploy/caddy/Caddyfile` — final verification
- All ansible playbooks — final verification
- `scripts/loadtest.py` — rewrite for STT
- `scripts/capacity_test.py` — rewrite for STT

### Phase 8 — Documentation
- `README.md` — rewrite for Compadre
- `CLAUDE.md` — rewrite for Compadre

---

## New Files Summary

| File | Purpose |
|---|---|
| `src/compadre/download.py` | Whisper model auto-download (mirrors Comadre's download.py) |
| `src/compadre/stt.py` | WhisperEngine class wrapping faster-whisper |
| `src/compadre/preprocessing.py` | Audio decoding via ffmpeg (replaces TTS preprocessing) |
| `src/compadre/formatters.py` | Output formatting for 5 response_format values |
| `src/compadre/schemas.py` | Pydantic models for STT (replaces TTS schemas) |
| `src/compadre/api.py` | Transcription endpoint (replaces TTS api.py) |
| `deploy/landing/index.html` | STT landing page (replaces Comadre's) |
| `deploy/landing/logo-compadre.webp` | Compadre brand logo |
| `tests/test_stt.py` | WhisperEngine unit tests |
| `tests/test_preprocessing_audio.py` | Audio decode tests |
| `tests/test_formatters.py` | Formatter tests |
| `tests/test_api_transcriptions.py` | Transcription endpoint tests |

---

## Dependencies

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5
                                                      │
                                                      ├──► Phase 6 (parallel with Phase 5)
                                                      │
                                                      └──► Phase 7 ──► Phase 8 ──► Phase 9
```

- Phase 0 MUST be first (gate + study)
- Phase 1 MUST precede Phase 2 (delete TTS code before renaming what remains)
- Phase 2 MUST precede Phase 3 (all renames done before writing new code)
- Phase 3 MUST precede Phase 4 (engine must exist before API can use it)
- Phase 4 MUST precede Phase 5 (API must work before landing page can test it)
- Phase 6 can overlap Phase 5 (tests can be written while landing page is built)
- Phase 7 depends on Phases 5-6 (deploy artifacts verified last)
- Phase 8 depends on Phase 7 (docs describe final state)
- Phase 9 depends on all phases (acceptance checklist)

---

## Risks

1. **Comadre not at 100% validation yet.** The entire plan is gated on the project owner's signal. If Comadre changes after the fork, the fork must be re-synchronized or the delta manually applied. Mitigation: wait for explicit confirmation.

2. **faster-whisper model download size.** `large-v3-turbo` is ~1.5 GB. First startup will download this over the network. On slow VPS connections, this can take minutes and the container will appear hung during download. Mitigation: use `start_period: 300s` in the healthcheck and pre-download via ansible or a volume.

3. **Memory pressure with multiple replicas.** Per §9: 1.5-2 GB RAM per replica. With 16 GB total VPS, 3-4 replicas is tight. Mitigation: start with 2 replicas and scale as usage demands; document the memory constraint.

4. **ffmpeg subprocess security.** Audio decode uses subprocess with user-uploaded files. Mitigation: validate file type early (via extension + magic bytes before `ffprobe`), use `tempfile` for all intermediate files, set subprocess `timeout`, never shell-inject filenames.

5. **Rate limiter in-memory storage.** Per-replica buckets (mirroring Comadre). Multi-replica deployments multiply effective RPM. Mitigation: document this behavior (as Comadre does in `docs/rate-limiting.md`); operators configure per-replica RPM accordingly.

6. **Key registry schema compatibility.** The Compadre registry uses the same schema as Comadre. If Comadre adds a schema migration (v002+) after the fork, those migrations must be backported or the fork point kept in sync. Mitigation: fork at the validated commit and track Comadre's changelog.

7. **language=pt behavior on non-Portuguese audio.** The brief says `language="pt"` as default. Some callers will upload non-pt audio. Mitigation: the API accepts `language` parameter; when omitted, default is `pt` (as designed). Document that callers should pass `language=<auto>` or the appropriate code for other languages.

8. **SRT/VTT segment boundaries.** faster-whisper segments may not align perfectly with sentence boundaries. SRT/VTT output will have whatever boundaries the model returns. Mitigation: this is acceptable for MVP; post-MVP refinement can add VAD chunking if needed.

9. **Dockerfile `useradd` compatibility.** The Comadre Dockerfile uses `useradd --create-home --shell /bin/bash appuser`. The simplified Compadre Dockerfile must still pre-create the registry mountpoint owner. Mitigation: test the Dockerfile build on the target VPS OS.

10. **Landing page CORS.** If the landing page is served from the same domain as the API (via Caddy), no CORS is needed. If users access the API from external tools, CORS headers from Comadre may need to be in place. Mitigation: verify the Comadre CORS configuration in the Caddyfile and carry it over.

11. **Ambiguity: temperature parameter.** The brief says "accept for compat; ignored in MVP." The faster-whisper `transcribe` method does not accept a `temperature` parameter (unlike openai-whisper). Mitigation: accept the form field and silently ignore it; do not pass to the model call.

12. **Ambiguity: model parameter.** The brief says "optional; ignored — only one model exists, accept for OpenAI-SDK compatibility." Implementation: accept the form field, validate that it's not an unsupported value that would confuse the client, but always use `large-v3-turbo`.

---

## Critical Constraints Summary (from brief)

| Constraint | Source § |
|---|---|
| Do not start before Comadre validated + path provided | §1, §10, §12 |
| Inherit Comadre subsystems verbatim (no "improvements") | §2 |
| Single substitution: TTS → STT engine | §3 |
| Use `faster-whisper` with `large-v3-turbo`, `int8`, CPU-only | §3 |
| `condition_on_previous_text=False` (non-negotiable) | §3 |
| No dictionary/custom-vocabulary feature in MVP | §12 |
| No speaker diarization in MVP | §12 |
| No multiple model sizes | §12 |
| No audio-minute billing or registry schema migrations | §7, §12 |
| No VAD chunking, no noise reduction | §12 |
| No customer-facing dashboard | §12 |
| No rewriting/refactoring Comadre code | §2, §12 |
| Landing page matches Comadre's pattern exactly | §6 |
| Billing: `rate_limit_rpm` only in MVP | §7 |
