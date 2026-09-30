# PDF Recolor

<div align="center">

**Transform harsh, washed-out scanned PDFs into clean, readable, eye-friendly documents.**

[![GitHub Release](https://img.shields.io/github/v/release/singm/pdf-recolor?label=Download%20.exe&style=for-the-badge&color=4849B2)](https://github.com/singm/pdf-recolor/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-lightgrey.svg?style=for-the-badge&logo=windows)](https://github.com/singm/pdf-recolor/releases/latest)
[![Python: 3.9+](https://img.shields.io/badge/Python-3.9+-3776AB.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)

</div>

---

## ⚡ Why PDF Recolor?

Scanned PDFs are notorious for being difficult to read:
- Faded pencil or pen strokes that vanish when viewed or reprinted
- Blinding white backgrounds that cause severe eye strain during long reading sessions
- Inconsistent contrast across multi-page scans

**PDF Recolor** solves this with a lightweight, multi-threaded engine. It remaps tones so dark strokes become your chosen **content color**, the page background becomes your **paper color**, and thin lines are reinforced to survive scanning and printing.

---

## ✨ Key Features

- 🚀 **Zero Install Required**: Download the single portable `.exe` file and run it immediately.
- 🎨 **One-Click Presets**:
  - **Indigo**: Modern cool navy ink on soft slate paper (default).
  - **Paper**: Warm, natural reading book tones.
  - **Night**: Dark mode for midnight study sessions.
  - **Mint**: Low-contrast calming green.
  - **Sepia**: Classic archival parchment look.
- 🖌️ **Custom Colors**: Pick any paper and ink color with standard hex/palette selectors.
- 🖊️ **Intelligent Line Thickening**: 3x3 morphological minimum filtering sharpens faint pencil marks and thin technical lines.
- 👁️ **Instant First-Page Preview**: Test your color combinations before starting batch operations.
- 🏎️ **Multi-Core Parallel Rendering**: Dynamically distributes pages across CPU cores for maximum conversion speed.
- 🛡️ **Non-Destructive & Private**:
  - Original files are **never overwritten**.
  - **100% offline**: Your documents never leave your local machine.
- 📁 **Batch File & Folder Processing**: Drag and drop 1 or 100 PDFs at once.

---

## 📥 Download & Quick Start (Windows)

No Python or terminal knowledge required.

1. Head over to **[Releases](../../releases/latest)** and download `PDF Recolor.exe`.
2. Double-click `PDF Recolor.exe` to launch.
3. Click **Add PDFs** (or drag files directly into the window).
4. Select a look preset (e.g. *Indigo*, *Night*, *Sepia*) or choose custom colors.
5. Click **Preview first page** to inspect.
6. Click **Convert**!

---

## 💻 Developer Guide: Run from Source

If you prefer to run or modify the Python source code directly:

### Prerequisites
- Windows 10 or 11
- Python 3.9+ (make sure `Add python.exe to PATH` was checked during installation)

### 1. Clone the repository
```powershell
git clone https://github.com/singm/pdf-recolor.git
cd pdf-recolor
```

### 2. Set up virtual environment & dependencies
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 3. Run the application
```powershell
python PDF_Recolor_App.py
```

*Alternatively, double-click `Run_PDF_Recolor.bat` which will automatically configure a local environment and launch the app.*

---

## 🛠️ Building the Standalone `.exe`

To compile your own standalone Windows binary:

```powershell
.\Build_EXE.bat
```

Or execute via PyInstaller directly:
```powershell
pyinstaller --noconfirm --clean --onefile --windowed `
  --name "PDF Recolor" --collect-all tkinterdnd2 PDF_Recolor_App.py
```
The compiled executable will be output to `dist/PDF Recolor.exe`.

---

## ⚙️ How It Works

1. **Rendering**: Pages are extracted using `PyMuPDF` (`fitz`) at your selected DPI (200, 300, or 400).
2. **Stroke Thickening**: A fast 2-pass 1D minimum filter reinforces fine strokes without image distortion.
3. **Lookup Table (LUT) Remapping**: Pixel luminosity is mapped against a precomputed 766-entry lookup table transitioning from ink to paper color, eliminating per-pixel floating-point math.
4. **Assembly**: Converted page streams are compressed via JPEG deflate and repacked into a fresh PDF, preserving document table of contents (TOC) bookmarks where available.

---

## 🔒 Security & Privacy

- **Hardened Path Handling**: Filename suffixes and directory selections are strictly sanitized against path traversal vulnerabilities.
- **Resource Limits**: Page renders are constrained by memory safety budgets to guard against decompression bombs and oversized media.
- **UNC Isolation**: Network/SMB path connections are rejected to prevent NTLM credential leaks.
- **Restricted Preferences**: Configuration files in user homes are saved with owner-only file permissions.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE) — you are free to use, modify, distribute, and integrate it into private or commercial workflows.