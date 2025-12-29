#!/usr/bin/env python3
import os
import json
import random
import shutil
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import numpy as np
from PIL import (
    Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter, ImageChops, ImageOps,
    UnidentifiedImageError
)

ROOT = Path(__file__).parent
WORK = ROOT / "_arg_work"
ASSETS = WORK / "assets"
FRAMES = WORK / "frames"
OUTPUT = ROOT / "output"

CONFIG_PATH = ROOT / "config.json"
USER_IMAGE = ROOT / "user_image.png"  # optional

WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WIKI_RANDOM_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


# -----------------------------
# helpers
# -----------------------------
def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed:\n{cmd}\n\n{p.stderr[:1600]}")


def safe_get(url, params=None, timeout=25):
    r = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "arg-analogue-horror/1.1 (github actions)"},
        allow_redirects=True
    )
    r.raise_for_status()
    return r


def ensure_dirs():
    for d in (WORK, ASSETS, FRAMES, OUTPUT):
        d.mkdir(parents=True, exist_ok=True)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def now_seed():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    # deterministic per run, but different each run
    seed = (int(stamp[-8:]) ^ random.randint(0, 2**31 - 1)) & 0xFFFFFFFF
    return stamp, seed


def pick_font(size=22):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return ImageFont.truetype(c, size=size)
    return ImageFont.load_default()


def wrap_text(s, width=56):
    return textwrap.fill(s, width=width)


# -----------------------------
# online fetch (permissive-ish)
# -----------------------------
def fetch_wikimedia_images(n=8):
    """
    Fetch random Commons files. We only accept mime types we can decode reliably in CI.
    """
    imgs = []
    tries = 0
    while len(imgs) < n and tries < n * 10:
        tries += 1
        params = {
            "action": "query",
            "format": "json",
            "generator": "random",
            "grnnamespace": 6,  # File:
            "grnlimit": 1,
            "prop": "imageinfo",
            "iiprop": "url|mime|extmetadata",
        }
        try:
            data = safe_get(WIKIMEDIA_API, params=params).json()
        except Exception:
            continue

        pages = (data.get("query") or {}).get("pages") or {}
        for _, page in pages.items():
            ii = (page.get("imageinfo") or [{}])[0]
            url = ii.get("url")
            mime = (ii.get("mime") or "").lower()
            meta = ii.get("extmetadata") or {}
            lic = (meta.get("LicenseShortName") or {}).get("value", "")

            allowed_license = any(x in lic for x in ["CC0", "Public domain", "CC BY", "CC-BY", "CC BY-SA", "CC-BY-SA"])
            allowed_mime = mime in ("image/jpeg", "image/png")  # keep it simple + reliable

            if url and allowed_license and allowed_mime:
                imgs.append({"url": url, "license": lic, "mime": mime})
    return imgs


def download_image(url: str, out_path: Path) -> None:
    r = safe_get(url, timeout=30)
    ctype = (r.headers.get("Content-Type") or "").lower()

    if "text/html" in ctype or "application/json" in ctype:
        raise RuntimeError(f"Non-image response (Content-Type={ctype})")

    out_path.write_bytes(r.content)

    # verify decodable
    try:
        with Image.open(out_path) as im:
            im.verify()
    except Exception as e:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"Unreadable image: {e}")


def fetch_wikipedia_snippets(n=6):
    snippets = []
    for _ in range(n):
        try:
            j = safe_get(WIKI_RANDOM_SUMMARY, timeout=20).json()
            title = (j.get("title") or "UNTITLED").strip()
            extract = (j.get("extract") or "").strip()
            if extract:
                snippets.append(f"{title}: {extract}")
        except Exception:
            continue
    return snippets


def fetch_weather(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,wind_speed_10m,weather_code",
    }
    j = safe_get(OPEN_METEO, params=params, timeout=20).json()
    cur = j.get("current") or {}
    return {
        "temp": cur.get("temperature_2m", "?"),
        "wind": cur.get("wind_speed_10m", "?"),
        "code": cur.get("weather_code", "?")
    }


def pseudo_traffic(seed):
    random.seed(seed ^ 0xA11CE)
    roads = ["TRANSIT RD", "WALDEN AVE", "BROADWAY", "I-90", "RING ROAD", "VIA NAZIONALE", "VIA DEL CORSO"]
    states = ["CLEAR", "SLOW", "STOP/GO", "INCIDENT", "LANE CLOSED", "LOW VISIBILITY"]
    return f"TRAFFIC: {random.choice(roads)} {random.choice(states)} ({random.randint(5,45)} MIN DELAY)"


# -----------------------------
# VHS effects (kept lightweight)
# -----------------------------
def add_scanlines(img: Image.Image, alpha=0.15):
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    a = int(255 * alpha)
    for y in range(0, h, 2):
        d.line([(0, y), (w, y)], fill=(0, 0, 0, a))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def add_noise(img: Image.Image, amount=0.10):
    arr = np.array(img).astype(np.int16)
    n = np.random.normal(0, 255 * amount, size=arr.shape).astype(np.int16)
    out = np.clip(arr + n, 0, 255).astype(np.uint8)
    return Image.fromarray(out)


def chroma_shift(img: Image.Image, px=2):
    r, g, b = img.split()
    r = ImageChops.offset(r, px, 0)
    b = ImageChops.offset(b, -px, 0)
    return Image.merge("RGB", (r, g, b))


def tracking_tear(img: Image.Image):
    w, h = img.size
    y = random.randint(80, h - 120)
    band_h = random.randint(18, 70)
    dx = random.randint(-35, 35)
    band = img.crop((0, y, w, y + band_h))
    img.paste(band, (dx, y))
    return img


def vhs_overlay(img: Image.Image, t, fps, label="CH-03"):
    draw = ImageDraw.Draw(img)
    f1 = pick_font(18)
    draw.rectangle((14, 14, 390, 135), fill=(0, 0, 0))
    sec = int(t)
    frame = int((t - sec) * fps)
    timecode = f"{(sec//3600)%24:02d}:{(sec//60)%60:02d}:{sec%60:02d}:{frame:02d}"
    signal = random.randint(18, 99)
    bitrate = random.randint(140, 560)
    err = random.randint(1, 9) if random.random() < 0.10 else 0
    lines = [
        "REC   VCR:PLAY",
        f"TC {timecode}  {label}",
        f"SIGNAL {signal}%   BITRATE {bitrate}kbps",
        f"ERR {err}",
    ]
    y = 26
    for s in lines:
        draw.text((28, y), s, font=f1, fill=(200, 255, 245))
        y += 26
    return img


# -----------------------------
# scene cards
# -----------------------------
def make_card(w, h, title, body, danger=False):
    img = Image.new("RGB", (w, h), (5, 8, 10))
    draw = ImageDraw.Draw(img)
    f_title = pick_font(44)
    f_body = pick_font(22)

    border = (255, 90, 140) if danger else (120, 255, 220)
    draw.rectangle((40, 120, w - 40, h - 120), fill=(0, 0, 0), outline=border, width=4)
    draw.text((70, 160), title, font=f_title, fill=border)

    y = 240
    for line in wrap_text(body, 64).split("\n"):
        draw.text((70, y), line, font=f_body, fill=(210, 255, 245))
        y += 30
    return img


def missing_person_card(w, h, face_img: Image.Image, seed, location_name="UNKNOWN"):
    bg = Image.new("RGB", (w, h), (2, 5, 6))
    draw = ImageDraw.Draw(bg)
    f_big = pick_font(60)
    f = pick_font(22)

    draw.rectangle((60, 90, w - 60, h - 90), fill=(0, 0, 0), outline=(255, 90, 140), width=4)
    draw.text((90, 130), "MISSING", font=f_big, fill=(255, 90, 140))

    draw.rectangle((90, 220, 360, 560), fill=(10, 18, 18), outline=(120, 255, 220), width=2)
    face = face_img.copy().convert("RGB").resize((270, 340))
    face = ImageEnhance.Contrast(face).enhance(1.15)
    face = face.filter(ImageFilter.GaussianBlur(radius=0.6))
    bg.paste(face, (90, 220))

    random.seed(seed ^ 0xBADA55)
    age = random.randint(17, 23)
    last_seen = random.choice(["TRANSIT RD", "PARK ACCESS", "BUS STOP", "NEAR WALDEN", "NEAR STATION"])

    lines = [
        ("NAME", "J. DOE (UNCONFIRMED)"),
        ("AGE", str(age)),
        ("LAST SEEN", f"{last_seen} / {location_name}"),
        ("CLOTHING", "COSTUME / UNKNOWN"),
        ("CONTACT", "REPORT IMMEDIATELY"),
    ]
    y = 250
    for k, v in lines:
        draw.text((390, y), f"{k}:", font=f, fill=(210, 255, 245))
        draw.text((520, y), v, font=f, fill=(180, 255, 240))
        y += 46

    draw.text((90, 595), "DO NOT APPROACH. DO NOT OFFER A RIDE.", font=f, fill=(255, 90, 140))
    return bg


def weather_traffic_crawl(weather, traffic):
    wx = f"WEATHER: {weather['temp']}C  WIND {weather['wind']}km/h  CODE {weather['code']}"
    return f"{wx}   |   {traffic}   |   REMINDER: LOCK DOORS   |"


# -----------------------------
# main render
# -----------------------------
def render_video(config):
    stamp, seed = now_seed()
    random.seed(seed)
    np.random.seed(seed)

    fps = int(config["fps"])
    dur = float(config["duration_sec"])
    w, h = int(config["width"]), int(config["height"])
    total = int(dur * fps)

    # clean work each run (keeps “always different” honest)
    if WORK.exists():
        shutil.rmtree(WORK)
    ensure_dirs()

    # online content
    weather = fetch_weather(config["lat"], config["lon"])
    traffic = pseudo_traffic(seed)
    crawl = weather_traffic_crawl(weather, traffic)
    snippets = fetch_wikipedia_snippets(int(config.get("fetch_text_snippets", 6)))

    # fetch + parallel download
    img_meta = fetch_wikimedia_images(int(config.get("fetch_images", 6)))
    downloaded: list[Path] = []

    def dl_task(i, url):
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in [".jpg", ".jpeg", ".png"]:
            ext = ".jpg"
        out_path = ASSETS / f"img_{i:02d}{ext}"
        download_image(url, out_path)
        return out_path

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = []
        for i, m in enumerate(img_meta):
            futures.append(ex.submit(dl_task, i, m["url"]))
        for fut in as_completed(futures):
            try:
                downloaded.append(fut.result())
            except Exception:
                continue

    if not downloaded:
        noise = Image.fromarray(np.random.randint(0, 255, (h, w, 3), dtype=np.uint8))
        p = ASSETS / "fallback.png"
        noise.save(p)
        downloaded = [p]

    # preload and resize backgrounds ONCE (huge speed gain)
    preloaded_bg: list[Image.Image] = []
    for p in downloaded:
        try:
            with Image.open(p) as im:
                preloaded_bg.append(im.convert("RGB").resize((w, h)))
        except Exception:
            continue
    if not preloaded_bg:
        preloaded_bg = [Image.fromarray(np.random.randint(0, 255, (h, w, 3), dtype=np.uint8))]

    # face source
    if USER_IMAGE.exists():
        base_face = Image.open(USER_IMAGE).convert("RGB")
    else:
        base_face = preloaded_bg[0].copy()

    missing = missing_person_card(w, h, base_face, seed, config.get("location_name", "UNKNOWN"))
    entity = make_card(
        w, h,
        "PUBLIC SAFETY BULLETIN",
        "BE AWARE OF AN UNKNOWN ENTITY LURKING NEAR LOW-LIGHT ROADS. "
        "IT MIMICS FAMILIAR VOICES AND MAY APPEAR AS SOMEONE YOU RECOGNIZE. "
        "IF ENCOUNTERED: DO NOT SPEAK. DO NOT FOLLOW. DO NOT ACKNOWLEDGE IT. "
        "LEAVE THE AREA. GO TO A LIT LOCATION. REPORT IMMEDIATELY.",
        danger=True
    )
    endcard = make_card(
        w, h,
        "END OF TRANSMISSION",
        "IF YOU HAVE INFORMATION REGARDING ANY MISSING PERSONS, CONTACT LOCAL AUTHORITIES. "
        "DO NOT SEARCH ALONE. DO NOT ENTER WOODED AREAS AFTER DARK. "
        "IF YOU HEAR YOUR NAME CALLED FROM THE DARK, DO NOT ANSWER.",
        danger=False
    )

    jumpscare_t = float(config.get("jumpscare_time_sec", 34.0))

    def scene_at(t):
        if t < 8.0:
            return "NORMAL"
        if t < 18.0:
            return "MISSING"
        if t < 28.0:
            return "ENTITY"
        if t < jumpscare_t - 1.0:
            return "TRAFFIC"
        if t < jumpscare_t + 0.25:
            return "JUMP"
        if t < jumpscare_t + 2.0:
            return "ERROR"
        return "END"

    # jumpscare frame
    def make_jumpscare():
        img = preloaded_bg[-1].copy()
        img = ImageEnhance.Contrast(img).enhance(2.2)
        img = ImageEnhance.Color(img).enhance(0.15)
        img = img.filter(ImageFilter.DETAIL)
        img = ImageOps.invert(img.convert("RGB"))
        img = ImageOps.posterize(img, bits=3)
        d = ImageDraw.Draw(img)
        d.text((60, h // 2 - 40), "LOOK AWAY", font=pick_font(92), fill=(255, 90, 140))
        return img

    jumpscare = make_jumpscare()

    # render frames as JPG (fast!)
    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True, exist_ok=True)

    for i in range(total):
        t = i / fps
        bg = preloaded_bg[i % len(preloaded_bg)].copy()

        # tiny motion without re-decoding
        if random.random() < 0.40:
            bg = ImageChops.offset(bg, random.randint(-4, 4), random.randint(-3, 3))

        sc = scene_at(t)
        if sc == "NORMAL":
            frame = bg
        elif sc == "MISSING":
            frame = Image.blend(bg, missing, 0.78)
        elif sc == "ENTITY":
            frame = Image.blend(bg, entity, 0.82)
        elif sc == "TRAFFIC":
            frame = bg
            d = ImageDraw.Draw(frame)
            d.rectangle((50, 120, w - 50, 280), fill=(0, 0, 0))
            d.text((70, 150), "TRAFFIC & WEATHER SERVICE", font=pick_font(34), fill=(180, 255, 240))
            s = snippets[i % max(1, len(snippets))] if snippets else "NO DATA"
            d.text((70, 210), wrap_text(s, 78)[:420], font=pick_font(18), fill=(210, 255, 245))
        elif sc == "JUMP":
            frame = jumpscare.copy()
        elif sc == "ERROR":
            frame = make_card(w, h, "SIGNAL LOST", "DATA CORRUPT. FRAME REUSE ENGAGED. DO NOT ADJUST YOUR SET.", danger=True)
        else:
            frame = Image.blend(bg, endcard, 0.82)

        # ticker crawl
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, h - 48, w, h), fill=(0, 0, 0))
        x = int(w - ((t * 140) % (w + 2200)))
        draw.text((x, h - 36), crawl, font=pick_font(20), fill=(220, 255, 245))

        # glitches
        if random.random() < 0.08:
            frame = tracking_tear(frame)
        if random.random() < 0.20:
            frame = frame.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.0, 1.0)))

        # VHS pass
        if random.random() < 0.55:
            frame = chroma_shift(frame, px=int(config.get("chroma_shift_px", 3)))
        frame = add_scanlines(frame, alpha=float(config.get("scanline_alpha", 0.16)))
        frame = add_noise(frame, amount=float(config.get("vhs_noise", 0.12)))
        frame = vhs_overlay(frame, t, fps, label="CH-03")

        # JPEG frames (fast)
        frame.save(FRAMES / f"frame_{i:05d}.jpg", quality=82, subsampling=2)

    # audio build (same features, low overhead)
    bed = WORK / "bed.wav"
    voice = WORK / "voice.wav"
    mix = WORK / "mix.wav"
    jump = WORK / "jump.wav"
    final_audio = WORK / "final_audio.wav"

    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={dur}",
        "-f", "lavfi", "-i", f"sine=frequency=55:duration={dur}",
        "-f", "lavfi", "-i", f"sine=frequency=73:duration={dur}",
        "-filter_complex", "amix=inputs=3:weights='0.55 0.25 0.20',volume=0.35",
        str(bed)
    ])

    # TTS (keep feature, skip if espeak-ng missing)
    tts_ok = False
    if bool(config.get("tts_enabled", True)):
        script_lines = [
            "This is a local emergency broadcast.",
            "Missing person notice. Do not approach.",
            "Be aware of an unknown entity lurking near low light roads.",
            "If encountered: do not speak. Do not follow. Leave the area immediately.",
            "Report immediately. End of transmission."
        ]
        try:
            run(["espeak-ng", "-s", "135", "-p", "18", "-a", "110", "-w", str(voice), " ".join(script_lines)])
            tts_ok = True
        except Exception:
            tts_ok = False

    audio_in = bed
    if tts_ok:
        run([
            "ffmpeg", "-y",
            "-i", str(bed), "-i", str(voice),
            "-filter_complex", "amix=inputs=2:weights='0.70 1.0',volume=1.0",
            str(mix)
        ])
        audio_in = mix

    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anoisesrc=color=white:duration=0.35",
        "-filter:a", "volume=0.9",
        str(jump)
    ])

    run([
        "ffmpeg", "-y",
        "-i", str(audio_in),
        "-i", str(jump),
        "-filter_complex",
        f"[0:a]asetrate=44100,aresample=44100[a0];"
        f"[1:a]adelay={int(jumpscare_t*1000)}|{int(jumpscare_t*1000)}[a1];"
        f"[a0][a1]amix=inputs=2:weights='1.0 1.2',volume=1.0",
        str(final_audio)
    ])

    # encode video faster
    tmp_vid = WORK / "temp_video.mp4"
    out_path = OUTPUT / f"arg_{stamp}_{seed}.mp4"

    run([
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(FRAMES / "frame_%05d.jpg"),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        str(tmp_vid)
    ])

    run([
        "ffmpeg", "-y",
        "-i", str(tmp_vid),
        "-i", str(final_audio),
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(out_path)
    ])

    print(f"OK: wrote {out_path}")


def main():
    config = load_config()
    render_video(config)


if __name__ == "__main__":
    main()


