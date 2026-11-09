# Photo Metadata Inspector

A local browser app for JPEG exports. Inspect EXIF, IPTC, XMP and C2PA metadata, then optionally clean supported AI metadata in place. Photos never go to an external service.

## Start on a Mac

Requires Python 3.9 or newer and [ExifTool](https://exiftool.org/). Install ExifTool with Homebrew:

```sh
brew install exiftool
```

Double-click **Start Inspector.command**. The first run installs Flask into a project-local `.venv`; later runs work offline. Keep its Terminal window open while using the app. Press **Control-C** in that window to quit and delete temporary session copies.

Alternatively, from this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

The app opens at `http://127.0.0.1:8765`. If the port is busy, use `--port 8766`. Use `--no-browser` to skip opening a tab, or `--input-dir /path/to/exports` to set the root for **Inspect configured folder**. `EXIFTOOL_PATH` may point to an alternate ExifTool executable.

## Use

1. Use **Choose folders** to open one or more shoot folders in the native Mac picker, or use **Choose photos** for individual files. The app inspects JPG and JPEG files throughout the folder tree. Drop files or folders into the app for inspection only.
2. Review the results and expand any photo's complete metadata. Folder imports show the source folder alongside the filename. **Export JSON report** saves the relative paths, findings, hashes, and cleanup verification.
3. Click **Clean original** on an editable photo with explicit AI evidence, or select several photos and click **Clean selected originals**. Each verified result is saved permanently at the original path.

Each JPEG is limited to 64 MiB. Photos opened through the Mac picker or configured folder are read directly. Cleanup stages one JPEG at a time on the original volume and checks for enough free space before writing. Dropped files go to a server bound only to this Mac's loopback address and use temporary session storage. Normal shutdown removes those temporary imports. A forced shutdown or power failure can leave temporary `photo-metadata-*` session folders or hidden `.photo-metadata-*` staging folders.

## What findings mean

- **AI editing recorded:** explicit embedded evidence of generative editing, such as Adobe's `Healing changed, Uses GenAI` or IPTC's `compositeWithTrainedAlgorithmicMedia` declaration.
- **AI generation recorded:** a `trainedAlgorithmicMedia` declaration is present. This can describe an ingredient in an editing history; read the evidence paths for context.
- **Credentials present without explicit AI evidence:** provenance data exists; its presence alone is not evidence of AI use.
- **No embedded AI indicators found:** the supported metadata scan found no explicit declarations. This is not proof that AI was never used.
- **Inspection error:** the JPEG or metadata could not be read completely; cleanup is unavailable.

Reports describe declarations; cryptographic credential signatures are **not verified**. This tool does not examine pixels to detect AI, inspect cloud provenance, or detect invisible watermarks.

## Cleanup guarantees and limits

Cleanup removes a complete embedded C2PA/JUMBF credential when it contains AI evidence, including other provenance inside that credential. It also removes supported explicit generative-AI `DigitalSourceType` XMP declarations. It does **not** delete all metadata or rewrite signed history to claim a different origin. Credentials with no explicit AI evidence are retained.

Before replacing the original, the app rescans a temporary JPEG and compares compressed scans, image tables, ICC color profiles, orientation, EXIF, copyright, and unrelated metadata. For an XMP declaration removal, only the XMP packet may be rewritten and unrelated parsed XMP values must match. The app checks the source again for changes, preserves filesystem permissions and Mac extended attributes, and atomically installs the verified JPEG at the same path. If verification fails, the staged file is removed and the original stays in place. Cleanup requires write access and a regular file without other hard links.

Removing metadata cannot guarantee how Instagram, Facebook, or any other platform will label a photograph. Unsupported evidence is not scrubbed speculatively. Only JPG/JPEG exports are supported; RAW, PSD, TIFF, and PNG files are excluded.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The three private client JPEGs are intentionally excluded from Git. Tests requiring those samples skip when they are absent. To run the full suite locally, retain the original `25EE0779-Edit.jpg`, `398A7418.jpg`, and `398A7475.jpg` in the project root. Integration tests read them and create any outputs in temporary directories.

No external service is used. There is no database, analytics, cloud deployment, or account system.
