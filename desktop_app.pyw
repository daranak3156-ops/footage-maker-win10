"""Desktop editor for Footage Maker. Double-click on Windows with Python installed."""

import json
import os
import queue
import sys
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from footage_maker import choose_assets, make_plan, render


def load_config():
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        return json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Монтаж по сценарию")
        self.geometry("1160x760")
        self.minsize(850, 600)
        self.jobs = queue.Queue()
        self.busy = False
        self.plan = None
        config = load_config()
        self.script = tk.StringVar()
        self.output = tk.StringVar(value=str(Path.home() / "Videos" / "rough_cut.mp4"))
        self.voice = tk.StringVar()
        self.count = tk.StringVar(value=str(config.get("preview_clips", 12)))
        self.format = tk.StringVar(value=config.get("format", "9:16"))
        self.pexels = tk.BooleanVar(value=False)
        self.key = tk.StringVar(value=os.getenv("PEXELS_API_KEY", ""))
        self.status = tk.StringVar(value="Выберите сценарий, затем создайте монтажный план.")
        self.build()
        self.after(100, self.poll)

    def build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(5, weight=1)
        ttk.Label(outer, text="Монтаж по сценарию", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Label(outer, text="Сценарий (.txt)").grid(row=1, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.script).grid(row=1, column=1, sticky="ew", padx=9, pady=4)
        ttk.Button(outer, text="Выбрать…", command=self.choose_script).grid(row=1, column=2)

        ttk.Label(outer, text="Готовый ролик (.mp4)").grid(row=2, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.output).grid(row=2, column=1, sticky="ew", padx=9, pady=4)
        ttk.Button(outer, text="Сохранить как…", command=self.choose_output).grid(row=2, column=2)

        ttk.Label(outer, text="Озвучка (необязательно)").grid(row=3, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.voice).grid(row=3, column=1, sticky="ew", padx=9, pady=4)
        ttk.Button(outer, text="Выбрать…", command=self.choose_voice).grid(row=3, column=2)

        options = ttk.Frame(outer)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Label(options, text="Кадров (0 = весь текст):").pack(side="left")
        ttk.Entry(options, textvariable=self.count, width=6).pack(side="left", padx=(5, 18))
        ttk.Label(options, text="Формат:").pack(side="left")
        ttk.Radiobutton(options, text="Вертикальный 9:16", variable=self.format, value="9:16").pack(side="left", padx=5)
        ttk.Radiobutton(options, text="Горизонтальный 16:9", variable=self.format, value="16:9").pack(side="left", padx=5)
        ttk.Checkbutton(options, text="Современное видео Pexels, если нет архива", variable=self.pexels).pack(side="left", padx=16)
        ttk.Label(options, text="Ключ:").pack(side="left")
        ttk.Entry(options, textvariable=self.key, show="•", width=18).pack(side="left", padx=5)

        center = ttk.LabelFrame(outer, text="Кадры — двойной щелчок меняет поисковый запрос", padding=6)
        center.grid(row=5, column=0, columnspan=3, sticky="nsew")
        center.columnconfigure(0, weight=1)
        center.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(center, columns=("n", "text", "query", "status", "source"), show="headings", selectmode="browse")
        for key, label, width in (("n", "№", 40), ("text", "Текст", 430), ("query", "Поиск", 230),
                                  ("status", "Статус", 110), ("source", "Источник", 220)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=35, stretch=key in {"text", "query", "source"})
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(center, orient="vertical", command=self.table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.bind("<Double-1>", self.edit_query)

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(11, 5))
        self.buttons = []
        for label, callback in (("1. Создать план", self.plan_action), ("2. Найти кадры", self.search_action),
                                ("3. Собрать MP4", self.render_action), ("Всё автоматически", self.all_action),
                                ("Свой файл для кадра", self.local_action), ("Открыть источник", self.open_source)):
            button = ttk.Button(actions, text=label, command=callback)
            button.pack(side="left", padx=(0, 7))
            self.buttons.append(button)

        ttk.Label(outer, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky="w", pady=(5, 2))
        self.log = tk.Text(outer, height=6, state="disabled", wrap="word")
        self.log.grid(row=8, column=0, columnspan=3, sticky="ew")

    def choose_script(self):
        path = filedialog.askopenfilename(filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")])
        if path:
            self.script.set(path)
            self.output.set(str(Path(path).with_name("rough_cut.mp4")))
            existing = Path(path).with_name("storyboard.json")
            if existing.exists():
                try:
                    self.plan = json.loads(existing.read_text(encoding="utf-8"))
                    self.refresh()
                    self.message(f"Загружен сохранённый план: {existing}")
                except (OSError, ValueError, KeyError) as exc:
                    self.message(f"Не удалось открыть план: {exc}")
            else:
                self.plan = None
                self.refresh()

    def choose_output(self):
        path = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if path:
            self.output.set(path)

    def choose_voice(self):
        path = filedialog.askopenfilename(filetypes=[("Аудио", "*.mp3 *.wav *.m4a *.aac"), ("Все файлы", "*.*")])
        if path:
            self.voice.set(path)

    def plan_path(self):
        return Path(self.script.get()).resolve().with_name("storyboard.json")

    def save(self):
        self.plan_path().write_text(json.dumps(self.plan, ensure_ascii=False, indent=2), encoding="utf-8")

    def require_script(self):
        path = Path(self.script.get())
        if not path.is_file():
            raise ValueError("Сначала выберите существующий файл сценария .txt")
        return path

    def make(self):
        path = self.require_script()
        try:
            count = int(self.count.get())
        except ValueError as exc:
            raise ValueError("Введите целое число кадров; 0 означает весь сценарий") from exc
        if count < 0:
            raise ValueError("Количество кадров не может быть отрицательным")
        plan = make_plan(path.read_text(encoding="utf-8-sig"), count, self.format.get() == "9:16")
        if not plan["shots"]:
            raise ValueError("Сценарий пуст")
        if len(plan["shots"]) > 100 and not messagebox.askyesno("Большой ролик", f"План содержит {len(plan['shots'])} кадров. Поиск и монтаж могут занять долгое время. Продолжить?"):
            return False
        self.plan = plan
        self.save()
        self.refresh()
        self.message(f"План: {len(plan['shots'])} кадров из {plan['total_script_shots']}. Файл: {self.plan_path()}")
        return True

    def plan_action(self):
        try:
            self.make()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Ошибка", str(exc))

    def selected(self):
        selection = self.table.selection()
        if not selection or not self.plan:
            messagebox.showinfo("Выбор кадра", "Сначала выберите строку в списке кадров.")
            return None
        return self.plan["shots"][int(selection[0]) - 1]

    def edit_query(self, _event=None):
        if self.busy:
            return
        shot = self.selected()
        if not shot:
            return
        new = simpledialog.askstring("Поисковый запрос", f"Кадр {shot['id']} — запрос для Wikimedia Commons:", initialvalue=shot["query"], parent=self)
        if new and new.strip() != shot["query"]:
            shot["query"] = new.strip()
            shot["asset"] = None
            shot["status"] = "unsearched"
            self.save()
            self.refresh()

    def local_action(self):
        if self.busy:
            return
        shot = self.selected()
        if not shot:
            return
        path = filedialog.askopenfilename(filetypes=[("Изображения и видео", "*.jpg *.jpeg *.png *.webp *.mp4 *.webm *.ogv"), ("Все файлы", "*.*")])
        if path:
            video = Path(path).suffix.lower() in {".mp4", ".webm", ".ogv"}
            shot["asset"] = {"kind": "video" if video else "image", "local_file": path,
                              "url": Path(path).as_uri(), "title": Path(path).name,
                              "page": "", "provider": "Личный файл", "license": "Проверить права",
                              "historical": False}
            shot["status"] = "selected"
            self.save()
            self.refresh()

    def open_source(self):
        shot = self.selected()
        if shot and (shot.get("asset") or {}).get("page", "").startswith("https://"):
            webbrowser.open(shot["asset"]["page"])

    def refresh(self):
        selected = self.table.selection()
        self.table.delete(*self.table.get_children())
        if self.plan:
            for shot in self.plan.get("shots", []):
                asset = shot.get("asset") or {}
                self.table.insert("", "end", iid=str(shot["id"]), values=(shot["id"], shot["text"],
                    shot["query"], shot["status"], asset.get("title", "")))
        if selected and self.table.exists(selected[0]):
            self.table.selection_set(selected[0])

    def message(self, line):
        self.status.set(line)
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def launch(self, work):
        if self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.configure(state="disabled")

        def runner():
            try:
                work(lambda line: self.jobs.put(("log", line)))
                self.jobs.put(("done", None))
            except Exception as exc:
                self.jobs.put(("error", str(exc)))
        threading.Thread(target=runner, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, data = self.jobs.get_nowait()
                if kind == "log":
                    self.message(data)
                else:
                    self.busy = False
                    for button in self.buttons:
                        button.configure(state="normal")
                    self.refresh()
                    if kind == "error":
                        self.message("Ошибка: " + data)
                        messagebox.showerror("Сборка остановлена", data)
                    else:
                        self.message("Операция завершена.")
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def search(self, log, key):
        choose_assets(self.plan, key, progress=log)
        self.save()
        missing = sum(not shot.get("asset") and shot.get("status") != "skip" for shot in self.plan["shots"])
        log(f"Поиск завершён. Требуют ручной проверки: {missing} кадров.")

    def search_action(self):
        if not self.plan:
            messagebox.showinfo("Нет плана", "Сначала создайте план.")
            return
        if self.pexels.get() and not self.key.get().strip():
            messagebox.showerror("Ключ Pexels", "Введите свой ключ Pexels или выключите поиск на видеостоке.")
            return
        key = self.key.get().strip() if self.pexels.get() else ""
        self.launch(lambda log: self.search(log, key))

    def build_video(self, log, target, audio):
        if target.suffix.lower() != ".mp4":
            raise ValueError("Укажите выходной файл с расширением .mp4")
        if audio and not audio.is_file():
            raise ValueError("Файл озвучки не найден")
        render(self.plan, target, audio, progress=log)

    def render_action(self):
        if not self.plan:
            messagebox.showinfo("Нет плана", "Сначала создайте план.")
            return
        target = Path(self.output.get()).expanduser()
        audio = Path(self.voice.get()).expanduser() if self.voice.get().strip() else None
        self.launch(lambda log: self.build_video(log, target, audio))

    def all_action(self):
        try:
            if not self.make():
                return
            if self.pexels.get() and not self.key.get().strip():
                raise ValueError("Для Pexels нужен свой ключ API")
            key = self.key.get().strip() if self.pexels.get() else ""
            target = Path(self.output.get()).expanduser()
            audio = Path(self.voice.get()).expanduser() if self.voice.get().strip() else None
            def job(log):
                self.search(log, key)
                self.build_video(log, target, audio)
            self.launch(job)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Ошибка", str(exc))


if __name__ == "__main__":
    App().mainloop()
