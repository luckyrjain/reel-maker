# Reel Maker — Product Gap Analysis and Roadmap

**Date:** 2026-08-02
**Basis:** full source review, a live end-to-end run (Postgres, Redis, real workers, real LLM/footage/TTS/ffmpeg), and the existing `docs/roadmap.md` build log. Every gap below cites the file that proves it — this is not speculation about what a video tool "should" have.

---

## What the product is today

Reel Maker turns a text prompt into a scored, edited, rendered 9:16 MP4 for two platforms (YouTube Shorts, Instagram Reels), with a human review step before anything ships. The pipeline — enrich → generate → two-tier quality gate → render — is genuinely solid: 146 tests, idempotent Celery tasks, atomic file writes, per-beat asset pinning, real retry semantics (verified live against an actual timeout today), a 17-axis rule scorer plus an LLM judge, closed-loop retry with feedback. That machinery is further along than most side projects ever get.

It stops exactly at "review." Nothing downstream of `approved` exists: no publishing, no scheduling, no analytics, no cost visibility, no operator-facing history. It has been built and tuned against one niche (football) by one operator on one machine, with no authentication, no rate limiting, and no CI. That's not a criticism of sequencing — hardening the core before building on top of it was the right call — but it means the product, as a product, is roughly 40% built: the hard half (reliable generation + render) is done; the half that makes it useful day-to-day (see it, trust it, ship it, learn from it) has barely started.

---

## Gap analysis

Each gap: **evidence** (so it's checkable), **why it matters**, **effort**.

### A. Operator can't see or find their own work

| Gap | Evidence | Why it matters | Effort |
|---|---|---|---|
| No reel list endpoint at all | `api/routers/reels.py` has exactly: `POST /`, `GET /{id}`, `GET /{id}/active-job-fragment`. No `GET /`. `index.html` only renders the creation form. | Generate a reel, lose the URL, it's gone. There is no way to browse, search, or resume past work. This is the single most-felt gap in daily use. | **S** — one query + one template |
| No visibility into pipeline cost or health | `StageEvent` captures `latency_ms`, `tokens_in/out`, `cost_usd`, `score`, `ok` per stage — and nothing reads it. `cost_usd` is written nowhere (grep returns only the column definition). No template references `StageEvent`. | You cannot answer "what did this reel cost," "why did generation take 12 minutes," or "is quality trending up or down" without a raw SQL query. Exactly the data needed to run this as an actual operation is captured and then discarded. | **M** |
| No pre-generation cost/time estimate | Nothing in `ui/templates/index.html` or `api/routers/reels.py` estimates cost before submitting. | Standard path can fire 10+ LLM calls per reel (enrich + up to 3 generate attempts × judge + enrichment). An operator commits blind every time. | **S** |

### B. Reliability gaps the mocked test suite structurally cannot see

| Gap | Evidence | Why it matters | Effort |
|---|---|---|---|
| Zero integration tests until today | 146 tests, all mocked sessions or in-memory SQLite except `tests/test_compositor.py` (added today). | Today's live run found a bug that had shipped silently for months: every rendered video had zero audio because `AudioFileClip.audio_fadein()` doesn't exist in MoviePy 2.x, and a bare `except Exception: pass` swallowed the `AttributeError`. No unit test could ever have caught this — it requires a real `AudioFileClip` and a real ffmpeg pass. Fixed in `0e387b5`, but the failure mode (silent degradation, job reports `done`) is structural, not a one-off. | **M** — one golden-reel smoke test in CI closes most of this |
| `asset_sourcer.py` swallows every exception, degrades to black frames | `resolve_beat_assets()` — Pexels, Wikipedia, both HuggingFace sources each wrapped in `try/except: return None`, chained silently. | A Pexels outage produces a black-frame reel that still reports `done`. This is the same failure class as the audio bug — silent success on partial failure — just not yet triggered live. | **S–M** — surface a per-beat "used fallback" flag on `Cut`/`Job.meta` |
| NVIDIA model catalog drift has no guard | Discovered live today: `qwen/qwen3-next-80b-a3b-instruct` (the shipped default for both `NVIDIA_GENERATION_MODEL` and `NVIDIA_ENRICHMENT_MODEL`) returns `410 Gone` — deprecated at NVIDIA's end, confirmed against their live `/v1/models` list. Fixed in `7f77857` (`nvidia/nemotron-3-super-120b-a12b`), but nothing checks model validity at startup. | Any hosted model can be deprecated without notice; the current failure mode is "every generation job fails" with no earlier warning. | **S** — one startup ping to `/v1/models` |
| `llm_enrich()` silently corrupted output on strict-JSON models | Found *while validating the model swap*: `context_enricher.py` called `llm.complete(messages)` with no `json_mode`, defaulting to `True`, even though the prompt wants free prose. Local Ollama models happened not to enforce `response_format` strictly; the new NVIDIA model does, and wrapped the enriched context as `{"text": "..."}` literal JSON — which would have been stored in `reel.enriched_context` and fed into every downstream call. Fixed in `7f77857`. | Same class again: a contract violation invisible until a stricter backend is used. Worth an audit pass over every `llm.complete()` call site for the same mismatch (there are 7 call sites; only this one was wrong, but it was found by accident during unrelated testing, not by design). | **S** |
| MoviePy video-reader fd leak | `ponytail:` comment in `compositor.py:384` — `_build_media_sub_clip` opens `VideoFileClip`s never explicitly closed; only `worker_max_tasks_per_child=10` reclaims them. | Fine today at low volume; will show up as fd exhaustion at any real render throughput. | **M** |

### C. No distribution — the product's stated purpose stops one step short

| Gap | Evidence | Why it matters | Effort |
|---|---|---|---|
| Zero publishing integration | `CutStatus` has `scheduled`/`publishing`/`published` states with no task that ever sets them past `approved`. `credentials` table + Fernet encryption exist; no OAuth flow anywhere. | The tool's own pitch — "turns a topic into a *publishable* video" — ends at a downloadable MP4. Every approved video is manually uploaded today. | **L** |
| TikTok isn't a platform at all | `CutPlatform` enum: `youtube_shorts`, `instagram_reels`. Only. `sportstiktok` appears once, as a hashtag string in a caption template (`generate.py:207`) — not a platform. | For short-form specifically, this is a real gap, not a nice-to-have — TikTok is arguably the primary distribution surface for this content type. | **M** once Phase 4's publish plumbing exists — TikTok's API has stricter content review than YouTube/Instagram |
| `safe_to_publish` is computed and never enforced | Written in `asset_sourcer.py` on every asset (Wikipedia CC-BY-SA images correctly get `False`); never read by `cuts.py`, `render.py`, or any template — grep confirms zero consumers outside the sourcer itself. | This is a licensing-risk gap waiting for Phase 4: right now it's inert data, but the moment a "Publish" button exists, shipping a cut containing a non-`safe_to_publish` asset with no gate is a real legal exposure, not a hypothetical one. | **S** — must land *before or with* Phase 4, not after |

### D. No feedback loop — the product can't learn from what it ships

| Gap | Evidence | Why it matters | Effort |
|---|---|---|---|
| No post-publish metrics | Depends on Phase 4 existing at all; `docs/roadmap.md` Phase 5b is planned, not started. | An automated content pipeline's actual value is a flywheel: which hooks/niches/visual styles perform, feeding back into prompts and the 17-axis scorer's weights. Today the `quality_score` (0–100, rule × 0.4 + LLM judge × 0.6) has never been validated against a single real view/engagement number — it's an internally-consistent proxy with zero ground truth. | **L** |
| Evaluator is tuned for one niche | `evaluator.py:348`: `_football = guide.niche.lower() in {"football","soccer","futbol"}` gates which regex vocabulary (`_INSIGHT_TACTICAL` vs `_INSIGHT_TACTICAL_UNIVERSAL`, `_VO_ACTIONS` vs `_VO_ACTIONS_UNIVERSAL`, `_SPECIFIC_CONTEXT` vs its universal twin) scores Insight Density, Script↔Visual Alignment, and Clip Availability — 45 of 100 points. The "universal" fallback exists, but it was written second and tested less; every fixture in `tests/test_evaluator.py` is football content. | A personal-finance or fitness reel is scored by regexes that were designed against sports vocabulary and only generically adapted. Whether it under- or over-scores non-sports niches is genuinely unknown — nobody has run the numbers. | **M** — needs real non-football test fixtures, not just code changes |

### E. Creative ceiling — the parts that make a video *good*, not just correct

| Gap | Evidence | Why it matters | Effort |
|---|---|---|---|
| No music, ever | `Beat.music_cue` is generated by every guide and read by nothing — `grep -rn "music_cue" engine/render/` returns zero hits outside the schema. `docs/roadmap.md` "not yet built" section confirms: intended FFmpeg `amix`+`agate` sidechain, unimplemented. | Silence (or VO-only) under a short-form video reads as unfinished next to anything from CapCut, Opus, or a human editor. This is probably the single highest perceived-quality gap versus any competitor. | **M** |
| One guide per reel, no hook variants | `generate_guide` produces exactly one `MasterGuide`; best-of-3 is a *retry* mechanism (accept the highest-scoring of 3 attempts if none clears threshold), not variant generation for A/B testing. | Standard short-form practice tests 2–3 hook variants per piece of content; the pipeline has no concept of "generate N candidates, let the operator (or later, real engagement data) pick." | **M** |
| Thumbnail is just frame-at-0.5s | `compositor.py`: `thumb_t = min(0.5, final.duration - 0.05); frame = final.get_frame(thumb_t)`. No candidate frames, no text overlay, no LLM-selected "best moment." | Thumbnail is often the single highest-leverage creative asset on YouTube Shorts/Instagram; this is the least-invested part of the entire pipeline. | **S–M** |
| No brand customization | `compositor.py` `_FONT_CANDIDATES` is a hardcoded fallback list; text is always white-on-black-shadow, no configurable color, logo, or watermark. | Fine for one operator's one channel; blocks any path toward multiple brands/channels using the same tool. | **S–M** |
| Single hardcoded TTS voice, no per-reel choice | `EdgeTTSProvider.DEFAULT_VOICE = "en-GB-RyanNeural"`; nothing in `config.py`, `.env.example`, or the create-reel form exposes a voice choice — `get_tts_provider()` never passes `voice=`. | Every reel sounds identical regardless of niche or tone. Edge TTS has dozens of free neural voices sitting unused. | **S** |
| English-only, structurally | Hook-detection regexes (`_HOOK_ADDRESS_RE`, `_DIRECT_HOOK`, etc.), the evaluator's emotion word-lists, and the TTS voice are all English. | Not a bug — just a hard ceiling on addressable use if this is ever meant for non-English content. | **L** if ever tackled |

### F. Operational maturity — this runs on one laptop today

| Gap | Evidence | Why it matters | Effort |
|---|---|---|---|
| Zero authentication on any endpoint | `api/main.py` + every router — no `Depends()` auth check anywhere, confirmed by grep. | Correct and fine for "local-first, single-operator" (the README says exactly that). Flag it explicitly so it's a decision, not an oversight, before this is ever exposed past `localhost`. | **M** if ever needed |
| No rate limiting, no cost guardrails | No `slowapi`/`ratelimit` dependency anywhere in `pyproject.toml` or code. `quality_threshold` retry loop can already fire 10+ paid LLM calls per reel with no cap. | A stuck loop, a bad prompt, or a malicious actor (once network-exposed) has no ceiling. Directly compounds the "no cost visibility" gap in section A. | **S** |
| No CI | Repo has no `.github/workflows`, no CI config of any kind — only `docker-compose.yml` matches a `*.yml` search. | 146 tests exist and are never run automatically. Today's audio bug would have been caught immediately by a golden-reel smoke test running in CI on every push — it wasn't, because there is no CI. | **S** |
| No Dockerfile for the app itself | `docker-compose.yml` only defines `postgres` and `redis`; the API and workers run from a local venv. | Fine for dev; there is currently no path to deploying this anywhere but the developer's own machine. | **M** |
| `PIXABAY_API_KEY` and `ChatterboxProvider` are dead config | `config.py` defines both; `pixabay_api_key` has zero provider implementation, `TTS_PROVIDER=chatterbox` silently falls back to `SilentProvider` (this exact trap caused the audio-collapse bug fixed in `b7e444c` yesterday). | Config that looks live but does nothing is a standing trap for the next person (including future-you) who sets it expecting it to work. | **S** — remove or implement, don't leave half-wired |

---

## Roadmap

Numbered to extend `docs/roadmap.md`'s existing phase log (0–3.8 done). Sequenced by what unlocks what, not just by the order gaps were found above.

### Phase 4a — Operator visibility (do this before anything else)

*Nothing after this point is safe to build on top of without it.*

- `GET /api/reels` list view — the single most-felt daily gap
- Surface `StageEvent` data: per-reel cost/latency/score trend, at minimum a plain table
- Implement `StageEvent.cost_usd` (pricing lookup per provider — NVIDIA and HF publish per-token/per-call rates)
- Pre-generation cost/time estimate on the create-reel form
- Hard budget cap: reject or warn before a reel would exceed N paid LLM calls

**Why first:** Phase 4b (publishing) and Phase 5 (analytics) both assume you can already see what the pipeline is doing and what it costs. Building distribution before visibility means shipping blind.

### Phase 4b — Publishing (the roadmap's existing "Phase 4," sharpened)

- Enforce `safe_to_publish` as a hard gate in the approve/render flow — **land this with the first publish integration, not after**
- OAuth flow for YouTube Data API + Instagram Graph API (credential table already exists)
- Add TikTok as a third `CutPlatform` — separate workstream, TikTok's content-review requirements are stricter than YouTube/Instagram
- `scheduled` → `publishing` → `published` state machine already exists; wire the actual worker task

### Phase 5 — The feedback loop (the roadmap's existing "Phase 5," reframed around learning, not just reporting)

- Post-publish metrics pull-back (views, retention, engagement) per platform API
- Correlate `quality_score` against real engagement — this is the first time the scorer gets any ground truth at all
- Feed high/low performers back into prompt `prior_feedback` and evaluator axis weights
- Music mixing (`amix` + `agate` sidechain) — promote from "Phase 5c, planned" to same phase as metrics, since it's the highest perceived-quality gap independent of any data flywheel

### Phase 6 — Creative range

- ~~Hook/thumbnail variant generation (N candidates, operator — later, engagement data — picks)~~ ✅ Done — see `docs/roadmap.md` Phase 6a
- ~~Configurable TTS voice per reel (data + UI already 90% there; `voice` param just needs threading through)~~ ✅ Done — see `docs/roadmap.md` Phase 6b
- ~~Non-football niche validation: real test fixtures for 2–3 other niches, measure whether the "universal" evaluator patterns actually score fairly~~ ✅ Done — found and fixed 2 real fairness bugs, see `docs/roadmap.md` Phase 6c
- Brand customization: logo/watermark, ~~configurable text color~~ ✅ Done (see `docs/roadmap.md` Phase 6d), per-channel presets

### Phase 7 — Production hardening

- Golden-reel smoke test in CI (would have caught today's audio bug same-day instead of silently for months)
- CI pipeline: run the 146 tests + the golden-reel test on every push
- Startup validation: ping configured LLM model endpoints, fail fast on a dead model instead of failing every job individually
- Close the `asset_sourcer` silent-degradation gap: surface "used fallback footage" on the `Cut` so a black-frame reel is visible, not silently `done`
- Dockerfile for API + workers; a real deploy path
- Decide auth/rate-limiting scope explicitly — even "we chose not to, because X" is better than the current silent absence

---

## What NOT to do

- **Don't build multi-tenancy or team auth speculatively.** Nothing today needs it; it's real effort with no current user. Revisit only if this stops being single-operator.
- **Don't chase non-English support before Phase 6 validates non-football niches.** Language is a much bigger lift than niche, and niche isn't proven out yet.
- **Don't build a custom analytics dashboard before Phase 4a's plain table exists.** A sortable HTML table of `StageEvent` rows solves 80% of "I can't see what's happening" for a tenth the effort of a real dashboard.
