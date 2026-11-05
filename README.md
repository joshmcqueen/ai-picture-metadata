# Photo Metadata Inspector

A local browser app for JPEG exports. Inspect EXIF, IPTC, XMP and C2PA metadata, then optionally download verified cleaned copies. Photos never go to an external service. Originals are never overwritten.

## Start on a Mac

Requires Python 3.9 or newer and [ExifTool](https://exiftool.org/). Install ExifTool with Homebrew:

```sh
brew install exiftool
```

Double-click **Start Inspector.command**. The first run installs Flask into a project-local `.venv`; later runs work offline. Keep its Terminal window open while using the app. Press **Control-C** in that window to quit and delete temporary session copies. Downloads you saved remain in your Downloads folder.

Alternatively, from this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

The app opens at `http://127.0.0.1:8765`. If the port is busy, use `--port 8766`. Use `--no-browser` to skip opening a tab, or `--input-dir /path/to/exports` to change the folder inspected by the folder button. `EXIFTOOL_PATH` may point to an alternate ExifTool executable.

## Use

1. Choose photos, drop JPEG files onto the import area, or inspect JPEGs in this app's folder.
2. Review each result and expand its complete metadata. **Export JSON report** saves the session findings, hashes, and cleanup verification.
3. Click **Create cleaned copy** on a photo with explicit AI evidence, or select several photos and **Create cleaned copies**.
4. Download individual cleaned JPEGs, or select verified copies and download a ZIP. Duplicate filenames receive distinct names in the ZIP.

Uploads are to a server bound only to this Mac's loopback address. Each file is limited to 64 MiB and temporary session storage to approximately 2 GiB. All imported and cleaned files are temporary copies outside this project. Normal shutdown removes them; forcibly terminating the process or a power failure can leave a `photo-metadata-*` directory in the system temporary folder, which can be deleted manually.

## What findings mean

- **AI editing recorded:** explicit embedded evidence of generative editing, such as Adobe's `Healing changed, Uses GenAI` or IPTC's `compositeWithTrainedAlgorithmicMedia` declaration.
- **AI generation recorded:** a `trainedAlgorithmicMedia` declaration is present. This can describe an ingredient in an editing history; read the evidence paths for context.
- **Credentials present without explicit AI evidence:** provenance data exists; its presence alone is not evidence of AI use.
- **No embedded AI indicators found:** the supported metadata scan found no explicit declarations. This is not proof that AI was never used.
- **Inspection error:** the JPEG or metadata could not be read completely; cleanup is unavailable.

Reports describe declarations; cryptographic credential signatures are **not verified**. This tool does not examine pixels to detect AI, inspect cloud provenance, or detect invisible watermarks.

## Cleanup guarantees and limits

Cleanup removes a complete embedded C2PA/JUMBF credential when it contains AI evidence, including other provenance inside that credential. It also removes supported explicit generative-AI `DigitalSourceType` XMP declarations. It does **not** delete all metadata or rewrite signed history to claim a different origin. Credentials with no explicit AI evidence are retained.

Before offering a download, the app rescans the output and compares JPEG bytes. Compressed scans, image tables, ICC color profiles, orientation, EXIF, copyright, and unrelated metadata must remain unchanged. For an XMP declaration removal, only the XMP packet may be rewritten; all unrelated parsed XMP values must remain. If preservation cannot be verified, or supported evidence remains, the temporary output is removed and the app reports an error. The original's SHA-256 is checked again after cleanup.

Removing metadata cannot guarantee how Instagram, Facebook, or any other platform will label a photograph. Unsupported evidence is not scrubbed speculatively. Only JPG/JPEG exports are supported; RAW, PSD, TIFF, and PNG files are excluded.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The three private client JPEGs are intentionally excluded from Git. Tests requiring those samples skip when they are absent; parser, classification, and request-guard tests still run. To run all 16 tests locally, retain the original `25EE0779-Edit.jpg`, `398A7418.jpg`, and `398A7475.jpg` in the project root. Integration tests read them without modifying them and create any outputs in temporary directories.

No external service is used. There is no database, analytics, cloud deployment, or account system.
