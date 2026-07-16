# Avatar driving video

`avatar.animation` (LivePortrait) needs a short (~8-10s) video of a person
talking/moving naturally to drive the animation — motion transfer, not
generation. It supplies head motion and mouth movement; it does NOT need to
match the TTS words (LivePortrait is not audio-driven).

Place a royalty-free clip here as `talking_loop.mp4` (or point
`avatar.animation.driving_video` at another path). Requirements:
- A single clearly visible face, front-facing, natural head movement and
  speech-like mouth motion.
- Public-domain or a license that permits redistribution/derivative use
  (e.g. Pexels/Pixabay license, CC0). Record your own if in doubt.
- Short and loopable — it gets `-stream_loop`'d and trimmed to fit each
  segment's TTS duration at render time, so a jump cut at the loop point is
  fine.

Not bundled in this repo from an external source: it's binary media that
needs a verified license, so it isn't something to auto-generate or fetch
sight-unseen.

`talking_loop.mp4` currently present here is a **temporary development/
verification default**: a copy of LivePortrait's own bundled example clip
(`cache/liveportrait/assets/examples/driving/d0.mp4`), which ships under
LivePortrait's own license as a demo asset. It's fine for local testing —
`avatar_anim.py` detects it (by content hash, regardless of filename) and
logs a warning on every run reminding you to replace it. Swap in your own
driving footage before a real/production run.
