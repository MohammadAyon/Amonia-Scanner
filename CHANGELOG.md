# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-30

### Added
- **Batch PDF Recoloring**: Process individual PDF files or entire directories simultaneously.
- **5 One-Click Presets**: Indigo, Paper, Night, Mint, and Sepia.
- **Custom Color Picker**: Independent custom paper (background) and ink (content) selection.
- **Line Thickening**: Morphological minimum filter to make faint lines and handwriting bolder.
- **Multi-Core Parallel Rendering**: Background multiprocessing pool to utilize all available CPU cores.
- **First-Page Preview**: Instant full-color preview window before running conversions.
- **Configurable Output Quality**: 200, 300 (recommended), and 400 DPI modes.
- **Non-Destructive Processing**: Automatically resolves filename collisions to never overwrite originals.
- **Drag & Drop Support**: Direct drag-and-drop file ingestion via `tkinterdnd2`.
- **Standalone Windows Distribution**: Portable single `.exe` executable requiring zero dependencies or installation.
- **Automated CI/CD**: GitHub Actions workflow to build release executables on tag push.

### Security & Reliability Hardening
- **Path Traversal Protection**: Enforced strict sanitization of filename suffixes and output paths.
- **Config Validation & Hardening**: Restricted deserialization of `~/.pdf_recolor.json` to expected schemas, typed keys, clamped DPI (72-600), and validated hex codes.
- **NTLM / UNC Attack Mitigation**: Blocked remote SMB/UNC paths in output folders and folder launchers.
- **Memory Denial-of-Service Defense**: Enforced page rendering pixel budgets (100M pixels) to prevent OOM on malicious or oversized blueprints/posters.
- **TOCTOU Race Condition Prevention**: Bounded unique filename candidate iterations.
- **Worker Resource Management**: Registered process exit cleanup handlers to prevent leaked file descriptors.
- **Restricted Permissions**: Config files saved with owner-only access permissions (`chmod 600`).
