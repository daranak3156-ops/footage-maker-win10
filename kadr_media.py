"""Кадр: desktop slideshow editor with Wikimedia images and API narration."""
import json
import os
import re
import hashlib
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

import requests
from imageio_ffmpeg import get_ffmpeg_exe

COMMONS = "https://commons.wikimedia.org/w/api.php"
SPEECH = "https://api.openai.com/v1/audio/speech"
HEADERS = {"User-Agent": "KadrDesktop/1.0 (image search for user-created videos)"}
PHOTO_HOME = Path.home() / "Pictures" / "Kadr"


def tmdb_people(name, token, count=8):
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"} if "." in token else {}
    params = {"query": name}
    if not headers:
        params["api_key"] = token
    result = requests.get("https://api.themoviedb.org/3/search/person", params=params, headers=headers, timeout=25)
    result.raise_for_status()
    people = result.json().get("results", [])
    if not people:
        return []
    person = next((p for p in people if p.get("name", "").casefold() == name.casefold()), people[0])
    params = {} if headers else {"api_key": token}
    result = requests.get(f"https://api.themoviedb.org/3/person/{person['id']}/images", params=params, headers=headers, timeout=25)
    result.raise_for_status()
    return [{"title": f"{person['name']} · TMDB", "url": "https://image.tmdb.org/t/p/original" + p["file_path"], "page": f"https://www.themoviedb.org/person/{person['id']}", "license": "TMDB — проверьте права", "source": "TMDB"} for p in result.json().get("profiles", [])[:count]]


def bing_people_chrome(name, count=8):
    """Open three distinct tabs: image search, TMDB, Bing; extract Bing originals."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    options = Options()
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1200,900")
    browser = webdriver.Chrome(options=options)
    try:
        results = []
        targets = [
            "https://www.google.com/search?tbm=isch&q=" + quote(name + " portrait photo"),
            "https://www.themoviedb.org/search/person?query=" + quote(name),
            "https://www.bing.com/images/search?q=" + quote(name + " portrait photo"),
        ]
        for index, target in enumerate(targets):
            if index:
                browser.switch_to.new_window("tab")
            browser.get(target)
        browser.switch_to.window(browser.window_handles[2])
        for element in browser.find_elements(By.CSS_SELECTOR, "a.iusc"):
            try:
                data = json.loads(element.get_attribute("m") or "{}")
                url = data.get("murl", "")
                if url.startswith("https://") and url not in {i["url"] for i in results}:
                    results.append({"title": name + " · Bing", "url": url, "page": data.get("purl", url), "license": "Проверьте права у источника", "source": "Bing"})
            except (ValueError, TypeError):
                continue
            if len(results) >= count:
                break
        return results
    finally:
        browser.quit()


def save_photos(name, images, root=PHOTO_HOME):
    folder = root / re.sub(r"[^\w .-]+", "_", name, flags=re.UNICODE).strip(" .")[:80]
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "photos.json"
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        existing = []
    known = {entry["url"]: entry for entry in existing}
    for image in images:
        if image["url"] in known and (folder / known[image["url"]]["filename"]).exists():
            continue
        try:
            response = requests.get(image["url"], headers=HEADERS, timeout=30)
            response.raise_for_status()
            mime = response.headers.get("content-type", "").split(";")[0]
            if mime not in ("image/jpeg", "image/png", "image/webp") or len(response.content) > 20_000_000:
                continue
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
            filename = hashlib.sha256(image["url"].encode()).hexdigest()[:20] + ext
            (folder / filename).write_bytes(response.content)
            known[image["url"]] = {**image, "filename": filename}
        except requests.RequestException:
            continue
    manifest_path.write_text(json.dumps(list(known.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    return [{**entry, "path": str(folder / entry["filename"])} for entry in known.values() if (folder / entry["filename"]).exists()]


def search_images(query, count=6):
    response = requests.get(COMMONS, params={"action": "query", "generator": "search", "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": 6, "gsrlimit": 30, "prop": "imageinfo", "iiprop": "url|extmetadata|mime", "iiurlwidth": 1600, "format": "json"}, headers=HEADERS, timeout=25)
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", {}).values()
    output = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png") or not info.get("thumburl"):
            continue
        meta = info.get("extmetadata", {})
        output.append({"title": page["title"], "url": info["thumburl"], "page": info.get("descriptionurl", ""), "license": re.sub(r"<[^>]+>", "", meta.get("LicenseShortName", {}).get("value", "Проверить лицензию"))})
        if len(output) == count:
            break
    return output


def make_speech(key, script, voice, model, destination):
    if len(script) > 4096:
        raise ValueError("Текст озвучки должен быть не длиннее 4096 символов.")
    response = requests.post(SPEECH, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json={"model": model, "voice": voice, "input": script, "response_format": "mp3", "instructions": "Говори естественно и выразительно по-русски."}, timeout=180)
    if not response.ok:
        raise RuntimeError(f"Ошибка API ({response.status_code}): {response.text[:300]}")
    destination.write_bytes(response.content)


def probe_duration(audio):
    result = subprocess.run([get_ffmpeg_exe(), "-hide_banner", "-i", str(audio)], capture_output=True, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError("Не удалось определить длительность озвучки.")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def make_video(images, audio, output):
    duration = probe_duration(audio)
    seconds = max(1, duration / len(images))
    inputs = []
    filters = []
    for index, path in enumerate(images):
        inputs.extend(["-loop", "1", "-framerate", "25", "-t", f"{seconds + .12:.3f}", "-i", str(path)])
        filters.append(f"[{index}:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=25,trim=duration={seconds + .12:.3f},setpts=PTS-STARTPTS[v{index}]")
    inputs.extend(["-i", str(audio)])
    filters.append("".join(f"[v{i}]" for i in range(len(images))) + f"concat=n={len(images)}:v=1:a=0[v]")
    command = [get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[v]", "-map", f"{len(images)}:a", "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "-c:a", "aac", "-movflags", "+faststart", str(output)]
    subprocess.run(command, check=True, capture_output=True, text=True)


class App:
    def __init__(self, root):
        self.root = root
        root.title("Кадр · видео из фотографий")
        root.geometry("780x730")
        self.images = []
        self.topic = tk.StringVar()
        self.key = tk.StringVar(value=os.getenv("OPENAI_API_KEY", ""))
        self.tmdb_key = tk.StringVar(value=os.getenv("TMDB_API_KEY", ""))
        self.voice = tk.StringVar(value="marin")
        self.model = tk.StringVar(value="gpt-4o-mini-tts")
        self.status = tk.StringVar(value="Введите тему и найдите фотографии")
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="КАДР", font=("Arial", 22, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Фото личности из Bing и TMDB → озвучка → MP4").pack(anchor="w", pady=(0, 18))
        ttk.Label(frame, text="Имя человека (лучше полное имя)").pack(anchor="w")
        ttk.Entry(frame, textvariable=self.topic).pack(fill="x", pady=4)
        ttk.Label(frame, text="Ключ TMDB (необязателен; API key или Read Access Token)").pack(anchor="w", pady=(6, 2))
        ttk.Entry(frame, textvariable=self.tmdb_key, show="•").pack(fill="x")
        ttk.Button(frame, text="Найти фотографии", command=self.find).pack(anchor="w", pady=6)
        ttk.Label(frame, text="Найденные изображения — снимите отметку с неподходящих").pack(anchor="w", pady=(10, 4))
        listing = ttk.Frame(frame)
        listing.pack(fill="x")
        self.listing = listing
        ttk.Label(frame, text="Текст озвучки (напишите или отредактируйте)").pack(anchor="w", pady=(16, 3))
        self.script = tk.Text(frame, height=7, wrap="word")
        self.script.pack(fill="both", expand=True)
        ttk.Label(frame, text="OpenAI API-ключ (используется только для запроса, не сохраняется)").pack(anchor="w", pady=(12, 3))
        ttk.Entry(frame, textvariable=self.key, show="•").pack(fill="x")
        options = ttk.Frame(frame)
        options.pack(fill="x", pady=10)
        ttk.Label(options, text="Голос").pack(side="left")
        ttk.Combobox(options, textvariable=self.voice, values=["marin", "cedar", "alloy", "nova", "coral", "sage", "shimmer"], width=12).pack(side="left", padx=8)
        ttk.Label(options, text="Модель").pack(side="left", padx=(15, 0))
        ttk.Entry(options, textvariable=self.model, width=22).pack(side="left", padx=8)
        self.export_button = ttk.Button(frame, text="Создать видео MP4", command=self.export)
        self.export_button.pack(anchor="w", pady=8)
        ttk.Label(frame, textvariable=self.status, wraplength=700).pack(anchor="w", pady=4)
        ttk.Label(frame, text=f"Фотографии остаются в {PHOTO_HOME}. Проверьте права на использование каждого снимка.", wraplength=700).pack(anchor="w", pady=5)

    def work(self, fn):
        self.export_button.configure(state="disabled")
        def run():
            try:
                fn()
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): messagebox.showerror("Ошибка", error))
            finally:
                self.root.after(0, lambda: self.export_button.configure(state="normal"))
        threading.Thread(target=run, daemon=True).start()

    def set_status(self, text):
        self.root.after(0, lambda: self.status.set(text))

    def find(self):
        topic = self.topic.get().strip()
        tmdb_token = self.tmdb_key.get().strip()
        if not topic:
            messagebox.showinfo("Тема", "Введите тему поиска.")
            return
        def task():
            self.set_status("Ищу портреты через Chrome и TMDB…")
            results = []
            errors = []
            for source in (lambda: tmdb_people(topic, tmdb_token), lambda: bing_people_chrome(topic)):
                try:
                    results.extend(source())
                except Exception as exc:
                    errors.append(str(exc)[:120])
            self.set_status("Скачиваю и сохраняю фотографии…")
            results = save_photos(topic, results)
            if not results and errors:
                raise RuntimeError("Поиск не удался: " + "; ".join(errors))
            self.root.after(0, lambda: self.show_results(results, topic))
        self.work(task)

    def show_results(self, results, topic):
        for child in self.listing.winfo_children():
            child.destroy()
        self.images = results
        for image in results:
            image["enabled"] = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.listing, text=image["title"][:80] + " · " + image["license"], variable=image["enabled"]).pack(anchor="w")
        if not self.script.get("1.0", "end").strip():
            self.script.insert("1.0", f"Сегодня поговорим о человеке по имени {topic}. Перед вами фотографии из разных источников. Добавьте сюда проверенные факты о его жизни и деятельности.")
        self.status.set(f"Найдено фотографий: {len(results)}" if results else "Ничего не найдено. Попробуйте другой запрос.")

    def export(self):
        selected = [i for i in self.images if i["enabled"].get()]
        script = self.script.get("1.0", "end").strip()
        key = self.key.get().strip()
        if not selected or not script or not key:
            messagebox.showinfo("Не хватает данных", "Нужны фотографии, текст озвучки и API-ключ.")
            return
        output = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("Видео MP4", "*.mp4")])
        if not output:
            return
        voice, model = self.voice.get().strip(), self.model.get().strip()
        def task():
            with tempfile.TemporaryDirectory(prefix="kadr-") as temporary:
                temp = Path(temporary)
                paths = [Path(image["path"]) for image in selected]
                self.set_status("Создаю озвучку через OpenAI API…")
                audio = temp / "speech.mp3"
                make_speech(key, script, voice, model, audio)
                self.set_status("Собираю MP4…")
                make_video(paths, audio, Path(output))
            credits = Path(output).with_suffix(".credits.txt")
            credits.write_text("Источники фотографий (проверьте условия использования каждой лицензии):\n" + "\n".join(f"{i['title']} — {i['license']} — {i['page']}" for i in selected), encoding="utf-8")
            self.set_status(f"Готово: {output}")
            self.root.after(0, lambda: messagebox.showinfo("Готово", f"Видео создано:\n{output}"))
        self.work(task)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
