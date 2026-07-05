"""Thumbnail extraction: 3 candidate frames per clip — sharp (Laplacian
variance), face present (MediaPipe bonus), mid-action (moderate motion) —
spread across the clip, saved as jpg next to the clip."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from errors import ClipForgeError
from logutil import get_logger

log = get_logger("thumbs")


class ThumbnailError(ClipForgeError):
    stage = "thumbnails"


def extract_thumbnails(clip_path: str | Path, count: int = 3,
                       out_dir: str | Path | None = None) -> list[Path]:
    import cv2
    import mediapipe as mp

    clip_path = Path(clip_path)
    if not clip_path.exists():
        raise ThumbnailError(f"clip not found: {clip_path}")
    out_dir = Path(out_dir) if out_dir else clip_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(clip_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(fps * 0.5))          # sample every ~0.5s
    frames, scores = [], []
    prev_small = None
    with mp.solutions.face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=0.5) as fd:
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                small = cv2.resize(frame, (160, 284))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
                motion = 0.0
                if prev_small is not None:
                    motion = float(np.mean(cv2.absdiff(gray, prev_small)))
                prev_small = gray
                det = fd.process(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
                face_bonus = 400.0 if det.detections else 0.0
                # mid-action: moderate motion best (peak at ~8, falls off)
                action = 200.0 * np.exp(-((motion - 8.0) ** 2) / 32.0)
                frames.append((idx, frame.copy()))
                scores.append(sharp + face_bonus + action)
            idx += 1
    cap.release()
    if not frames:
        raise ThumbnailError(f"no frames decoded from {clip_path}")

    order = np.argsort(scores)[::-1]
    picked, min_gap = [], max(1, len(frames) // (count + 1))
    for i in order:
        if all(abs(int(i) - int(j)) >= min_gap for j in picked):
            picked.append(int(i))
        if len(picked) == count:
            break
    for i in order:  # fill up if spacing constraint left gaps
        if len(picked) == count:
            break
        if int(i) not in picked:
            picked.append(int(i))

    paths = []
    for rank, i in enumerate(sorted(picked), 1):
        p = out_dir / f"{clip_path.stem}_thumb{rank}.jpg"
        cv2.imwrite(str(p), frames[i][1], [cv2.IMWRITE_JPEG_QUALITY, 90])
        paths.append(p)
    log.info("thumbnails: %d written for %s", len(paths), clip_path.name)
    return paths


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: extract thumbnails")
    ap.add_argument("clip")
    a = ap.parse_args()
    for p in extract_thumbnails(a.clip):
        print(p)
