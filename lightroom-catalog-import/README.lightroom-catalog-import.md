# Lightroom Classic to FastStack Migration Tools

This directory contains a small set of Python scripts that were developed to answer one practical question:

> Can data from an Adobe Lightroom Classic catalog (`.lrcat`) be used to mark files as uploaded in FastStack?

When I was using Lightroom, I marked all of the files that I uploaded as green - this tool successfully migrated this flag to Faststack.

In this case, the Lightroom workflow was:

- images that had been uploaded were marked **Green** in Lightroom Classic
- FastStack stores upload state in a per-directory `faststack.json`
- the goal was to carry that historical Lightroom information into FastStack

These scripts were created on **April 1, 2026** while working with a real Lightroom Classic catalog from that date. They worked for that catalog and are **likely to work on various modern Lightroom Classic catalogs**, but there are **no guarantees**. Adobe does not publish a stable public schema for every internal Lightroom table, and catalog structure may vary across versions.

Use these tools carefully and keep backups. You should be able to use these tools to extract any data from your Lightroom catalog for use in your work.

---

## Overview of the four scripts

There are four scripts:

1. `lrcat_diff.py`
2. `inspect_lrcat_photo.py`
3. `test_lrcat_join.py`
4. `green2faststack.py`

They were written in that order of discovery:

- first, identify which Lightroom fields change when a photo is marked Green
- second, inspect one known photo and discover how it connects to file-path tables
- third, test the exact join needed to reconstruct file paths
- fourth, build a practical export-and-import tool for FastStack that can update a `faststack.json` by file path or directory path and create it if needed

The first three scripts are best understood as **reverse-engineering helpers**.
The fourth script, `green2faststack.py`, is the **main end-user tool**.

---

# Thought process and discovery path

## Problem statement

Lightroom Classic stores a great deal of metadata in a SQLite catalog file ending in `.lrcat`.
FastStack stores per-directory metadata in a JSON file named `faststack.json`.

To migrate data from Lightroom to FastStack, we needed to answer two questions:

1. Where does Lightroom store the fact that an image was marked Green?
2. How do we reconstruct the full file path for each green-labeled image?

## Step 1: prove where the Green label lives

The first experiment was:

1. close Lightroom
2. back up the catalog
3. open Lightroom
4. mark exactly one known image Green
5. close Lightroom
6. diff the before/after catalogs

That led to the key observation that in the tested catalog, the relevant field was:

- `Adobe_images.colorLabels = 'Green'`

## Step 2: prove how to recover the file path

Once one known green-labeled image was identified, the next task was to inspect the row and the surrounding tables to learn how Lightroom connects image metadata to filenames and folders.

The tested catalog showed a join chain that worked:

- `Adobe_images.rootFile -> AgLibraryFile.id_local`
- `AgLibraryFile.folder -> AgLibraryFolder.id_local`
- `AgLibraryFolder.rootFolder -> AgLibraryRootFolder.id_local`

Using that chain, plus the filename stem and extension, the scripts were able to reconstruct the full Lightroom path for each green-labeled file.

## Step 3: use the recovered paths to update FastStack

FastStack stores metadata in `faststack.json`, with image entries keyed by lowercase filename stem, for example:

- `P3270037.JPG` becomes `p3270037`

That means once a Lightroom-exported path is known, it can be converted into a lowercase stem and matched against FastStack JSON entries, or added as a new entry when needed.

This is helpful for RAW/JPG pairs too. If Lightroom recorded a RAW file like `foo.ORF` and FastStack has an entry keyed as `foo`, the stem still matches.

---

# Script-by-script documentation

## 1. `lrcat_diff.py`

### Purpose

`lrcat_diff.py` compares two Lightroom Classic catalogs and reports changed rows and columns.

It exists to answer questions like:

- What changed in the catalog when I marked a photo Green?
- Which tables are relevant to color labels?
- Did Lightroom store text, numeric state, or something more complex?

### Why this script matters

Without it, you are guessing.
With it, you can make a controlled edit in Lightroom and inspect what really changed.

This is the best starting point when adapting the workflow to a new Lightroom version.

### Typical usage

```bash
/usr/bin/python3 lrcat_diff.py before.lrcat after.lrcat --match "IMG_1234"
```

You can also run without `--match` to see all changed rows.

### What it helped discover

On the tested Lightroom catalog, this script showed that marking one image Green changed:

- `Adobe_images.colorLabels: '' -> 'Green'`

That was the breakthrough that made the rest of the process possible.

### Caveats

- It reads a lot of data and may be memory-heavy on very large catalogs.
- It is a discovery tool, not a migration tool.
- It works best when you make **one controlled Lightroom change at a time**.

---

## 2. `inspect_lrcat_photo.py`

### Purpose

`inspect_lrcat_photo.py` takes a catalog and an `Adobe_images.id_local` value and prints:

- the `Adobe_images` row
- rows from other tables that appear to reference the same image
- tables that contain likely path-related columns

### Why this script matters

Once `lrcat_diff.py` shows that a particular image row changed, the next task is figuring out how that image connects to file-path tables. This script helps explore those relationships.

### Typical usage

```bash
/usr/bin/python3 inspect_lrcat_photo.py catalog.lrcat 32638618
```

### What it helped discover

It showed that the image row had a `rootFile` field and that the catalog contained promising path-related tables such as:

- `AgLibraryFile`
- `AgLibraryFolder`
- `AgLibraryRootFolder`

That gave the next script a clear join target.

### Caveats

- This is exploratory output and can be noisy.
- It does not prove the final join by itself.
- It is intended for reverse engineering, not batch processing.

---

## 3. `test_lrcat_join.py`

### Purpose

`test_lrcat_join.py` tests the likely Lightroom join chain for one known image and prints the reconstructed path fields.

### Why this script matters

This is the bridge between “we think these tables connect” and “yes, this join reconstructs the expected file path.”

### Typical usage

```bash
/usr/bin/python3 test_lrcat_join.py catalog.lrcat 32638618
```

### What it helped discover

On the tested catalog, it confirmed that this join worked:

- `Adobe_images.rootFile -> AgLibraryFile.id_local`
- `AgLibraryFile.folder -> AgLibraryFolder.id_local`
- `AgLibraryFolder.rootFolder -> AgLibraryRootFolder.id_local`

That was sufficient to reconstruct full paths for green-labeled files.

### Caveats

- This is still a schema-discovery helper.
- A future Lightroom version could use different relationships.
- If the join stops working on your catalog, use the earlier helper scripts to rediscover the correct one.

---

## 4. `green2faststack.py`

### Purpose

`green2faststack.py` is the main tool.

It supports two modes:

1. **Export mode**: read a Lightroom Classic `.lrcat` file and write all Green-labeled paths to a text file
2. **JSON mode**: read that exported text file and update one `faststack.json`

In JSON mode, `--json` can point either at a `faststack.json` path or at the directory that should contain it. If the file is missing, the script creates it.

### Why it is designed this way

The design is intentionally two-step.

Instead of reading Lightroom every time you want to update FastStack, the script lets you:

1. read the catalog once
2. save the extracted paths in a simple text file
3. reuse that exported file as often as you want when updating different FastStack directories

This is useful if:

- you are done using Lightroom
- you want an auditable intermediate file
- you want to update FastStack later without touching the catalog again

### Export mode example

```bash
/usr/bin/python3 green2faststack.py -i backup.lrcat -o green.txt
```

This writes one Lightroom path per line.

### JSON mode example

```bash
/usr/bin/python3 green2faststack.py --paths green.txt --json /path/to/photo-directory
```

That updates `/path/to/photo-directory/faststack.json`, creating it if needed.

### Dry-run example

```bash
/usr/bin/python3 green2faststack.py --paths green.txt --json /path/to/photo-directory --dry-run --verbose
```

### What JSON mode does

In JSON mode, the script:

1. reads all exported Lightroom paths from the text file
2. filters those paths down to the ones that belong to the target `--json` directory
3. derives exact lowercase filename stems from those in-directory paths
4. loads the target `faststack.json`, or creates it if it does not exist
5. marks matching existing entries as uploaded
6. creates new uploaded entries for green-labeled files in that directory that are not yet tracked in the JSON
7. after processing the direct green-labeled files, propagates uploaded state to sibling originals in the same directory when an original stem is a prefix of a green stem followed by a space
8. preserves existing `uploaded_date` values when already present and creates an automatic backup before overwriting an existing JSON file

### Matching strategy

Matching is **stem-based**, not extension-based, but the script now preserves each file's exact lowercase stem instead of collapsing descriptive exports back to a shorter original stem.

That means:

- `IMG_1234.ORF` becomes `img_1234`
- `IMG_1234.JPG` becomes `img_1234`
- `IMG_1234 Description.JPG` becomes `img_1234 description`

The first two still share the same stem key because the filename stem itself is the same:

- `img_1234`

The third gets its own separate FastStack entry because it is a different file with a different stem.

This is intentional and is what makes the tool useful both for RAW/JPG pairs and for Lightroom exports that append descriptive text to the filename.

### Sibling propagation

If `IMG_1234 Description.JPG` is Green in Lightroom, FastStack keeps that export as its own entry under `img_1234 description`.

After that, the script scans sibling image files in the target directory and also marks `img_1234` as uploaded when it finds an original whose stem is a prefix of the green stem followed by a space. That is how a green-labeled derived export can mark its original as uploaded without merging the two entries into one.

For sibling detection, the current code scans common image extensions such as `.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`, `.bmp`, `.orf`, `.cr2`, `.cr3`, `.nef`, `.arw`, `.dng`, `.raf`, `.rw2`, `.pef`, `.srw`, and `.x3f`; see the `IMAGE_EXTENSIONS` set in `green2faststack.py` for the authoritative list.

### Uploaded date behavior

If a FastStack entry is newly marked uploaded and has no existing `uploaded_date`, the script uses a default date unless the user provides:

```bash
--uploaded-date YYYY-MM-DD
```

This project intentionally did **not** assume Lightroom preserved a trustworthy historical “date Green was applied” field for general use. That might exist in some form in some catalogs, but it was not established well enough to rely on.

### Backup behavior

Before overwriting an existing JSON file, the script creates a backup such as:

- `faststack.json.bak`
- `faststack.json.bak1`
- `faststack.json.bak2`

and so on.

### File existence checks

The script can report how many exported image paths currently exist on disk.

This is useful but imperfect.

A Lightroom catalog may store Windows-style paths such as:

- `C:/Users/...`

If the script is run under WSL or Linux, those exact strings may not pass an existence check even when the files are actually present at:

- `/mnt/c/Users/...`

So existence checks should be understood as **best-effort diagnostics**, not the core matching method.
The real FastStack update logic is based on stems.

---

# Recommended workflow

## If you want to adapt this to your own Lightroom catalog

### 1. Back up everything first

Make backups of:

- your `.lrcat`
- any `faststack.json` files you care about

Even though `green2faststack.py` creates JSON backups automatically, do not skip manual backups.

### 2. Confirm the schema on your catalog

Use the helper scripts first.

A safe discovery workflow is:

1. close Lightroom
2. copy the catalog to a backup file
3. open Lightroom
4. mark exactly one known image Green
5. close Lightroom
6. run `lrcat_diff.py`
7. verify that your catalog also uses `Adobe_images.colorLabels = 'Green'` or discover the equivalent in your version

Example:

```bash
/usr/bin/python3 lrcat_diff.py before.lrcat after.lrcat --match "IMG_1234"
```

### 3. Inspect one known changed image

Use `inspect_lrcat_photo.py` and `test_lrcat_join.py` to verify that the same join strategy works on your catalog.

Examples:

```bash
/usr/bin/python3 inspect_lrcat_photo.py after.lrcat 32638618
/usr/bin/python3 test_lrcat_join.py after.lrcat 32638618
```

### 4. Export Green-labeled paths

Once you are confident the schema matches, export all Green-labeled paths:

```bash
/usr/bin/python3 green2faststack.py -i after.lrcat -o green.txt
```

### 5. Update FastStack directories one at a time

Use the exported path list to update one FastStack directory at a time. The `--json` argument can point either at an existing `faststack.json` or at a directory that does not yet contain one.

Dry run first:

```bash
/usr/bin/python3 green2faststack.py --paths green.txt --json /path/to/photo-directory --dry-run --verbose
```

Then real run:

```bash
/usr/bin/python3 green2faststack.py --paths green.txt --json /path/to/photo-directory
```

That real run will create `/path/to/photo-directory/faststack.json` if it is missing.

### 6. Repeat for other directories as needed

Because JSON mode reads from the exported text file instead of the Lightroom catalog, you can reuse the same `green.txt` again and again.

---

# Why this worked for the tested catalog

This project worked because three separate observations lined up:

1. The Green state was stored plainly enough to discover.
2. The file path could be reconstructed from catalog tables.
3. FastStack tracks entries by lowercase stem, which made RAW/JPG pair handling practical.

That combination may hold for many modern Lightroom Classic catalogs, but it may not hold forever.

---

## Catalog version differences

A newer or older Lightroom Classic catalog may:

- rename tables
- move fields
- use different join relationships
- store label state differently

## OS path differences

Paths recorded by Lightroom may not match your current runtime environment exactly.

Examples:

- Windows path in catalog, script run in WSL
- moved drives
- offline volumes
- different mount letters or mount points

## FastStack assumptions

The scripts assume FastStack JSON behavior based on observed sample files, especially:

- lowercase stem keys
- `entries` dictionary
- `uploaded` and `uploaded_date` fields
- when creating a missing `faststack.json`, an empty file shape of `{"version": 2, "last_index": 0, "entries": {}, "stacks": []}`

If FastStack changes its JSON structure in the future, the migration script may need to be updated.

## Best-effort existence checks

A file may fail the existence check and still be a valid match for FastStack stem-based import.

---

# What “worked” means here

On the tested April 1, 2026 Lightroom Classic catalog, the workflow successfully:

- identified Green-labeled images in the catalog
- reconstructed their paths
- exported tens of thousands of Green-labeled paths to a text file
- used those exported paths to mark matching FastStack entries as uploaded in a target `faststack.json`

That is good evidence that the approach is practical.
It is **not** a promise that every Lightroom catalog will behave the same way.

---

# Recommendations for anyone using this on their own data

- work on copies first
- verify the schema with one controlled edit before bulk export
- use dry runs before writing JSON
- inspect the backup files the tool creates
- test on one directory before touching many
- treat helper-script output as discovery evidence, not gospel

---
