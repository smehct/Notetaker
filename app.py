"""Oberfläche für den lokalen Notetaker. Start: python app.py"""
import os
import queue
import re
import threading
import tkinter.font as tkfont
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

import notetaker
import speakers

SPEAKER_COUNTS = {"Automatisch": -1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6}
MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo"]
LANGUAGES = {"Deutsch": "de", "English": "en", "Français": "fr", "Italiano": "it",
             "Automatisch": ""}
MODES = {"Remote-Meeting": True, "Vor Ort": False}
MODE_HINTS = {
    True: "Teams, Zoom, Meet: Mikrofon und Ton aus dem Kopfhörer. Du bist „Ich“, die anderen „Andere“.",
    False: "Alle sitzen im Raum: nur das Mikrofon nimmt auf. Im Text steht „Raum“.",
}

# Farben nach Wispr Flow: warmes Hellgrau, fast weisse Fläche, Pfirsich, Schwarz, Dunkelgrün
BG, PANEL, GRAY, WHITE = "#F7F5F2", "#FCFBFA", "#F4F2EE", "#FFFFFF"
LINE, TRACK, SEL, HOVER = "#E6E2DB", "#E9E6E1", "#EDEAE5", "#F1EEE9"
INK, MUTED, FAINT = "#1A1A1A", "#6E6B66", "#B5B0A8"
BLACK, BLACK_HOVER = "#141414", "#2E2E2E"
PEACH, PEACH_HOVER, ORANGE = "#FDEBD6", "#FBDDBD", "#EDA05D"
GREEN, MINT = "#0F5C4A", "#CDEDE5"
RED_TEXT = "#B3261E"
SERIF, SANS = "Segoe UI Semibold", "Segoe UI"
SPEAKER_COLORS = ["#B4531A", "#2F5DA8", "#8A3FA0", "#7A6A1E", "#A02B4F", "#2A7A8C", "#6B4F2A"]
LINE_RE = re.compile(r"^\[(\d{2}:\d{2})\] \*\*(.+?):\*\* (.*)$", re.S)


def pill(parent, text, command, width=130, primary=False, danger=False, dark=False):
    if dark:
        fg, hover, color, border = BLACK, BLACK_HOVER, WHITE, 0
    elif primary:
        fg, hover, color, border = PEACH, PEACH_HOVER, INK, 0
    else:
        fg, hover, color, border = WHITE, HOVER, INK, 1
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=36, corner_radius=18,
        fg_color=fg, hover_color=hover, border_width=border, border_color=LINE,
        text_color=RED_TEXT if danger else color, text_color_disabled=FAINT,
        font=ctk.CTkFont(family=SANS, size=13))


def card(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=GRAY, corner_radius=22, **kw)


def font(size, weight="normal", family=SANS):
    return ctk.CTkFont(family=family, size=size, weight=weight)


class SessionRow(ctk.CTkFrame):
    """Eintrag in der Seitenleiste: Titel und darunter Datum, Dauer, Modus."""

    def __init__(self, parent, title, meta, on_click, selected):
        super().__init__(parent, corner_radius=14, fg_color=SEL if selected else "transparent")
        self.selected = selected
        text = title if len(title) <= 26 else title[:25] + "…"
        self.title_label = ctk.CTkLabel(self, text=text, anchor="w", text_color=INK,
                                        font=font(14, "bold"))
        self.meta_label = ctk.CTkLabel(self, text=meta, anchor="w", text_color=MUTED,
                                       font=font(11))
        self.title_label.pack(fill="x", padx=14, pady=(9, 0))
        self.meta_label.pack(fill="x", padx=14, pady=(0, 9))
        for w in (self, self.title_label, self.meta_label):
            w.bind("<Button-1>", lambda e: on_click())
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)

    def _enter(self, _):
        if not self.selected:
            self.configure(fg_color=HOVER)

    def _leave(self, _):
        if not self.selected:
            self.configure(fg_color="transparent")

    def set_title(self, title):
        self.title_label.configure(text=title if len(title) <= 26 else title[:25] + "…")

    def set_selected(self, selected):
        self.selected = selected
        self.configure(fg_color=SEL if selected else "transparent")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.configure(fg_color=BG)
        self.title("Notetaker")
        self.geometry("1120x780")
        self.minsize(1020, 700)

        self.recorder = None
        self.live = None
        self.live_upto = None
        self.busy = False
        self.selected = None
        self.rows = {}
        self.events = queue.Queue()
        self.mic_level = self.system_level = 0.0
        self.settings_open = False
        self.name_colors = {}
        self._closing = False
        self._title_job = None

        self._build()
        self.refresh_sessions()
        self.new_session()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._tick)

    # --- Aufbau -----------------------------------------------------------------

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()

        panel = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=30)
        panel.grid(row=0, column=1, sticky="nsew", padx=(0, 12), pady=12)
        main = ctk.CTkFrame(panel, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=34, pady=(24, 20))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(4, weight=1)

        # Titel
        # Kein textvariable: customtkinter zeigt den Platzhalter sonst nicht an.
        self.title_entry = ctk.CTkEntry(
            main, placeholder_text="Titel des Meetings eintragen", height=48, corner_radius=16,
            border_width=1, border_color=LINE, fg_color=WHITE, text_color=INK,
            placeholder_text_color=FAINT, font=font(20, "bold", SERIF))
        self.title_entry.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.title_entry.bind("<KeyRelease>", lambda e: self._schedule_title_commit())
        self.title_entry.bind("<Return>", lambda e: (self._flush_title(), self.focus()))
        self.title_entry.bind("<FocusOut>", lambda e: self._flush_title())
        self.subtitle = ctk.CTkLabel(main, text="", anchor="w", text_color=MUTED, font=font(12))
        self.subtitle.grid(row=1, column=0, sticky="w", padx=(6, 0), pady=(0, 14))

        # Aufnahme
        rec = card(main)
        rec.grid(row=2, column=0, sticky="ew")
        rec.grid_columnconfigure(1, weight=1)
        self.record_btn = ctk.CTkButton(
            rec, text="Aufnahme starten", width=210, height=56, corner_radius=28,
            font=font(16, "bold"), command=self.toggle_record, text_color_disabled=FAINT)
        self.record_btn.grid(row=0, column=0, rowspan=2, padx=22, pady=(22, 10))
        self._style_record_btn(recording=False)
        self.timer = ctk.CTkLabel(rec, text="00:00", text_color=INK, font=font(34, "bold", SERIF))
        self.timer.grid(row=0, column=1, padx=6, pady=(20, 0), sticky="w")
        self.devices = ctk.CTkLabel(rec, text="Bereit", anchor="w", justify="left",
                                    text_color=MUTED, font=font(11))
        self.devices.grid(row=1, column=1, padx=6, pady=(0, 10), sticky="w")
        meters = ctk.CTkFrame(rec, fg_color="transparent")
        meters.grid(row=0, column=2, rowspan=2, padx=24, sticky="e")
        self.mic_bar = self._meter(meters, "Mikrofon", 0)
        self.system_bar = self._meter(meters, "Systemton", 1)

        self.mode_var = ctk.StringVar(value=next(iter(MODES)))
        self.mode_btn = ctk.CTkSegmentedButton(
            rec, values=list(MODES), variable=self.mode_var, command=self._on_mode,
            fg_color=TRACK, selected_color=WHITE, selected_hover_color=HOVER,
            unselected_color=TRACK, unselected_hover_color=SEL, text_color=INK,
            corner_radius=16, font=font(13))
        self.mode_btn.grid(row=2, column=0, columnspan=2, padx=22, pady=(0, 6), sticky="w")
        self.mode_hint = ctk.CTkLabel(rec, text="", anchor="w", text_color=MUTED, font=font(11))
        self.mode_hint.grid(row=3, column=0, columnspan=3, padx=24, pady=(0, 16), sticky="w")
        self._on_mode()

        # Einstellungen (einklappbar)
        bar = ctk.CTkFrame(main, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", pady=(10, 4))
        self.settings_btn = ctk.CTkButton(
            bar, text="Einstellungen  ▾", width=150, height=30, corner_radius=15,
            fg_color="transparent", hover_color=HOVER, text_color=INK, anchor="w",
            font=font(13, "bold"), command=self.toggle_settings)
        self.settings_btn.pack(side="left")
        self.summary = ctk.CTkLabel(bar, text="", text_color=MUTED, font=font(12))
        self.summary.pack(side="left", padx=8)

        self.settings_frame = card(main)
        self._build_settings(self.settings_frame)
        self.settings_frame.grid(row=3, column=0, sticky="ew", pady=(46, 0))
        self.settings_frame.grid_remove()
        self._update_summary()

        # Transkript
        box = card(main)
        box.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        self.text = ctk.CTkTextbox(box, wrap="word", fg_color="transparent", text_color=INK,
                                   font=font(14), activate_scrollbars=True)
        self.text.pack(fill="both", expand=True, padx=14, pady=14)
        tb = self.text._textbox
        tb.configure(spacing3=4, borderwidth=0, highlightthickness=0, padx=8, pady=4)
        self.f_ts = tkfont.Font(family=SANS, size=10)
        self.f_name = tkfont.Font(family=SANS, size=11, weight="bold")
        self.f_body = tkfont.Font(family=SANS, size=12)
        self.f_hint = tkfont.Font(family=SANS, size=13, slant="italic")
        tb.tag_configure("ts", foreground=FAINT, font=self.f_ts)
        tb.tag_configure("body", foreground=INK, font=self.f_body)
        tb.tag_configure("hint", foreground=MUTED, font=self.f_hint)

        # Status und Aktionen
        bottom = ctk.CTkFrame(main, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        bottom.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bottom, text="", anchor="w", text_color=MUTED, font=font(12))
        self.status.grid(row=0, column=0, sticky="ew")
        self.progress = ctk.CTkProgressBar(bottom, width=180, height=8, corner_radius=4,
                                           progress_color=GREEN, fg_color=TRACK)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, padx=8)
        self.progress.grid_remove()
        actions = ctk.CTkFrame(bottom, fg_color="transparent")
        actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.retranscribe_btn = pill(actions, "Neu transkribieren", self.retranscribe, 140)
        self.detect_btn = pill(actions, "Sprecher erkennen", self.detect_speakers, 140)
        self.names_btn = pill(actions, "Sprecher benennen", self.name_speakers, 140)
        self.copy_btn = pill(actions, "Kopieren", self.copy_text, 90)
        self.folder_btn = pill(actions, "Ordner", self.open_folder, 80)
        self.delete_btn = pill(actions, "Löschen", self.delete_session, 90, danger=True)
        for btn in self.action_buttons:
            btn.pack(side="left", padx=(8, 0))
        self._update_action_buttons()

    @property
    def action_buttons(self):
        return (self.retranscribe_btn, self.detect_btn, self.names_btn,
                self.copy_btn, self.folder_btn, self.delete_btn)

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=290, corner_radius=0, fg_color=BG)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(4, weight=1)
        ctk.CTkLabel(side, text="Notetaker", text_color=INK, font=font(28, "bold", SERIF)
                     ).grid(row=0, column=0, padx=24, pady=(28, 0), sticky="w")
        ctk.CTkLabel(side, text="Lokal, privat, kostenlos", text_color=MUTED, font=font(12)
                     ).grid(row=1, column=0, padx=26, pady=(0, 18), sticky="w")
        pill(side, "◉  Neue Aufnahme", self.new_session, 242, primary=True
             ).grid(row=2, column=0, padx=24, pady=(0, 20), sticky="w")
        ctk.CTkLabel(side, text="AUFNAHMEN", text_color=MUTED, font=font(11, "bold")
                     ).grid(row=3, column=0, padx=26, pady=(0, 6), sticky="w")
        self.session_list = ctk.CTkScrollableFrame(side, fg_color="transparent")
        self.session_list.grid(row=4, column=0, sticky="nsew", padx=(14, 8), pady=(0, 14))

    def _build_settings(self, parent):
        def menu(parent, values, variable, width):
            return ctk.CTkOptionMenu(
                parent, values=values, variable=variable, width=width, corner_radius=16,
                fg_color=WHITE, button_color=WHITE, button_hover_color=HOVER, text_color=INK,
                dropdown_fg_color=WHITE, dropdown_text_color=INK, dropdown_hover_color=PEACH,
                font=font(13), dropdown_font=font(13))

        def check(parent, text, variable):
            return ctk.CTkCheckBox(
                parent, text=text, variable=variable, text_color=INK, font=font(13),
                fg_color=INK, hover_color="#333333", border_color=FAINT, corner_radius=6,
                checkmark_color=WHITE)

        row1 = ctk.CTkFrame(parent, fg_color="transparent")
        row1.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkLabel(row1, text="Modell", text_color=MUTED, font=font(12)).pack(side="left")
        self.model_var = ctk.StringVar(value="small")
        self.model_menu = menu(row1, MODELS, self.model_var, 140)
        self.model_menu.pack(side="left", padx=(8, 22))
        ctk.CTkLabel(row1, text="Sprache", text_color=MUTED, font=font(12)).pack(side="left")
        self.lang_var = ctk.StringVar(value="Deutsch")
        self.lang_menu = menu(row1, list(LANGUAGES), self.lang_var, 140)
        self.lang_menu.pack(side="left", padx=(8, 22))
        self.live_var = ctk.BooleanVar(value=True)
        check(row1, "Schon während der Aufnahme transkribieren", self.live_var).pack(side="left")

        row2 = ctk.CTkFrame(parent, fg_color="transparent")
        row2.pack(fill="x", padx=18, pady=(14, 16))
        self.diar_var = ctk.BooleanVar(value=True)
        check(row2, "Sprecher unterscheiden (nach der Aufnahme)", self.diar_var
              ).pack(side="left", padx=(0, 22))
        ctk.CTkLabel(row2, text="Anzahl Sprecher", text_color=MUTED, font=font(12)).pack(side="left")
        self.spk_var = ctk.StringVar(value="Automatisch")
        menu(row2, list(SPEAKER_COUNTS), self.spk_var, 130).pack(side="left", padx=8)

        for var in (self.model_var, self.lang_var, self.live_var, self.diar_var, self.spk_var):
            var.trace_add("write", lambda *a: self._update_summary())

    def _meter(self, parent, label, row):
        ctk.CTkLabel(parent, text=label, width=76, anchor="w", text_color=MUTED, font=font(12)
                     ).grid(row=row, column=0, pady=4)
        bar = ctk.CTkProgressBar(parent, width=190, height=8, corner_radius=4,
                                 progress_color=GREEN, fg_color=TRACK)
        bar.set(0)
        bar.grid(row=row, column=1, padx=(8, 0), pady=4)
        return bar

    def _style_record_btn(self, recording):
        if recording:
            self.record_btn.configure(text="■  Aufnahme beenden", fg_color=PEACH,
                                      hover_color=PEACH_HOVER, text_color=INK,
                                      border_width=1, border_color=ORANGE)
        else:
            self.record_btn.configure(text="Aufnahme starten", fg_color=BLACK,
                                      hover_color=BLACK_HOVER, text_color=WHITE, border_width=0)

    def _on_mode(self, _=None):
        self.mode_hint.configure(text=MODE_HINTS[MODES[self.mode_var.get()]])

    def toggle_settings(self):
        self.settings_open = not self.settings_open
        if self.settings_open:
            self.settings_frame.grid()
            self.settings_btn.configure(text="Einstellungen  ▴")
        else:
            self.settings_frame.grid_remove()
            self.settings_btn.configure(text="Einstellungen  ▾")

    def _update_summary(self):
        parts = [self.model_var.get(), self.lang_var.get(),
                 "live" if self.live_var.get() else "nach der Aufnahme"]
        if self.diar_var.get():
            parts.append("mit Sprechertrennung")
        self.summary.configure(text="  ·  ".join(parts))

    # --- Hilfsfunktionen --------------------------------------------------------

    def post(self, fn):
        """Aus Hintergrund-Threads: führt fn im UI-Thread aus."""
        self.events.put(fn)

    def set_status(self, msg):
        self.status.configure(text=msg)

    def _speaker_tag(self, who):
        if who not in self.name_colors:
            n = len(self.name_colors)
            color = GREEN if who == "Ich" else SPEAKER_COLORS[n % len(SPEAKER_COLORS)]
            tag = f"spk{n}"
            self.text._textbox.tag_configure(tag, foreground=color, font=self.f_name)
            self.name_colors[who] = tag
        return self.name_colors[who]

    def _show_hint(self, msg):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", msg, "hint")
        self.text.configure(state="disabled")

    def _show_transcript(self, md, scroll_end=False):
        paragraphs = [p for p in md.strip().split("\n\n") if p.strip()]
        if not paragraphs:
            self._show_hint("Keine Sprache erkannt.")
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for p in paragraphs:
            m = LINE_RE.match(p.strip())
            if m:
                ts, who, body = m.groups()
                self.text.insert("end", ts + "   ", "ts")
                self.text.insert("end", who, self._speaker_tag(who))
                self.text.insert("end", "   " + body + "\n\n", "body")
            else:
                self.text.insert("end", p + "\n\n", "body")
        self.text.configure(state="disabled")
        if scroll_end:
            self.text.see("end")

    def _update_action_buttons(self):
        has = self.selected is not None and not self.busy and self.recorder is None
        for btn in self.action_buttons:
            btn.configure(state="normal" if has else "disabled")

    def _tick(self):
        while True:
            try:
                self.events.get_nowait()()
            except queue.Empty:
                break
        if self.recorder:
            elapsed = int(self.recorder.elapsed())
            self.timer.configure(text=f"{elapsed // 60:02d}:{elapsed % 60:02d}")
            mic, system = self.recorder.take_levels()
            self.mic_level = max(mic, self.mic_level * 0.75)
            self.system_level = max(system, self.system_level * 0.75)
            self.mic_bar.set(min(1.0, self.mic_level ** 0.5))
            self.system_bar.set(min(1.0, self.system_level ** 0.5))
            if self.live and self.live_upto is not None:
                done = int(self.live_upto)
                lag = max(0, elapsed - done)
                self.set_status(f"Live: transkribiert bis {done // 60:02d}:{done % 60:02d}"
                                + (f"  (Rückstand {lag // 60} min {lag % 60} s)" if lag > 60 else ""))
            elif self.live and elapsed > 5 and not self.status.cget("text").startswith("Lade"):
                self.set_status("Live: erster Abschnitt wird transkribiert ...")
        self.after(100, self._tick)

    # --- Aufnahmen-Liste und Titel ----------------------------------------------

    @staticmethod
    def _session_meta(d):
        try:
            label = datetime.strptime(d.name, "%Y-%m-%d_%H-%M-%S").strftime("%d.%m.%Y · %H:%M")
        except ValueError:
            label = d.name
        mic = d / "mic.wav"
        if mic.exists():
            try:
                minutes = notetaker.wav_samples(mic) / notetaker.wav_rate(mic) / 60
                label += f" · {minutes:.0f} min" if minutes >= 1 else " · < 1 min"
            except OSError:
                pass
            if not (d / "system.wav").exists():
                label += " · vor Ort"
        if not (d / "transcript.md").exists():
            label += " · ohne Text"
        return label

    def refresh_sessions(self, select=None):
        for row in self.rows.values():
            row.destroy()
        self.rows = {}
        dirs = []
        if notetaker.SESSIONS_DIR.exists():
            dirs = sorted((d for d in notetaker.SESSIONS_DIR.iterdir() if d.is_dir()),
                          reverse=True)
        for d in dirs:
            row = SessionRow(self.session_list, notetaker.get_title(d) or "Ohne Titel",
                             self._session_meta(d), lambda d=d: self.select_session(d),
                             selected=(d == self.selected))
            row.pack(fill="x", pady=2)
            self.rows[d] = row
        if select:
            self.select_session(select)
        elif self.selected not in self.rows:
            self.selected = None

    def _get_title(self):
        return self.title_entry.get().strip()

    def _set_title_text(self, text):
        self.title_entry.delete(0, "end")
        if text:
            self.title_entry.insert(0, text)

    def _schedule_title_commit(self):
        """Speichert den Titel kurz nach der letzten Eingabe. Fokus-Wechsel allein
        genügt nicht, ein Klick auf die Seitenleiste nimmt dem Feld den Fokus nicht."""
        if self._title_job:
            self.after_cancel(self._title_job)
        self._title_job = self.after(500, self.commit_title)

    def _flush_title(self):
        if self._title_job:
            self.after_cancel(self._title_job)
        self.commit_title()

    def commit_title(self):
        self._title_job = None
        if self._closing:
            return
        target = self.recorder.session_dir if self.recorder else self.selected
        if not target:
            return
        title = self._get_title()
        if title != notetaker.get_title(target):
            notetaker.set_title(target, title)
            if target in self.rows:
                self.rows[target].set_title(title or "Ohne Titel")

    def new_session(self):
        """Leerer Ausgangszustand: Titel eintragen und Aufnahme starten."""
        if self.recorder or self.busy:
            return
        self._flush_title()
        self.selected = None
        self._set_title_text("")
        self.subtitle.configure(text="Neue Aufnahme  ·  Titel oben eintragen (optional)")
        for row in self.rows.values():
            row.set_selected(False)
        self._show_hint("Gib oben einen Titel ein und starte die Aufnahme.\n"
                        "Der Text erscheint hier, sobald er transkribiert ist.")
        self.timer.configure(text="00:00")
        self._update_action_buttons()

    def select_session(self, d):
        if self.recorder or self.busy:
            return
        self._flush_title()
        self.selected = d
        self._set_title_text(notetaker.get_title(d))
        self.subtitle.configure(text=self._session_meta(d))
        for path, row in self.rows.items():
            row.set_selected(path == d)
        transcript = d / "transcript.md"
        if transcript.exists():
            self._show_transcript(transcript.read_text(encoding="utf-8"))
        else:
            self._show_hint("Noch nicht transkribiert.\nMit „Neu transkribieren“ starten.")
        self._update_action_buttons()

    def delete_session(self):
        d = self.selected
        if not d or self.busy or self.recorder:
            return
        name = notetaker.get_title(d) or self._session_meta(d)
        if not messagebox.askyesno(
                "Aufnahme löschen",
                f"„{name}“ endgültig löschen?\n\nAudio, Text und Sprecherdaten gehen verloren.",
                icon="warning", parent=self):
            return
        try:
            notetaker.delete_session(d)
        except Exception as e:
            self.set_status(f"Löschen fehlgeschlagen: {e}")
            return
        self.selected = None
        self.refresh_sessions()
        self.new_session()
        self.set_status("Aufnahme gelöscht.")

    # --- Aufnahme ---------------------------------------------------------------

    def toggle_record(self):
        if self.recorder:
            self.stop_recording()
        elif not self.busy:
            self.start_recording()

    def start_recording(self):
        if self.selected:
            self.new_session()
        title = self._get_title()
        try:
            self.recorder = notetaker.Recorder(with_system=MODES[self.mode_var.get()])
        except Exception as e:
            self.set_status(f"Aufnahme nicht möglich: {e}")
            return
        rec = self.recorder
        notetaker.set_title(rec.session_dir, title)
        self._style_record_btn(recording=True)
        for widget in (self.mode_btn, self.model_menu, self.lang_menu):
            widget.configure(state="disabled")
        self.devices.configure(text="\n".join(n for n in (rec.mic_name, rec.system_name) if n))
        self.subtitle.configure(text="Aufnahme läuft")
        self.live = None
        self.live_upto = None
        if self.live_var.get():
            self.live = notetaker.LiveTranscriber(
                rec.session_dir, rec.tracks, self.model_var.get(), LANGUAGES[self.lang_var.get()],
                on_update=self._on_live_update,
                on_progress=lambda f: self.post(lambda: self.progress.set(f)),
                log=lambda msg: self.post(lambda: self.set_status(msg)))
            self.live.start()
            self.set_status("Aufnahme läuft, Live-Transkription startet ...")
            self._show_hint("Der Text erscheint hier, sobald die ersten 25–30 Sekunden "
                            "transkribiert sind ...")
        else:
            self.set_status("Aufnahme läuft.")
            self._show_hint("Aufnahme läuft ...")
        self.refresh_sessions()
        self._update_action_buttons()

    def _on_live_update(self, text, upto):
        """Aus dem Live-Thread: neuen Transkripttext anzeigen."""
        def show():
            self.live_upto = upto
            self._show_transcript(text, scroll_end=True)
        self.post(show)

    def stop_recording(self):
        self.commit_title()
        rec, self.recorder = self.recorder, None
        live, self.live = self.live, None
        self.busy = True
        self.record_btn.configure(text="Speichere ...", state="disabled",
                                  fg_color=HOVER, text_color=MUTED)
        self.mic_bar.set(0)
        self.system_bar.set(0)
        session_dir = rec.session_dir
        diarize = self.diar_var.get()
        num_speakers = SPEAKER_COUNTS[self.spk_var.get()]

        def work():
            try:
                rec.stop()
                if live:
                    self.post(lambda: (self.set_status("Transkribiere den Rest ..."),
                                       self.progress.set(0), self.progress.grid()))
                    live.finish()
                    if diarize:
                        self._run_diarization(session_dir, num_speakers)
                    self.post(lambda: self.set_status("Fertig."))
                else:
                    self.post(lambda: self.set_status(
                        "Aufnahme gespeichert. Mit „Neu transkribieren“ den Text erzeugen."))
            except Exception as e:
                self.post(lambda: self.set_status(
                    f"Transkription fehlgeschlagen: {e}. Mit „Neu transkribieren“ wiederholen."))
            self.post(lambda: self._finish(session_dir))

        threading.Thread(target=work, daemon=True).start()

    def _finish(self, session_dir):
        self.busy = False
        for widget in (self.model_menu, self.lang_menu):
            widget.configure(state="normal")
        self.progress.grid_remove()
        self.record_btn.configure(state="normal")
        self._style_record_btn(recording=False)
        self.devices.configure(text="Bereit")
        self.mode_btn.configure(state="normal")
        self.refresh_sessions(select=session_dir)

    # --- Transkription und Sprecher ---------------------------------------------

    def _run_transcription(self, session_dir, model, language):
        self.post(lambda: (self.progress.set(0), self.progress.grid()))
        notetaker.transcribe(
            session_dir, model, language,
            log=lambda msg: self.post(lambda: self.set_status(msg)),
            progress=lambda f: self.post(lambda: self.progress.set(f)))
        self.post(lambda: self.set_status("Fertig."))

    def _run_diarization(self, session_dir, num_speakers):
        self.post(lambda: (self.progress.set(0), self.progress.grid()))
        notetaker.diarize(
            session_dir, num_speakers,
            log=lambda msg: self.post(lambda: self.set_status(msg)),
            progress=lambda f: self.post(lambda: self.progress.set(f)))

    def _run_in_background(self, job, error_prefix):
        """Führt job() für die gewählte Sitzung aus und sperrt währenddessen die Oberfläche."""
        session_dir = self.selected
        self.busy = True
        self.record_btn.configure(state="disabled")
        self._update_action_buttons()

        def work():
            try:
                job(session_dir)
            except Exception as e:
                self.post(lambda: self.set_status(f"{error_prefix}: {e}"))
            self.post(lambda: self._finish(session_dir))

        threading.Thread(target=work, daemon=True).start()

    def retranscribe(self):
        if not self.selected or self.busy or self.recorder:
            return
        model, language = self.model_var.get(), LANGUAGES[self.lang_var.get()]
        self._run_in_background(lambda d: self._run_transcription(d, model, language),
                                "Transkription fehlgeschlagen")

    def detect_speakers(self):
        if not self.selected or self.busy or self.recorder:
            return
        num_speakers = SPEAKER_COUNTS[self.spk_var.get()]
        self._run_in_background(lambda d: self._run_diarization(d, num_speakers),
                                "Sprechertrennung fehlgeschlagen")

    def name_speakers(self):
        if not self.selected or self.busy or self.recorder:
            return
        session_dir = self.selected
        info = speakers.speaker_overview(session_dir)
        if not info:
            self.set_status("Keine unterscheidbaren Sprecher. Zuerst „Sprecher erkennen“ ausführen.")
            return

        dlg = ctk.CTkToplevel(self, fg_color=BG)
        dlg.title("Sprecher benennen")
        dlg.geometry("580x%d" % min(150 + 84 * len(info), 680))
        dlg.transient(self)
        dlg.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dlg, text="Wer ist wer?", text_color=INK, font=font(22, "bold", SERIF)
                     ).grid(row=0, column=0, columnspan=2, padx=22, pady=(20, 0), sticky="w")
        ctk.CTkLabel(dlg, text="Leer lassen, um „Sprecher N“ zu behalten.", text_color=MUTED,
                     font=font(12)).grid(row=1, column=0, columnspan=2, padx=22, pady=(0, 6),
                                         sticky="w")
        entries = {}
        for i, item in enumerate(info):
            row = 2 + 2 * i
            ctk.CTkLabel(dlg, text=item["default"], text_color=INK, font=font(13, "bold")
                         ).grid(row=row, column=0, padx=(22, 10), pady=(10, 0), sticky="w")
            entry = ctk.CTkEntry(dlg, placeholder_text=item["default"], height=34,
                                 corner_radius=17, fg_color=WHITE, border_color=LINE,
                                 text_color=INK, font=font(13))
            if item["name"]:
                entry.insert(0, item["name"])
            entry.grid(row=row, column=1, padx=(0, 22), pady=(10, 0), sticky="ew")
            entries[item["key"]] = entry
            ctk.CTkLabel(dlg, text=item["sample"], anchor="w", justify="left", wraplength=520,
                         text_color=MUTED, font=font(11)
                         ).grid(row=row + 1, column=0, columnspan=2, padx=22, sticky="w")

        def save():
            speakers.set_names(session_dir, {k: e.get() for k, e in entries.items()})
            speakers.render_transcript(session_dir)
            dlg.destroy()
            self.select_session(session_dir)

        buttons = ctk.CTkFrame(dlg, fg_color="transparent")
        buttons.grid(row=3 + 2 * len(info), column=0, columnspan=2, padx=22, pady=18, sticky="e")
        pill(buttons, "Abbrechen", dlg.destroy, 100).pack(side="left", padx=8)
        pill(buttons, "Speichern", save, 100, dark=True).pack(side="left")
        dlg.after(200, lambda: (dlg.grab_set(), dlg.focus()))

    # --- Aktionen ---------------------------------------------------------------

    def copy_text(self):
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end").strip())
        self.set_status("In die Zwischenablage kopiert.")

    def open_folder(self):
        if self.selected:
            os.startfile(self.selected)

    def on_close(self):
        self._closing = True
        if self.recorder:
            rec, self.recorder = self.recorder, None
            notetaker.set_title(rec.session_dir, self._get_title())
            rec.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
