import re
from typing import Optional, Tuple

from Backend.helper.metadata import caption_with_id, parse_media_name
from Backend.helper.pyro import clean_filename, finalize_media_name, get_readable_file_size, is_media, message_has_thumb
from Backend.helper.split_files import parse_split_info, strip_part_suffix
from Backend.logger import LOGGER
from Backend.helper.task_manager import edit_message

_PRIVATE_LINK = re.compile(r"t\.me/c/(\d+)(?:/\d+)*/(\d+)")
_PUBLIC_LINK = re.compile(r"t\.me/([A-Za-z][\w]{3,})/(?:\d+/)?(\d+)")
_IMDB_ID_RE = re.compile(r"(?i)\bIMDb\s*ID\s*:\s*(tt\d{7,10})\b")
_TMDB_ID_RE = re.compile(r"(?i)\bTMDB\s*ID\s*:\s*(\d+)\b")


def parse_season_episode(filename: str, parsed: dict = None):
    """Return (season, episode) from common TV filename conventions.

    Supported examples: S01E06, s1e6, E6, Naruto 2 E6, Naruto 2 episode 6.
    A trailing bare number (e.g. Naruto 6) is treated as episode 6, season 1.
    Existing parse_media_name results take priority when they contain both values.
    """
    parsed = parsed or {}
    season = parsed.get("season")
    episode = parsed.get("episode")
    if season not in (None, "") and episode not in (None, ""):
        try:
            return int(season), int(episode)
        except (TypeError, ValueError):
            pass

    name = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", (filename or "").strip())
    patterns = [
        # S01E06 / s1e6 / S1 E6 / S01.E06
        (re.compile(r"(?i)(?:^|\b)s(\d{1,3})[ ._-]*e(\d{1,4})\b"), "both"),
        # Naruto 2 E6 / Naruto Season 2 Episode 6 / Naruto 2 episode 6
        (re.compile(r"(?i)\b(?:season\s*)?(\d{1,3})[ ._-]*(?:e|ep|episode)\s*0*(\d{1,4})\b"), "both"),
        (re.compile(r"(?i)\bseason\s*(\d{1,3})\b.*?\b(?:episode|ep|e)\s*0*(\d{1,4})\b"), "both"),
        # E6 / EP06 / Episode 6; default season to 1 if no season is present.
        (re.compile(r"(?i)\b(?:e|ep|episode)\s*0*(\d{1,4})\b"), "episode"),
    ]
    for pattern, kind in patterns:
        match = pattern.search(name)
        if match:
            if kind == "both":
                return int(match.group(1)), int(match.group(2))
            return 1, int(match.group(1))

    # A plain trailing number is accepted only when it follows a title-like word
    # and is not a year or common resolution/codec marker. Example: "Naruto 6".
    match = re.search(r"(?i)\b([A-Za-z][A-Za-z0-9 .'-]*?)\s+(\d{1,4})$", name)
    if match:
        number = int(match.group(2))
        if 1 <= number <= 999 and not 1900 <= number <= 2099:
            return 1, number
    return season, episode


#----- Parse a Telegram post link into (chat_ref, msg_id).
#----- chat_ref is a full -100 int for private channels or a @username string for public ones.
def parse_telegram_link(url: str) -> Tuple[Optional[object], Optional[int]]:
    url = (url or "").strip()
    private = _PRIVATE_LINK.search(url)
    if private:
        return int(f"-100{private.group(1)}"), int(private.group(2))
    public = _PUBLIC_LINK.search(url)
    if public:
        return public.group(1), int(public.group(2))
    return None, None


#----- Map a video pixel height to a standard quality label
def quality_from_height(height: int) -> str:
    if not height:
        return ""
    for threshold, label in ((1800, "2160p"), (1200, "1440p"), (900, "1080p"),
                             (620, "720p"), (400, "480p"), (260, "360p")):
        if height >= threshold:
            return label
    return "240p"


#----- Fetch a message and return the stream fields the manual-add flow needs.
async def resolve_telegram_message(client, url: str = None, chat_id=None, msg_id=None) -> dict:
    if url:
        chat_ref, msg_id = parse_telegram_link(url)
        if chat_ref is None:
            raise ValueError("Could not read that Telegram link. Use a t.me/c/... or t.me/<channel>/... message link.")
    elif chat_id and msg_id:
        chat_ref = int(f"-100{str(chat_id).replace('-100', '')}")
        msg_id = int(msg_id)
    else:
        raise ValueError("Provide a Telegram message link, or a chat id and message id.")

    message = await client.get_messages(chat_ref, msg_id)
    if not message or getattr(message, "empty", False):
        raise ValueError("That message was not found. Make sure the bot is in the channel.")

    media = is_media(message)
    if not media:
        raise ValueError("That message has no downloadable file.")

    #----- Prefer the caption over the raw file name, then normalise it to the exact
    #----- filename receiver.py stores (clean, split-suffix stripped, video extension).
    caption = (getattr(message, "caption", None) or "").strip()
    # The Telegram media filename is the search input. Never treat the whole
    # caption (which may contain plot/IDs/metadata) as the actual filename.
    telegram_filename = (getattr(media, "file_name", None) or "").strip()
    raw_name = telegram_filename or caption or "video"
    cleaned = clean_filename(raw_name)
    imdb_match = _IMDB_ID_RE.search(caption)
    tmdb_match = _TMDB_ID_RE.search(caption)
    split_info = parse_split_info(cleaned)
    raw_size = getattr(media, "file_size", 0) or 0
    parsed = parse_media_name(strip_part_suffix(cleaned) if split_info else cleaned)
    season, episode = parse_season_episode(strip_part_suffix(cleaned) if split_info else cleaned, parsed)
    file_name = finalize_media_name(raw_name, bool(split_info))

    #----- Real video dimensions beat the filename; documents fall back to the name
    height = getattr(media, "height", 0) or 0
    quality = quality_from_height(height) or parsed.get("quality") or ""

    #----- Original upload date (forward source if forwarded, else this message's date)
    original_date = getattr(message, "forward_date", None) or getattr(message, "date", None)
    upload_year = original_date.year if original_date else 0

    return {
        "chat_id": str(message.chat.id).replace("-100", ""),
        "msg_id": message.id,
        "name": file_name,
        "telegram_filename": telegram_filename or file_name,
        "caption": caption,
        "caption_imdb_id": imdb_match.group(1) if imdb_match else None,
        "caption_tmdb_id": int(tmdb_match.group(1)) if tmdb_match else None,
        "raw_size": raw_size,
        "size": get_readable_file_size(raw_size),
        "quality": quality,
        "season": season,
        "episode": episode,
        "width": getattr(media, "width", 0) or 0,
        "height": height,
        "has_thumb": message_has_thumb(message),
        "upload_year": upload_year,
        "split_key": split_info[0] if split_info else None,
        "part_number": split_info[1] if split_info else None,
    }



async def stamp_caption_with_id(message, metadata_info: dict) -> bool:
    try:
        media = message.video or message.document
        base = (getattr(message, "caption", None) or getattr(media, "file_name", None) or "")
        new_caption = caption_with_id(base, metadata_info)
        if not new_caption:
            return False
        
        await edit_message(message.chat.id, message.id, new_caption)
        return True
    except Exception as e:
        LOGGER.warning(f"[Caption] Could not stamp id on message {getattr(message, 'id', '?')}: {e}")
        return False


async def stamp_caption_by_ref(client, chat_id, msg_id, metadata_info: dict) -> bool:
    try:
        chat_ref = int(f"-100{str(chat_id).replace('-100', '')}")
        message = await client.get_messages(chat_ref, int(msg_id))
        if not message or getattr(message, "empty", False):
            return False
        return await stamp_caption_with_id(message, metadata_info)
    except Exception as e:
        LOGGER.warning(f"[Caption] Could not stamp id on message {msg_id}: {e}")
        return False
