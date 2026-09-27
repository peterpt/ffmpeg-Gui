#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ffgui.py — PyQt5 front-end for ffmpeg video analysis + conversion.

Features
--------
- Probe any input with ffprobe: container, duration, streams, codecs, resolution,
  bitrate, fps, channels, language, subtitle tracks.
- Per-stream selection: pick which video / audio track to keep, drop subtitles.
- Output container + video/audio codec lists pulled LIVE from your ffmpeg build
  (only encoders your ffmpeg actually supports are shown).
- Quality mode toggle:
    * CRF (quality-targeted, single pass)
    * Size cap (two-pass, bitrate computed from target MB - audio budget)
- Screen size: Original, or downscale to a preset height (2160/1440/1080/720/480/360)
  or custom, aspect ratio preserved (width auto, even).
- 10-bit -> 8-bit handling, pixel format override.
- Audio: channel downmix (keep / stereo / mono), sample rate, bitrate.
- Live progress bar + log, driven by parsing ffmpeg's -progress output.
- Cancel button that kills the running ffmpeg cleanly.
- Copy-the-command button so you can see / reuse exactly what will run.

Dependencies: python3, PyQt5, ffmpeg + ffprobe on PATH.
    Devuan/Debian:  apt install python3-pyqt5 ffmpeg
"""

import os
import re
import sys
import json
import shlex
import time
import signal
import subprocess

from PyQt5.QtCore import Qt, QProcess, pyqtSignal, QObject
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QSpinBox, QFileDialog, QTextEdit, QProgressBar,
    QGroupBox, QGridLayout, QVBoxLayout, QHBoxLayout, QFormLayout, QMessageBox,
    QRadioButton, QButtonGroup, QTableWidget, QTableWidgetItem, QHeaderView,
    QSizePolicy, QDoubleSpinBox
)


# --------------------------------------------------------------------------- #
#  ffmpeg / ffprobe helpers
# --------------------------------------------------------------------------- #

def which(prog):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(p, prog)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


FFMPEG = which("ffmpeg")
FFPROBE = which("ffprobe")


def run(cmd):
    """Run a command, return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def list_encoders():
    """Return dict {'video': [...], 'audio': [...]} of available ffmpeg encoders."""
    out = {"video": [], "audio": []}
    if not FFMPEG:
        return out
    rc, so, se = run([FFMPEG, "-hide_banner", "-encoders"])
    for line in so.splitlines():
        line = line.rstrip()
        # Format: " V..... libx264   ..." — first token is capability flags.
        m = re.match(r"\s*([VAS])[.A-Z]{5}\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        kind, name, desc = m.group(1), m.group(2), m.group(3)
        if kind == "V":
            out["video"].append((name, desc))
        elif kind == "A":
            out["audio"].append((name, desc))
    out["video"].sort()
    out["audio"].sort()
    return out


def list_muxers():
    """Return list of (name, desc) container muxers ffmpeg supports."""
    res = []
    if not FFMPEG:
        return res
    rc, so, se = run([FFMPEG, "-hide_banner", "-muxers"])
    for line in so.splitlines():
        m = re.match(r"\s*[.E]{2}\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        res.append((m.group(1), m.group(2)))
    res.sort()
    return res


def probe(path):
    """Full ffprobe JSON for a media file."""
    if not FFPROBE:
        return None, "ffprobe not found on PATH"
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    rc, so, se = run(cmd)
    if rc != 0:
        return None, se.strip() or "ffprobe failed"
    try:
        return json.loads(so), None
    except Exception as e:
        return None, str(e)


# Common container extensions mapped to a sensible ffmpeg -f muxer.
CONTAINER_PRESETS = {
    "mp4": "mp4", "mkv": "matroska", "mov": "mov", "webm": "webm",
    "avi": "avi", "flv": "flv", "ts": "mpegts", "m4v": "mp4",
    "ogg": "ogg", "gif": "gif",
}

HEIGHT_PRESETS = ["Original", "2160", "1440", "1080", "720", "480", "360", "Custom"]

PIX_FMTS = ["auto", "yuv420p", "yuv420p10le", "yuv422p", "yuv444p", "nv12", "rgb24"]

# Video container extensions this GUI accepts as input.
SUPPORTED_INPUT_EXTS = [
    ".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".flv", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".ogv", ".ogg",
    ".3gp", ".3g2", ".divx", ".f4v", ".rm", ".rmvb", ".asf", ".mxf",
    ".gif",
]

APP_AUTHOR = "peterpt"
APP_AI_CREDIT = "Anthropic Opus 4.8 AI"
APP_NAME = "ffgui"


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #

class FFGui(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ffgui — ffmpeg analysis & conversion")
        self.resize(1040, 720)

        self.info = None          # parsed ffprobe dict
        self.duration = 0.0       # seconds
        self.proc = None          # QProcess for ffmpeg
        self.pass_stage = 0       # 0 idle, 1 first pass, 2 second pass
        self.passlog = None
        self.stage_start = 0.0    # wall-clock start of current ffmpeg run
        self.stage_label = ""     # "Encoding" / "Pass 1/2" etc for status line
        self.last_speed = 0.0     # last reported ffmpeg speed multiplier
        self.input_has_errors = False
        self.remux_running = False

        self.encoders = list_encoders()
        self.muxers = list_muxers()

        self._build_ui()
        self._check_tools()

    # ---- UI construction -------------------------------------------------- #

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Input row ---
        in_box = QGroupBox("Input")
        in_l = QHBoxLayout(in_box)
        self.in_edit = QLineEdit()
        self.in_edit.setPlaceholderText("Path to source video…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.on_browse)
        analyze = QPushButton("Analyze")
        analyze.clicked.connect(self.on_analyze)
        in_l.addWidget(self.in_edit, 1)
        in_l.addWidget(browse)
        in_l.addWidget(analyze)
        root.addWidget(in_box)

        # --- Analysis table ---
        an_box = QGroupBox("Analysis")
        an_l = QVBoxLayout(an_box)
        self.meta_lbl = QLabel("No file analyzed yet.")
        self.meta_lbl.setWordWrap(True)
        self.stream_tbl = QTableWidget(0, 7)
        self.stream_tbl.setHorizontalHeaderLabels(
            ["#", "Type", "Codec", "Detail", "Bitrate", "Lang", "Keep"])
        self.stream_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.stream_tbl.verticalHeader().setVisible(False)
        self.stream_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        # Height is fitted to content (see _fit_stream_table); don't reserve space.
        self.stream_tbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.stream_tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fit_stream_table()   # size for the empty state initially
        an_l.addWidget(self.meta_lbl)
        an_l.addWidget(self.stream_tbl)
        root.addWidget(an_box)

        # --- Output settings ---
        out_box = QGroupBox("Output settings")
        grid = QGridLayout(out_box)

        # Container
        grid.addWidget(QLabel("Container:"), 0, 0)
        self.container_cb = QComboBox()
        for ext in CONTAINER_PRESETS:
            self.container_cb.addItem(ext)
        self.container_cb.setCurrentText("mp4")
        grid.addWidget(self.container_cb, 0, 1)

        # Video codec
        grid.addWidget(QLabel("Video codec:"), 0, 2)
        self.vcodec_cb = QComboBox()
        self.vcodec_cb.addItem("copy")
        for name, desc in self.encoders["video"]:
            self.vcodec_cb.addItem(name)
        self._select_default(self.vcodec_cb, "libx264")
        grid.addWidget(self.vcodec_cb, 0, 3)

        # Audio codec
        grid.addWidget(QLabel("Audio codec:"), 0, 4)
        self.acodec_cb = QComboBox()
        self.acodec_cb.addItem("copy")
        for name, desc in self.encoders["audio"]:
            self.acodec_cb.addItem(name)
        self._select_default(self.acodec_cb, "aac")
        grid.addWidget(self.acodec_cb, 0, 5)

        # Screen size
        grid.addWidget(QLabel("Screen size:"), 1, 0)
        self.height_cb = QComboBox()
        self.height_cb.addItems(HEIGHT_PRESETS)
        self.height_cb.setCurrentText("720")
        self.height_cb.currentTextChanged.connect(self._toggle_custom_h)
        grid.addWidget(self.height_cb, 1, 1)
        self.custom_h = QSpinBox()
        self.custom_h.setRange(16, 4320)
        self.custom_h.setValue(720)
        self.custom_h.setEnabled(False)
        self.custom_h.setSuffix(" px")
        grid.addWidget(self.custom_h, 1, 2)

        # Pixel format
        grid.addWidget(QLabel("Pixel format:"), 1, 3)
        self.pix_cb = QComboBox()
        self.pix_cb.addItems(PIX_FMTS)
        self.pix_cb.setCurrentText("yuv420p")
        grid.addWidget(self.pix_cb, 1, 4)

        # fps override
        grid.addWidget(QLabel("FPS:"), 1, 5)
        self.fps_edit = QLineEdit()
        self.fps_edit.setPlaceholderText("keep")
        grid.addWidget(self.fps_edit, 1, 6)

        # --- Quality mode ---
        q_box = QGroupBox("Quality mode")
        q_l = QGridLayout(q_box)
        self.mode_grp = QButtonGroup(self)
        self.rb_crf = QRadioButton("CRF (quality, 1-pass)")
        self.rb_size = QRadioButton("Size cap (2-pass)")
        self.rb_crf.setChecked(True)
        self.rb_size.setEnabled(False)   # locked until a file is analyzed
        self.rb_size.setToolTip("Load and analyze a video first "
                                "(duration is needed to size the encode).")
        self.mode_grp.addButton(self.rb_crf)
        self.mode_grp.addButton(self.rb_size)
        self.rb_crf.toggled.connect(self._toggle_mode)
        q_l.addWidget(self.rb_crf, 0, 0)
        q_l.addWidget(self.rb_size, 0, 1)

        q_l.addWidget(QLabel("CRF:"), 1, 0)
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 63)
        self.crf_spin.setValue(21)
        q_l.addWidget(self.crf_spin, 1, 1)

        q_l.addWidget(QLabel("Preset:"), 1, 2)
        self.preset_cb = QComboBox()
        self.preset_cb.addItems(["ultrafast", "superfast", "veryfast", "faster",
                                 "fast", "medium", "slow", "slower", "veryslow"])
        self.preset_cb.setCurrentText("medium")
        q_l.addWidget(self.preset_cb, 1, 3)

        q_l.addWidget(QLabel("Target size:"), 2, 0)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(1, 100000)
        self.size_spin.setValue(1000)
        self.size_spin.setSuffix(" MB")
        self.size_spin.setEnabled(False)
        q_l.addWidget(self.size_spin, 2, 1)
        self.calc_lbl = QLabel("")
        q_l.addWidget(self.calc_lbl, 2, 2, 1, 2)

        # --- Audio options ---
        a_box = QGroupBox("Audio")
        a_l = QGridLayout(a_box)
        a_l.addWidget(QLabel("Channels:"), 0, 0)
        self.chan_cb = QComboBox()
        self.chan_cb.addItems(["keep", "stereo (2)", "mono (1)"])
        self.chan_cb.setCurrentText("stereo (2)")
        a_l.addWidget(self.chan_cb, 0, 1)
        a_l.addWidget(QLabel("Sample rate:"), 0, 2)
        self.sr_cb = QComboBox()
        self.sr_cb.addItems(["keep", "48000", "44100", "32000"])
        a_l.addWidget(self.sr_cb, 0, 3)
        a_l.addWidget(QLabel("Audio bitrate:"), 0, 4)
        self.ab_cb = QComboBox()
        self.ab_cb.addItems(["96k", "128k", "160k", "192k", "256k", "320k"])
        self.ab_cb.setCurrentText("128k")
        a_l.addWidget(self.ab_cb, 0, 5)

        self.no_subs = QCheckBox("Exclude subtitles")
        self.no_subs.setChecked(True)
        a_l.addWidget(self.no_subs, 1, 0, 1, 2)

        # --- Output file row ---
        of_box = QGroupBox("Output file")
        of_l = QHBoxLayout(of_box)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Output path (auto-suggested on analyze)…")
        out_browse = QPushButton("Save as…")
        out_browse.clicked.connect(self.on_out_browse)
        of_l.addWidget(self.out_edit, 1)
        of_l.addWidget(out_browse)

        # --- Action buttons ---
        act = QHBoxLayout()
        self.about_btn = QPushButton("ABOUT")
        self.about_btn.clicked.connect(self.on_about)
        self.cmd_btn = QPushButton("Show command")
        self.cmd_btn.clicked.connect(self.on_show_cmd)
        self.remux_btn = QPushButton("Remux/Repair")
        self.remux_btn.setToolTip("Rebuild the container to fix a damaged/"
                                  "unseekable input before converting.")
        self.remux_btn.clicked.connect(self.on_remux)
        self.remux_btn.setEnabled(False)
        self.run_btn = QPushButton("Convert")
        self.run_btn.clicked.connect(self.on_run)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.cancel_btn.setEnabled(False)
        act.addWidget(self.about_btn)
        act.addWidget(self.cmd_btn)
        act.addWidget(self.remux_btn)
        act.addStretch(1)
        act.addWidget(self.cancel_btn)
        act.addWidget(self.run_btn)

        # --- Progress + log ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status_lbl = QLabel("Idle.")
        self.status_lbl.setTextFormat(Qt.PlainText)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace", 9))
        self.log.setMinimumHeight(150)

        root.addWidget(out_box)
        row = QHBoxLayout()
        row.addWidget(q_box, 1)
        row.addWidget(a_box, 1)
        root.addLayout(row)
        root.addWidget(of_box)
        root.addLayout(act)
        root.addWidget(self.progress)
        root.addWidget(self.status_lbl)
        root.addWidget(self.log)

        self.size_spin.valueChanged.connect(self._update_calc)

    def _select_default(self, cb, name):
        idx = cb.findText(name)
        if idx >= 0:
            cb.setCurrentIndex(idx)

    # ---- Tool check ------------------------------------------------------- #

    def _check_tools(self):
        missing = []
        if not FFMPEG:
            missing.append("ffmpeg")
        if not FFPROBE:
            missing.append("ffprobe")
        if missing:
            self.log.append(
                "WARNING: not found on PATH: " + ", ".join(missing) +
                "\nInstall with: apt install ffmpeg")
            QMessageBox.critical(
                self, "Missing dependencies",
                "Required tool(s) not found on PATH:\n\n  "
                + "\n  ".join(missing) +
                "\n\nInstall on Devuan/Debian with:\n    "
                "sudo apt install ffmpeg\n\n"
                "The GUI will open but conversion is disabled until these exist.")
            self.run_btn.setEnabled(False) if hasattr(self, "run_btn") else None
        else:
            self.log.append(f"ffmpeg: {FFMPEG}\nffprobe: {FFPROBE}")
            self.log.append(
                f"Encoders available — video: {len(self.encoders['video'])}, "
                f"audio: {len(self.encoders['audio'])}")

    # ---- Toggles ---------------------------------------------------------- #

    def _toggle_custom_h(self, text):
        self.custom_h.setEnabled(text == "Custom")

    def _toggle_mode(self):
        crf = self.rb_crf.isChecked()
        self.crf_spin.setEnabled(crf)
        self.size_spin.setEnabled(not crf)
        self._update_calc()

    def _update_calc(self):
        if self.rb_crf.isChecked() or self.duration <= 0:
            self.calc_lbl.setText("")
            return
        vb = self._computed_video_bitrate()
        min_mb = self._min_achievable_mb()
        rng = f"  (min ~{min_mb:.0f} MB)" if min_mb else ""
        if vb:
            self.calc_lbl.setText(f"→ video ≈ {vb} kb/s{rng}")
        else:
            self.calc_lbl.setText(f"target too small{rng}")

    # ---- File dialogs ----------------------------------------------------- #

    def on_browse(self):
        patterns = " ".join("*" + e for e in SUPPORTED_INPUT_EXTS)
        flt = f"Video files ({patterns});;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Select video", "", flt)
        if path:
            if not self._ext_supported(path):
                QMessageBox.warning(
                    self, "Unsupported file",
                    "That file type isn't a supported video container.\n\n"
                    "Supported: " + ", ".join(SUPPORTED_INPUT_EXTS))
                return
            self.in_edit.setText(path)
            self.on_analyze()   # auto-analyze on load (also unlocks size cap)

    @staticmethod
    def _ext_supported(path):
        return os.path.splitext(path)[1].lower() in SUPPORTED_INPUT_EXTS

    def on_out_browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save output as")
        if path:
            self.out_edit.setText(path)

    # ---- Analysis --------------------------------------------------------- #

    def on_analyze(self):
        path = self.in_edit.text().strip()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "No file", "Select a valid input file first.")
            return
        if not self._ext_supported(path):
            QMessageBox.warning(
                self, "Unsupported file",
                "That file type isn't a supported video container.\n\n"
                "Supported: " + ", ".join(SUPPORTED_INPUT_EXTS))
            return
        info, err = probe(path)
        if err:
            QMessageBox.critical(self, "ffprobe error", err)
            return
        self.info = info
        self._populate_analysis(info)
        # Unlock size cap now that a file (with known duration) is loaded.
        self.rb_size.setEnabled(True)
        if self.rb_size.isChecked():
            self.size_spin.setEnabled(True)
        # Suggest an output name
        base, _ = os.path.splitext(path)
        h = self.height_cb.currentText()
        tag = h if h not in ("Original", "Custom") else "conv"
        ext = self.container_cb.currentText()
        self.out_edit.setText(f"{base}.{tag}p.{ext}"
                              if tag != "conv" else f"{base}.{tag}.{ext}")
        self._update_calc()
        # Check the input for decode errors (non-blocking-ish, quick scan).
        self._check_input_integrity(path)

    def _check_input_integrity(self, path):
        """Quick error scan of the input; offer remux if problems found."""
        self.status_lbl.setText("Checking input for errors…")
        QApplication.processEvents()
        # -v error with null muxer surfaces decode problems without full re-encode.
        cmd = [FFMPEG, "-v", "error", "-i", path, "-map", "0:v:0?",
               "-t", "60", "-f", "null", os.devnull]
        rc, so, se = run(cmd)
        errs = [l for l in se.splitlines() if l.strip()]
        if errs:
            self.input_has_errors = True
            self.log.append(f"\n[INPUT WARNING] ffmpeg reported {len(errs)} "
                            f"decode issue(s) in first 60s. Sample:")
            for l in errs[:5]:
                self.log.append("  " + l)
            self.remux_btn.setEnabled(True)
            self.status_lbl.setText(
                "Input may have errors — 'Remux/Repair' available, or convert "
                "with auto error-tolerance.")
        else:
            self.input_has_errors = False
            self.remux_btn.setEnabled(False)
            self.status_lbl.setText("Input OK.")

    def _populate_analysis(self, info):
        fmt = info.get("format", {})
        self.duration = float(fmt.get("duration", 0) or 0)
        dur_str = self._fmt_time(self.duration)
        size = int(fmt.get("size", 0) or 0)
        br = fmt.get("bit_rate")
        br_str = f"{int(br)//1000} kb/s" if br else "?"
        self.meta_lbl.setText(
            f"<b>{os.path.basename(fmt.get('filename',''))}</b><br>"
            f"Container: {fmt.get('format_name','?')} · "
            f"Duration: {dur_str} · "
            f"Size: {size/1024/1024:.1f} MB · "
            f"Overall bitrate: {br_str}")

        streams = info.get("streams", [])
        self.stream_tbl.setRowCount(len(streams))
        for r, s in enumerate(streams):
            idx = s.get("index", r)
            ctype = s.get("codec_type", "?")
            codec = s.get("codec_name", "?")
            detail = ""
            if ctype == "video":
                w, h = s.get("width"), s.get("height")
                fps = s.get("r_frame_rate", "")
                try:
                    n, d = fps.split("/")
                    fps = f"{float(n)/float(d):.2f}" if float(d) else fps
                except Exception:
                    pass
                detail = f"{w}x{h} {s.get('pix_fmt','')} {fps}fps"
            elif ctype == "audio":
                detail = (f"{s.get('sample_rate','')}Hz "
                          f"{s.get('channel_layout', s.get('channels',''))}")
            elif ctype == "subtitle":
                detail = s.get("codec_long_name", "")
            sbr = s.get("bit_rate")
            sbr_str = f"{int(sbr)//1000}k" if sbr else ""
            lang = s.get("tags", {}).get("language", "")

            self.stream_tbl.setItem(r, 0, QTableWidgetItem(str(idx)))
            self.stream_tbl.setItem(r, 1, QTableWidgetItem(ctype))
            self.stream_tbl.setItem(r, 2, QTableWidgetItem(codec))
            self.stream_tbl.setItem(r, 3, QTableWidgetItem(detail))
            self.stream_tbl.setItem(r, 4, QTableWidgetItem(sbr_str))
            self.stream_tbl.setItem(r, 5, QTableWidgetItem(lang))
            keep = QCheckBox()
            # Default: keep first video + first audio, drop subs
            first_v = ctype == "video" and not any(
                streams[i].get("codec_type") == "video" for i in range(r))
            first_a = ctype == "audio" and not any(
                streams[i].get("codec_type") == "audio" for i in range(r))
            keep.setChecked(first_v or first_a)
            if ctype == "subtitle":
                keep.setChecked(False)
            w = QWidget()
            wl = QHBoxLayout(w)
            wl.addWidget(keep)
            wl.setAlignment(Qt.AlignCenter)
            wl.setContentsMargins(0, 0, 0, 0)
            self.stream_tbl.setCellWidget(r, 6, w)
            # stash for later
            keep.setProperty("stream_index", idx)
            keep.setProperty("codec_type", ctype)
        self._fit_stream_table()

    def _fit_stream_table(self, max_rows=8):
        """Resize the stream table to exactly fit its rows (header + content),
        so a 2-stream file doesn't reserve empty space. Caps at max_rows;
        beyond that a vertical scrollbar appears instead of growing further."""
        tbl = self.stream_tbl
        header_h = tbl.horizontalHeader().height()
        if header_h <= 0:
            header_h = tbl.horizontalHeader().sizeHint().height() or 26
        rows = tbl.rowCount()
        # Height of one row (fall back to a sane default before any row exists).
        row_h = tbl.rowHeight(0) if rows else tbl.verticalHeader().defaultSectionSize()
        if row_h <= 0:
            row_h = 30
        visible = min(max(rows, 1), max_rows)   # at least 1 row's worth of body
        frame = 2 * tbl.frameWidth()
        total = header_h + visible * row_h + frame
        # If more rows than the cap, allow scrolling; otherwise hide the bar.
        if rows > max_rows:
            tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            total += tbl.horizontalScrollBar().height()  # spacing safety
        else:
            tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tbl.setFixedHeight(total)

    # ---- Command building ------------------------------------------------- #

    def _selected_streams(self):
        """Return list of (index, codec_type) the user chose to keep."""
        keeps = []
        for r in range(self.stream_tbl.rowCount()):
            w = self.stream_tbl.cellWidget(r, 6)
            if not w:
                continue
            cb = w.findChild(QCheckBox)
            if cb and cb.isChecked():
                keeps.append((cb.property("stream_index"),
                              cb.property("codec_type")))
        return keeps

    def _computed_video_bitrate(self):
        """kb/s for video given target size minus audio budget."""
        if self.duration <= 0:
            return None
        target_bits = self.size_spin.value() * 1024 * 1024 * 8
        ab = int(self.ab_cb.currentText().replace("k", "")) * 1000
        # audio only counts if we're keeping an audio stream and not copying
        audio_bits = ab * self.duration
        vb = (target_bits - audio_bits) / self.duration / 1000
        return int(vb) if vb > 0 else None

    def _target_height(self):
        """Effective output height in px (for min-bitrate estimation)."""
        h = self.height_cb.currentText()
        if h == "Custom":
            return self.custom_h.value()
        if h == "Original":
            # derive from the source video stream if known
            for s in (self.info or {}).get("streams", []):
                if s.get("codec_type") == "video" and s.get("height"):
                    return int(s["height"])
            return 720
        try:
            return int(h)
        except ValueError:
            return 720

    def _min_video_bitrate_kbps(self):
        """A practical floor for watchable video at the target height.
        Rough heuristic: ~0.9 kb/s per line of height, min 120."""
        return max(120, int(self._target_height() * 0.9))

    def _audio_bits_total(self):
        ab = int(self.ab_cb.currentText().replace("k", "")) * 1000
        return ab * self.duration

    def _min_achievable_mb(self):
        """Smallest sensible output size: floor video bitrate + audio, in MB."""
        if self.duration <= 0:
            return None
        bits = self._min_video_bitrate_kbps() * 1000 * self.duration \
            + self._audio_bits_total()
        return bits / 8 / 1024 / 1024

    def _max_sensible_mb(self):
        """Above the source size, shrinking is pointless; use source size."""
        try:
            src = os.path.getsize(self.in_edit.text().strip()) / 1024 / 1024
            return src
        except Exception:
            return None

    def _scale_filter(self):
        h = self.height_cb.currentText()
        if h == "Original":
            return None
        if h == "Custom":
            h = str(self.custom_h.value())
        # width auto, keep even numbers
        return f"scale=-2:{h}"

    def _build_cmd(self, out_path, pass_num=None, passlog=None):
        """Return an ffmpeg argv list. pass_num in {1,2} for two-pass."""
        inp = self.in_edit.text().strip()
        cmd = [FFMPEG, "-y"]
        # Auto error-tolerance: if the input probed with errors, tell the
        # decoder to skip corrupt packets and keep going rather than abort.
        if self.input_has_errors:
            cmd += ["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt"]
        cmd += ["-i", inp]

        keeps = self._selected_streams()
        # Mapping
        for idx, ctype in keeps:
            if ctype == "subtitle" and self.no_subs.isChecked():
                continue
            cmd += ["-map", f"0:{idx}"]
        if self.no_subs.isChecked():
            cmd += ["-sn"]

        vcodec = self.vcodec_cb.currentText()
        acodec = self.acodec_cb.currentText()

        # Video
        cmd += ["-c:v", vcodec]
        if vcodec != "copy":
            vf = self._scale_filter()
            if vf:
                cmd += ["-vf", vf]
            if self.pix_cb.currentText() != "auto":
                cmd += ["-pix_fmt", self.pix_cb.currentText()]
            fps = self.fps_edit.text().strip()
            if fps:
                cmd += ["-r", fps]
            if self.rb_crf.isChecked():
                cmd += ["-crf", str(self.crf_spin.value())]
                if vcodec in ("libx264", "libx265"):
                    cmd += ["-preset", self.preset_cb.currentText()]
            else:
                vb = self._computed_video_bitrate()
                cmd += ["-b:v", f"{vb}k"]
                if vcodec in ("libx264", "libx265"):
                    cmd += ["-preset", self.preset_cb.currentText()]

        # Two-pass overrides
        if pass_num:
            cmd += ["-pass", str(pass_num), "-passlogfile", passlog]

        # Audio
        if pass_num == 1:
            cmd += ["-an"]
        else:
            cmd += ["-c:a", acodec]
            if acodec != "copy":
                ch = self.chan_cb.currentText()
                if ch.startswith("stereo"):
                    cmd += ["-ac", "2"]
                elif ch.startswith("mono"):
                    cmd += ["-ac", "1"]
                sr = self.sr_cb.currentText()
                if sr != "keep":
                    cmd += ["-ar", sr]
                cmd += ["-b:a", self.ab_cb.currentText()]

        # Container / format
        muxer = CONTAINER_PRESETS.get(self.container_cb.currentText())
        if pass_num == 1:
            cmd += ["-f", muxer or "null", os.devnull]
        else:
            if muxer:
                cmd += ["-f", muxer]
            cmd += ["-progress", "pipe:1", "-nostats", out_path]
        return cmd

    def on_show_cmd(self):
        if not self._validate():
            return
        out = self.out_edit.text().strip()
        if self.rb_size.isChecked():
            base = os.path.splitext(out)[0] + "_2pass"
            c1 = self._build_cmd(out, 1, base)
            c2 = self._build_cmd(out, 2, base)
            txt = ("PASS 1:\n" + " ".join(shlex.quote(x) for x in c1) +
                   "\n\nPASS 2:\n" + " ".join(shlex.quote(x) for x in c2))
        else:
            c = self._build_cmd(out)
            txt = " ".join(shlex.quote(x) for x in c)
        self.log.append("\n" + txt + "\n")
        QApplication.clipboard().setText(txt)
        self.log.append("(command copied to clipboard)")

    # ---- Validation ------------------------------------------------------- #

    def _validate(self):
        if not FFMPEG:
            QMessageBox.critical(self, "Missing ffmpeg", "ffmpeg not found on PATH.")
            return False
        if not os.path.isfile(self.in_edit.text().strip()):
            QMessageBox.warning(self, "Input", "Select a valid input file.")
            return False
        if not self.out_edit.text().strip():
            QMessageBox.warning(self, "Output", "Set an output path.")
            return False
        if self.rb_size.isChecked():
            if self.duration <= 0:
                QMessageBox.warning(self, "Analyze", "Analyze the file first "
                                    "so duration is known for 2-pass sizing.")
                return False
            target = self.size_spin.value()
            min_mb = self._min_achievable_mb()
            max_mb = self._max_sensible_mb()
            vb = self._computed_video_bitrate()

            # Too small: below the point where even floor-quality video + audio fit.
            if min_mb and target < min_mb:
                floor_kbps = self._min_video_bitrate_kbps()
                ans = QMessageBox.warning(
                    self, "Target size too small",
                    f"{target:.0f} MB is not achievable for this "
                    f"{self._fmt_time(self.duration)} video at "
                    f"{self._target_height()}p.\n\n"
                    f"Minimum sensible size is about "
                    f"{min_mb:.0f} MB (video floor {floor_kbps} kb/s + "
                    f"audio {self.ab_cb.currentText()}).\n\n"
                    f"Use {min_mb:.0f} MB instead?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if ans == QMessageBox.Yes:
                    self.size_spin.setValue(round(min_mb) + 1)
                    self._update_calc()
                return False

            if not vb:
                QMessageBox.warning(self, "Size too small",
                                    "Target size too small for the audio budget.")
                return False

            # Too large relative to source: shrinking gains nothing.
            if max_mb and target >= max_mb:
                ans = QMessageBox.question(
                    self, "Target larger than source",
                    f"Target {target:.0f} MB is at/above the source size "
                    f"({max_mb:.0f} MB). Re-encoding would only add size or "
                    f"waste time.\n\nContinue anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ans == QMessageBox.No:
                    return False
        return True

    # ---- Run / QProcess --------------------------------------------------- #

    def on_run(self):
        if not self._validate():
            return
        out = self.out_edit.text().strip()
        if os.path.abspath(out) == os.path.abspath(self.in_edit.text().strip()):
            QMessageBox.warning(self, "Same file", "Output must differ from input.")
            return
        self.progress.setValue(0)
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        if self.rb_size.isChecked():
            self.passlog = os.path.splitext(out)[0] + "_2pass"
            self.pass_stage = 1
            self.stage_label = "Pass 1/2"
            self.log.append("\n=== Two-pass: PASS 1 ===")
            self._start(self._build_cmd(out, 1, self.passlog))
        else:
            self.pass_stage = 0
            self.stage_label = "Encoding"
            self.log.append("\n=== Encoding ===")
            self._start(self._build_cmd(out))

    def _start(self, cmd):
        self.log.append(" ".join(shlex.quote(x) for x in cmd))
        self.stage_start = time.monotonic()
        self.last_speed = 0.0
        self.status_lbl.setText(f"{self.stage_label}: starting…")
        self.proc = QProcess(self)
        self.proc.setProgram(cmd[0])
        self.proc.setArguments(cmd[1:])
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.start()

    def _on_stdout(self):
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        encoded_s = None
        for line in data.splitlines():
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    encoded_s = int(line.split("=")[1]) / 1e6
                except Exception:
                    pass
            elif line.startswith("out_time_us="):   # newer ffmpeg key
                try:
                    encoded_s = int(line.split("=")[1]) / 1e6
                except Exception:
                    pass
            elif line.startswith("speed="):
                try:
                    val = line.split("=")[1].strip().rstrip("x")
                    if val and val != "N/A":
                        self.last_speed = float(val)
                except Exception:
                    pass
        if encoded_s is not None and self.duration > 0:
            self._update_progress(encoded_s)

    def _update_progress(self, encoded_s):
        """Update bar + ETA given how many seconds of media are encoded so far."""
        pct = min(100, max(0, int(encoded_s / self.duration * 100)))
        self.progress.setValue(pct)

        elapsed = time.monotonic() - self.stage_start
        remaining_media = max(0.0, self.duration - encoded_s)

        # Prefer ffmpeg's own speed multiplier; fall back to observed throughput.
        speed = self.last_speed
        if speed <= 0 and elapsed > 0 and encoded_s > 0:
            speed = encoded_s / elapsed

        if speed > 0:
            eta = remaining_media / speed
            eta_str = self._fmt_hms(eta)
        else:
            eta_str = "…"

        # For 2-pass, note that a whole second pass still follows pass 1.
        stage = self.stage_label
        self.status_lbl.setText(
            f"{stage}  {pct}%   elapsed {self._fmt_hms(elapsed)}   "
            f"ETA {eta_str}   speed {speed:.2f}x")

    def _on_stderr(self):
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        # ffmpeg progress on stderr too; keep log light
        for line in data.splitlines():
            if line.strip():
                self.log.append(line)
        self.log.moveCursor(self.log.textCursor().End)

    def _on_finished(self, code, status):
        if self.pass_stage == -1:      # user cancelled; on_cancel already tidied up
            return
        if self.remux_running:
            self.remux_running = False
            self.progress.setRange(0, 100)
            self.run_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            inp = self.in_edit.text().strip()
            base, ext = os.path.splitext(inp)
            out = base + ".repaired" + ext
            if code == 0 and os.path.isfile(out):
                self.progress.setValue(100)
                self.log.append(f"\n[REMUX DONE] {out}")
                self.status_lbl.setText("Remux complete.")
                ans = QMessageBox.question(
                    self, "Load repaired file?",
                    f"Repaired file written:\n{out}\n\nLoad it as the new input?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if ans == QMessageBox.Yes:
                    self.in_edit.setText(out)
                    self.on_analyze()
            else:
                self._fail("Remux failed.")
            return
        if self.pass_stage == 1:
            if code != 0:
                self._fail("Pass 1 failed.")
                return
            self.pass_stage = 2
            self.stage_label = "Pass 2/2"
            self.progress.setValue(0)
            self.log.append("\n=== Two-pass: PASS 2 ===")
            self._start(self._build_cmd(self.out_edit.text().strip(),
                                        2, self.passlog))
            return
        # done (single pass or pass 2)
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if code == 0:
            self.progress.setValue(100)
            out = self.out_edit.text().strip()
            sz = os.path.getsize(out) / 1024 / 1024 if os.path.isfile(out) else 0
            self.log.append(f"\n[DONE] {out}  ({sz:.1f} MB)")
            self.status_lbl.setText(f"Done — {sz:.1f} MB in "
                                    f"{self._fmt_hms(time.monotonic()-self.stage_start)}")
            self._cleanup_passlog()
        else:
            self._fail(f"ffmpeg exited with code {code}.")

    def _fail(self, msg):
        self.remux_running = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.log.append("[FAILED] " + msg)
        self.status_lbl.setText("Failed. See log.")
        self._cleanup_passlog()

    def _cleanup_passlog(self):
        if self.passlog:
            for suff in ("-0.log", "-0.log.mbtree", ".log", ".log.mbtree"):
                f = self.passlog + suff
                if os.path.isfile(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            self.passlog = None

    def on_cancel(self):
        # Mark cancelled so the finished handler doesn't auto-start pass 2.
        self.pass_stage = -1
        self.remux_running = False
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.log.append("Cancelled.")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_lbl.setText("Cancelled.")
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._cleanup_passlog()

    # ---- About / Remux ---------------------------------------------------- #

    def on_about(self):
        ff = "detected" if FFMPEG else "NOT found"
        text = (
            f"<h3>{APP_NAME}</h3>"
            "ffmpeg analysis &amp; conversion GUI<br><br>"
            f"<b>Author:</b> {APP_AUTHOR}<br>"
            f"<b>Built with:</b> {APP_AI_CREDIT}<br><br>"
            f"<b>ffmpeg:</b> {ff}"
            + (f"<br><small>{FFMPEG}</small>" if FFMPEG else "")
        )
        box = QMessageBox(self)
        box.setWindowTitle("About")
        box.setTextFormat(Qt.RichText)
        box.setText(text)
        box.exec_()

    def on_remux(self):
        """Rebuild the container losslessly to repair a damaged input."""
        inp = self.in_edit.text().strip()
        if not os.path.isfile(inp):
            QMessageBox.warning(self, "Input", "Load a valid file first.")
            return
        base, ext = os.path.splitext(inp)
        out = base + ".repaired" + ext
        ans = QMessageBox.question(
            self, "Remux / Repair",
            "This rebuilds the container and regenerates timestamps without "
            "re-encoding (fast, lossless), writing:\n\n"
            f"{out}\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans != QMessageBox.Yes:
            return
        cmd = [FFMPEG, "-y", "-err_detect", "ignore_err",
               "-fflags", "+genpts+discardcorrupt", "-i", inp,
               "-map", "0", "-c", "copy", out]
        self.remux_running = True
        self.stage_label = "Remux/Repair"
        self.progress.setRange(0, 0)   # busy indicator (unknown duration mapping)
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.log.append("\n=== Remux/Repair ===")
        self._start(cmd)

    @staticmethod
    def _fmt_time(sec):
        sec = int(sec)
        return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"

    @staticmethod
    def _fmt_hms(sec):
        """Compact H:MM:SS / M:SS for durations and ETAs."""
        sec = int(round(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"


def main():
    app = QApplication(sys.argv)
    w = FFGui()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
