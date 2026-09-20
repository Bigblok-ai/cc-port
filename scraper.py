import requests
import json
import hashlib
import re
import time
import os
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

try:
    from curl_cffi import requests as cffi_requests
    _HAVE_CURL_CFFI = True
except ImportError:
    _HAVE_CURL_CFFI = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

API_V1      = "https://api.chuoichientv.net"
API_V2      = "https://api-v2.chuoichientv.net"
SITE        = "https://live06.chuoichientv.me"
# Referer đã thấy 200 trong player (iframe nhúng). Nếu VLC test đòi giá trị khác -> sửa đây.
STREAM_REFERER = "https://fhd-01.cctvsignal.xyz/"
SITE_LOGO   = "https://media.chuoichientv.net/media/20251115_113435_320106f9.png"  # tạm dùng logo league; thay nếu có logo site

LIST_TYPES     = ("blv", "live")  # merge 2 loại danh sách; nếu thiếu trận -> thêm type khác vào đây
LIST_LIMIT     = 50
PAGE_CAP       = 5
WINDOW_PAST_H  = 6
FOOTBALL_FUT_H = 24
DETAIL_SLEEP   = 0.15

THUMBS_DIR    = "thumbs"
REPO_RAW      = os.environ.get("REPO_RAW", "")
THUMB_VERSION = "c1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": SITE + "/",
    "Origin": SITE,
}

CATE_MAP = {
    "football": "⚽ Bóng Đá", "basketball": "🏀 Bóng Rổ", "tennis": "🎾 Tennis",
    "bongchuyen": "🏐 Bóng Chuyền", "esport": "🎮 Esport", "caulong": "🏸 Cầu Lông",
    "vothuat": "🥊 Võ Thuật", "bongchay": "⚾ Bóng Chày", "duaxe": "🏎️ Đua Xe",
    "bongban": "🏓 Bóng Bàn", "billiards": "🎱 Billiards",
}
CATE_ORDER = ["football", "basketball", "tennis", "bongchuyen", "esport", "caulong",
              "vothuat", "bongchay", "duaxe", "bongban", "billiards"]

EXCLUDE_LEAGUES_AMERICA = [
    "mls", "major league soccer", "liga mx", "brasileirao", "brasileirão", "serie a brasil",
    "campeonato brasileiro", "copa do brasil", "argentine", "argentina", "liga profesional",
    "colombian", "colombia", "liga betplay", "chile", "ecuador", "peru", "venezuela",
    "paraguay", "uruguay", "bolivia", "inter miami", "la galaxy", "concacaf", "conmebol",
    "copa america", "copa sudamericana", "copa libertadores",
]

LIVE_STATUS = {"live", "1h", "2h", "ht", "et", "bt", "p", "pen", "aet", "l"}
DONE_STATUS = {"ft", "aft", "post", "pst", "canc", "abd", "awd", "wo", "susp", "int"}

# ─────────────────────────────────────────────────────────────────────────────
# TIME & UTILS
# ─────────────────────────────────────────────────────────────────────────────

VN_TZ = timezone(timedelta(hours=7))

def now_vn() -> datetime:
    return datetime.now(tz=VN_TZ)

def parse_matchtime(s: str):
    """'2026-09-20T16:30:00Z' (UTC) -> datetime giờ VN."""
    if not s: return None
    try:
        s2 = s.strip().replace("Z", "+0000")
        dt = datetime.strptime(s2[:25], "%Y-%m-%dT%H:%M:%S%z")
        return dt.astimezone(VN_TZ)
    except Exception:
        try:  # fallback: không có Z, coi như UTC
            dt = datetime.strptime(s.strip()[:19], "%Y-%m-%dT%H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).astimezone(VN_TZ)
        except Exception:
            return None

def http_get(url, headers=None, timeout=15, allow_redirects=True):
    h = dict(HEADERS); h.update(headers or {})
    if _HAVE_CURL_CFFI:
        return cffi_requests.get(url, headers=h, timeout=timeout,
                                 impersonate="chrome", allow_redirects=allow_redirects)
    return requests.get(url, headers=h, timeout=timeout, allow_redirects=allow_redirects)

def make_id(text, prefix): return f"{prefix}-{hashlib.md5(text.encode()).hexdigest()[:10]}"

def norm_text(s): return re.sub(r"\s+", " ", (s or "").strip())

def is_america_league(name):
    if not name: return False
    tl = name.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", tl) for kw in EXCLUDE_LEAGUES_AMERICA)

def get_stream_type(url):
    if not url: return "hls"
    clean = url.lower().split("?")[0]
    if clean.endswith(".flv"): return "httpflv"
    if clean.endswith(".mpd"): return "dash"
    if clean.endswith(".mp4"): return "mp4"
    return "hls"

def is_live_status(s):
    return str(s or "").strip().lower() in LIVE_STATUS

def is_done_status(s):
    return str(s or "").strip().lower() in DONE_STATUS

# ─────────────────────────────────────────────────────────────────────────────
# 1) DANH SÁCH TRẬN (defensive parsing — shape listing chưa xem trực tiếp)
# ─────────────────────────────────────────────────────────────────────────────

def find_match_lists(obj, found):
    """Đệ quy tìm mọi list mà item là dict có 'externalId' (hoặc teams+matchTime)."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and \
           ("externalId" in obj[0] or ("teams" in obj[0] and "matchTime" in obj[0])):
            found.extend([it for it in obj if isinstance(it, dict)])
        else:
            for v in obj: find_match_lists(v, found)
    elif isinstance(obj, dict):
        for v in obj.values(): find_match_lists(v, found)

def fetch_listing():
    seen = {}
    for t in LIST_TYPES:
        for page in range(1, PAGE_CAP + 1):
            url = f"{API_V2}/v2/matches?page={page}&limit={LIST_LIMIT}&type={t}"
            try:
                r = http_get(url, timeout=15)
                if r.status_code != 200:
                    print(f"  [list {t}] HTTP {r.status_code} (page {page})")
                    break
                items = []
                find_match_lists(r.json(), items)
            except Exception as e:
                print(f"  [list {t}] FAIL {type(e).__name__}: {e}")
                break
            n_new = 0
            for it in items:
                eid = str(it.get("externalId") or it.get("id") or "")
                if eid and eid not in seen:
                    seen[eid] = it; n_new += 1
            print(f"  [list {t}] page {page}: {len(items)} item (+{n_new} mới)")
            if len(items) < LIST_LIMIT:
                break
            time.sleep(0.2)
    return list(seen.values())

# ─────────────────────────────────────────────────────────────────────────────
# 2) CHI TIẾT TRẬN -> BLV + STREAM (schema đã xác minh)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_detail(external_id):
    url = f"{API_V1}/v1/matches/external/{external_id}"
    for attempt in range(2):
        try:
            r = http_get(url, timeout=12)
            if r.status_code == 200:
                d = r.json()
                return d.get("data") if isinstance(d, dict) else None
            if r.status_code == 401:
                print(f"  [detail] 401 (cần token?) | id={external_id}")
                return None
            print(f"  [detail] HTTP {r.status_code} | id={external_id}")
            return None
        except Exception as e:
            if attempt == 0: time.sleep(1)
            else: print(f"  [detail] FAIL {type(e).__name__}: {e} | id={external_id}")
    return None

def extract_streams(detail):
    """
    Merge blvs + blvs_bonglau + blvs_nguoitho (3 mảng trùng nhau) -> [(tên_stream, url)].
    Tên stream = '{Tên BLV} {label}' (vd: 'Chuối Kem HD'). Dedupe theo URL.
    """
    if not isinstance(detail, dict):
        return []
    blv_lists = [detail.get(k) for k in ("blvs", "blvs_bonglau", "blvs_nguoitho")]
    out, seen_urls, seen_blv = [], set(), set()
    for arr in blv_lists:
        if not isinstance(arr, list): continue
        for blv in arr:
            if not isinstance(blv, dict): continue
            bname = norm_text(blv.get("name") or blv.get("username") or "")
            bkey = norm_text(blv.get("username") or bname)
            if not bname or bkey in seen_blv: continue
            seen_blv.add(bkey)
            for st in blv.get("streams") or []:
                if not isinstance(st, dict): continue
                url = st.get("url") or ""
                label = norm_text(st.get("label") or "")
                if not url or url in seen_urls: continue
                seen_urls.add(url)
                out.append((f"{bname} {label}".strip(), url))
    return out

# ─────────────────────────────────────────────────────────────────────────────
# THUMBNAIL (khung giovang, giữ nguyên)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_image(url):
    try:
        res = http_get(url, timeout=8)
        res.raise_for_status()
        return Image.open(BytesIO(res.content)).convert("RGBA")
    except Exception:
        return None

def make_thumbnail(match, match_id_safe):
    os.makedirs(THUMBS_DIR, exist_ok=True)
    cache_key = (match.get("logo_a") or "") + (match.get("logo_b") or "") + THUMB_VERSION
    logo_hash = hashlib.md5(cache_key.encode()).hexdigest()[:8]
    date_str = now_vn().strftime("%Y%m%d")
    out_path = f"{THUMBS_DIR}/{match_id_safe}_{logo_hash}_{date_str}.png"
    if os.path.exists(out_path):
        return out_path

    W, H = 1600, 1200
    HEADER_H, FOOTER_H = 180, 160
    bg = Image.new("RGB", (W, H), (245, 245, 248))
    draw = ImageDraw.Draw(bg)
    for y in range(HEADER_H, H - FOOTER_H):
        ratio = (y - HEADER_H) / (H - FOOTER_H - HEADER_H)
        gray = int(248 - ratio * 18)
        draw.line([(0, y), (W, y)], fill=(gray, gray, gray + 4))
    draw.rectangle([(0, 0), (W, HEADER_H)], fill=(13, 20, 40))
    draw.rectangle([(0, H - FOOTER_H), (W, H)], fill=(13, 20, 40))
    ACCENT = (220, 30, 40)
    draw.rectangle([(0, HEADER_H), (W, HEADER_H + 5)], fill=ACCENT)
    draw.rectangle([(0, H - FOOTER_H - 5), (W, H - FOOTER_H)], fill=ACCENT)

    FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        font_vs = ImageFont.truetype(FONT_BOLD, 160)
        font_time = ImageFont.truetype(FONT_BOLD, 100)
        font_team = ImageFont.truetype(FONT_BOLD, 58)
    except Exception:
        font_vs = font_time = font_team = ImageFont.load_default()

    content_top, content_bot = HEADER_H + 5, H - FOOTER_H - 5
    content_h = content_bot - content_top
    logo_size, name_h, time_h = 360, 120, 110
    gap_logo_name, gap_name_time = 40, 60
    block_top = content_top + (content_h - (logo_size + gap_logo_name + name_h + gap_name_time + time_h)) // 2
    logo_y = block_top
    name_center = block_top + logo_size + gap_logo_name + name_h // 2
    time_y = block_top + logo_size + gap_logo_name + name_h + gap_name_time + time_h // 2

    def draw_team_name(text, cx):
        max_width, font_size, f = W // 2 - 60, 58, font_team
        while font_size >= 28:
            try: f = ImageFont.truetype(FONT_BOLD, font_size)
            except Exception: f = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), text, font=f)
            if (bbox[2] - bbox[0]) <= max_width: break
            font_size -= 3
        draw.text((cx, name_center), text, fill=(20, 20, 20), font=f, anchor="mm")

    for key, cx in (("logo_a", W // 4), ("logo_b", W * 3 // 4)):
        if match.get(key):
            img = fetch_image(match[key])
            if img:
                try:
                    resized = img.resize((logo_size, logo_size), Image.LANCZOS)
                    bg.paste(resized, (cx - logo_size // 2, logo_y), resized)
                except Exception:
                    pass

    draw.text((W // 2, logo_y + logo_size // 2), "VS", fill=ACCENT, font=font_vs, anchor="mm")
    if match.get("team_a"): draw_team_name(match["team_a"], W // 4)
    if match.get("team_b"): draw_team_name(match["team_b"], W * 3 // 4)

    time_display = f"{match.get('time','')} {match.get('date','')}".strip()
    if time_display:
        font_size, f_time = 100, font_time
        while font_size >= 40:
            try: f_time = ImageFont.truetype(FONT_BOLD, font_size)
            except Exception: f_time = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), time_display, font=f_time)
            if (bbox[2] - bbox[0]) <= W - 100: break
            font_size -= 4
        draw.text((W // 2 + 4, time_y + 4), time_display, fill=ACCENT, font=f_time, anchor="mm")
        draw.text((W // 2, time_y), time_display, fill=(15, 15, 15), font=f_time, anchor="mm")

    if match.get("league"):
        league_text = match["league"].upper()
        font_size, f = 62, None
        while font_size >= 28:
            try: f = ImageFont.truetype(FONT_BOLD, font_size)
            except Exception: f = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), league_text, font=f)
            if (bbox[2] - bbox[0]) <= W - 60: break
            font_size -= 3
        draw.text((W // 2, HEADER_H // 2), league_text, fill=(255, 255, 255), font=f, anchor="mm")

    draw.rectangle([(0, 0), (W - 1, H - 1)], outline=(180, 180, 180), width=3)
    bg.save(out_path, "PNG", optimize=True)
    return out_path

def cleanup_old_thumbs(days=3):
    if not os.path.exists(THUMBS_DIR): return
    cutoff = now_vn() - timedelta(days=days)
    for fname in os.listdir(THUMBS_DIR):
        if not fname.endswith(".png"): continue
        m = re.search(r'_(\d{8})\.png$', fname)
        if m:
            try:
                if datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=VN_TZ) < cutoff:
                    os.remove(os.path.join(THUMBS_DIR, fname))
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# BUILD CHANNEL (schema giovang: 1 label, org_metadata 10 key, root image)
# ─────────────────────────────────────────────────────────────────────────────

def build_channel(entry, streams):
    mid_safe = entry["external_id"]
    uid, src_id = make_id(mid_safe, "cc"), make_id(mid_safe, "src")
    ct_id, st_id = make_id(mid_safe, "ct"), make_id(mid_safe, "st")
    is_live = entry["is_live"]

    stream_links = []
    for name, url in streams:
        stream_links.append({
            "id": make_id(url + name, "lnk"),
            "name": name,
            "type": get_stream_type(url),
            "default": len(stream_links) == 0,
            "url": url,
            "request_headers": [
                {"key": "Referer", "value": STREAM_REFERER},
                {"key": "User-Agent", "value": HEADERS["User-Agent"]},
            ],
        })

    ko = entry["start_dt"]
    time_fmt = ko.strftime("%H:%M") if ko else ""
    date_fmt = ko.strftime("%d/%m") if ko else ""
    time_full = ko.strftime("%H:%M:%S") if ko else ""
    display_name = f'{entry["team_a"]} vs {entry["team_b"]}'
    if time_fmt: display_name += f" | {time_fmt} {date_fmt}"

    labels = [{"text": "● LIVE" if is_live else "🕐 Sắp",
               "position": "top-left", "color": "#00000080",
               "text_color": "#ff4444" if is_live else "#aaaaaa"}]

    channel = {
        "id": uid,
        "name": display_name,
        "type": "single",
        "display": "thumbnail-only",
        "enable_detail": False,
        "labels": labels,
        "sources": [{
            "id": src_id,
            "name": "ChuoiChienTV",
            "contents": [{
                "id": ct_id,
                "name": f'{entry["team_a"]} vs {entry["team_b"]}',
                "streams": [{"id": st_id, "name": "CC", "stream_links": stream_links}],
            }],
        }],
        "org_metadata": {
            "league": entry["league"],
            "team_a": entry["team_a"], "team_b": entry["team_b"],
            "logo_a": entry["logo_a"], "logo_b": entry["logo_b"],
            "time": time_full, "date": date_fmt,
            "blv": ", ".join(sorted({n.rsplit(" ", 1)[0] for n, _ in streams})) or "",
            "is_live": is_live,
            "cate_type": entry["cate_type"],
        },
    }

    thumb_path = make_thumbnail({
        "team_a": entry["team_a"], "team_b": entry["team_b"],
        "logo_a": entry["logo_a"], "logo_b": entry["logo_b"],
        "league": entry["league"], "time": time_fmt, "date": date_fmt,
    }, mid_safe)
    cache_key = entry["logo_a"] + entry["logo_b"] + THUMB_VERSION
    logo_hash = hashlib.md5(cache_key.encode()).hexdigest()[:8]
    if REPO_RAW:
        channel["image"] = {"padding": 1, "background_color": "#ffffff", "display": "contain",
                            "url": f"{REPO_RAW}/{thumb_path}?v={logo_hash}", "width": 1600, "height": 1200}
    return channel

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(THUMBS_DIR, exist_ok=True)
    cleanup_old_thumbs(days=3)
    print(f"Gio VN hien tai : {now_vn().strftime('%H:%M %d/%m/%Y')}")
    print(f"HTTP client     : {'curl_cffi (impersonate=chrome)' if _HAVE_CURL_CFFI else 'python-requests (khuyen nghi: pip install curl_cffi)'}")

    print("\n1) Lay danh sach tran (type=blv + type=live) ...")
    listing = fetch_listing()
    print(f"   Tong unique: {len(listing)} tran")

    print("\n2) Lay chi tiet + stream cho tung tran ...")
    built, skipped = [], 0
    now = now_vn()
    for i, item in enumerate(sorted(listing,
            key=lambda m: str(m.get("matchTime") or ""))):
        eid = str(item.get("externalId") or item.get("id") or "")
        if not eid:
            continue
        detail = fetch_detail(eid) or item   # detail fail -> dùng item trong listing (có thể thiếu stream)

        if is_done_status(detail.get("status")):
            continue

        ko = parse_matchtime(detail.get("matchTime") or "")
        status = str(detail.get("status") or "").lower()
        live = is_live_status(status)

        # filter thời gian cho trận chưa/không live
        if not live and not is_live_status(status):
            if ko is None:
                pass  # không biết giờ -> giữ
            else:
                if ko < now - timedelta(hours=WINDOW_PAST_H):
                    continue
                if str(detail.get("sport") or "football").lower() == "football" \
                   and ko > now + timedelta(hours=FOOTBALL_FUT_H):
                    continue

        league_obj = detail.get("league") or {}
        league = norm_text(league_obj.get("name") if isinstance(league_obj, dict) else str(league_obj))
        if str(detail.get("sport") or "football").lower() == "football" and is_america_league(league):
            continue

        teams = detail.get("teams") or {}
        t1 = norm_text((teams.get("home") or {}).get("name")) or "Đội A"
        t2 = norm_text((teams.get("away") or {}).get("name")) or "Đội B"
        cate = str(detail.get("sport") or "football").lower().strip()

        streams = extract_streams(detail)
        if not streams:
            skipped += 1
            print(f'[SKIP {i+1}/{len(listing)}] {t1} vs {t2} | chua co BLV/stream')
            continue

        entry = {
            "external_id": eid,
            "team_a": t1, "team_b": t2,
            "logo_a": (teams.get("home") or {}).get("logo") or "",
            "logo_b": (teams.get("away") or {}).get("logo") or "",
            "league": league,
            "start_dt": ko,
            "is_live": live or is_live_status(status),
            "cate_type": cate,
        }
        status_s = "LIVE" if entry["is_live"] else "SAP"
        tdisp = ko.strftime("%H:%M %d/%m") if ko else "?"
        blv_str = ", ".join(sorted({n.rsplit(" ", 1)[0] for n, _ in streams}))
        print(f'[{status_s} {i+1}/{len(listing)}] {t1} vs {t2} ({tdisp}) | BLV: {blv_str} | {len(streams)} link')
        built.append(build_channel(entry, streams))
        time.sleep(DETAIL_SLEEP)

    cate_channels = {}
    for ch in built:
        cate_channels.setdefault(ch["org_metadata"]["cate_type"], []).append(ch)

    out_groups = []
    for cate in CATE_ORDER:
        chs = cate_channels.pop(cate, [])
        if not chs: continue
        label = CATE_MAP[cate]
        live_cnt = sum(1 for c in chs if c["org_metadata"]["is_live"])
        name = f"{label} ({live_cnt} LIVE)" if live_cnt else label
        out_groups.append({"id": f"cate_{cate}", "name": name, "display": "vertical",
                           "grid_number": 2, "enable_detail": False, "channels": chs})
    for cate, chs in cate_channels.items():
        live_cnt = sum(1 for c in chs if c["org_metadata"]["is_live"])
        label = "🏅 " + cate.replace("_", " ").title()
        name = f"{label} ({live_cnt} LIVE)" if live_cnt else label
        out_groups.append({"id": f"cate_{cate}", "name": name, "display": "vertical",
                           "grid_number": 2, "enable_detail": False, "channels": chs})

    output = {"id": "chuoichientv", "url": SITE, "name": "ChuoiChienTV", "color": "#c8102e",
              "grid_number": 3,
              "image": {"type": "cover", "url": SITE_LOGO},
              "groups": out_groups}

    staging = "output_staging.json"
    with open(staging, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total = sum(len(g["channels"]) for g in out_groups)

    def normalize(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.dumps(json.load(f), sort_keys=True, ensure_ascii=False)
        except Exception:
            return ""

    if normalize("output.json") != normalize(staging):
        os.replace(staging, "output.json")
        print(f"\n✅ Xong! {total} kenh, {len(out_groups)} mon the thao -> output.json (DA CAP NHAT)")
    else:
        os.remove(staging)
        print(f"\n✅ Xong! {total} kenh, {len(out_groups)} mon the thao -> Khong co thay doi")

    print(f"""
─── LƯU Ý ───
- {skipped} tran bi SKIP (BLV chua cap stream — thuong la tran chua kickoff).
- Referer stream dang dung https://fhd-01.cctvsignal.xyz/ (domain player nhung).
  Neu VLC/app bao 403: thu doi STREAM_REFERER thanh {SITE}/ roi chay lai.
- Neu THIEU tran sap dau co BLV: them type khac vao LIST_TYPES (vd 'upcoming','schedule','today')
  va gui body cua /v2/matches?type=... de chinh xac 1 dong parse.""")

if __name__ == "__main__":
    main()
