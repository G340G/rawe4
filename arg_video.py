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

WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WIKI_RANDOM_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


# -----------------------------
# subprocess / io
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
        headers={"User-Agent": "arg-analogue-horror/1.2 (github actions)"},
        allow_redirects=True,
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
    seed = (int(stamp[-8:]) ^ random.randint(0, 2**31 - 1)) & 0xFFFFFFFF
    return stamp, seed

def pick_font(size=22):
    # match that crude HTML monospace vibe
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return ImageFont.truetype(c, size=size)
    return ImageFont.load_default()

def wrap_text(s, width=64):
    return textwrap.fill(s, width=width)


# -----------------------------
# Online content (permissive-ish)
# -----------------------------
def fetch_wikimedia_images(n=6):
    imgs = []
    tries = 0
    while len(imgs) < n and tries < n * 10:
        tries += 1
        params = {
            "action": "query",
            "format": "json",
            "generator": "random",
            "grnnamespace": 6,   # File:
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
            allowed_mime = mime in ("image/jpeg", "image/png")  # reliable on CI

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
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"Unreadable image: {e}")

def fetch_wikipedia_snippets(n=6):
    out = []
    for _ in range(n):
        try:
            j = safe_get(WIKI_RANDOM_SUMMARY, timeout=20).json()
            title = (j.get("title") or "UNTITLED").strip()
            extract = (j.get("extract") or "").strip()
            if extract:
                out.append(f"{title}: {extract}")
        except Exception:
            continue
    return out

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
    roads = ["I-90", "SR-400", "TRANSIT RD", "WALDEN AVE", "BROADWAY", "RING ROAD"]
    states = ["CLEAR", "SLOW", "STOP/GO", "INCIDENT", "LANE CLOSED", "LOW VISIBILITY"]
    return f"TRAFFIC: {random.choice(roads)} {random.choice(states)} ({random.randint(5,45)} MIN DELAY)"


# -----------------------------
# VHS + crude HTML style primitives
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
    y = random.randint(60, h - 120)
    band_h = random.randint(18, 70)
    dx = random.randint(-35, 35)
    band = img.crop((0, y, w, y + band_h))
    img.paste(band, (dx, y))
    return img

def draw_crude_hud(img: Image.Image, t: float, fps: int, mode: str, label="CH-03", logo: Image.Image | None = None):
    # Mirrors your HTML HUD vibe: neon mono, boxes, fake stats
    w, h = img.size
    d = ImageDraw.Draw(img)
    f = pick_font(14)

    # panel
    d.rectangle((12, 12, 350, 125), fill=(0, 0, 0))
    d.rectangle((12, 12, 350, 125), outline=(120, 255, 220), width=1)

    sec = int(t)
    frame = int((t - sec) * fps)
    # fake 1982-ish timestamp like the HTML
    base = datetime(1982, 10, 31, 3, 12, 0, tzinfo=timezone.utc)
    stamp = (base.timestamp() + sec)
    ts = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z"

    bitrate = random.randint(120, 420)
    sig = random.randint(12, 99)
    drop = "YES" if random.random() < 0.15 else "NO"
    err = random.randint(1, 9) if random.random() < 0.10 else 0

    lines = [
        f"REC  {label}   VCR: PLAY",
        f"UTC {ts}",
        f"BITRATE {bitrate}kbps   SIGNAL {sig}%",
        f"FRAME {str(sec*fps).zfill(6)}   DROP {drop}",
        f"MODE: {mode}   ERR:{err}",
    ]
    y = 28
    for s in lines:
        d.text((24, y), s, font=f, fill=(190, 255, 240))
        y += 18

    if logo is not None:
        try:
            lw, lh = logo.size
            thumb = logo.resize((64, int(64*lh/lw))).convert("RGBA")
            img.paste(thumb, (w - 84, 16), thumb)
        except Exception:
            pass

def draw_crude_ticker(img: Image.Image, t: float, weather: dict, traffic: str):
    w, h = img.size
    d = ImageDraw.Draw(img)
    f = pick_font(18)

    cond = random.choice(["CLEAR", "PARTLY CLOUDY", "LIGHT RAIN", "FOG", "LOW VISIBILITY"])
    wx = f"WEATHER: {cond}  {weather['temp']}C  WIND {weather['wind']}km/h"
    msg = f"{wx}   |   {traffic}   |   REMINDER: CHECK SMOKE ALARMS   |"
    x = int(w - ((t * 140) % (w + 2200)))

    d.rectangle((0, h - 44, w, h), fill=(0, 0, 0))
    d.text((x, h - 34), msg, font=f, fill=(210, 255, 245))

def uncanny_still_pil(w: int, h: int, seed: float) -> Image.Image:
    # PIL version of your HTML uncannyStill: fog + silhouette + “eyes”
    random.seed(int(seed * 1000) ^ 0xC0FFEE)
    img = Image.new("RGB", (w, h), (5, 8, 10))
    d = ImageDraw.Draw(img)

    # radial-ish fog via layered ellipses
    cx = int(w * 0.55 + math.sin(seed) * 30)
    cy = int(h * 0.58 + math.cos(seed * 1.3) * 20)

    for r in range(520, 0, -25):
        a = int(255 * (1 - r / 520) * 0.20)
        col = (80, 120, 110, a)
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    # silhouette
    sil = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sil)
    sd.ellipse((cx - 140, cy - 240, cx + 140, cy + 240), fill=(0, 0, 0, 220))
    img = Image.alpha_composite(img.convert("RGBA"), sil).convert("RGB")

    # eyes
    eye = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ed = ImageDraw.Draw(eye)
    ed.ellipse((cx - 64, cy - 50, cx - 20, cy - 30), fill=(220, 255, 250, 20))
    ed.ellipse((cx + 20, cy - 50, cx + 64, cy - 30), fill=(220, 255, 250, 20))
    img = Image.alpha_composite(img.convert("RGBA"), eye).convert("RGB")

    # speckle + blur
    arr = np.array(img).astype(np.int16)
    speck = np.random.randint(0, 255, arr.shape, dtype=np.int16)
    mask = (np.random.rand(h, w, 3) < 0.06)
    arr = np.where(mask, np.clip(arr + (speck * 0.10), 0, 255), arr)
    img = Image.fromarray(arr.astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius=0.8))
    return img


# -----------------------------
# content cards
# -----------------------------
def make_card(w, h, title, body, danger=False):
    img = Image.new("RGB", (w, h), (3, 6, 7))
    d = ImageDraw.Draw(img)
    ft = pick_font(38)
    fb = pick_font(20)
    border = (255, 90, 140) if danger else (120, 255, 220)

    d.rectangle((40, 140, w - 40, h - 120), fill=(0, 0, 0))
    d.rectangle((40, 140, w - 40, h - 120), outline=border, width=2)
    d.text((80, 205), title, font=ft, fill=border)

    y = 260
    for line in wrap_text(body, 70).split("\n"):
        d.text((80, y), line, font=fb, fill=(210, 255, 245))
        y += 28
    return img

def load_user_assets(cfg):
    ua = (cfg.get("user_assets") or {})
    def load_img(name):
        p = ROOT / str(name)
        if p.exists():
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                return None
        return None

    custom_lines = []
    tfile = ua.get("custom_text_file")
    if tfile:
        p = ROOT / str(tfile)
        if p.exists():
            try:
                custom_lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            except Exception:
                pass

    return {
        "use_user_images": bool(ua.get("use_user_images", True)),
        "face": load_img(ua.get("user_face", "user_image.png")),
        "bg": load_img(ua.get("user_bg", "user_bg.png")),
        "logo": load_img(ua.get("user_logo", "user_logo.png")),
        "custom_lines": custom_lines
    }

def missing_person_card(w, h, face_rgb: Image.Image, pz: dict):
    img = Image.new("RGB", (w, h), (2, 5, 6))
    d = ImageDraw.Draw(img)

    f_big = pick_font(52)
    f = pick_font(20)
    border = (255, 90, 140)

    d.rectangle((70, 110, w - 70, h - 110), fill=(0, 0, 0))
    d.rectangle((70, 110, w - 70, h - 110), outline=border, width=3)
    d.text((95, 155), "MISSING", font=f_big, fill=border)

    # photo box
    d.rectangle((95, 225, 355, 545), fill=(10, 20, 20))
    d.rectangle((95, 225, 355, 545), outline=(200, 255, 245), width=1)

    face = face_rgb.resize((260, 320)).filter(ImageFilter.GaussianBlur(radius=0.6))
    face = ImageEnhance.Contrast(face).enhance(1.15)
    img.paste(face, (95, 225))

    # details
    name = pz.get("missing_name", "J. DOE (UNCONFIRMED)")
    age_lo, age_hi = pz.get("missing_age_range", [17, 22])
    age = str(random.randint(int(age_lo), int(age_hi)))
    last_seen = pz.get("last_seen", "NEAR TRANSIT RD / WALDEN")
    contact = pz.get("contact_line", "REPORT IMMEDIATELY")

    labels = [("NAME", name), ("AGE", age), ("LAST SEEN", last_seen), ("CLOTHING", "COSTUME / UNKNOWN"), ("CONTACT", contact)]
    y = 260
    for k, v in labels:
        d.text((385, y), f"{k}:", font=f, fill=(210, 255, 245))
        d.text((500, y), v, font=f, fill=(180, 255, 240))
        y += 44

    d.text((95, 585), "DO NOT APPROACH. DO NOT OFFER A RIDE.", font=f, fill=border)
    return img


# -----------------------------
# render pipeline
# -----------------------------
def render_video(cfg):
    stamp, seed = now_seed()
    random.seed(seed)
    np.random.seed(seed)

    fps = int(cfg["fps"])
    dur = float(cfg["duration_sec"])
    w, h = int(cfg["width"]), int(cfg["height"])
    total = int(dur * fps)

    # Clean work each run
    if WORK.exists():
        shutil.rmtree(WORK)
    ensure_dirs()
    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True, exist_ok=True)

    # load personalization + user assets
    pz = (cfg.get("personalization") or {})
    style_mix = (cfg.get("style_mix") or {})
    user_assets = load_user_assets(cfg)

    # Online content
    weather = fetch_weather(cfg["lat"], cfg["lon"])
    traffic = pseudo_traffic(seed)
    snippets = fetch_wikipedia_snippets(int(cfg.get("fetch_text_snippets", 6)))

    # Append custom lines (optional)
    custom_lines = user_assets["custom_lines"]
    if custom_lines:
        snippets = (snippets + custom_lines)[: max(4, len(snippets))]

    # Wikimedia images: fetch + parallel download
    img_meta = fetch_wikimedia_images(int(cfg.get("fetch_images", 6)))
    downloaded: list[Path] = []

    def dl_task(i, url):
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in [".jpg", ".jpeg", ".png"]:
            ext = ".jpg"
        out_path = ASSETS / f"img_{i:02d}{ext}"
        download_image(url, out_path)
        return out_path

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(dl_task, i, m["url"]) for i, m in enumerate(img_meta)]
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

    # Preload bg images
    preloaded_bg: list[Image.Image] = []
    for p in downloaded:
        try:
            with Image.open(p) as im:
                preloaded_bg.append(im.convert("RGB").resize((w, h)))
        except Exception:
            continue
    if not preloaded_bg:
        preloaded_bg = [Image.fromarray(np.random.randint(0, 255, (h, w, 3), dtype=np.uint8))]

    # Face image: user > scraped
    if user_assets["use_user_images"] and user_assets["face"] is not None:
        face_rgb = user_assets["face"].convert("RGB")
    else:
        face_rgb = preloaded_bg[0].copy()

    # crude HTML backgrounds: user_bg or generated uncannyStill
    def crude_bg(t):
        if user_assets["use_user_images"] and user_assets["bg"] is not None:
            bg = user_assets["bg"].convert("RGB").resize((w, h))
        else:
            bg = uncanny_still_pil(w, h, seed=(t*0.7 + (seed % 997)))
        return bg

    # build key cards
    missing = missing_person_card(w, h, face_rgb, pz)
    entity_name = pz.get("entity_name", "UNKNOWN ENTITY")
    rules = pz.get("entity_rules") or [
        "DO NOT SPEAK.",
        "DO NOT FOLLOW.",
        "DO NOT ACKNOWLEDGE IT.",
        "LEAVE THE AREA.",
        "GO TO A LIT LOCATION."
    ]
    entity = make_card(
        w, h,
        "PUBLIC SAFETY BULLETIN",
        f"BE AWARE OF AN {entity_name} LURKING NEAR LOW-LIGHT ROADS AND PARK ACCESS POINTS. "
        f"IT MIMICS FAMILIAR VOICES AND MAY APPEAR AS SOMEONE YOU RECOGNIZE. "
        f"IF ENCOUNTERED: {' '.join(rules)}",
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

    jumpscare_t = float(cfg.get("jumpscare_time_sec", 34.0))

    # style weights
    use_crude = bool(style_mix.get("use_crude_html_style", True))
    crude_w = float(style_mix.get("crude_html_weight", 0.45))
    scraped_w = float(style_mix.get("scraped_weight", 0.55))
    if not use_crude:
        crude_w = 0.0
        scraped_w = 1.0

    # scenes: keep the original “broadcast rhythm” but mix styles inside segments
    def scene_at(t):
        if t < 8.0:
            return "CRUDE_NORMAL"
        if t < 18.0:
            return "MISSING_MIX"
        if t < 28.0:
            return "ENTITY_CRUDE"
        if t < jumpscare_t - 1.0:
            return "SCRAPED_INFO"
        if t < jumpscare_t + 0.25:
            return "JUMP"
        if t < jumpscare_t + 2.0:
            return "CRUDE_ERROR"
        return "END_MIX"

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

    # Render frames (JPEG for speed + crude compression vibe)
    for i in range(total):
        t = i / fps
        sc = scene_at(t)

        # scraped base
        scraped = preloaded_bg[i % len(preloaded_bg)].copy()
        if random.random() < 0.35:
            scraped = ImageChops.offset(scraped, random.randint(-4, 4), random.randint(-3, 3))

        # crude base
        crude = crude_bg(t)
        if random.random() < 0.30:
            crude = ImageChops.offset(crude, random.randint(-3, 3), random.randint(-2, 2))

        if sc == "CRUDE_NORMAL":
            frame = crude
            # add “community update” vibe like your HTML
            d = ImageDraw.Draw(frame)
            d.text((60, 80), "COMMUNITY WEATHER UPDATE", font=pick_font(26), fill=(180, 255, 240))
            d.text((60, 115), "Sunny intervals. Mild winds. Drive carefully.", font=pick_font(18), fill=(210, 255, 245))

        elif sc == "MISSING_MIX":
            # blend styles: scraped background + crude UI framing + missing poster
            frame = Image.blend(scraped, crude, crude_w)
            frame = Image.blend(frame, missing, 0.78)

        elif sc == "ENTITY_CRUDE":
            frame = Image.blend(crude, entity, 0.82)

        elif sc == "SCRAPED_INFO":
            # show scraped imagery with “crude UI overlay” box + random wiki snippet
            frame = scraped
            d = ImageDraw.Draw(frame)
            d.rectangle((50, 120, w - 50, 280), fill=(0, 0, 0))
            d.rectangle((50, 120, w - 50, 280), outline=(120, 255, 220), width=1)
            d.text((70, 150), "TRAFFIC & WEATHER SERVICE", font=pick_font(28), fill=(180, 255, 240))
            s = snippets[i % max(1, len(snippets))] if snippets else "NO DATA"
            d.text((70, 205), wrap_text(s, 78)[:420], font=pick_font(18), fill=(210, 255, 245))

        elif sc == "JUMP":
            frame = jumpscare.copy()

        elif sc == "CRUDE_ERROR":
            msg = "DATA CORRUPT. DO NOT ADJUST YOUR SET. IF YOU HEAR YOUR NAME, DO NOT ANSWER."
            frame = make_card(w, h, "SIGNAL LOST", msg, danger=True)

        else:  # END_MIX
            frame = Image.blend(scraped, crude, crude_w)
            frame = Image.blend(frame, endcard, 0.82)

        # ticker (crude HTML-style)
        draw_crude_ticker(frame, t, weather, traffic)

        # glitches + VHS
        if random.random() < 0.08:
            frame = tracking_tear(frame)
        if random.random() < 0.20:
            frame = frame.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.0, 1.0)))
        if random.random() < 0.55:
            frame = chroma_shift(frame, px=int(cfg.get("chroma_shift_px", 3)))

        frame = add_scanlines(frame, alpha=float(cfg.get("scanline_alpha", 0.16)))
        frame = add_noise(frame, amount=float(cfg.get("vhs_noise", 0.12)))

        # crude HUD on top (with optional logo)
        draw_crude_hud(frame, t, fps, mode=sc, label="CH-03", logo=user_assets["logo"])

        # save fast
        frame.save(FRAMES / f"frame_{i:05d}.jpg", quality=82, subsampling=2)

    # -----------------------------
    # audio: eerie bed + optional TTS + jumpscare burst
    # -----------------------------
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

    tts_ok = False
    if bool(cfg.get("tts_enabled", True)):
        lines = (pz.get("tts_lines") or [
            "This is a local emergency broadcast.",
            "Missing person notice. Do not approach.",
            "Be aware of an unknown entity lingering near low light roads.",
            "If encountered: do not speak. Do not follow. Leave the area immediately.",
            "Report immediately. End of transmission."
        ])
        # append custom lines, if provided
        if custom_lines:
            lines = lines + custom_lines[:4]

        try:
            run(["espeak-ng", "-s", "135", "-p", "18", "-a", "110", "-w", str(voice), " ".join(lines)])
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

    # -----------------------------
    # encode: faster settings, no feature loss
    # -----------------------------
    OUTPUT.mkdir(parents=True, exist_ok=True)
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
    cfg = load_config()
    render_video(cfg)

if __name__ == "__main__":
    main()



