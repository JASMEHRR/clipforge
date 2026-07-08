# ClipForge Phase R — Research (overnight run 2026-07-09)

Time-boxed (~45 min). Goal: ground the overnight feature set (esp. virality v2 and the new
user options) in what leading clip tools ship and what published retention guidance says.

## Landscape — leading clip tools

| Tool | Core value | Notable features relevant to us |
|------|-----------|--------------------------------|
| Opus Clip | Long→many clip finder; ranks clips with a 0–99 **virality score** | Auto captions (97%+ acc, animated, 20+ langs), contextual B-roll, virality ranking. Reviews **flag the score as unreliable** (a 40 sometimes beats an 85). |
| Submagic | Make one clip look great | Word-level captions, **emoji triggers**, keyword sound effects, deep typography control, B-roll. No virality score. |
| Klap | Long→shorts | Auto reframe, captions, templates. |
| Vizard / AutoShorts | Clip finding + scheduling | Auto captions, templates, hooks, posting. |

Key takeaway for us: our differentiators are already the style-refiner (hook/ending/pacing
decisions) and keyless local operation. The gaps vs the market are **caption polish (emoji,
keyword color), CTA/branding controls, music, and an honest virality signal**. That directly
shapes Feature 5's SELECTED list and Feature 4's honesty framing.

Sources:
- https://www.submagic.co/blog/opus-clip-vs-submagic
- https://www.opus.pro/tools/opusclip-captions
- https://www.ngram.com/blog/opus-clip-vs-submagic
- https://reap.video/reports/state-of-top-ai-video-clipping-tools-2026

## Retention guidance (published)

- **Hook / first 3s**: ~71% of viewers decide in the first few seconds; average attention
  span ~8.2s. Hook content in the opening is the single largest retention lever.
- **Duration sweet spot**: videos <90s hold ~50% retention; **50–60s earn the most views**
  (avg ~4.1M). Sub-10s Shorts underperform badly (~19k avg). So the sweet spot is a broad
  plateau roughly **30–60s**, penalise <15s and >75s.
- **Watch-rate distribution**: 59% of shorts are watched 41–80% through; 30% exceed 81%.
- **Captions**: 85% watch without sound; bold captions + visual storytelling lift completion
  ~40%. Word-level / short lines (readable in a glance) matter — keep lines short.
- **Pacing**: frequent cuts sustain attention; the refiner's pause-trimming already targets
  this — score pacing relative to the active StyleProfile rather than an absolute.

Sources:
- https://virvid.ai/blog/ai-shorts-increase-retention-watch-time
- https://metricool.com/social-media-short-video-report-2025/
- https://autofaceless.ai/blog/short-form-video-statistics-2026
- https://driveeditor.com/blog/trends-short-form-video-hooks

## Virality v2 — sub-scores, weights, rationale

Presented as **engagement signals**, never a guarantee (Opus Clip's own score is publicly
unreliable — we make ours explainable with a visible breakdown). Each sub-score 0–10, from
data the pipeline already has. Weights reflect the retention evidence above (hook dominates).

| Sub-score | Weight | Signal (already available) | Rationale |
|-----------|-------:|----------------------------|-----------|
| Hook | 0.28 | first-3s word content + existing hook classifier + `weak_hook` flag | Largest retention lever (71% decide early). |
| Completeness | 0.18 | refiner `self_contained` + ending `complete` (reused, not recomputed) | Payoff/closure drives full watches & shares. |
| Pacing | 0.16 | surviving pause profile + cuts/min vs active StyleProfile | Cuts sustain attention; relative to profile. |
| Captions | 0.14 | words-per-line readability, coverage, emphasis present | 85% watch muted; readability = completion. |
| Duration | 0.14 | distance from 30–60s sweet spot | Strong empirical view/retention curve. |
| Delivery | 0.10 | audio energy variance from the wav | Dynamic delivery correlates with attention. |

Total = round(sum(weight*sub)*10) → 0–100. Bands: **Strong ≥ 70, Promising ≥ 45, Weak < 45**
(mirrors the retention watch-rate tiers). When a real LLM key is present, the existing
`clip_score` rubric is folded in as one extra averaged signal; under mock the heuristics carry
it so the keyless gate stays green. Keep/drop logic is untouched (it uses `rescore_clips`, not
virality) — virality remains display + sort only.

## SELECTED — user options to build (Feature 5)

1. **Custom CTA text** — expose existing `style.cta.text` per run. Effort: XS. High value, config already exists.
2. **Emoji captions toggle** — rule-based keyword→emoji map appended per caption chunk (LLM-enhanced w/ key). Effort: S. Matches Submagic's signature feature.
3. **Keyword highlight color** — feeds caption preset emphasis color. Effort: S. Direct caption-polish parity.
4. **Watermark / brand text overlay** — config'd position, off by default. Effort: S. Creator branding, common request.
5. **Background music toggle + volume** — `music.py` exists; add `music:` config + per-run UI. Effort: S. Already half-wired in Create tab.
6. **Pacing aggressiveness slider** — maps to `style.max_pause_s`/`target_pause_s` within safe bounds. Effort: S. Leverages refiner without changing its decisions.
7. **Clip count / length preference** — map to existing config bounds. Effort: XS. Already partly present.

## REJECTED — considered, skipped

- **AI B-roll insertion** — needs stock/generation services + heavy deps. Out of scope (keyless, no new deps).
- **Keyword sound effects** — needs an SFX asset library + timing model; low ROI tonight.
- **Auto-posting / scheduling** — external platform APIs + auth; out of overnight scope.
- **Multi-language caption translation** — needs translation service/key; keyless constraint.
- **AI-generated voiceover / dubbing** — heavy deps, external services.
- **Template marketplace / preset sharing** — infra-heavy, low value for a local tool.
