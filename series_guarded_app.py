import re

import series_strict_app as strict
import series_auto_app as auto
import series_test_app as series

# Keep every existing STRIMA endpoint, but harden series filename parsing before
# any worker starts scanning Telegram.
app = strict.app


def _clean_series_title(value: str) -> str:
    text = str(value or "")
    text = text.replace("_", " ").replace(".", " ")
    text = re.sub(r"^[\s\-–—_:;\[\]()]+", "", text)
    text = re.sub(r"\bVJ\b.*$", "", text, flags=re.I)
    text = re.sub(r"\b(?:19\d{2}|20\d{2})\b.*$", "", text)
    text = re.sub(
        r"\b(?:360p|480p|540p|720p|1080p|1440p|2160p|4k|uhd|hdr|x264|x265|h264|h265|hevc|webrip|web[- ]?dl|bluray|brrip|dvdrip)\b.*$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" -–—_:;[]()")
    return auto._clean_title(text)


def _title_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def guarded_parse_episode(message):
    """Parse common STRIMA series filename styles without cross-series guessing.

    Supported examples include:
      NAUTILUS S01 E01 EMMY.2026.mkv
      S01 E03 .NAUTILUS__VJ EMMY.2026.mkv
      NARCOS MEXICO S3-10 VJ TONNY 2026.mkv
      JANG YEONG SIL EP24 VJ HD.mkv
      EP24 JANG YEONG SIL VJ HD.mkv
      MAN ON FIRE Season 1 Episode 1 VJ JUNIOR.mkv
    """
    text = auto._clean_label(message)
    if not text:
        return None

    text = re.sub(r"\s+", " ", text).strip()

    patterns = [
        # Episode/season first: S01 E03 NAUTILUS ...
        r"^\s*S(?:EASON)?\s*0*(?P<season>\d{1,2})\s*[-_. ]*\s*E(?:P(?:ISODE)?)?\s*0*(?P<ep>\d{1,3})\b[\s._:;\-–—]*(?P<title>.+)$",
        # Episode/season first compact dash: S3-10 NARCOS MEXICO ...
        r"^\s*S\s*0*(?P<season>\d{1,2})\s*[-/]\s*0*(?P<ep>\d{1,3})\b[\s._:;\-–—]*(?P<title>.+)$",
        # Title first: NAUTILUS S01 E01 ... / MAN ON FIRE Season 1 Episode 1 ...
        r"^(?P<title>.+?)\s+S(?:EASON)?\s*0*(?P<season>\d{1,2})\s*[-_. ]*\s*E(?:P(?:ISODE)?)?\s*0*(?P<ep>\d{1,3})\b",
        # Title first compact dash: NARCOS MEXICO S3-10 ...
        r"^(?P<title>.+?)\s+S\s*0*(?P<season>\d{1,2})\s*[-/]\s*0*(?P<ep>\d{1,3})\b",
        # Episode first without explicit season: EP24 JANG YEONG SIL ...
        r"^\s*E(?:P(?:ISODE)?)?\s*0*(?P<ep>\d{1,3})\b[\s._:;\-–—]*(?P<title>.+)$",
        # Title first without explicit season: JANG YEONG SIL EP24 ...
        r"^(?P<title>.+?)\s+E(?:P(?:ISODE)?)?\s*0*(?P<ep>\d{1,3})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue

        title = _clean_series_title(match.group("title"))
        if not auto._valid_title(title):
            continue

        try:
            episode = int(match.group("ep"))
            season_raw = match.groupdict().get("season")
            season_number = int(season_raw) if season_raw else 1
        except (TypeError, ValueError):
            continue

        if not (1 <= episode <= 500 and 1 <= season_number <= 50):
            continue

        return title, season_number, episode

    return None


def guarded_episode_number(message, series_title: str):
    """Direct single-series worker may only accept a message whose parsed title
    is the same requested series. This prevents an episode from another show
    being attached merely because its number looks similar.
    """
    parsed = guarded_parse_episode(message)
    if not parsed:
        return None
    parsed_title, _season_number, episode = parsed
    if _title_key(parsed_title) != _title_key(series_title):
        return None
    return episode


# The auto/strict workers resolve these globals at runtime, so replacing them
# here protects every scan launched through the deployed FastAPI app.
auto._parse_episode = guarded_parse_episode
series._episode_number = guarded_episode_number


@app.get("/admin/telegram/series/parser-safety")
async def parser_safety():
    return {
        "ok": True,
        "parser": "STRIMA guarded series parser v1",
        "supports": [
            "TITLE S01 E01",
            "S01 E01 TITLE",
            "TITLE S3-10",
            "EP24 TITLE",
            "TITLE EP24",
            "TITLE Season 1 Episode 1",
        ],
        "publication_guard": "Supabase blocks incomplete published movies/series/episodes",
        "mapping_guard": "source/destination Telegram messages and season episode slots are unique",
    }
