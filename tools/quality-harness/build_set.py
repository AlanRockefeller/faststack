#!/usr/bin/env python3
"""Build the blind image-quality comparison set for FastStack.

Renders every candidate change at the exact geometry FastStack puts on screen,
crops both versions identically, verifies they really are comparable, and emits
`data.json` + `img/` for the blind-comparison page.

    python tools/quality-harness/build_set.py \
        --photos "/path/to/finished/photos" \
        --camera "/path/to/camera/JPGs" \
        --out    ./quality-set

Then publish `out/index.html` (copied from page_template.html) with the files in
`out/`.  See README.md for how to add a comparison.

Two invariants are enforced, because both were violated the first time:
  1. The two versions of a pair must be the same pixel size.
  2. They must show the same field of view.  Equal size does NOT imply this --
     a frame rendered small and never scaled up to the display size is a zoom
     difference wearing the same dimensions.  Checked by cross-correlating over
     candidate scales; the best match must be exactly 1.0.
"""

from __future__ import annotations

import argparse
import collections
import glob
import io
import json
import os
import re
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from faststack.imaging.exif_fast import read_orientation  # noqa: E402
from faststack.imaging.orientation import apply_orientation_to_np  # noqa: E402

try:
    from turbojpeg import TJPF_RGB, TurboJPEG

    _TJ = TurboJPEG(os.environ.get("FASTSTACK_TURBOJPEG_LIB") or None)
except Exception as exc:  # pragma: no cover - environment dependent
    raise SystemExit(f"TurboJPEG is required: {exc}")

TILE = 800
SCALING_FACTORS = sorted(
    [(1, 8), (1, 4), (3, 8), (1, 2), (5, 8), (3, 4), (7, 8), (1, 1)],
    key=lambda f: f[0] / f[1],
)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
def fit_down(w, h, bw, bh):
    """Size a decode/buffer into a box. Never upscales -- decoding more pixels
    than the source has is meaningless."""
    s = min(bw / w, bh / h, 1.0)
    return max(1, round(w * s)), max(1, round(h * s))


def fit_screen(w, h, bw, bh):
    """Size something for DISPLAY in a box. May upscale, because that is what a
    fit-to-window viewer does to a small navigation frame. Using fit_down here
    is the bug that put three magnifications into one comparison."""
    s = min(bw / w, bh / h)
    return max(1, round(w * s)), max(1, round(h * s))


def covering_factor(constraining_dim, target):
    for num, den in SCALING_FACTORS:
        if constraining_dim * num / den >= target:
            return (num, den)
    return (1, 1)


def decode_at(data, box_w, box_h, transpose_for_orientation=False, orientation=1):
    """Decode as FastStack's display path would, for a given target box."""
    w, h, _, _ = _TJ.decode_header(data)
    if transpose_for_orientation and orientation in (5, 6, 7, 8):
        box_w, box_h = box_h, box_w
    if w * box_h > h * box_w:
        target, constraining = box_w, w
    else:
        target, constraining = box_h, h
    dec = _TJ.decode(
        data,
        scaling_factor=covering_factor(constraining, target),
        pixel_format=TJPF_RGB,
    )
    return dec, (box_w, box_h)


# --------------------------------------------------------------------------
# crop selection
# --------------------------------------------------------------------------
def score_tiles(paths):
    rows = []
    for path in paths:
        try:
            data = Path(path).read_bytes()
            arr = _TJ.decode(data, pixel_format=TJPF_RGB)
        except Exception:
            continue
        orientation = read_orientation(data[:65536])
        if orientation > 1:
            arr = np.ascontiguousarray(apply_orientation_to_np(arr, orientation))
        height, width = arr.shape[:2]
        if height < TILE or width < TILE:
            continue
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY).astype(np.float32)
        for y in range(0, height - TILE + 1, TILE):
            for x in range(0, width - TILE + 1, TILE):
                tile = gray[y : y + TILE, x : x + TILE]
                crop = arr[y : y + TILE, x : x + TILE]
                nyq = float(np.mean(np.abs(tile - cv2.GaussianBlur(tile, (0, 0), 0.7))))
                gx = cv2.Sobel(tile, cv2.CV_32F, 1, 0, ksize=3)
                gy = cv2.Sobel(tile, cv2.CV_32F, 0, 1, ksize=3)
                mag = np.hypot(gx, gy)
                ang = np.arctan2(gy, gx)[mag > 60]
                hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
                rows.append(
                    dict(
                        path=str(path),
                        x=int(x),
                        y=int(y),
                        nyq=nyq,
                        sd=float(tile.std()),
                        mean=float(tile.mean()),
                        diag=(
                            float(np.mean(np.abs(np.cos(2 * ang)) < 0.55))
                            if ang.size > 500
                            else 0.0
                        ),
                        sat=float(hsv[:, :, 1].mean()),
                        dark=float((tile < 45).mean()),
                        bright=float((tile > 228).mean()),
                    )
                )
    return rows


# A crop can only expose a resampling difference if it has BOTH fine structure
# (near-Nyquist energy) and enough contrast to see it.  Scoring on detail alone
# selects out-of-focus bokeh and near-black backgrounds -- about a third of the
# first set was unusable for exactly this reason.
USABLE = lambda t: t["nyq"] >= 2.5 and t["sd"] >= 15.0  # noqa: E731

CATEGORIES = {
    "gills_fine_detail": lambda t: t["nyq"],
    "diagonal_edges": lambda t: t["diag"] * t["nyq"],
    "hf_texture": lambda t: t["nyq"] * min(t["sd"], 60) / 60,
    "foliage_saturated": lambda t: t["sat"] * min(t["nyq"], 9),
    "shadow_detail": lambda t: t["dark"] * t["nyq"] if t["dark"] > 0.3 else 0.0,
    "highlight_rolloff": lambda t: t["bright"] * min(t["nyq"], 8),
}


def choose_scenes(rows, per_category=2):
    chosen, used_images = {}, set()
    for name, score in CATEGORIES.items():
        pool = [t for t in rows if USABLE(t)]
        pool.sort(key=lambda t: -score(t))
        picks = []
        for tile in pool:
            if score(tile) <= 0 or tile["path"] in used_images:
                continue
            used_images.add(tile["path"])
            picks.append(tile)
            if len(picks) >= per_category:
                break
        if picks:
            chosen[name] = picks
    return chosen


# --------------------------------------------------------------------------
# comparisons -- add one by adding an entry here
# --------------------------------------------------------------------------
def _nav(cap):
    def render(ctx):
        bw, bh = min(ctx.vp[0], cap), min(ctx.vp[1], cap)
        dec, _ = decode_at(ctx.data, bw, bh)
        dw, dh = fit_down(dec.shape[1], dec.shape[0], bw, bh)
        out = (
            cv2.resize(dec, (dw, dh), interpolation=cv2.INTER_LINEAR)
            if (dw, dh) != (dec.shape[1], dec.shape[0])
            else dec
        )
        out = ctx.orient(out)
        return cv2.resize(out, ctx.screen, interpolation=cv2.INTER_LINEAR)

    return render


def _settled(interp, transpose=True):
    def render(ctx):
        dec, (bw, bh) = decode_at(ctx.data, *ctx.vp, transpose, ctx.orientation)
        dw, dh = fit_down(dec.shape[1], dec.shape[0], bw, bh)
        out = cv2.resize(dec, (dw, dh), interpolation=interp)
        out = ctx.orient(out)
        return cv2.resize(out, ctx.screen, interpolation=cv2.INTER_LINEAR)

    return render


def _thumb_full(resampler):
    def render(ctx):
        w, h, _, _ = _TJ.decode_header(ctx.data)
        sf = 1
        while w // (sf * 2) >= ctx.thumb and h // (sf * 2) >= ctx.thumb and sf < 8:
            sf *= 2
        small = ctx.orient(
            _TJ.decode(ctx.data, scaling_factor=(1, sf), pixel_format=TJPF_RGB)
        )
        tw, th = fit_screen(
            *fit_down(small.shape[1], small.shape[0], ctx.thumb, ctx.thumb),
            ctx.thumb,
            ctx.thumb,
        )
        if resampler == "pil":
            return np.asarray(
                Image.fromarray(small).resize((tw, th), Image.Resampling.LANCZOS)
            )
        return cv2.resize(small, (tw, th), interpolation=cv2.INTER_AREA)

    return render


def _thumb_embedded(ctx):
    head = ctx.data[:160000]
    best = None
    for m in re.finditer(b"\xff\xd8\xff", head[4:]):
        start = m.start() + 4
        end = head.find(b"\xff\xd9", start)
        if end < 0:
            continue
        try:
            im = Image.open(io.BytesIO(head[start : end + 2]))
            im.load()
            if best is None or im.size[0] > best.size[0]:
                best = im
        except Exception:
            pass
    if best is None:
        return None
    emb = ctx.orient(np.asarray(best.convert("RGB")))
    tw, th = fit_screen(
        *fit_down(emb.shape[1], emb.shape[0], ctx.thumb, ctx.thumb),
        ctx.thumb,
        ctx.thumb,
    )
    return cv2.resize(emb, (tw, th), interpolation=cv2.INTER_LINEAR)


COMPARISONS = {
    "resize_kernel": dict(
        title="Settled downscale filter",
        cond="settled_fit",
        crop=True,
        a=("INTER_AREA (what FastStack does now)", _settled(cv2.INTER_AREA)),
        b=("INTER_LANCZOS4", _settled(cv2.INTER_LANCZOS4)),
        win="B costs ~1.7x A",
    ),
    "nav_tier_down": dict(
        title="Rapid-nav tier: lower",
        cond="rapid_nav",
        crop=True,
        a=("1600 px - balanced (current)", _nav(1600)),
        b=("800 px - performance", _nav(800)),
        win="B decodes ~2x cheaper",
    ),
    "nav_tier_up": dict(
        title="Rapid-nav tier: higher",
        cond="rapid_nav",
        crop=True,
        a=("1600 px - balanced (current)", _nav(1600)),
        b=("2400 px - detailed", _nav(2400)),
        win="B decodes ~1.3x dearer",
    ),
    "orient_box": dict(
        title="Orientation-aware decode box",
        cond="settled_fit",
        crop=True,
        rotated_only=True,
        a=(
            "Oversized buffer, GPU downscales (current)",
            _settled(cv2.INTER_AREA, False),
        ),
        b=("Exact-size buffer, CPU downscales", _settled(cv2.INTER_AREA, True)),
        win="B uses 44% less memory per frame",
    ),
    "thumb_kernel": dict(
        title="Thumbnail filter",
        cond="thumbnail_200",
        crop=False,
        a=("Pillow LANCZOS (current)", _thumb_full("pil")),
        b=("OpenCV INTER_AREA", _thumb_full("cv2")),
        win="B is ~3x faster per thumbnail",
    ),
    "thumb_embedded": dict(
        title="Thumbnail from embedded EXIF",
        cond="thumbnail_200",
        crop=False,
        a=("Full decode (current)", _thumb_full("pil")),
        b=("Embedded EXIF thumbnail", _thumb_embedded),
        win="B is 186x faster: 0.4 ms vs 82 ms",
    ),
}

CONDITIONS = {
    "settled_fit": "Settled fit-to-window, shown 1:1",
    "rapid_nav": "Held-arrow rapid navigation frame, upscaled to screen",
    "thumbnail_200": "Grid thumbnail at 200 px, shown 1:1",
}


class Ctx:
    def __init__(self, path, vp, thumb):
        self.path = path
        self.data = Path(path).read_bytes()
        self.vp = vp
        self.thumb = thumb
        self.orientation = read_orientation(self.data[:65536])
        w, h, _, _ = _TJ.decode_header(self.data)
        ow, oh = (h, w) if self.orientation in (5, 6, 7, 8) else (w, h)
        self.screen = fit_screen(ow, oh, *vp)

    def orient(self, arr):
        if self.orientation > 1:
            return np.ascontiguousarray(apply_orientation_to_np(arr, self.orientation))
        return arr


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
def same_field_of_view(a, b):
    """True only if b aligns to a best at scale 1.0."""
    ga = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY).astype(np.float32)
    best = (None, -9.0)
    for s in (0.5, 0.66, 0.8, 0.9, 1.0, 1.11, 1.25, 1.5, 2.0):
        h, w = ga.shape
        rb = cv2.resize(
            gb, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA
        )
        hh, ww = min(ga.shape[0], rb.shape[0]), min(ga.shape[1], rb.shape[1])
        x = ga[:hh, :ww] - ga[:hh, :ww].mean()
        y = rb[:hh, :ww] - rb[:hh, :ww].mean()
        denom = np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9
        r = float((x * y).sum() / denom)
        if r > best[1]:
            best = (s, r)
    return best[0] == 1.0, best


def metrics(a, b):
    fa, fb = a.astype(np.float32), b.astype(np.float32)
    diff = np.abs(fa - fb)
    mse = float((diff**2).mean())

    def ssim(x, y):
        x = cv2.cvtColor(x, cv2.COLOR_RGB2GRAY).astype(np.float32)
        y = cv2.cvtColor(y, cv2.COLOR_RGB2GRAY).astype(np.float32)
        c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
        m1, m2 = cv2.GaussianBlur(x, (11, 11), 1.5), cv2.GaussianBlur(y, (11, 11), 1.5)
        s11 = cv2.GaussianBlur(x * x, (11, 11), 1.5) - m1 * m1
        s22 = cv2.GaussianBlur(y * y, (11, 11), 1.5) - m2 * m2
        s12 = cv2.GaussianBlur(x * y, (11, 11), 1.5) - m1 * m2
        num = (2 * m1 * m2 + c1) * (2 * s12 + c2)
        den = (m1**2 + m2**2 + c1) * (s11 + s22 + c2)
        return float((num / den).mean())

    return dict(
        max_abs_diff=float(diff.max()),
        mean_abs_diff=round(float(diff.mean()), 4),
        psnr_db=round(float(10 * np.log10(255**2 / mse)), 2) if mse > 0 else None,
        ssim=round(ssim(a, b), 6),
        pct_pixels_differing=round(float((diff.max(axis=2) > 0).mean() * 100), 2),
    )


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", required=True, help="folder of FINISHED photographs")
    ap.add_argument("--camera", help="folder of camera JPEGs (for EXIF-rotated scenes)")
    ap.add_argument("--out", default="quality-set")
    ap.add_argument("--viewport", default="3000x1700")
    ap.add_argument("--crop", type=int, default=592)
    ap.add_argument("--thumb", type=int, default=200)
    ap.add_argument("--per-category", type=int, default=2)
    ap.add_argument("--stride", type=int, default=20, help="sample every Nth photo")
    args = ap.parse_args()

    vp = tuple(int(v) for v in args.viewport.lower().split("x"))
    out = Path(args.out)
    (out / "img").mkdir(parents=True, exist_ok=True)

    photos = sorted(
        p
        for ext in ("jpg", "JPG", "jpeg")
        for p in glob.glob(str(Path(args.photos) / f"*.{ext}"))
    )[:: args.stride]
    print(f"scoring {len(photos)} photographs ...")
    scenes = choose_scenes(score_tiles(photos), args.per_category)
    if not scenes:
        raise SystemExit("no usable crops found -- try a lower --stride")

    # EXIF-rotated scenes come from camera JPEGs, which are focus-stack frames:
    # most of each frame is out of focus, so take the sharpest tile rather than
    # the centre, exactly as for the finished photographs.
    rotated = []
    if args.camera:
        cam = [
            p
            for p in sorted(glob.glob(str(Path(args.camera) / "*.JPG")))[
                :: max(1, args.stride)
            ]
            if read_orientation(Path(p).read_bytes()[:65536]) in (6, 8)
        ][: args.per_category * 6]
        # Focus-stack frames are mostly out of focus, so the finished-photo
        # usability bar rejects all of them. Take the sharpest tiles available
        # and report what they scored, rather than dropping the comparison.
        cand = score_tiles(cam)
        cand.sort(key=lambda t: -(t["nyq"] * min(t["sd"], 40)))
        seen = set()
        for t in cand:
            if t["path"] in seen:
                continue
            seen.add(t["path"])
            rotated.append(t)
            if len(rotated) >= args.per_category * 2:
                break
        if rotated:
            worst = min(rotated, key=lambda t: t["nyq"])
            print(
                f"  EXIF-rotated scenes: {len(rotated)} "
                f"(sharpest tile detail {max(t['nyq'] for t in rotated):.1f}, "
                f"weakest {worst['nyq']:.1f}"
                + ("; camera frames are soft by nature" if worst["nyq"] < 2.5 else "")
                + ")"
            )

    def crop_at(arr, fx, fy, n):
        h, w = arr.shape[:2]
        x = int(np.clip(fx * w - n // 2, 0, max(0, w - n)))
        y = int(np.clip(fy * h - n // 2, 0, max(0, h - n)))
        return np.ascontiguousarray(arr[y : y + n, x : x + n])

    def save(name, arr):
        Image.fromarray(np.ascontiguousarray(arr)).save(
            out / "img" / name, "WEBP", lossless=True, quality=100, method=4
        )
        return name

    trials, refs, failures = [], {}, []
    jobs = [
        (f"{cat}_{i}", t["path"], t)
        for cat, ts in scenes.items()
        for i, t in enumerate(ts)
    ]
    jobs += [(f"camera_rotated_{i}", t["path"], t) for i, t in enumerate(rotated)]

    for scene, path, tile in jobs:
        ctx = Ctx(path, vp, args.thumb)
        full = ctx.orient(_TJ.decode(ctx.data, pixel_format=TJPF_RGB))
        if tile is not None:
            fx = (tile["x"] + TILE / 2) / full.shape[1]
            fy = (tile["y"] + TILE / 2) / full.shape[0]
        else:
            fx = fy = 0.5
        refs[scene] = save(f"{scene}__reference.webp", crop_at(full, fx, fy, args.crop))

        for key, spec in COMPARISONS.items():
            if spec.get("rotated_only") and ctx.orientation not in (5, 6, 7, 8):
                continue
            if not spec.get("rotated_only") and scene.startswith("camera_rotated"):
                continue
            try:
                a = spec["a"][1](ctx)
                b = spec["b"][1](ctx)
            except Exception as exc:
                failures.append(f"{scene}/{key}: {exc}")
                continue
            if a is None or b is None:
                continue
            if spec["crop"]:
                a, b = crop_at(a, fx, fy, args.crop), crop_at(b, fx, fy, args.crop)
            if a.shape != b.shape:
                failures.append(f"{scene}/{key}: size {a.shape} vs {b.shape}")
                continue
            ok, best = same_field_of_view(a, b)
            if not ok:
                failures.append(
                    f"{scene}/{key}: different field of view "
                    f"(best alignment at scale {best[0]}, r={best[1]:.3f})"
                )
                continue
            trials.append(
                dict(
                    id=f"{scene}_{key}",
                    cmp=key,
                    cond=spec["cond"],
                    cat=scene.rsplit("_", 1)[0],
                    scene=scene,
                    a=save(f"{scene}__{key}__A.webp", a),
                    b=save(f"{scene}__{key}__B.webp", b),
                    # Thumbnail variants keep their aspect ratio, so they are
                    # not the square crop size. The page frames each trial at
                    # its own size or the comparison stops being pixel-exact.
                    w=int(a.shape[1]),
                    h=int(a.shape[0]),
                    m=metrics(a, b),
                )
            )
        print(f"  {scene}")

    data = dict(
        crop=args.crop,
        viewport=list(vp),
        conditions=CONDITIONS,
        comparisons={
            k: dict(title=v["title"], a=v["a"][0], b=v["b"][0], win=v["win"])
            for k, v in COMPARISONS.items()
        },
        trials=trials,
        refs=refs,
    )
    (out / "data.json").write_text(json.dumps(data, separators=(",", ":")))

    total = sum(f.stat().st_size for f in (out / "img").glob("*.webp"))
    print(
        f"\n{len(trials)} trials, {len(list((out/'img').glob('*.webp')))} images, "
        f"{total/1e6:.1f} MB"
    )
    by_cmp = collections.Counter(t["cmp"] for t in trials)
    for k, n in by_cmp.items():
        print(f"  {k:18s} {n} pairs")
    if failures:
        print(f"\n{len(failures)} pair(s) REJECTED by the invariant checks:")
        for f in failures:
            print("  ", f)
    if total > 60e6:
        print(
            "\nWARNING: over the 64 MB artifact budget -- lower --crop or --per-category"
        )


if __name__ == "__main__":
    main()
