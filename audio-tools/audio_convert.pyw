#!/usr/bin/env python3
# audio_convert.pyw — Standalone audio converter + optional Whisper transcribe
# - Pick file(s) or folder -> convert to WAV (16k mono for whisper) or other format
# - Output folder picker, progress bar, log
# - Optional whisper.cpp transcribe (CUDA) -> .txt alongside
# - Tray minimize optional (pystray if installed)
# Usage: double-click (pythonw) or: python audio_convert.pyw
import os, sys, threading, subprocess, pathlib, shutil
from pathlib import Path

# --- paths (auto-detect) ---
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
WHISPER_CLI = Path("I:/whisper.cpp/build_cuda7/bin/whisper-cli.exe")
WHISPER_MODEL = Path("I:/whisper.cpp/models/ggml-base.bin")
WHISPER_MODEL_SMALL = Path("I:/whisper.cpp/models/ggml-small.bin")

# --- try tray ---
_TRAY = False
try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY = True
except: pass

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SUPPORTED_IN = {".m4a",".mp3",".mp4",".wav",".flac",".ogg",".wma",".aac",".opus",".webm",".m4a",".mkv",".mov",".avi"}
CONVERT_PRESETS = {
    "WAV 16k mono (Whisper)": {"ext":"wav", "args":["-ar","16000","-ac","1","-c:a","pcm_s16le"]},
    "WAV 44.1k stereo": {"ext":"wav", "args":["-ar","44100","-ac","2","-c:a","pcm_s16le"]},
    "MP3 320k": {"ext":"mp3", "args":["-c:a","libmp3lame","-b:a","320k"]},
    "M4A AAC 128k": {"ext":"m4a", "args":["-c:a","aac","-b:a","128k"]},
}

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Audio Convert — DWGLS")
        self.geometry("720x560")
        try: self.iconbitmap(default="")
        except: pass
        self.files = []  # list[Path]
        self.out_dir = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.preset = tk.StringVar(value="WAV 16k mono (Whisper)")
        self.do_transcribe = tk.BooleanVar(value=False)
        self.whisper_model = tk.StringVar(value=str(WHISPER_MODEL))
        self.lang = tk.StringVar(value="en")
        self._build()
        self._tray = None
        if _TRAY:
            self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        # top toolbar
        bar = ttk.Frame(self); bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="+ Files", command=self.add_files).pack(side="left", padx=2)
        ttk.Button(bar, text="+ Folder", command=self.add_folder).pack(side="left", padx=2)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=8)
        ttk.Button(bar, text="Open Output", command=self.open_output).pack(side="right", padx=2)

        # list
        self.listbox = tk.Listbox(self, height=12, selectmode="extended")
        self.listbox.pack(fill="both", expand=True, padx=8, pady=4)
        # drag-drop (tkdnd optional)
        try:
            from tkinterdnd2 import DND_FILES
            self.drop_target_register(DND_FILES)
            self.dnd_bind('<<Drop>>', self._on_drop)
        except: pass

        # output row
        out = ttk.Frame(self); out.pack(fill="x", padx=8, pady=4)
        ttk.Label(out, text="Output:").pack(side="left")
        ttk.Entry(out, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Browse…", command=self.pick_out).pack(side="left")

        # options
        opt = ttk.Frame(self); opt.pack(fill="x", padx=8, pady=4)
        ttk.Label(opt, text="Format:").pack(side="left")
        ttk.Combobox(opt, textvariable=self.preset, values=list(CONVERT_PRESETS.keys()), state="readonly", width=24).pack(side="left", padx=6)
        ttk.Checkbutton(opt, text="Transcribe (whisper.cpp → .txt)", variable=self.do_transcribe, command=self._toggle_whisper).pack(side="left", padx=12)

        wh = ttk.Frame(self); wh.pack(fill="x", padx=8, pady=2)
        self.wh_frame = wh
        ttk.Label(wh, text="Whisper model:").pack(side="left")
        ttk.Entry(wh, textvariable=self.whisper_model, width=38).pack(side="left", padx=6)
        ttk.Button(wh, text="…", width=3, command=self.pick_model).pack(side="left")
        ttk.Label(wh, text="Lang:").pack(side="left", padx=(10,2))
        ttk.Entry(wh, textvariable=self.lang, width=6).pack(side="left")
        self._toggle_whisper()

        # progress
        self.pbar = ttk.Progressbar(self, mode="determinate")
        self.pbar.pack(fill="x", padx=8, pady=6)
        self.status = tk.StringVar(value="Ready — drop files/folder here")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8)

        # log
        self.log = tk.Text(self, height=8, wrap="word", bg="#111", fg="#0f0", font=("Consolas",8))
        self.log.pack(fill="both", padx=8, pady=4)

        # convert button
        self.btn = ttk.Button(self, text="▶ CONVERT", command=self.convert)
        self.btn.pack(pady=8, ipadx=20, ipady=4)

    def _toggle_whisper(self):
        st = "normal" if self.do_transcribe.get() else "disabled"
        for c in self.wh_frame.winfo_children():
            try: c.configure(state=st)
            except: pass

    def _log(self, s):
        self.log.insert("end", s+"\n"); self.log.see("end"); self.update_idletasks()

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Select audio files", filetypes=[("Audio","*.m4a *.mp3 *.mp4 *.wav *.flac *.ogg *.wma *.aac *.opus *.webm"),("All","*.*")])
        for p in paths:
            pp = Path(p)
            if pp not in self.files:
                self.files.append(pp); self.listbox.insert("end", str(pp))
        self.status.set(f"{len(self.files)} file(s)")

    def add_folder(self):
        d = filedialog.askdirectory(title="Select folder")
        if not d: return
        root = Path(d)
        added=0
        for p in root.rglob("*"):
            if p.suffix.lower() in SUPPORTED_IN and p.is_file():
                if p not in self.files:
                    self.files.append(p); self.listbox.insert("end", str(p)); added+=1
        self.status.set(f"Added {added} from folder — total {len(self.files)}")

    def _on_drop(self, e):
        for p in self.tk.splitlist(e.data):
            pp = Path(p.strip("{}"))
            if pp.is_dir():
                for f in pp.rglob("*"):
                    if f.suffix.lower() in SUPPORTED_IN and f not in self.files:
                        self.files.append(f); self.listbox.insert("end", str(f))
            elif pp.is_file() and pp not in self.files:
                self.files.append(pp); self.listbox.insert("end", str(pp))
        self.status.set(f"{len(self.files)} file(s)")

    def clear(self):
        self.files.clear(); self.listbox.delete(0,"end"); self.status.set("Cleared")

    def pick_out(self):
        d=filedialog.askdirectory(title="Output folder")
        if d: self.out_dir.set(d)

    def pick_model(self):
        p=filedialog.askopenfilename(title="Whisper ggml model", filetypes=[("ggml","*.bin"),("All","*.*")])
        if p: self.whisper_model.set(p)

    def open_output(self):
        d=Path(self.out_dir.get())
        if d.exists(): os.startfile(d)  # type: ignore
        else: messagebox.showwarning("Not found", str(d))

    def convert(self):
        if not self.files:
            messagebox.showinfo("No files", "Add files or folder first"); return
        out = Path(self.out_dir.get())
        out.mkdir(parents=True, exist_ok=True)
        preset = CONVERT_PRESETS[self.preset.get()]
        self.btn.configure(state="disabled")
        self.pbar.configure(maximum=len(self.files), value=0)
        threading.Thread(target=self._worker, args=(out, preset), daemon=True).start()

    def _worker(self, out: Path, preset):
        ok=fail=0
        for i, src in enumerate(list(self.files),1):
            self.status.set(f"[{i}/{len(self.files)}] {src.name}…")
            try:
                dst = out / f"{src.stem}.{preset['ext']}"
                # avoid overwrite: add _1
                if dst.exists():
                    n=1
                    while (out / f"{src.stem}_{n}.{preset['ext']}").exists(): n+=1
                    dst = out / f"{src.stem}_{n}.{preset['ext']}"
                cmd = [FFMPEG, "-y", "-i", str(src)] + preset["args"] + [str(dst)]
                self._log(f"$ {' '.join(cmd)}")
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
                if r.returncode!=0:
                    self._log(f"  ffmpeg ERR: {r.stderr[:600]}")
                    fail+=1
                else:
                    self._log(f"  → {dst.name} ({dst.stat().st_size//1024} KB)")
                    ok+=1
                    if self.do_transcribe.get():
                        self._transcribe(dst)
            except Exception as e:
                self._log(f"  ERR {src.name}: {e}"); fail+=1
            self.pbar.configure(value=i)
        self.status.set(f"Done — {ok} ok, {fail} fail → {out}")
        self._log(f"\nDone {ok} ok, {fail} fail")
        self.btn.configure(state="normal")
        if fail==0:
            try: os.startfile(out)  # type: ignore
            except: pass

    def _transcribe(self, wav: Path):
        cli = Path(self.whisper_model.get())
        if not cli.exists() if False else not WHISPER_CLI.exists():
            self._log("  whisper-cli not found, skipping transcribe"); return
        # whisper.cpp expects wav 16k mono — our preset already is, else convert temp
        model = self.whisper_model.get().strip() or str(WHISPER_MODEL)
        lang = self.lang.get().strip() or "en"
        out_base = str(wav.with_suffix(""))  # will add .txt
        cmd = [str(WHISPER_CLI), "-m", model, "-f", str(wav), "--output-txt", "--output-file", out_base, "-t","8","-l",lang]
        self._log(f"  whisper: {' '.join(cmd)}")
        try:
            r=subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            txt = wav.with_suffix(".txt")
            # whisper outputs to out_base.txt
            alt = Path(out_base+".txt")
            if alt.exists():
                self._log(f"  txt → {alt.name} ({alt.stat().st_size//1024} KB)")
            elif txt.exists():
                self._log(f"  txt → {txt.name}")
            else:
                self._log(f"  whisper no txt, stderr: {r.stderr[:400]}")
        except Exception as e:
            self._log(f"  whisper err: {e}")

    def _on_close(self):
        # minimize to tray instead of quit if pystray available
        if _TRAY and self._tray is None:
            self.withdraw()
            self._create_tray()
        else:
            self.destroy()

    def _create_tray(self):
        import pystray
        from PIL import Image, ImageDraw
        img = Image.new("RGB",(64,64),"#0a84ff")
        d=ImageDraw.Draw(img); d.ellipse((16,16,48,48), fill="white")
        def _show(icon, item): icon.stop(); self.deiconify()
        def _quit(icon, item): icon.stop(); self.destroy()
        menu=pystray.Menu(pystray.MenuItem("Show", _show), pystray.MenuItem("Quit", _quit))
        self._tray=pystray.Icon("audio_convert", img, "Audio Convert", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()
        self._log("Minimized to tray — click tray icon to restore")

if __name__=="__main__":
    # ensure ffmpeg exists
    if not shutil.which("ffmpeg"):
        # try common Hermes ffmpeg?
        pass
    app=App()
    app.mainloop()
