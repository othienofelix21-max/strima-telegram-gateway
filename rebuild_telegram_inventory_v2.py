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
    "jingo", "vivi", "vj", "nos", "no's"
}
NOISE_RE = re.compile(r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|uhd|hdr|bluray|webrip|web[- ]?dl|x264|x265|hevc|h264|h265|aac|hd)\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
EXPLICIT_EP_PATTERNS = [
    re.compile(r"(?i)\bS(?:EASON)?\s*0*(\d{1,2})\s*[._\- ]*E(?:P(?:ISODE)?)?\s*0*(\d{1,3})([AB])?\b"),
    re.compile(r"(?i)\bS\s*0*(\d{1,2})\s*[._\-]+0*(\d{1,3})([AB])?\b"),
    re.compile(r"(?i)\bSEASON\s*0*(\d{1,2})\s*[._\- ]*EPISODE\s*0*(\d{1,3})([AB])?\b"),
    re.compile(r"(?i)\b0*(\d{1,2})\s*[xX]\s*0*(\d{1,3})([AB])?\b"),
]


def strip_ext(name):
    p = Path(name)
    return p.stem if p.suffix else name


def spacing(text):
    text = str(text or "").replace("_", " ")
    text = re.sub(r"[.]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_.")


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def strip_source_suffixes(text):
    text = NOISE_RE.sub(" ", text)
    text = re.sub(r"(?i)\s*VJ\s*(?:JR|JUNIOR|SOUL|EMMY|SAMMY|MUBA|MUKANO|SHIELD|ICE|JINGO|IVO)?\b.*$", " ", text)
    text = re.sub(r"(?i)\s*BY\s+VJ\b.*$", " ", text)
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)

    # Remove trailing dash-separated source/uploader tags only when they look like known tags.
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


def parse_series_filename(filename):
    raw = spacing(strip_ext(filename))
    season = None
    episode_num = None
    episode_suffix = ""
    working = raw

    # 1) Strong explicit season/episode formats.
    match = None
    for pat in EXPLICIT_EP_PATTERNS:
        match = pat.search(working)
        if match:
            season = int(match.group(1))
            episode_num = int(match.group(2))
            episode_suffix = (match.group(3) or "").upper()
            working = (working[:match.start()] + " " + working[match.end():]).strip()
            break

    # 2) Leading episode number: "10 - Flex X Cop - IVO".
    if episode_num is None:
        m = re.match(r"^\s*0*(\d{1,3})([AB])?\s*[-:–—]+\s*(.+)$", working, flags=re.I)
        if m:
            episode_num = int(m.group(1))
            episode_suffix = (m.group(2) or "").upper()
            season = 1
            working = m.group(3).strip()

    # 3) Trailing episode number: "100 DAYS MY PRINCE 10".
    if episode_num is None:
        m = re.match(r"^(.+?\b[A-Za-z][A-Za-z'&-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&-]*)+)\s+0*(\d{1,3})([AB])?$", working, flags=re.I)
        if m:
            candidate_title = m.group(1).strip()
            ep = int(m.group(2))
            # Be conservative: typical episode numbers only.
            if 1 <= ep <= 250:
                episode_num = ep
                episode_suffix = (m.group(3) or "").upper()
                season = 1
                working = candidate_title

    if episode_num is None:
        return None

    title = strip_source_suffixes(working)
    title = YEAR_RE.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(" -_.")
    if len(norm(title)) < 2 or norm(title).isdigit():
        return None

    return {
        "series_title": title,
        "series_key": norm(title),
        "season": season or 1,
        "episode": f"{episode_num}{episode_suffix}",
        "episode_number": episode_num,
    }


def compress_numbers(nums):
    nums = sorted(set(nums))
    if not nums:
        return ""
    ranges = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def rebuild_series():
    if not SERIES_EPISODES_CSV.exists():
        raise FileNotFoundError(SERIES_EPISODES_CSV)

    rows = list(csv.DictReader(SERIES_EPISODES_CSV.open("r", encoding="utf-8-sig", newline="")))
    grouped = defaultdict(list)
    unparsed = []

    for row in rows:
        parsed = parse_series_filename(row.get("filename", ""))
        if not parsed:
            unparsed.append(row)
            continue
        grouped[parsed["series_key"]].append({**row, **parsed})

    summary = []
    for key, items in grouped.items():
        title = Counter(i["series_title"] for i in items).most_common(1)[0][0]
        by_season = defaultdict(list)
        for i in items:
            by_season[int(i["season"])].append(i)
        season_text = []
        for season in sorted(by_season):
            nums = [int(i["episode_number"]) for i in by_season[season]]
            season_text.append(f"S{season}:E{compress_numbers(nums)}")
        summary.append({
            "series_title": title,
            "season_count": len(by_season),
            "episode_files": len(items),
            "seasons_and_episodes": " | ".join(season_text),
        })

    summary.sort(key=lambda x: x["series_title"].lower())
    out_csv = INV_DIR / "telegram_series_master_inventory_v2.csv"
    with out_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["number", "series_title", "season_count", "episode_files", "seasons_and_episodes"])
        writer.writeheader()
        for n, item in enumerate(summary, 1):
            writer.writerow({"number": n, **item})

    out_txt = INV_DIR / "telegram_series_master_checklist_v2.txt"
    with out_txt.open("w", encoding="utf-8") as fh:
        fh.write("STRIMA TELEGRAM SERIES MASTER CHECKLIST V2\n")
        fh.write(f"CONFIDENT SERIES GROUPS: {len(summary)}\n")
        fh.write(f"PARSED EPISODE FILES: {sum(x['episode_files'] for x in summary)}\n")
        fh.write(f"UNPARSED FILES (kept separate, NOT counted as series): {len(unparsed)}\n\n")
        for n, item in enumerate(summary, 1):
            fh.write(f"[ ] {n:04d}. {item['series_title']} | {item['seasons_and_episodes']} | files:{item['episode_files']}\n")

    unparsed_csv = INV_DIR / "telegram_series_unparsed_v2.csv"
    with unparsed_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        fields = list(rows[0].keys()) if rows else ["filename"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(unparsed)

    return summary, unparsed


def explicit_series_marker(filename):
    raw = spacing(strip_ext(filename))
    return any(p.search(raw) for p in EXPLICIT_EP_PATTERNS)


def clean_movie(filename):
    raw = spacing(strip_ext(filename))
    year_match = YEAR_RE.search(raw)
    year = year_match.group(1) if year_match else ""
    title = strip_source_suffixes(raw)
    title = YEAR_RE.sub(" ", title)
    title = re.sub(r"\b(?:part|pt)\s*[a-z0-9]+\b", lambda m: m.group(0), title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -_.")
    return title, year


def rebuild_movies():
    if not MOVIES_CSV.exists():
        raise FileNotFoundError(MOVIES_CSV)
    rows = list(csv.DictReader(MOVIES_CSV.open("r", encoding="utf-8-sig", newline="")))
    possible_series = []
    groups = defaultdict(list)

    for row in rows:
        filename = row.get("filename", "")
        if explicit_series_marker(filename):
            possible_series.append(row)
            continue
        title, year = clean_movie(filename)
        if not norm(title):
            continue
        key = norm(title) + (f"|{year}" if year else "")
        groups[key].append({**row, "clean_title": title, "year": year})

    summary = []
    for items in groups.values():
        title = Counter(i["clean_title"] for i in items).most_common(1)[0][0]
        year = Counter(i["year"] for i in items if i["year"]).most_common(1)[0][0] if any(i["year"] for i in items) else ""
        summary.append({"title": title, "year": year, "telegram_files": len(items)})
    summary.sort(key=lambda x: (x["title"].lower(), x["year"]))

    with (INV_DIR / "telegram_movies_master_inventory_v2.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["number", "title", "year", "telegram_files"])
        writer.writeheader()
        for n, item in enumerate(summary, 1):
            writer.writerow({"number": n, **item})

    with (INV_DIR / "telegram_movies_master_checklist_v2.txt").open("w", encoding="utf-8") as fh:
        fh.write("STRIMA TELEGRAM MOVIE MASTER CHECKLIST V2\n")
        fh.write(f"RAW VIDEO FILES IN MOVIE CHANNEL: {len(rows)}\n")
        fh.write(f"UNIQUE MOVIE TITLE GROUPS: {len(summary)}\n")
        fh.write(f"POSSIBLE SERIES EPISODE FILES EXCLUDED: {len(possible_series)}\n\n")
        for n, item in enumerate(summary, 1):
            year = f" ({item['year']})" if item["year"] else ""
            copies = f" | files:{item['telegram_files']}" if item["telegram_files"] > 1 else ""
            fh.write(f"[ ] {n:04d}. {item['title']}{year}{copies}\n")

    with (INV_DIR / "movie_channel_possible_series_files_v2.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fields = list(rows[0].keys()) if rows else ["filename"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(possible_series)

    return summary, possible_series


def main():
    print("=" * 72)
    print("STRIMA - TELEGRAM INVENTORY REBUILDER V2")
    print("Uses the CSV files already scanned from Telegram. NO Telegram rescan.")
    print("Does NOT copy, upload, delete, or edit any Telegram message.")
    print("=" * 72)

    movies, possible_series = rebuild_movies()
    series, unparsed = rebuild_series()

    print(f"Movie unique title groups        : {len(movies)}")
    print(f"Movie-channel series files       : {len(possible_series)}")
    print(f"Confident series groups          : {len(series)}")
    print(f"Series files still unparsed      : {len(unparsed)}")
    print("\nCreated:")
    print("  telegram_movies_master_checklist_v2.txt")
    print("  telegram_movies_master_inventory_v2.csv")
    print("  telegram_series_master_checklist_v2.txt")
    print("  telegram_series_master_inventory_v2.csv")
    print("  telegram_series_unparsed_v2.csv")
    print("  movie_channel_possible_series_files_v2.csv")
    print("=" * 72)


if __name__ == "__main__":
    main()
