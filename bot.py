#!/usr/bin/env python3
"""
Telegram Lyrics Bot
===================
Прогрессивный вывод текста песни с редактированием одного сообщения.
Очередь треков с автовоспроизведением по кругу.

Команды:
  /start            — Приветствие
  /help             — Справка
  /play <запрос>    — Найти песню и запустить анимацию (добавляет в очередь)
  /type <текст>     — Анимировать переданный текст напрямую
  /paste            — Режим вставки: следующее сообщение = текст песни
  /add <запрос>     — Добавить песню в очередь (не прерывая текущую)
  /addtype <текст>  — Добавить текст в очередь
  /queue            — Показать очередь
  /remove <N>       — Убрать трек №N из очереди
  /clear            — Очистить очередь
  /shuffle          — Перемешать очередь
  /loop             — Вкл/выкл повтор очереди по кругу
  /skip             — Пропустить текущий трек
  /words <N>        — Кол-во слов за одно редактирование
  /delay <S>        — Задержка в секундах между редактированиями
  /settings         — Показать текущие настройки
  /stop             — Остановить анимацию и очистить очередь
  /target <@канал>  — Установить целевой чат/канал для анимации
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional, List, Tuple

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.client.default import DefaultBotProperties

import aiohttp

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lyrics_bot")

# ============================================================
# Configuration
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DEFAULT_WORDS_PER_EDIT = int(os.environ.get("WORDS_PER_EDIT", "5"))
DEFAULT_DELAY = float(os.environ.get("DELAY", "3.0"))
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")
MIN_DELAY = 2.0
MAX_WORDS = 50
MAX_DELAY = 30.0
TELEGRAM_MSG_LIMIT = 4096
MAX_EDITS_PER_MINUTE = 25
PAUSE_BETWEEN_TRACKS = 4.0  # seconds between tracks in queue


# ============================================================
# Track Queue
# ============================================================

@dataclass
class Track:
    """A single track in the queue."""
    title: str           # Display name (Artist — Title)
    lyrics: str          # Full lyrics text
    source: str = ""     # How it was added (search query, /type, /paste)


@dataclass
class TrackQueue:
    """Per-chat track queue with loop support."""
    tracks: List[dict] = field(default_factory=list)  # list of Track as dicts
    current_index: int = -1
    loop: bool = False
    is_playing: bool = False

    def add(self, track: Track):
        self.tracks.append(asdict(track))

    def remove(self, index: int) -> Optional[str]:
        if 0 <= index < len(self.tracks):
            removed = self.tracks.pop(index)
            # Adjust current_index if needed
            if self.current_index > index:
                self.current_index -= 1
            elif self.current_index == index:
                self.current_index = min(self.current_index, len(self.tracks) - 1)
            return removed.get("title", "Unknown")
        return None

    def clear(self):
        self.tracks.clear()
        self.current_index = -1
        self.is_playing = False

    def shuffle(self):
        if len(self.tracks) > 1:
            current_track = None
            if 0 <= self.current_index < len(self.tracks):
                current_track = self.tracks.pop(self.current_index)
            random.shuffle(self.tracks)
            if current_track is not None:
                self.tracks.insert(0, current_track)
                self.current_index = 0

    def current(self) -> Optional[dict]:
        if 0 <= self.current_index < len(self.tracks):
            return self.tracks[self.current_index]
        return None

    def next_track(self) -> Optional[dict]:
        """Advance to next track. Returns it or None if queue is done."""
        if not self.tracks:
            return None

        next_idx = self.current_index + 1

        if next_idx < len(self.tracks):
            self.current_index = next_idx
            return self.tracks[next_idx]

        # Reached the end
        if self.loop:
            self.current_index = 0
            return self.tracks[0]

        # No loop — queue finished
        self.current_index = len(self.tracks)
        return None

    def peek_next(self) -> Optional[dict]:
        """Look at the next track without advancing."""
        if not self.tracks:
            return None
        next_idx = self.current_index + 1
        if next_idx < len(self.tracks):
            return self.tracks[next_idx]
        if self.loop:
            return self.tracks[0]
        return None

    def display(self) -> str:
        """Format queue for display."""
        if not self.tracks:
            return "📭 Очередь пуста"

        lines = []
        for i, t in enumerate(self.tracks):
            marker = ""
            if i == self.current_index:
                marker = "▶️ "
            elif i < self.current_index:
                marker = "✅ "
            else:
                marker = f"{i + 1}. "

            title = t.get("title", "Unknown")
            if len(title) > 50:
                title = title[:47] + "..."
            lines.append(f"{marker}{title}")

        loop_status = " 🔁" if self.loop else ""
        return f"🎵 <b>Очередь{loop_status}</b> ({len(self.tracks)} треков)\n\n" + "\n".join(lines)


# Per-chat queues
queues: Dict[str, TrackQueue] = {}


def get_queue(chat_id: int) -> TrackQueue:
    key = str(chat_id)
    if key not in queues:
        queues[key] = TrackQueue()
    return queues[key]


# ============================================================
# Persistent settings
# ============================================================

@dataclass
class ChatSettings:
    words_per_edit: int = DEFAULT_WORDS_PER_EDIT
    delay: float = DEFAULT_DELAY
    target_chat: Optional[str] = None
    loop: bool = False  # Persist loop preference


def _migrate_chat_id(key: str) -> str:
    return str(key)


def load_settings() -> Dict[str, ChatSettings]:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for k, v in raw.items():
            result[_migrate_chat_id(k)] = ChatSettings(**v)
        return result
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}


def save_settings(store: Dict[str, ChatSettings]):
    serializable = {k: asdict(v) for k, v in store.items()}
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("Cannot save settings: %s", exc)


settings_store: Dict[str, ChatSettings] = load_settings()


def get_settings(chat_id: int) -> ChatSettings:
    key = str(chat_id)
    if key not in settings_store:
        settings_store[key] = ChatSettings()
        save_settings(settings_store)
    return settings_store[key]


def update_settings(chat_id: int, **kwargs):
    key = str(chat_id)
    s = get_settings(chat_id)
    for k, v in kwargs.items():
        if v is not None:
            setattr(s, k, v)
    save_settings(settings_store)


# ============================================================
# Animation cancel events
# ============================================================
cancel_events: Dict[str, asyncio.Event] = {}


# ============================================================
# Lyrics search helpers
# ============================================================

async def _http_get_json(url: str, timeout: float = 10.0) -> Optional[dict | list]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.debug("HTTP GET %s failed: %s", url, exc)
    return None


def parse_lrc(lrc_text: str) -> str:
    """Parse LRC timed-lyrics format into plain text."""
    lines = []
    for raw_line in lrc_text.strip().split("\n"):
        line = re.sub(r"\[\d+:\d+[\.:]\d+\]\s*", "", raw_line)
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


async def search_lrclib(query: str) -> List[dict]:
    """Search lrclib.net and return list of result dicts."""
    encoded = urllib.parse.quote(query)
    data = await _http_get_json(f"https://lrclib.net/api/search?q={encoded}")
    if not data or not isinstance(data, list):
        return []
    results = []
    for item in data[:10]:
        lyrics = ""
        if item.get("syncedLyrics"):
            lyrics = parse_lrc(item["syncedLyrics"])
        elif item.get("plainLyrics"):
            lyrics = item["plainLyrics"]
        if lyrics:
            results.append({
                "artist": item.get("artistName", "Unknown"),
                "title": item.get("trackName", "Unknown"),
                "lyrics": lyrics,
                "duration": item.get("duration"),
            })
    return results


async def search_lyrics_ovh(artist: str, title: str) -> Optional[str]:
    """Search lyrics.ovh API (free, no key required)."""
    a = urllib.parse.quote(artist.strip())
    t = urllib.parse.quote(title.strip())
    data = await _http_get_json(f"https://api.lyrics.ovh/v1/{a}/{t}")
    if data and isinstance(data, dict) and data.get("lyrics"):
        return data["lyrics"]
    return None


async def search_lyrics(query: str) -> Tuple[Optional[str], List[dict]]:
    """Search for song lyrics. Returns (lyrics_text, search_results)."""
    lrclib_results = await search_lrclib(query)
    if lrclib_results:
        return lrclib_results[0]["lyrics"], lrclib_results

    if " - " in query:
        parts = query.split(" - ", 1)
        lyrics = await search_lyrics_ovh(parts[0].strip(), parts[1].strip())
        if lyrics:
            return lyrics, [{
                "artist": parts[0].strip(),
                "title": parts[1].strip(),
                "lyrics": lyrics,
            }]

    return None, []


# ============================================================
# Text animation engine
# ============================================================

def split_into_display_units(text: str) -> List[str]:
    units = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i > 0:
            units.append("\n")
        words = line.split()
        for word in words:
            units.append(word)
    return units


def build_chunks(units: List[str], words_per_edit: int) -> List[List[str]]:
    chunks = []
    current = []
    word_count = 0
    for unit in units:
        current.append(unit)
        if unit != "\n":
            word_count += 1
            if word_count >= words_per_edit:
                chunks.append(current)
                current = []
                word_count = 0
    if current:
        chunks.append(current)
    return chunks


def render_units(units: List[str]) -> str:
    text = ""
    for unit in units:
        if unit == "\n":
            text += "\n"
        else:
            if text and not text.endswith("\n"):
                text += " "
            text += unit
    return text


async def _safe_edit(
    bot: Bot,
    chat_id,
    message_id: int,
    text: str,
    recent_edits: List[float],
    max_retries: int = 3,
) -> bool:
    """Edit a message with flood-control handling."""
    now = time.monotonic()
    window = [t for t in recent_edits if now - t < 60.0]
    if len(window) >= MAX_EDITS_PER_MINUTE:
        wait = 60.0 - (now - window[0]) + 1.0
        logger.info("Rate limit pre-empt: sleeping %.1fs (edits in last 60s: %d)", wait, len(window))
        await asyncio.sleep(wait)

    for attempt in range(max_retries):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
            recent_edits.append(time.monotonic())
            recent_edits[:] = [t for t in recent_edits if time.monotonic() - t < 60.0]
            return True

        except TelegramRetryAfter as e:
            wait_time = e.retry_after + 1.0
            logger.warning(
                "Flood control: retry after %ds. Sleeping %.1fs (attempt %d/%d)",
                e.retry_after, wait_time, attempt + 1, max_retries,
            )
            await asyncio.sleep(wait_time)
            continue

        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return True
            logger.warning("Edit bad request: %s", e)
            return False

        except Exception as e:
            logger.error("Unexpected edit error: %s", e)
            if attempt < max_retries - 1:
                await asyncio.sleep(2.0)
            continue

    return False


async def animate_text(
    bot: Bot,
    chat_id,
    message_id: int,
    full_text: str,
    settings: ChatSettings,
    cancel_event: asyncio.Event,
) -> bool:
    """
    Progressively reveal text by editing a single Telegram message.
    Returns True if animation completed fully, False if interrupted/failed.
    """
    units = split_into_display_units(full_text)
    chunks = build_chunks(units, settings.words_per_edit)

    if not chunks:
        return True

    displayed: List[str] = []
    recent_edits: List[float] = []

    for i, chunk in enumerate(chunks):
        if cancel_event.is_set():
            try:
                current = render_units(displayed)
                await _safe_edit(bot, chat_id, message_id, current + "\n\n⏹ Остановлено", recent_edits)
            except Exception:
                pass
            return False

        displayed.extend(chunk)
        text_to_show = render_units(displayed)

        if len(text_to_show) > TELEGRAM_MSG_LIMIT:
            text_to_show = text_to_show[:TELEGRAM_MSG_LIMIT - 30] + "\n\n... (продолжение ниже)"
            await _safe_edit(bot, chat_id, message_id, text_to_show, recent_edits)
            return True

        success = await _safe_edit(bot, chat_id, message_id, text_to_show, recent_edits)
        if not success:
            logger.warning("Edit failed at chunk %d/%d, stopping animation", i + 1, len(chunks))
            await asyncio.sleep(2.0)
            try:
                current = render_units(displayed)
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=current + "\n\n⚠️ Анимация прервана (лимит Telegram)",
                )
            except Exception:
                pass
            return False

        if i < len(chunks) - 1:
            await asyncio.sleep(settings.delay)

    # Animation complete
    try:
        final_text = render_units(displayed)
        await _safe_edit(bot, chat_id, message_id, final_text + "\n\n🎵", recent_edits)
    except Exception:
        pass
    return True


# ============================================================
# Queue playback engine
# ============================================================

async def _play_queue(
    bot: Bot,
    chat_id: int,
    control_chat_id: int,
    settings: ChatSettings,
    cancel_event: asyncio.Event,
    key: str,
):
    """
    Play all tracks in the queue sequentially, with loop support.
    This is the main playback loop that runs as a background task.
    """
    q = get_queue(chat_id)
    q.is_playing = True

    try:
        while True:
            track = q.current()
            if track is None:
                # Try to start from beginning if looping
                if q.loop and q.tracks:
                    q.current_index = 0
                    track = q.current()
                else:
                    break

            title = track.get("title", "Unknown")
            lyrics = track.get("lyrics", "")

            if not lyrics:
                # Skip empty tracks
                track = q.next_track()
                continue

            # Send "now playing" card
            target_chat = settings.target_chat
            if target_chat:
                try:
                    tc = target_chat.strip()
                    if not tc.startswith("@"):
                        tc = int(tc)
                except (ValueError, AttributeError):
                    tc = target_chat
            else:
                tc = chat_id

            # Build now-playing message
            next_t = q.peek_next()
            np_text = f"🎵 <b>{title}</b>"
            if next_t:
                np_text += f"\n⏭ Далее: {next_t.get('title', 'Unknown')}"
            loop_icon = " 🔁" if q.loop else ""
            np_text += f"\n📋 Трек {q.current_index + 1}/{len(q.tracks)}{loop_icon}"

            try:
                card_msg = await bot.send_message(chat_id=tc, text=np_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error("Cannot send now-playing card: %s", e)
                break

            await asyncio.sleep(1.5)

            # Send the animation message
            anim_msg = await bot_send(bot, tc, "🎵")
            if anim_msg is None:
                break

            # Run the animation
            completed = await animate_text(
                bot=bot,
                chat_id=tc,
                message_id=anim_msg.message_id,
                full_text=lyrics,
                settings=settings,
                cancel_event=cancel_event,
            )

            if cancel_event.is_set():
                break

            if not completed:
                break

            # Advance to next track
            next_track = q.next_track()
            if next_track is None:
                # Queue finished
                if q.loop and q.tracks:
                    # Loop back — small pause then continue
                    try:
                        await bot.send_message(
                            chat_id=tc,
                            text="🔁 <b>Очередь начинается сначала</b>",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(PAUSE_BETWEEN_TRACKS)
                    continue
                else:
                    # Queue done, no loop
                    try:
                        await bot.send_message(
                            chat_id=tc,
                            text="✅ Очередь закончилась",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                    break

            # Pause between tracks
            await asyncio.sleep(PAUSE_BETWEEN_TRACKS)

    finally:
        q.is_playing = False
        cancel_events.pop(key, None)


# ============================================================
# Bot handlers
# ============================================================

router = Router()


class BotStates(StatesGroup):
    waiting_for_lyrics = State()
    selecting_song = State()
    selecting_song_for_queue = State()


def _resolve_target(message: types.Message, settings: ChatSettings):
    """Resolve the actual chat_id where animation should play."""
    if settings.target_chat:
        tc = settings.target_chat.strip()
        if tc.startswith("@"):
            return tc
        try:
            return int(tc)
        except ValueError:
            return tc
    return message.chat.id


async def bot_send(bot: Bot, chat_id, text: str) -> Optional[types.Message]:
    try:
        return await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error("Cannot send to %s: %s", chat_id, e)
        return None


# ── /start ──────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "🎵 <b>Lyrics Bot</b>\n\n"
        "Я пишу текст песни слово за словом, "
        "редактируя одно сообщение.\n\n"
        "<b>Основные команды:</b>\n"
        "/play <i>запрос</i> — найти и играть песню\n"
        "/add <i>запрос</i> — добавить в очередь\n"
        "/queue — показать очередь\n"
        "/skip — пропустить трек\n"
        "/loop — повтор по кругу 🔁\n\n"
        "<b>Настройки:</b>\n"
        "/words <i>N</i> — слов за редактирование\n"
        "/delay <i>S</i> — задержка (сек)\n"
        "/target <i>@канал</i> — писать в канал\n"
        "/settings — текущие настройки\n\n"
        "Пример: <code>/play Imagine - John Lennon</code>",
        parse_mode=ParseMode.HTML,
    )


# ── /help ───────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 <b>Справка Lyrics Bot</b>\n\n"

        "<b>Воспроизведение:</b>\n"
        "/play &lt;запрос&gt; — найти песню и играть (добавляет в очередь)\n"
        "/type &lt;текст&gt; — анимировать текст напрямую\n"
        "/paste — режим вставки текста\n\n"

        "<b>Очередь:</b>\n"
        "/add &lt;запрос&gt; — добавить песню в очередь\n"
        "/addtype &lt;текст&gt; — добавить текст в очередь\n"
        "/queue — показать текущую очередь\n"
        "/remove &lt;N&gt; — убрать трек №N\n"
        "/clear — очистить очередь\n"
        "/shuffle — перемешать очередь\n"
        "/loop — вкл/выкл повтор по кругу 🔁\n"
        "/skip — пропустить текущий трек\n\n"

        "<b>Настройки:</b>\n"
        "/words &lt;N&gt; — слов за редактирование (1–50)\n"
        "/delay &lt;S&gt; — задержка между редактированиями (2–30 сек)\n"
        "/target &lt;@канал&gt; — писать анимацию в канал\n"
        "/settings — текущие настройки\n"
        "/stop — остановить анимацию и очистить очередь\n\n"

        "💡 <i>Совет:</i> /play автоматически создаёт очередь.\n"
        "Добавляйте треки через /add пока играет текущий."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ── /settings ───────────────────────────────────────────────

@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    s = get_settings(message.chat.id)
    q = get_queue(message.chat.id)
    target_info = s.target_chat if s.target_chat else "текущий чат"
    loop_status = "🔁 Вкл" if s.loop else "❌ Выкл"
    queue_info = f"{len(q.tracks)} треков" if q.tracks else "пуста"
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"📝 Слов за редактирование: <b>{s.words_per_edit}</b>\n"
        f"⏱ Задержка: <b>{s.delay}</b> сек\n"
        f"🎯 Целевой чат: <b>{target_info}</b>\n"
        f"🔁 Повтор очереди: <b>{loop_status}</b>\n"
        f"📋 Очередь: <b>{queue_info}</b>\n\n"
        "Изменить: /words N, /delay S, /target @канал, /loop"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ── /words ──────────────────────────────────────────────────

@router.message(Command("words"))
async def cmd_words(message: types.Message, command: Command):
    args = command.args
    if not args:
        s = get_settings(message.chat.id)
        await message.answer(
            f"📝 Текущее: <b>{s.words_per_edit}</b> слов/редакт.\n"
            f"Изменить: <code>/words N</code> (1–{MAX_WORDS})",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        n = int(args.strip())
        if n < 1 or n > MAX_WORDS:
            raise ValueError
    except ValueError:
        await message.answer(f"❌ Число от 1 до {MAX_WORDS}. Пример: <code>/words 5</code>", parse_mode=ParseMode.HTML)
        return
    update_settings(message.chat.id, words_per_edit=n)
    await message.answer(f"✅ Слов за редактирование: <b>{n}</b>", parse_mode=ParseMode.HTML)


# ── /delay ──────────────────────────────────────────────────

@router.message(Command("delay"))
async def cmd_delay(message: types.Message, command: Command):
    args = command.args
    if not args:
        s = get_settings(message.chat.id)
        await message.answer(
            f"⏱ Текущая задержка: <b>{s.delay}</b> сек\n"
            f"Изменить: <code>/delay S</code> ({MIN_DELAY}–{MAX_DELAY})",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        d = float(args.strip())
        if d < MIN_DELAY or d > MAX_DELAY:
            raise ValueError
    except ValueError:
        await message.answer(f"❌ Число от {MIN_DELAY} до {MAX_DELAY}. Пример: <code>/delay 3.0</code>", parse_mode=ParseMode.HTML)
        return
    update_settings(message.chat.id, delay=d)
    await message.answer(f"✅ Задержка: <b>{d}</b> сек", parse_mode=ParseMode.HTML)


# ── /target ─────────────────────────────────────────────────

@router.message(Command("target"))
async def cmd_target(message: types.Message, command: Command):
    args = command.args
    if not args:
        s = get_settings(message.chat.id)
        target = s.target_chat or "текущий чат"
        await message.answer(
            f"🎯 Целевой чат: <b>{target}</b>\n"
            "Установить: <code>/target @канал</code>\n"
            "Сбросить: <code>/target off</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    arg = args.strip()
    if arg.lower() in ("off", "нет", "сброс", "reset"):
        update_settings(message.chat.id, target_chat=None)
        await message.answer("✅ Целевой чат сброшен.")
        return

    if not arg.startswith("@") and not arg.lstrip("-").isdigit():
        await message.answer("❌ Формат: <code>/target @канал</code> или <code>/target -1001234567890</code>", parse_mode=ParseMode.HTML)
        return

    update_settings(message.chat.id, target_chat=arg)
    await message.answer(f"✅ Целевой чат: <b>{arg}</b>", parse_mode=ParseMode.HTML)


# ── /loop ───────────────────────────────────────────────────

@router.message(Command("loop"))
async def cmd_loop(message: types.Message):
    s = get_settings(message.chat.id)
    q = get_queue(message.chat.id)
    new_val = not s.loop
    update_settings(message.chat.id, loop=new_val)
    q.loop = new_val

    status = "🔁 Повтор очереди: <b>ВКЛ</b>" if new_val else "🔁 Повтор очереди: <b>ВЫКЛ</b>"
    await message.answer(status, parse_mode=ParseMode.HTML)


# ── /queue ──────────────────────────────────────────────────

@router.message(Command("queue"))
async def cmd_queue(message: types.Message):
    q = get_queue(message.chat.id)
    await message.answer(q.display(), parse_mode=ParseMode.HTML)


# ── /clear ──────────────────────────────────────────────────

@router.message(Command("clear"))
async def cmd_clear(message: types.Message):
    q = get_queue(message.chat.id)
    q.clear()
    await message.answer("🗑 Очередь очищена.")


# ── /shuffle ────────────────────────────────────────────────

@router.message(Command("shuffle"))
async def cmd_shuffle(message: types.Message):
    q = get_queue(message.chat.id)
    if not q.tracks:
        await message.answer("📭 Очередь пуста — нечего мешать.")
        return
    q.shuffle()
    await message.answer(f"🔀 Очередь перемешана!\n\n{q.display()}", parse_mode=ParseMode.HTML)


# ── /remove ─────────────────────────────────────────────────

@router.message(Command("remove"))
async def cmd_remove(message: types.Message, command: Command):
    args = command.args
    if not args:
        await message.answer("❌ Укажите номер: <code>/remove 2</code>", parse_mode=ParseMode.HTML)
        return
    try:
        n = int(args.strip())
        if n < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Номер от 1. Пример: <code>/remove 2</code>", parse_mode=ParseMode.HTML)
        return

    q = get_queue(message.chat.id)
    removed = q.remove(n - 1)  # Convert 1-based to 0-based
    if removed:
        await message.answer(f"✅ Убрано: <b>{removed}</b>", parse_mode=ParseMode.HTML)
    else:
        await message.answer(f"❌ Трек #{n} не найден.")


# ── /stop ───────────────────────────────────────────────────

@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    key = str(message.chat.id)
    q = get_queue(message.chat.id)

    if key in cancel_events:
        cancel_events[key].set()
        del cancel_events[key]

    q.clear()
    await message.answer("⏹ Остановлено. Очередь очищена.")


# ── /skip ───────────────────────────────────────────────────

@router.message(Command("skip"))
async def cmd_skip(message: types.Message):
    key = str(message.chat.id)
    q = get_queue(message.chat.id)

    if not q.tracks:
        await message.answer("ℹ️ Очередь пуста.")
        return

    current_title = q.current().get("title", "Unknown") if q.current() else "Unknown"

    # Cancel current animation — the queue loop will advance automatically
    if key in cancel_events:
        cancel_events[key].set()
        # Don't delete — let _play_queue handle it

    next_t = q.peek_next()
    if next_t:
        await message.answer(f"⏭ Пропускаю: <b>{current_title}</b>\n▶️ Далее: <b>{next_t.get('title', 'Unknown')}</b>", parse_mode=ParseMode.HTML)
    else:
        if q.loop:
            await message.answer(f"⏭ Пропускаю: <b>{current_title}</b>\n🔁 Начинаю сначала", parse_mode=ParseMode.HTML)
        else:
            await message.answer(f"⏭ Пропускаю: <b>{current_title}</b>\n📋 Это был последний трек")


# ── /add — add to queue without interrupting ────────────────

@router.message(Command("add"))
async def cmd_add(message: types.Message, command: Command, state: FSMContext):
    args = command.args
    if not args:
        await message.answer("🔍 Укажите запрос: <code>/add Artist - Song</code>", parse_mode=ParseMode.HTML)
        return

    status_msg = await message.answer(f"🔍 Ищу: <b>{args}</b>...", parse_mode=ParseMode.HTML)
    lyrics, results = await search_lyrics(args)

    if not lyrics:
        await status_msg.edit_text(
            "😔 Текст не найден.\n"
            "Попробуйте: <code>/add Artist - Song</code>\n"
            "Или добавьте текст: <code>/addtype ваш текст</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(results) > 1:
        await state.update_data(search_results=results, adding_to_queue=True)
        await state.set_state(BotStates.selecting_song_for_queue)

        buttons = []
        for idx, r in enumerate(results[:8]):
            label = f"{r['artist']} — {r['title']}"
            if len(label) > 60:
                label = label[:57] + "..."
            buttons.append([types.InlineKeyboardButton(text=label, callback_data=f"qsel:{idx}")])
        buttons.append([types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"qsel:cancel")])

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
        await status_msg.edit_text(
            f"🔍 Найдено {len(results)} вариантов для <b>{args}</b>:\nВыберите:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    # Single result — add directly
    r = results[0]
    title = f"{r['artist']} — {r['title']}"
    q = get_queue(message.chat.id)
    q.add(Track(title=title, lyrics=r["lyrics"], source=args))

    q_len = len(q.tracks)
    position = q_len
    await status_msg.edit_text(
        f"✅ Добавлено в очередь: <b>{title}</b>\n"
        f"📋 Позиция: {position}/{q_len}",
        parse_mode=ParseMode.HTML,
    )

    # If nothing is playing, start playback
    await _maybe_start_queue(message, state)


# ── /addtype — add text to queue ────────────────────────────

@router.message(Command("addtype"))
async def cmd_addtype(message: types.Message, command: Command):
    args = command.args
    if not args:
        await message.answer("✏️ Укажите текст: <code>/addtype Ваш текст песни</code>", parse_mode=ParseMode.HTML)
        return

    q = get_queue(message.chat.id)
    # Use first 30 chars as title
    title = args[:50].replace("\n", " ")
    if len(args) > 50:
        title += "..."
    q.add(Track(title=title, lyrics=args, source="/addtype"))

    q_len = len(q.tracks)
    position = q_len
    await message.answer(
        f"✅ Добавлено в очередь: <b>{title}</b>\n"
        f"📋 Позиция: {position}/{q_len}",
        parse_mode=ParseMode.HTML,
    )

    await _maybe_start_queue_from_text(message)


# ── /play — search, play and add to queue ───────────────────

@router.message(Command("play"))
async def cmd_play(message: types.Message, command: Command, state: FSMContext):
    args = command.args
    if not args:
        await message.answer("🔍 Укажите запрос: <code>/play Artist - Song</code>", parse_mode=ParseMode.HTML)
        return

    status_msg = await message.answer(f"🔍 Ищу: <b>{args}</b>...", parse_mode=ParseMode.HTML)
    lyrics, results = await search_lyrics(args)

    if not lyrics:
        await status_msg.edit_text(
            "😔 Текст не найден.\n\n"
            "Попробуйте:\n"
            "• <code>/play Artist - Song</code>\n"
            "• <code>/type ваш текст</code>\n"
            "• <code>/paste</code> — режим вставки",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(results) > 1:
        await state.update_data(search_results=results, adding_to_queue=False)
        await state.set_state(BotStates.selecting_song)

        buttons = []
        for idx, r in enumerate(results[:8]):
            label = f"{r['artist']} — {r['title']}"
            if len(label) > 60:
                label = label[:57] + "..."
            buttons.append([types.InlineKeyboardButton(text=label, callback_data=f"sel:{idx}")])
        buttons.append([types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"sel:cancel")])

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
        await status_msg.edit_text(
            f"🔍 Найдено {len(results)} вариантов для <b>{args}</b>:\nВыберите:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    # Single result
    r = results[0]
    title = f"{r['artist']} — {r['title']}"
    q = get_queue(message.chat.id)

    # Stop current animation
    key = str(message.chat.id)
    if key in cancel_events:
        cancel_events[key].set()
        await asyncio.sleep(0.3)

    q.clear()
    q.add(Track(title=title, lyrics=r["lyrics"], source=args))
    q.current_index = 0

    await status_msg.edit_text(
        f"▶️ Играю: <b>{title}</b>\n📋 Очередь: 1 трек",
        parse_mode=ParseMode.HTML,
    )

    # Start playback
    await _start_queue_playback(message)


# ── Callback: song selection for /play ──────────────────────

@router.callback_query(BotStates.selecting_song, F.data.startswith("sel:"))
async def process_song_selection(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    idx = int(action)
    state_data = await state.get_data()
    results = state_data.get("search_results", [])
    if idx >= len(results):
        await callback.answer("❌ Ошибка выбора", show_alert=True)
        return

    selected = results[idx]
    title = f"{selected['artist']} — {selected['title']}"
    await state.clear()

    key = str(callback.message.chat.id)
    if key in cancel_events:
        cancel_events[key].set()
        await asyncio.sleep(0.3)

    q = get_queue(callback.message.chat.id)
    q.clear()
    q.add(Track(title=title, lyrics=selected["lyrics"], source="search"))
    q.current_index = 0

    await callback.message.edit_text(
        f"▶️ Играю: <b>{title}</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer(f"▶️ {title}")

    await _start_queue_playback_from_callback(callback)


# ── Callback: song selection for /add ───────────────────────

@router.callback_query(BotStates.selecting_song_for_queue, F.data.startswith("qsel:"))
async def process_queue_song_selection(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    idx = int(action)
    state_data = await state.get_data()
    results = state_data.get("search_results", [])
    if idx >= len(results):
        await callback.answer("❌ Ошибка выбора", show_alert=True)
        return

    selected = results[idx]
    title = f"{selected['artist']} — {selected['title']}"
    await state.clear()

    q = get_queue(callback.message.chat.id)
    q.add(Track(title=title, lyrics=selected["lyrics"], source="search"))

    q_len = len(q.tracks)
    await callback.message.edit_text(
        f"✅ Добавлено: <b>{title}</b>\n📋 Позиция: {q_len}/{q_len}",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer(f"✅ {title}")

    # If nothing is playing, start
    if not q.is_playing:
        await _start_queue_playback_from_callback(callback)


# ── /type — animate provided text ──────────────────────────

@router.message(Command("type"))
async def cmd_type(message: types.Message, command: Command):
    args = command.args
    if not args:
        await message.answer("✏️ Укажите текст: <code>/type Ваш текст</code>", parse_mode=ParseMode.HTML)
        return

    q = get_queue(message.chat.id)
    key = str(message.chat.id)

    if key in cancel_events:
        cancel_events[key].set()
        await asyncio.sleep(0.3)

    title = args[:50].replace("\n", " ")
    if len(args) > 50:
        title += "..."

    q.clear()
    q.add(Track(title=title, lyrics=args, source="/type"))
    q.current_index = 0

    await _start_queue_playback(message)


# ── /paste — multi-line input mode ─────────────────────────

@router.message(Command("paste"))
async def cmd_paste(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_lyrics)
    await message.answer(
        "📋 <b>Режим вставки</b>\n\n"
        "Отправьте текст песни следующим сообщением.\n"
        "Отмена: /cancel",
        parse_mode=ParseMode.HTML,
    )


# ── Cancel paste mode ──────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current is not None:
        await state.clear()
        await message.answer("❌ Отменено.")
    else:
        await message.answer("ℹ️ Нечего отменять.")


# ── Handle lyrics text in paste mode ───────────────────────

@router.message(BotStates.waiting_for_lyrics, F.text)
async def process_paste_lyrics(message: types.Message, state: FSMContext):
    await state.clear()

    q = get_queue(message.chat.id)
    key = str(message.chat.id)

    if key in cancel_events:
        cancel_events[key].set()
        await asyncio.sleep(0.3)

    title = message.text[:50].replace("\n", " ")
    if len(message.text) > 50:
        title += "..."

    q.clear()
    q.add(Track(title=title, lyrics=message.text, source="/paste"))
    q.current_index = 0

    await _start_queue_playback(message)


# ── Handle plain text messages (smart detection) ───────────

@router.message(F.text, ~F.text.startswith("/"))
async def handle_free_text(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    text = message.text.strip()

    # Multi-line or long text → treat as lyrics, add to queue
    if "\n" in text or len(text.split()) > 20:
        q = get_queue(message.chat.id)
        key = str(message.chat.id)

        if key in cancel_events:
            cancel_events[key].set()
            await asyncio.sleep(0.3)

        title = text[:50].replace("\n", " ")
        if len(text) > 50:
            title += "..."

        q.clear()
        q.add(Track(title=title, lyrics=text, source="text"))
        q.current_index = 0

        await _start_queue_playback(message)
        return

    # Short single-line text → search and add to queue
    status_msg = await message.answer(f"🔍 Ищу: <b>{text}</b>...", parse_mode=ParseMode.HTML)
    lyrics, results = await search_lyrics(text)

    if not lyrics:
        await status_msg.edit_text(
            "😔 Текст не найден.\n\n"
            "Вставьте текст: <code>/paste</code>\n"
            "Или: <code>/type ваш текст</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(results) > 1:
        await state.update_data(search_results=results, adding_to_queue=False)
        await state.set_state(BotStates.selecting_song)

        buttons = []
        for idx, r in enumerate(results[:8]):
            label = f"{r['artist']} — {r['title']}"
            if len(label) > 60:
                label = label[:57] + "..."
            buttons.append([types.InlineKeyboardButton(text=label, callback_data=f"sel:{idx}")])
        buttons.append([types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"sel:cancel")])

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
        await status_msg.edit_text(
            f"🔍 Найдено {len(results)} вариантов:\nВыберите:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    # Single result
    r = results[0]
    title = f"{r['artist']} — {r['title']}"
    q = get_queue(message.chat.id)
    key = str(message.chat.id)

    if key in cancel_events:
        cancel_events[key].set()
        await asyncio.sleep(0.3)

    q.clear()
    q.add(Track(title=title, lyrics=r["lyrics"], source=text))
    q.current_index = 0

    await status_msg.edit_text(f"▶️ Играю: <b>{title}</b>", parse_mode=ParseMode.HTML)

    await _start_queue_playback(message)


# ── Queue playback starters ─────────────────────────────────

async def _maybe_start_queue(message: types.Message, state: FSMContext):
    """Start queue playback if nothing is currently playing."""
    q = get_queue(message.chat.id)
    if q.is_playing:
        return
    if not q.tracks:
        return

    q.current_index = 0
    await _start_queue_playback(message)


async def _maybe_start_queue_from_text(message: types.Message):
    """Start queue playback if nothing is currently playing (for /addtype)."""
    q = get_queue(message.chat.id)
    if q.is_playing:
        return
    if not q.tracks:
        return

    q.current_index = 0
    await _start_queue_playback(message)


async def _start_queue_playback(message: types.Message):
    """Launch the queue playback background task."""
    key = str(message.chat.id)
    s = get_settings(message.chat.id)
    q = get_queue(message.chat.id)
    q.loop = s.loop

    cancel_event = asyncio.Event()
    cancel_events[key] = cancel_event

    asyncio.create_task(
        _play_queue(
            bot=message.bot,
            chat_id=message.chat.id,
            control_chat_id=message.chat.id,
            settings=s,
            cancel_event=cancel_event,
            key=key,
        )
    )


async def _start_queue_playback_from_callback(callback: types.CallbackQuery):
    """Launch the queue playback from a callback (inline button)."""
    key = str(callback.message.chat.id)
    s = get_settings(callback.message.chat.id)
    q = get_queue(callback.message.chat.id)
    q.loop = s.loop

    cancel_event = asyncio.Event()
    cancel_events[key] = cancel_event

    asyncio.create_task(
        _play_queue(
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            control_chat_id=callback.message.chat.id,
            settings=s,
            cancel_event=cancel_event,
            key=key,
        )
    )


# ============================================================
# Main entry point
# ============================================================

async def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN environment variable is required!")
        print("Set it with: export BOT_TOKEN=your_telegram_bot_token")
        sys.exit(1)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    import signal as sig_module
    shutdown_event = asyncio.Event()

    def _signal_handler():
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (sig_module.SIGINT, sig_module.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
    except NotImplementedError:
        pass

    logger.info("Lyrics Bot starting...")

    try:
        await dp.start_polling(bot, handle_signals=False)
    except Exception as e:
        logger.error("Polling error: %s", e)
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
