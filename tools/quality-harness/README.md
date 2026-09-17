# Blind image-quality harness

For deciding whether a change to FastStack's display pipeline is *visible*,
rather than whether it is measurable. Built 2026-09 when the imaging campaign
needed to know which speedups were perceptually free.

Two pieces:

- `build_set.py` — renders every candidate change at the exact geometry
  FastStack puts on screen, crops both versions identically, verifies they are
  genuinely comparable, and emits `data.json` + `img/`.
- `page_template.html` — the blind comparison page. Copy it into the output
  folder as `index.html` and publish the folder as a Claude artifact.

## Running it

```bash
FASTSTACK_TURBOJPEG_LIB=/path/to/libturbojpeg.so \
python tools/quality-harness/build_set.py \
    --photos "$HOME/Pictures/0916-proccessed" \
    --camera "$HOME/Pictures/olympus.stack.input.photos/2026/2026-09-05" \
    --out    ./quality-set
```

`--photos` must be **finished** photographs — sharp, in focus, processed.
`--camera` is optional and only feeds the EXIF-rotated scenes, which need
camera JPEGs because nothing else carries an orientation tag.

Useful flags: `--per-category` (scenes per failure mode), `--stride` (sample
every Nth photo; lower is slower but finds better crops), `--crop`, `--viewport`,
`--thumb`. Keep the total under ~60 MB or the artifact will not publish.

## Adding a comparison

Add one entry to `COMPARISONS`. Each side is `(label, render_fn)` where
`render_fn(ctx)` returns an RGB array **at the size the screen would show it**.
`ctx` gives you `.data` (JPEG bytes), `.orientation`, `.orient(arr)`,
`.vp` (viewport), `.screen` (on-screen geometry for this image) and `.thumb`.

```python
"my_change": dict(
    title="What the tester sees as the row name",
    cond="settled_fit",          # which viewing condition
    crop=True,                   # crop to --crop, or False for whole thumbnails
    a=("current behaviour", render_current),
    b=("candidate", render_candidate),
    win="what B buys if it is indistinguishable",
),
```

Put the current behaviour in `a`. The page reshuffles which side appears as
image 1, and reports results in terms of A and B, so a consistent convention is
what makes the output readable.

## The two invariants, and why they are checked

Both of these were violated the first time and both produced confident,
meaningless results.

1. **Same pixel size.** Obvious, and not sufficient on its own.
2. **Same field of view.** A frame rendered small and never scaled up to the
   display size is a *zoom* difference wearing identical dimensions. The first
   run compared 800 px, 1600 px and 2400 px navigation frames at three
   magnifications and the tester correctly said the images "look much
   different". `same_field_of_view()` cross-correlates over candidate scales;
   the best alignment must be exactly 1.0, or the pair is rejected and named.

The structural defence matters more than the check: on-screen geometry is
computed **once per scene** in `Ctx.screen`, and every renderer resizes to it.
Never size a display step with `fit_down` — that clamps at 1:1, which is right
for choosing a decode size and wrong for a viewer that upscales.

## Choosing crops

`USABLE` requires **both** near-Nyquist detail and contrast. Scoring on detail
alone selects out-of-focus bokeh and near-black backgrounds: about a third of
the first set could not have shown a resampling difference at all, and the
tester reported one crop as "just a grey square".

EXIF-rotated scenes are exempt, because focus-stack frames are soft by nature
and there is no other source of rotated files. The tool prints what they scored.

## Reading the results

The page records, per trial, the preference, how clear the difference was, the
peak magnification used, and which algorithm was on screen as image 1. That
last field exists because the first run's free-text notes ("1 is sharper") could
not be mapped back to an algorithm afterwards.

Weigh **unmagnified** trials. A difference only resolvable at 3x or 4x is one
FastStack's display never shows. In the 2026-09 run, 74% of locked-1x trials
were called "the same".
