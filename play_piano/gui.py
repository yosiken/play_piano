"""tkinter GUI: 曲とピアニストを選んで再生し、つまみで表現をリアルタイムに変える。"""
from __future__ import annotations

import copy
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .engine import AudioEngine
from .expression import PARAM_INFO, PRESETS, Params, Performer
from .acoustics import ROOMS
from .sampler import SFZBank, find_sfz
from . import score as score_mod
from .score import BUILTIN, list_piece_files, load_midi, load_piece
from .shuffle import ShufflePicker, estimate_seconds

BLACK = {1, 3, 6, 8, 10}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Virtual Pianist")
        root.geometry("1060x800")
        root.minsize(900, 560)

        self.engine = AudioEngine()
        # 音源: sounds/ にSFZがあれば録音ピアノを既定にする
        self.instruments = {"合成ピアノ": None}
        sfz = find_sfz()
        if sfz:
            try:
                bank = SFZBank(sfz)
                self.instruments = {f"{bank.name}（録音）": bank, "合成ピアノ": None}
            except Exception as e:  # noqa: BLE001
                print("SFZの読み込みに失敗:", e)
        self.engine.sampler = next(iter(self.instruments.values()))
        self.engine.start()
        self.params = Params()
        self.performer: Performer | None = None
        # 表示名 → 読み込み関数(選ばれたときに読み込む)
        self.catalog = {}
        for f in BUILTIN.values():
            sc = f()
            self.catalog[sc.title] = (lambda sc=sc: sc)
        for name, path in list_piece_files():
            self.catalog[name] = (lambda path=path: load_piece(path))
        self.loaded = {}
        self.score = self._get_score(next(iter(self.catalog)))
        self.vars: dict[str, tk.DoubleVar] = {}
        self.pressed: dict[int, int] = {}
        self._preparing = False
        self._after_prepared = None  # 準備完了後に呼ぶ処理(おまかせ連続演奏で使う)
        # 裏のスレッドから画面を操作しないよう、依頼をキューで受け渡して _poll で実行する
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.shuffle: ShufflePicker | None = None
        self._shuffle_token = 0      # 古い「次の曲へ」の予約を無効にするための番号

        self._build()
        self.preset_box.current(list(PRESETS).index("ルービンシュタイン風（気品ある歌）"))
        self._apply_preset(self.preset_box.get())
        self._prepare_async()
        self._poll()
        root.protocol("WM_DELETE_WINDOW", self._close)
        if score_mod.CATALOG_ERROR:
            root.after(200, lambda: messagebox.showwarning("曲の一覧", score_mod.CATALOG_ERROR))

    # ---- 画面 ----
    def _build(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="曲").grid(row=0, column=0, sticky="w")
        self.piece_box = ttk.Combobox(top, state="readonly", width=46, height=20, values=list(self.catalog))
        self.piece_box.current(0)
        self.piece_box.grid(row=0, column=1, padx=6)
        self.piece_box.bind("<<ComboboxSelected>>", lambda e: self._select_piece())
        files = ttk.Frame(top)
        files.grid(row=0, column=2, padx=4)
        ttk.Button(files, text="MIDIを開く…", command=self._open_midi).pack(side="left")
        self.url_btn = ttk.Button(files, text="URLから採譜…", command=self._transcribe_dialog)
        self.url_btn.pack(side="left", padx=(4, 0))

        ttk.Label(top, text="ピアニスト").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.preset_box = ttk.Combobox(top, state="readonly", width=46, values=list(PRESETS))
        self.preset_box.grid(row=1, column=1, padx=6, pady=(8, 0))
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self._apply_preset(self.preset_box.get()))

        self.play_btn = ttk.Button(top, text="▶ 再生", command=self._play)
        self.play_btn.grid(row=0, column=3, rowspan=2, padx=(16, 4), ipady=8)
        ttk.Button(top, text="■ 停止", command=self._stop).grid(row=0, column=4, rowspan=2, padx=4, ipady=8)

        # おまかせ連続演奏
        auto = ttk.Frame(top)
        auto.grid(row=2, column=0, columnspan=5, sticky="w", pady=(10, 0))
        self.shuffle_btn = ttk.Button(auto, text="🔀 おまかせ連続演奏", command=self._toggle_shuffle)
        self.shuffle_btn.pack(side="left")
        self.skip_btn = ttk.Button(auto, text="⏭ 次へ", command=self._shuffle_skip)
        self.skip_btn.pack(side="left", padx=(6, 16))
        self.skip_btn.state(["disabled"])
        ttk.Label(auto, text="長さの上限").pack(side="left")
        self.max_len = ttk.Combobox(auto, state="readonly", width=6, values=["5分", "12分", "20分", "なし"])
        self.max_len.set("12分")
        self.max_len.pack(side="left", padx=(4, 16))
        self.skip_plain = tk.BooleanVar(value=True)
        ttk.Checkbutton(auto, text="「機械的」は選ばない", variable=self.skip_plain).pack(side="left")

        self.desc = ttk.Label(top, text="", wraplength=900, foreground="#555")
        self.desc.grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self.status = ttk.Label(top, text="", foreground="#246")
        self.status.grid(row=4, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self.tstatus = ttk.Label(top, text="", foreground="#642")  # URLからの採譜の進み具合
        self.tstatus.grid(row=5, column=0, columnspan=5, sticky="w")

        sliders = ttk.LabelFrame(self.root, text="表現（再生中も変更できます）", padding=8)
        sliders.pack(fill="both", expand=True, padx=10)
        half = (len(PARAM_INFO) + 1) // 2
        for i, (name, label, lo, hi) in enumerate(PARAM_INFO):
            col = 0 if i < half else 3
            row = i if i < half else i - half
            var = tk.DoubleVar(value=getattr(self.params, name))
            self.vars[name] = var
            ttk.Label(sliders, text=label, width=20).grid(row=row, column=col, sticky="w")
            ttk.Scale(sliders, from_=lo, to=hi, variable=var, length=260,
                      command=lambda v, n=name: self._on_slider(n)).grid(row=row, column=col + 1, padx=4, pady=2)
            val = ttk.Label(sliders, width=6)
            val.grid(row=row, column=col + 2, sticky="w", padx=(0, 18))
            var.trace_add("write", lambda *a, n=name, l=val: l.config(text=self._fmt(n)))
            val.config(text=self._fmt(name))

        sound = ttk.LabelFrame(self.root, text="響き", padding=8)
        sound.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(sound, text="音源").grid(row=0, column=0, sticky="w")
        self.inst_box = ttk.Combobox(sound, state="readonly", width=30, values=list(self.instruments))
        self.inst_box.current(0)
        self.inst_box.grid(row=0, column=1, padx=(4, 18), sticky="w")
        self.inst_box.bind("<<ComboboxSelected>>", lambda e: self._set_instrument())
        ttk.Label(sound, text="会場").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.room_box = ttk.Combobox(sound, state="readonly", width=30, values=list(ROOMS))
        self.room_box.set(self.engine.room)
        self.room_box.grid(row=1, column=1, padx=(4, 18), pady=(6, 0), sticky="w")
        self.room_box.bind("<<ComboboxSelected>>", lambda e: self._set_room())
        self.sound_vars = {}
        for i, (attr, label, hi) in enumerate((("wet", "残響の量", 0.8),
                                               ("resonance", "弦の共鳴", 2.0),
                                               ("noise", "機械ノイズ", 2.0))):
            var = tk.DoubleVar(value=getattr(self.engine, attr))
            self.sound_vars[attr] = var
            ttk.Label(sound, text=label).grid(row=i, column=2, sticky="e")
            ttk.Scale(sound, from_=0, to=hi, variable=var, length=220,
                      command=lambda v, a=attr: setattr(self.engine, a, float(self.sound_vars[a].get()))
                      ).grid(row=i, column=3, padx=(4, 16))

        self.kb = tk.Canvas(self.root, height=110, bg="#222", highlightthickness=0)
        self.kb.pack(fill="x", padx=10, pady=10)
        self.kb.bind("<Configure>", lambda e: self._draw_keyboard())

    def _fmt(self, name):
        v = self.vars[name].get()
        return f"{v:.2f}" if abs(v) < 2 and name not in ("dyn_level", "bass") else f"{v:.0f}"

    def _draw_keyboard(self):
        c = self.kb
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        whites = [m for m in range(21, 109) if m % 12 not in BLACK]
        kw = w / len(whites)
        self._key_items = {}
        for i, m in enumerate(whites):
            self._key_items[m] = c.create_rectangle(i * kw, 0, (i + 1) * kw, h - 14, fill="white", outline="#888")
        for m in range(21, 109):
            if m % 12 in BLACK:
                i = whites.index(m - 1)
                x = (i + 1) * kw
                self._key_items[m] = c.create_rectangle(x - kw * 0.32, 0, x + kw * 0.32, (h - 14) * 0.62,
                                                        fill="black", outline="black")
        self._pedal_item = c.create_text(8, h - 7, anchor="w", text="", fill="#9cf", font=("", 9))
        for m, v in self.pressed.items():
            self._paint_key(m, v)

    def _paint_key(self, m, vel):
        item = getattr(self, "_key_items", {}).get(m)
        if item is None:
            return
        if vel:
            g = int(200 - vel * 1.3)
            self.kb.itemconfig(item, fill=f"#ff{max(40, g):02x}{max(20, g - 60):02x}")
        else:
            self.kb.itemconfig(item, fill="black" if m % 12 in BLACK else "white")

    # ---- 操作 ----
    def _apply_preset(self, name):
        desc, params = PRESETS[name]
        self.params.__dict__.update(copy.copy(params).__dict__)
        for k, var in self.vars.items():
            var.set(getattr(self.params, k))
        self.desc.config(text=desc)

    def _on_slider(self, name):
        setattr(self.params, name, float(self.vars[name].get()))

    def _get_score(self, name):
        if name not in self.loaded:
            self.loaded[name] = self.catalog[name]()
        return self.loaded[name]

    def _select_piece(self):
        try:
            self.score = self._get_score(self.piece_box.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("読み込みエラー", str(e))
            return
        self._prepare_async()

    def _set_instrument(self):
        self._stop()
        self.engine.sampler = self.instruments[self.inst_box.get()]
        self._prepare_async()

    def _set_room(self):
        self.engine.set_room(self.room_box.get())
        self.sound_vars["wet"].set(self.engine.wet)

    def _open_midi(self):
        path = filedialog.askopenfilename(filetypes=[("MIDI", "*.mid *.midi"), ("すべて", "*.*")])
        if not path:
            return
        try:
            score = load_midi(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("読み込みエラー", str(e))
            return
        self.catalog[score.title] = lambda score=score: score
        self.loaded[score.title] = score
        self.piece_box.config(values=list(self.catalog))
        self.piece_box.set(score.title)
        self.score = score
        self._prepare_async()

    def _transcribe_dialog(self):
        """URL（または音声ファイル）を入力して、ピアノ演奏を自動採譜する。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("URLから採譜（ピアノ演奏 → MIDI）")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both")
        fields = {}
        rows = (("source", "URL / 音声ファイル", 56), ("title", "曲名（空なら動画タイトル）", 40),
                ("composer", "作曲者・ゲーム名など", 40), ("start", "開始（例 0:12）", 10),
                ("end", "終了（例 3:40）", 10))
        for i, (key, label, width) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            ttk.Entry(frm, textvariable=var, width=width).grid(row=i, column=1, sticky="w", padx=6, pady=3)
            fields[key] = var
        def paste_clipboard_url(_=None):
            """クリップボードに YouTube のURLがあれば、URL欄が空のときに入れておく。"""
            if fields["source"].get().strip():
                return
            try:
                clip = self.root.clipboard_get()
            except tk.TclError:  # クリップボードが空、または文字列でない
                return
            m = re.search(r"https?://(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)/\S+", clip)
            if m:
                fields["source"].set(m.group(0))

        paste_clipboard_url()
        # 画面を開いた後でURLをコピーした場合も、この画面に戻ったときに入れる
        dlg.bind("<FocusIn>", paste_clipboard_url)

        def browse():
            path = filedialog.askopenfilename(parent=dlg, filetypes=[
                ("音声", "*.wav *.mp3 *.m4a *.flac *.ogg *.opus"), ("すべて", "*.*")])
            if path:
                fields["source"].set(path)

        ttk.Button(frm, text="ファイル…", command=browse).grid(row=0, column=2, padx=(0, 4))
        ttk.Label(frm, foreground="#555", wraplength=520, justify="left", text=(
            "ピアノソロの演奏ほど正確に採譜できます。MIDI は pieces/youtube/ に保存され、曲の一覧にも登録されます。\n"
            "動画は、ダウンロードが認められているもの（自分の演奏など）を使い、個人の練習用にとどめてください。"
        )).grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(8, 4))

        def ok():
            v = {k: var.get().strip() for k, var in fields.items()}
            if not v["source"]:
                messagebox.showwarning("URLから採譜", "URL か音声ファイルを入力してください。", parent=dlg)
                return
            try:
                from .transcribe import seconds
                seconds(v["start"]), seconds(v["end"])
            except ValueError:
                messagebox.showwarning("URLから採譜", "開始・終了は 1:23 や 83 のように入力してください。", parent=dlg)
                return
            dlg.destroy()
            self._transcribe_async(**v)

        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 1, column=0, columnspan=3, sticky="e", pady=(6, 0))
        ttk.Button(btns, text="変換", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="キャンセル", command=dlg.destroy).pack(side="left")
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()

    def _transcribe_async(self, source, title, composer, start, end):
        self.url_btn.state(["disabled"])

        def log(msg):
            self._ui_queue.put(lambda: self.tstatus.config(text=f"採譜：{msg[:120]}"))

        def work():
            try:
                from .transcribe import convert
                midi_path, name = convert(source, title=title, composer=composer,
                                          start=start or None, end=end or None, log=log)
            except Exception as e:  # noqa: BLE001
                err = str(e) or type(e).__name__
                if isinstance(e, ImportError):
                    err += "\n\n採譜には torch と piano_transcription_inference が必要です（README 参照）。"
                self._ui_queue.put(lambda: self._transcribed(None, err))
                return
            display = f"{composer}：{name}" if composer else name
            self._ui_queue.put(lambda: self._transcribed((display, midi_path), None))

        threading.Thread(target=work, daemon=True).start()

    def _transcribed(self, result, error):
        self.url_btn.state(["!disabled"])
        if error:
            self.tstatus.config(text="採譜：失敗しました")
            messagebox.showerror("URLから採譜", error)
            return
        display, path = result
        def load(path=path, display=display):
            sc = load_piece(path)
            sc.title = display
            return sc

        self.catalog[display] = load
        self.loaded.pop(display, None)
        self.piece_box.config(values=list(self.catalog))
        self.tstatus.config(text=f"採譜：完了しました（{display} を曲の一覧に追加）")
        if self.performer is None and self.shuffle is None:  # 演奏中でなければ、できた曲を選ぶ
            def select():
                self.piece_box.set(display)
                self._select_piece()
            if not self._preparing:
                select()
            elif self._after_prepared is None:
                self._after_prepared = select

    def _prepare_async(self):
        pitches = [n.pitch for n in self.score.notes]
        self._preparing = True
        self.play_btn.state(["disabled"])

        def progress(i, n):
            self._ui_queue.put(lambda: self.status.config(text=f"ピアノの音を準備中… {i}/{n}"))

        def work():
            self.engine.prepare(pitches, progress)
            self._ui_queue.put(self._prepared)

        threading.Thread(target=work, daemon=True).start()

    def _prepared(self):
        self._preparing = False
        self.play_btn.state(["!disabled"])
        self.status.config(text=f"準備完了：{self.score.title}")
        if self._after_prepared:
            fn, self._after_prepared = self._after_prepared, None
            fn()

    def _play(self):
        if self._preparing:
            return
        self._stop_performer()  # おまかせ連続演奏は続ける
        self.performer = Performer(self.engine, self.score, self.params)
        self.performer.start()

    def _stop(self):
        """停止ボタン: おまかせ連続演奏も終わる。"""
        was_active = self.shuffle is not None or self.performer is not None
        self._end_shuffle()
        self._stop_performer()
        if was_active and not self._preparing:
            self.status.config(text=f"停止しました：{self.score.title}")

    def _stop_performer(self):
        if self.performer:
            self.performer.stop()
            self.performer = None

    # ---- おまかせ連続演奏 ----
    def _toggle_shuffle(self):
        if self.shuffle:
            self._stop()
            return
        presets = [p for p in PRESETS if not (self.skip_plain.get() and p.startswith("機械的"))]
        self.shuffle = ShufflePicker(list(self.catalog), presets)
        self.shuffle_btn.config(text="■ おまかせを終了")
        self.skip_btn.state(["!disabled"])
        self._shuffle_next()

    def _end_shuffle(self):
        if self.shuffle:
            self.shuffle = None
            self._shuffle_token += 1
            self._after_prepared = None
            self.shuffle_btn.config(text="🔀 おまかせ連続演奏")
            self.skip_btn.state(["disabled"])

    def _shuffle_skip(self):
        if self.shuffle and not self._preparing:
            self._shuffle_next()

    def _max_seconds(self):
        v = self.max_len.get()
        return None if v == "なし" else float(v.rstrip("分")) * 60

    def _shuffle_next(self):
        """次の曲とピアニストを選んで、準備ができたら演奏する。"""
        sh = self.shuffle
        if not sh:
            return
        self._shuffle_token += 1
        self._stop_performer()
        preset = sh.next_preset()
        limit = self._max_seconds()
        for _ in range(len(self.catalog)):
            name = sh.next_piece()
            if name is None:
                break
            try:
                score = self._get_score(name)
            except Exception:  # noqa: BLE001  読み込めない曲は飛ばす
                sh.exclude_piece(name)
                continue
            if limit and estimate_seconds(score, PRESETS[preset][1].tempo) > limit:
                sh.exclude_piece(name)
                continue
            break
        else:
            name = None
        if name is None:
            self._end_shuffle()
            self.status.config(text="おまかせ連続演奏：条件に合う曲がありません（長さの上限を見直してください）")
            return
        sh.count += 1
        self.piece_box.set(name)
        self.preset_box.set(preset)
        self._apply_preset(preset)
        self.score = score
        self._after_prepared = self._play
        self._prepare_async()

    def _poll(self):
        while not self._ui_queue.empty():
            self._ui_queue.get()()
        vis = self.engine.visual
        while vis:
            ev = vis.popleft()
            if ev[0] == "pedal":
                self.kb.itemconfig(self._pedal_item, text="● ペダル" if ev[1] else "")
            elif ev[0] == "all_off":
                for m in list(self.pressed):
                    self._paint_key(m, 0)
                self.pressed.clear()
                self.kb.itemconfig(self._pedal_item, text="")
            else:
                m, v = ev
                if v:
                    self.pressed[m] = v
                else:
                    self.pressed.pop(m, None)
                self._paint_key(m, v)
        pf = self.performer
        if pf and not self._preparing:
            sc = self.score
            bar = sc.bar_index(pf.position) + 1
            bpm = pf.bpm_at(pf.position)
            head = f"おまかせ {self.shuffle.count}曲目｜" if self.shuffle else ""
            who = self.preset_box.get()
            if pf.finished:
                self.performer = None
                if self.shuffle:
                    self.status.config(text=f"{head}演奏終了：{sc.title}　…まもなく次の曲へ")
                    token = self._shuffle_token
                    self.root.after(2500, lambda: token == self._shuffle_token and self._shuffle_next())
                else:
                    self.status.config(text=f"演奏終了：{sc.title}")
            else:
                self.status.config(text=f"{head}演奏中：{sc.title} × {who}　第{max(1, bar)}小節　♩≈{bpm:.0f}")
        self.root.after(30, self._poll)

    def _close(self):
        self._stop()
        self.engine.close()
        self.root.destroy()


def run():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
