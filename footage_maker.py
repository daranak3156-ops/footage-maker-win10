#!/usr/bin/env python3
"""Script-to-storyboard and sourced rough-cut editor. Python 3.10+, ffmpeg."""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "FootageMaker/0.1 (storyboard tool; contact: https://commons.wikimedia.org/)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PEXELS_API = "https://api.pexels.com/v1/videos/search"
TOPICS = [
    (r"чернобыл|припят|радиац|реактор", "Chernobyl disaster 1986", "Chernobyl"),
    (r"шахт|донбасс|угол", "Donbass coal mining Soviet", "coal mining"),
    (r"металл|стал|домен|приднепр", "Ukraine Soviet steel industry", "steel factory"),
    (r"киев|столиц", "Kyiv Soviet 1980", "Kyiv city"),
    (r"харьков|машиностро|трактор|завод|турбин|ракет", "Soviet Ukraine factory", "factory machinery"),
    (r"квартир|жиль|жилплощ|дом|коммунал", "Soviet apartment Ukraine", "apartment building"),
    (r"очеред|дефицит|магазин|прилав|снабжен|талон", "Soviet grocery store queue", "grocery store"),
    (r"село|деревн|урожа|огород|поле|колхоз", "Ukraine Soviet agriculture", "wheat field"),
    (r"перестройк|гласност|демонстрац", "Perestroika Ukraine 1980", "protest crowd"),
    (r"прибалт|эстон|литв|латви", "Soviet Baltic states 1980", "Baltic city"),
    (r"порт|мор|одесс|курорт", "Odessa Soviet port", "sea port"),
]
ALLOWED_LICENSE = re.compile(r"^(?:CC0(?: [1-4]\.0)?|CC BY(?:-SA)?(?: [1-4]\.0)?|Public domain|PD(?:-[A-Za-z0-9 .-]+)?)$", re.I)
MEDIA_SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                "video/webm": ".webm", "video/ogg": ".ogv", "video/mp4": ".mp4"}


def request_json(url, headers=None, retries=2):
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})}), timeout=25) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise RuntimeError(f"API недоступен: {url.split('?')[0]}: {exc}") from exc
            time.sleep(1 + attempt * 2)


def split_script(text, max_words=13):
    """Preserve order and punctuation; prefer boundaries between sentences."""
    sentences = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    shots = []
    for sentence in sentences:
        words = sentence.split()
        while words:
            remaining = len(words)
            if remaining <= max_words + 3:
                take = remaining
            else:
                take = min(max_words, remaining - 4)
                # Break at a comma nearby if possible.
                for index in range(take - 1, max(6, take - 4), -1):
                    if words[index].endswith((",", ";", ":")):
                        take = index + 1
                        break
            chunk = " ".join(words[:take])
            words = words[take:]
            if chunk:
                shots.append(chunk)
    return shots


def search_terms(text, previous=""):
    lowered = text.lower()
    for pattern, commons, stock in TOPICS:
        if re.search(pattern, lowered):
            return commons, stock
    return ("Ukraine Soviet life 1980", "Ukraine")


def make_plan(text, limit=12, portrait=True):
    chunks = split_script(text)
    if limit > 0:
        chunks = chunks[:limit]
    shots = []
    previous = ""
    for index, chunk in enumerate(chunks, 1):
        query, stock_query = search_terms(chunk, previous)
        previous = query
        duration = round(max(3, min(6, len(chunk.split()) * 60 / 150)), 2)
        shots.append({"id": index, "text": chunk, "seconds": duration,
                      "query": query, "stock_query": stock_query,
                      "asset": None, "status": "unsearched"})
    return {"version": 1, "format": "9:16" if portrait else "16:9",
            "shots": shots, "total_script_shots": len(split_script(text))}


def metadata_value(metadata, key):
    value = metadata.get(key, {})
    return html.unescape(re.sub(r"<[^>]*>", "", value.get("value", ""))).strip()


def commons_search(query, max_bytes=60_000_000):
    params = {"action": "query", "format": "json", "formatversion": "2", "generator": "search",
              "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo",
              "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 1280,
              "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|Restrictions|AttributionRequired",
              "maxlag": 5}
    data = request_json(COMMONS_API + "?" + urllib.parse.urlencode(params))
    results = []
    for page in data.get("query", {}).get("pages", []):
        info = next(iter(page.get("imageinfo", [])), {})
        mime = info.get("mime", "")
        if mime not in MEDIA_SUFFIX or info.get("size", max_bytes + 1) > max_bytes:
            continue
        meta = info.get("extmetadata", {})
        license_name = metadata_value(meta, "LicenseShortName")
        # If license cannot be determined or reuse needs individual review, do not download.
        if not ALLOWED_LICENSE.match(license_name) or metadata_value(meta, "Restrictions"):
            continue
        kind = "video" if mime.startswith("video/") else "image"
        url = info.get("url") if kind == "video" else info.get("thumburl", info.get("url"))
        if not url:
            continue
        results.append({"kind": kind, "url": url, "page": info.get("descriptionurl", ""),
                        "title": page.get("title", ""), "author": metadata_value(meta, "Artist"),
                        "credit": metadata_value(meta, "Credit"), "license": license_name,
                        "license_url": metadata_value(meta, "LicenseUrl"), "provider": "Wikimedia Commons",
                        "historical": True, "mime": mime})
    return results


def pexels_video_search(query, api_key):
    params = urllib.parse.urlencode({"query": query, "per_page": 12})
    data = request_json(PEXELS_API + "?" + params, {"Authorization": api_key})
    results = []
    for video in data.get("videos", []):
        files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")]
        files.sort(key=lambda f: abs((f.get("width") or 1280) - 1280))
        if not files:
            continue
        results.append({"kind": "video", "url": files[0]["link"], "page": video.get("url", ""),
                        "title": f"Pexels video {video.get('id')}", "author": video.get("user", {}).get("name", ""),
                        "license": "Pexels License", "license_url": "https://www.pexels.com/license/",
                        "provider": "Pexels", "historical": False, "mime": "video/mp4"})
    return results


def choose_assets(plan, pexels_key="", fetch_commons=commons_search, fetch_pexels=pexels_video_search, progress=None):
    cache = {}
    usage = {}
    for shot in plan["shots"]:
        if shot.get("asset") or shot.get("status") == "skip":
            continue
        query = shot["query"]
        if query not in cache:
            try:
                cache[query] = fetch_commons(query)
            except RuntimeError as exc:
                if progress:
                    progress(f"Предупреждение: {exc}")
                elif sys.stderr:
                    print(f"Предупреждение: {exc}", file=sys.stderr)
                cache[query] = []
        candidates = cache[query]
        # Commons archive imagery takes priority; generic stock is only an optional fallback.
        if not candidates and pexels_key:
            stock = shot.get("stock_query") or "Ukraine landscape"
            key = "pexels:" + stock
            if key not in cache:
                try:
                    cache[key] = fetch_pexels(stock, pexels_key)
                except RuntimeError as exc:
                    if progress:
                        progress(f"Предупреждение: {exc}")
                    elif sys.stderr:
                        print(f"Предупреждение: {exc}", file=sys.stderr)
                    cache[key] = []
            candidates = cache[key]
        if candidates:
            # Rotate within each query to avoid repeating exactly the same photograph.
            key = query if cache[query] else "pexels:" + (shot.get("stock_query") or "Ukraine landscape")
            offset = usage.get(key, 0)
            candidates = sorted(candidates, key=lambda a: a["kind"] != "video")
            shot["asset"] = candidates[offset % len(candidates)].copy()
            shot["status"] = "selected"
            usage[key] = offset + 1
        else:
            shot["status"] = "needs_review"
        if progress:
            progress(f"Поиск: кадр {shot['id']}/{len(plan['shots'])} — {shot['status']}")
    return plan


def download(url, dest, max_bytes=65_000_000):
    if dest.exists() and dest.stat().st_size:
        return
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Разрешены только HTTPS-адреса")
    temp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=35) as response, temp.open("wb") as target:
            total = 0
            while True:
                data = response.read(1024 * 1024)
                if not data:
                    break
                total += len(data)
                if total > max_bytes:
                    raise ValueError("Файл больше лимита 65 МБ")
                target.write(data)
        temp.replace(dest)
    finally:
        temp.unlink(missing_ok=True)


def probe_duration(path):
    p = subprocess.run([program("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        return float(p.stdout)
    except ValueError:
        return 0


def execute(args):
    result = subprocess.run(args, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError((result.stderr or "FFmpeg завершился с ошибкой")[-1800:])


def program(name):
    """Find binaries bundled in a PyInstaller build, or in the normal PATH."""
    filename = name + (".exe" if os.name == "nt" else "")
    roots = [Path(sys.executable).resolve().parent / "_ffmpeg"] if getattr(sys, "frozen", False) else []
    roots += [Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))]
    for root in roots:
        binary = root / filename
        if binary.is_file():
            return str(binary)
    return shutil.which(name)


def render(plan, output, voiceover=None, progress=None):
    if not program("ffmpeg") or not program("ffprobe"):
        raise RuntimeError("Не найдены ffmpeg и ffprobe. Установите их и добавьте в PATH")
    progress = progress or print
    shots = [s for s in plan["shots"] if s.get("status") != "skip"]
    missing = [s["id"] for s in shots if not s.get("asset")]
    if missing:
        raise RuntimeError(f"Нет проверенного материала для кадров {missing[:20]}; исправьте storyboard.json")
    if not shots:
        raise RuntimeError("Нет кадров для монтажа")
    work = output.parent / (output.stem + "_work")
    media = work / "media"
    media.mkdir(parents=True, exist_ok=True)
    width, height = (540, 960) if plan.get("format") == "9:16" else (960, 540)
    durations = [float(s["seconds"]) for s in shots]
    if voiceover:
        voice_len = probe_duration(voiceover)
        if voice_len <= 0:
            raise RuntimeError("Не удалось прочитать длительность озвучки")
        scale = voice_len / sum(durations)
        durations = [duration * scale for duration in durations]
    clips = []
    credits = []
    for i, (shot, duration) in enumerate(zip(shots, durations), 1):
        asset = shot["asset"]
        mime = asset.get("mime", "image/jpeg")
        suffix = MEDIA_SUFFIX.get(mime, ".mp4" if asset["kind"] == "video" else ".jpg")
        source = Path(asset["local_file"]) if asset.get("local_file") else media / (hashlib.sha256(asset["url"].encode()).hexdigest()[:20] + suffix)
        if not asset.get("local_file"):
            download(asset["url"], source)
        clip = work / f"shot_{i:04d}.mp4"
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=25,format=yuv420p"
        common = [program("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
        if asset["kind"] == "video":
            length = probe_duration(source)
            if length < 0.25:
                raise RuntimeError(f"Повреждённый видеофайл для кадра {shot['id']}")
            start = min(max(0, (length - duration) / 2), length * .3)
            # Short sources loop to fill the allotted voiceover interval.
            command = common + ["-stream_loop", "-1", "-ss", f"{start:.3f}", "-i", str(source),
                                "-t", f"{duration:.3f}", "-an", "-vf", vf]
        else:
            command = common + ["-loop", "1", "-framerate", "25", "-i", str(source),
                                "-t", f"{duration:.3f}", "-vf", vf]
        execute(command + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p", str(clip)])
        clips.append(clip)
        credits.append({"shot": shot["id"], "text": shot["text"], "provider": asset.get("provider"),
                        "title": asset.get("title"), "author": asset.get("author"),
                        "credit": asset.get("credit"), "license": asset.get("license"),
                        "license_url": asset.get("license_url"), "source": asset.get("page"),
                        "illustrative_stock": not asset.get("historical", False)})
        progress(f"Кадр {i}/{len(shots)} готов")
    concat = work / "concat.txt"
    # All clips are produced by this tool in the same codec, size and frame rate.
    concat.write_text("".join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in clips), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [program("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat)]
    if voiceover:
        command += ["-i", str(voiceover), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "160k", "-shortest"]
    else:
        command += ["-c", "copy"]
    execute(command + ["-movflags", "+faststart", str(output)])
    (output.parent / "credits.json").write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(f"Готово: {output}")


def main():
    parser = argparse.ArgumentParser(description="Автоматический черновой монтаж по сценарию")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Разбить текст на кадры, без доступа к сети")
    p.add_argument("script", type=Path)
    p.add_argument("--out", type=Path, default=Path("storyboard.json"))
    p.add_argument("--max-clips", type=int, default=12, help="0 = весь сценарий")
    p.add_argument("--landscape", action="store_true")
    s = sub.add_parser("search", help="Подобрать материалы через интернет")
    s.add_argument("storyboard", type=Path)
    s.add_argument("--pexels", action="store_true", help="Разрешить современный сток для пустых кадров; нужен PEXELS_API_KEY")
    r = sub.add_parser("render", help="Скачать и смонтировать MP4")
    r.add_argument("storyboard", type=Path)
    r.add_argument("--out", type=Path, default=Path("rough_cut.mp4"))
    r.add_argument("--voiceover", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            if args.max_clips < 0:
                parser.error("--max-clips должен быть неотрицательным")
            result = make_plan(args.script.read_text(encoding="utf-8-sig"), args.max_clips, not args.landscape)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{len(result['shots'])} кадров записано в {args.out}; всего в сценарии: {result['total_script_shots']}")
        elif args.command == "search":
            result = json.loads(args.storyboard.read_text(encoding="utf-8"))
            key = os.environ.get("PEXELS_API_KEY", "") if args.pexels else ""
            if args.pexels and not key:
                parser.error("Задайте PEXELS_API_KEY перед --pexels")
            choose_assets(result, key)
            args.storyboard.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            count = sum(bool(s.get("asset")) for s in result["shots"])
            print(f"Подобрано {count}/{len(result['shots'])}; проверьте storyboard.json перед рендером")
        else:
            result = json.loads(args.storyboard.read_text(encoding="utf-8"))
            render(result, args.out, args.voiceover)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
