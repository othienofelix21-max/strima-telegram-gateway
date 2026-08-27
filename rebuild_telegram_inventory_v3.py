import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INV_DIR = BASE_DIR / "telegram_inventory"
MOVIES_CSV = INV_DIR / "telegram_movies_inventory.csv"
SERIES_EPISODES_CSV = INV_DIR / "telegram_series_episodes.csv"

SOURCE_WORDS = {
    "ivo", "shimperd", "banks", "jbs", "tv", "ks", "mzabi", "jadewind", "phoenix",
    "soul", "mukano", "muba", "junior", "jr", "emmy", "sammy", "shield", "ice",
    "jingo", "vivi", "vj", "nos", "no's", "ulio", "baros", "muscalarv", "bmc"
}

NOISE_RE = re.compile(r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|uhd|hdr|bluray|webrip|web[- ]?dl|x264|x265|hevc|h264|h265|aac|hd)\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
HASH_RE = re.compile(r"^[0-9a-f]{20,}$", re.I)

EXPLICIT_EP_PATTERNS = [
    re.compile(r"(?i)\bS(?:EASON)?\s*0*(\d{1,2})\s*[._\- ]*E(?:P(?:ISODE)?)?\s*0*(\d{1,3})([AB])?\b"),
    re.compile(r"(?i)\bS\s*0*(\d{1,2})\s*[._\-]+0*(\d{1,3})([AB])?\b"),
    re.compile(r"(?i)\bSEASON\s*0*(\d{1,2})\s*[._\- ]*EPISODE\s*0*(\d{1,3})([AB])?\b"),
    re.compile(r"(?i)\b0*(\d{1,2})\s*[xX]\s*0*(\d{1,3})([AB])?\b"),
]


def strip_ext(name):
    p = Path(str(name or ""))
    return p.stem if p.suffix else str(name or "")


def spacing(text):
    text = str(text or "").replace("_", " ")
    text = re.sub(r"[.]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_.")


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def is_noise_filename(filename):
    raw = str(filename or "").strip()
    stem = norm(strip_ext(raw))
    if not stem:
        return True
    if raw.startswith("/root/") or raw.startswith("\\root\\"):
        return True
    if HASH_RE.fullmatch(re.sub(r"[^0-9a-f]", "", stem, flags=re.I) or "") and len(re.sub(r"[^0-9a-f]", "", stem, flags=re.I)) >= 20:
        return True
    if re.fullmatch(r"\d+(?:\s+\d+)*", stem):
        return True
    if "trailer" in stem or stem.startswith("free ") or stem.startswith("free"):
        return True
    if "end of season" in stem or stem in {"end", "season end", "end season"}:
        return True
    return False


def strip_source_suffixes(text):
    text = str(text or "")
    text = NOISE_RE.sub(" ", text)
    text = re.sub(r"(?i)\s*VJ\s*(?:JR|JUNIOR|SOUL|EMMY|SAMMY|MUBA|MUKANO|SHIELD|ICE|JINGO|IVO|ULIO|BAROS|KS)?\b.*$", " ", text)
    text = re.sub(r"(?i)\s*BY\s+VJ\b.*$", " ", text)
    text = re.sub(r"(?i)\s*@\s*[A-Z0-9_]+.*$", " ", text)
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)

    # Remove year after it has served as useful metadata.
    text = YEAR_RE.sub(" ", text)

    # Remove known uploader/source words from the tail only.
    words = text.split()
    while words and norm(words[-1]) in SOURCE_WORDS:
        words.pop()
    text = " ".join(words)

    parts = [p.strip() for p in re.split(r"\s+-\s+", text) if p.strip()]
    while len(parts) > 1:
        last_words = set(norm(parts[-1]).split())
        if last_words and (last_words <= SOURCE_WORDS or len(last_words & SOURCE_WORDS) >= 1):
            parts.pop()
        else:
            break
    text = " - ".join(parts)
    text = re.sub(r"\s+", " ", text).strip(" -_.")
    return text


def valid_title(title):
    k = norm(title)
    if len(k) < 2 or k.isdigit():
        return False
    words = set(k.split())
    if words and words <= SOURCE_WORDS:
        return False
    if k in {"end of season one", "end of season two", "end", "trailer"}:
        return False
    return True


def make_result(title, season, ep, suffix="", method=""):
    title = re.sub(r"\s+", " ", title).strip(" -_.")
    if not valid_title(title):
        return None
    return {
        "series_title": title,
        "series_key": norm(title),
        "season": int(season or 1),
        "episode": f"{int(ep)}{suffix}",
        "episode_number": int(ep),
        "parse_method": method,
    }


def parse_series_filename(filename):
    if is_noise_filename(filename):
        return None

    raw = spacing(strip_ext(filename))
    season = None
    episode_num = None
    episode_suffix = ""
    working = raw

    # A. Strong explicit SxxExx / 1x02 patterns.
    for pat in EXPLICIT_EP_PATTERNS:
        m = pat.search(working)
        if m:
            season = int(m.group(1))
            episode_num = int(m.group(2))
            episode_suffix = (m.group(3) or "").upper()
            title_part = (working[:m.start()] + " " + working[m.end():]).strip()
            title = strip_source_suffixes(title_part)
            return make_result(title, season, episode_num, episode_suffix, "explicit_season_episode")

    # Clean uploader/year/noise before the looser patterns. This is what lets
    # "18 Again 12 by Vj Ivo" become title="18 Again", episode=12.
    cleaned = strip_source_suffixes(working)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_.")

    # B. Special duplicate-number format: "12DESTINED WITH YOU 12".
    m = re.match(r"^0*(\d{1,3})([A-Za-z].*?)\s+0*\1([AB])?$", cleaned, flags=re.I)
    if m:
        ep = int(m.group(1))
        if 1 <= ep <= 250:
            return make_result(m.group(2).strip(), 1, ep, (m.group(3) or "").upper(), "leading_and_trailing_same_episode")

    # C. Trailing episode: "100 DAYS MY PRINCE 10", "18 Again 1",
    # "13 Reasons Why 23". Require at least one letter in the title.
    m = re.match(r"^(.+?[A-Za-z][A-Za-z0-9'&:\- ]*?)\s+0*(\d{1,3})([AB])?$", cleaned, flags=re.I)
    if m:
        ep = int(m.group(2))
        if 1 <= ep <= 250 and valid_title(m.group(1)):
            return make_result(m.group(1).strip(), 1, ep, (m.group(3) or "").upper(), "trailing_episode")

    # D. Leading episode with punctuation/separator: "10 - Flex X Cop - IVO",
    # "1.MAN ON FIRE EMMY.2026", "17.THE PENALTY ZONE VJ KS 2025".
    m = re.match(r"^0*(\d{1,3})([AB])?\s*[-:–—. ]+\s*(.+)$", cleaned, flags=re.I)
    if m:
        ep = int(m.group(1))
        title = m.group(3).strip()
        if 1 <= ep <= 250 and valid_title(title):
            return make_result(title, 1, ep, (m.group(2) or "").upper(), "leading_episode")

    # E. Leading zero-padded episode immediately followed by title:
    # "01 IF WISHES COULD KILL". Restrict this to zero-padded 01-09 so titles
    # such as "18 Again" are not mistaken for episode 18.
    m = re.match(r"^0([1-9])\s*([A-Za-z].+)$", cleaned, flags=re.I)
    if m:
        return make_result(m.group(2).strip(), 1, int(m.group(1)), "", "leading_zero_padded_episode")

    return None


def compress_numbers(nums):
    nums = sorted(set(int(n) for n in nums))
    if not nums:
        return ""
    out = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(out)


def rebuild_series():
    if not SERIES_EPISODES_CSV.exists():
        raise FileNotFoundError(SERIES_EPISODES_CSV)

    rows = list(csv.DictReader(SERIES_EPISODES_CSV.open("r", encoding="utf-8-sig", newline="")))
    grouped = defaultdict(list)
    unparsed = []
    noise = []
    methods = Counter()

    for row in rows:
        fn = row.get("filename", "")
        if is_noise_filename(fn):
            noise.append(row)
            continue
        parsed = parse_series_filename(fn)
        if not parsed:
            unparsed.append(row)
            continue
        methods[parsed["parse_method"]] += 1
        grouped[parsed["series_key"]].append({**row, **parsed})

    summary = []
    for items in grouped.values():
        title = Counter(i["series_title"] for i in items).most_common(1)[0][0]
        by_season = defaultdict(list)
        for i in items:
            by_season[int(i["season"])].append(i)
        season_text = []
        for season in sorted(by_season):
            nums = [i["episode_number"] for i in by_season[season]]
            season_text.append(f"S{season}:E{compress_numbers(nums)}")
        summary.append({
            "series_title": title,
            "season_count": len(by_season),
            "episode_files": len(items),
            "seasons_and_episodes": " | ".join(season_text),
        })

    summary.sort(key=lambda x: x["series_title"].lower())

    with (INV_DIR / "telegram_series_master_inventory_v3.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["number", "series_title", "season_count", "episode_files", "seasons_and_episodes"])
        writer.writeheader()
        for n, item in enumerate(summary, 1):
            writer.writerow({"number": n, **item})

    with (INV_DIR / "telegram_series_master_checklist_v3.txt").open("w", encoding="utf-8") as fh:
        fh.write("STRIMA TELEGRAM SERIES MASTER CHECKLIST V3\n")
        fh.write(f"CONFIDENT SERIES GROUPS: {len(summary)}\n")
        fh.write(f"PARSED EPISODE FILES: {sum(x['episode_files'] for x in summary)}\n")
        fh.write(f"UNPARSED FILES: {len(unparsed)}\n")
        fh.write(f"NOISE/JUNK FILES KEPT OUT OF CATALOGUE: {len(noise)}\n\n")
        for n, item in enumerate(summary, 1):
            fh.write(f"[ ] {n:04d}. {item['series_title']} | {item['seasons_and_episodes']} | files:{item['episode_files']}\n")

    for filename, data in [
        ("telegram_series_unparsed_v3.csv", unparsed),
        ("telegram_series_noise_v3.csv", noise),
    ]:
        with (INV_DIR / filename).open("w", encoding="utf-8-sig", newline="") as fh:
            fields = list(rows[0].keys()) if rows else ["filename"]
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(data)

    return summary, unparsed, noise, methods


def clean_movie(filename):
    raw = spacing(strip_ext(filename))
    year_match = YEAR_RE.search(raw)
    year = year_match.group(1) if year_match else ""
    title = strip_source_suffixes(raw)
    title = re.sub(r"\s+", " ", title).strip(" -_.")
    return title, year


def rebuild_movies():
    if not MOVIES_CSV.exists():
        raise FileNotFoundError(MOVIES_CSV)

    rows = list(csv.DictReader(MOVIES_CSV.open("r", encoding="utf-8-sig", newline="")))
    possible_series = []
    noise = []
    groups = defaultdict(list)

    for row in rows:
        filename = row.get("filename", "")
        if is_noise_filename(filename):
            noise.append(row)
            continue
        if parse_series_filename(filename):
            possible_series.append(row)
            continue
        title, year = clean_movie(filename)
        if not valid_title(title):
            noise.append(row)
            continue
        key = norm(title) + (f"|{year}" if year else "")
        groups[key].append({**row, "clean_title": title, "year": year})

    summary = []
    for items in groups.values():
        title = Counter(i["clean_title"] for i in items).most_common(1)[0][0]
        years = [i["year"] for i in items if i["year"]]
        year = Counter(years).most_common(1)[0][0] if years else ""
        summary.append({"title": title, "year": year, "telegram_files": len(items)})
    summary.sort(key=lambda x: (x["title"].lower(), x["year"]))

    with (INV_DIR / "telegram_movies_master_inventory_v3.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["number", "title", "year", "telegram_files"])
        writer.writeheader()
        for n, item in enumerate(summary, 1):
            writer.writerow({"number": n, **item})

    with (INV_DIR / "telegram_movies_master_checklist_v3.txt").open("w", encoding="utf-8") as fh:
        fh.write("STRIMA TELEGRAM MOVIE MASTER CHECKLIST V3\n")
        fh.write(f"RAW VIDEO FILES IN MOVIE CHANNEL: {len(rows)}\n")
        fh.write(f"UNIQUE MOVIE TITLE GROUPS: {len(summary)}\n")
        fh.write(f"SERIES-LIKE FILES EXCLUDED: {len(possible_series)}\n")
        fh.write(f"NOISE/JUNK FILES EXCLUDED: {len(noise)}\n\n")
        for n, item in enumerate(summary, 1):
            year = f" ({item['year']})" if item["year"] else ""
            copies = f" | files:{item['telegram_files']}" if item["telegram_files"] > 1 else ""
            fh.write(f"[ ] {n:04d}. {item['title']}{year}{copies}\n")

    return summary, possible_series, noise


def main():
    print("=" * 72)
    print("STRIMA - TELEGRAM INVENTORY REBUILDER V3")
    print("Uses the CSV files already scanned from Telegram. NO Telegram rescan.")
    print("Does NOT copy, upload, delete, or edit any Telegram message.")
    print("=" * 72)

    movies, movie_series, movie_noise = rebuild_movies()
    series, unparsed, series_noise, methods = rebuild_series()

    print(f"Movie unique title groups        : {len(movies)}")
    print(f"Movie-channel series-like files  : {len(movie_series)}")
    print(f"Movie-channel noise/junk files   : {len(movie_noise)}")
    print(f"Confident series groups          : {len(series)}")
    print(f"Series files still unparsed      : {len(unparsed)}")
    print(f"Series noise/junk files          : {len(series_noise)}")
    print("Parse methods:")
    for k, v in methods.most_common():
        print(f"  {k:34s}: {v}")
    print("\nCreated:")
    print("  telegram_movies_master_checklist_v3.txt")
    print("  telegram_movies_master_inventory_v3.csv")
    print("  telegram_series_master_checklist_v3.txt")
    print("  telegram_series_master_inventory_v3.csv")
    print("  telegram_series_unparsed_v3.csv")
    print("  telegram_series_noise_v3.csv")
    print("=" * 72)


if __name__ == "__main__":
    main()
