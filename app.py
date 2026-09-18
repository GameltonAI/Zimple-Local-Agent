import atexit
import base64
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, g, has_request_context, jsonify, render_template,
    request, send_from_directory, send_file,
)

import browser_agent
import projects_agent
import computer_agent
import local_files_agent

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "zimple_data.json"
LEGACY_MARKER = BASE_DIR / "zimple_data.json.migrated"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DEVICES_DIR = BASE_DIR / "devices"
DEVICES_DIR.mkdir(exist_ok=True)
DEBUG_DIR = BASE_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".heic"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".ogv"}
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env",
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".rb", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h",
    ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cs", ".php", ".sh",
    ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".lua", ".r", ".pl",
    ".html", ".htm", ".xml", ".svg", ".css", ".scss", ".less", ".sass",
    ".sql", ".dockerfile", ".gitignore", ".editorconfig",
}

PORT = 5000
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
# ВАЖНО: имя должно точно совпадать с тем, что загружено в LM Studio.
# Открой LM Studio → вкладка моделей → скопируй имя справа (например
# "gemma-3-12b-it" или "qwen2.5-coder-32b-instruct").
LM_MODEL = os.environ.get("ZIMPLE_MODEL", "zai-org/glm-4.6v-flash")
LM_TIMEOUT = 240
HISTORY_LIMIT = 30
MAX_LLM_RETRIES = 3
MAX_AGENT_STEPS = 100


MEMORY_FILE = BASE_DIR / "memory.md"
MEMORY_LIMIT = 16000


def read_memory() -> str:
    try:
        if MEMORY_FILE.exists():
            return MEMORY_FILE.read_text(encoding="utf-8")
    except Exception as e:
        print("[zimple] memory read failed:", e)
    return ""


def write_memory(text: str) -> str:
    text = (text or "")[:MEMORY_LIMIT]
    try:
        MEMORY_FILE.write_text(text, encoding="utf-8")
    except Exception as e:
        print("[zimple] memory write failed:", e)
    return text


def append_memory(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return read_memory()
    cur = read_memory().rstrip()
    stamp = datetime.now().strftime("%Y-%m-%d")
    line = f"- {text}  _(записано {stamp})_"
    if cur:
        new = cur + "\n" + line + "\n"
    else:
        new = "# Память Zimple AI\n\n" + line + "\n"
    return write_memory(new)


def _memory_prompt_section() -> str:
    mem = read_memory().strip()
    if not mem:
        return (
            "\n\nПАМЯТЬ (memory.md): пока пуста. Если пользователь сообщает\n"
            "что-то, что пригодится в будущих чатах (имя, ОС, предпочтения,\n"
            "рабочие пути, привычки) — сохрани это блоком\n"
            '{"type":"memory","action":"append","text":"..."}.\n'
        )
    if len(mem) > MEMORY_LIMIT:
        mem = mem[:MEMORY_LIMIT] + "\n... [усечено]"
    return (
        "\n\nПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ (файл memory.md, общий для всех чатов) —\n"
        "учитывай это и не переспрашивай уже известное:\n"
        "-----\n" + mem + "\n-----\n"
        "Новые важные факты сохраняй блоком\n"
        '{"type":"memory","action":"append","text":"..."} (коротко, по делу).\n'
    )


# ============================================================
#  PROMPTS
# ============================================================
CHAT_SYSTEM_PROMPT = """Ты — Zimple AI, агентный ассистент. Ты умеешь:

1) Управлять РЕАЛЬНЫМ браузером (Chromium через Playwright): открывать сайты,
   кликать, вводить текст, нажимать клавиши, скроллить.
2) Управлять РАБОЧИМ СТОЛОМ Windows: кликать по элементам открытых окон,
   печатать текст, нажимать горячие клавиши, двигать мышь.
3) Сохранять файлы прямо на диск пользователя (рабочий стол, документы,
   загрузки, произвольная папка) — не только в чат-сообщение.

ТВОЙ ОТВЕТ — РОВНО ОДИН JSON-ОБЪЕКТ. Никаких пояснений и markdown вокруг.

Схема: {"blocks":[...], "content":"итог", "done":true/false}

Поле "done":
  - true  — задача полностью выполнена, действий больше не будет
  - false — задача ещё в процессе, обязательно должен быть action-блок

В обычном чате (просто болтаем, отвечаем на вопрос) "done" можно
не указывать — это не агентская сессия.

Типы блоков:
- reasoning: {"type":"reasoning","text":"...","delay":0}
- status:    {"type":"status","text":"...","ok":true,"delay":1}
- browser:   {"type":"browser","text":"...","action":"goto","url":"https://...","delay":2}
- computer:  {"type":"computer","text":"...","action":"click","target":"el_5","delay":1}
- file:      {"type":"file","action":"write_file","path":"desktop:report.md",
              "content":"...ПОЛНЫЙ ТЕКСТ ФАЙЛА...","text":"Сохраняю отчёт на рабочий стол","delay":0.5}
- memory:    {"type":"memory","action":"append","text":"Пользователя зовут Вадим, Windows 11","delay":0}

Browser-действия: goto, click, type, press, scroll, back, wait, download.
Computer-действия: click, double_click, right_click, type, key, scroll,
                   move, drag, wait, screenshot.
File-действия: write_file (создаёт/перезаписывает), append_file (дописывает).
Memory-действия: append (добавить факт), replace (переписать всю память),
                 clear (очистить).

СКАЧИВАНИЕ ФАЙЛОВ ИЗ БРАУЗЕРА:
- {"type":"browser","action":"download","selector":"a.download-link","text":"Скачиваю файл"}
  или с прямой ссылкой: {"type":"browser","action":"download","url":"https://.../file.pdf"}
- Файл сохраняется в папку загрузок пользователя. В следующем шаге система
  пришлёт тебе полный путь до него — обязательно укажи этот путь в ответе
  пользователю, например: «Файл скачан и лежит по пути: C:\\Users\\...\\file.pdf».

ПАМЯТЬ МЕЖДУ ЧАТАМИ:
- Память общая для всех чатов. Сохраняй туда только устойчивые факты
  (имя, ОС, любимые инструменты, рабочие папки, предпочтения по стилю),
  а не детали одной задачи.
- memory-блок можно отправлять вместе с обычным ответом, он не считается
  действием и не мешает завершить задачу.

ВАЖНО ПРО СЕЛЕКТОРЫ vs TARGET (частая ошибка):
- В браузере элемент указывается через "selector" (CSS-селектор), например
  {"type":"browser","action":"click","selector":"a.result-title","text":"..."}.
  Бери селектор из списка «Интерактивные элементы» браузера — там даны id
  (#id), class (.class) или name ([name=...]).
- На рабочем столе элемент указывается через "target" (el_N из списка
  элементов окна), например
  {"type":"computer","action":"click","target":"el_5","text":"..."}.
- НЕ путай их: у browser-блока никогда не бывает "target", у computer-блока
  никогда не бывает "selector". Для click/type без нужного поля блок будет
  отброшен и шаг потрачен впустую.

ПРО FILE-БЛОК (запись на диск пользователя):
- "path" — путь до файла. Поддерживаются алиасы: "desktop:имя.md" (рабочий
  стол), "documents:имя.md" (документы), "downloads:имя.md" (загрузки),
  "home:папка/имя.md" (домашняя папка). Можно указать и обычный абсолютный
  путь. Если алиас не указан — файл ляжет в документы.
- "content" в write_file — ГОТОВЫЙ ПОЛНЫЙ ТЕКСТ файла, не описание того,
  что в нём будет.
- Используй file-блок, когда пользователь просит СОХРАНИТЬ, СДЕЛАТЬ ФАЙЛОМ,
  "положить на рабочий стол/в документы" результат — например, после того,
  как собрал информацию с нескольких сайтов через browser-блоки. Не нужно
  в таком случае вываливать всё найденное в content чата — оформи это как
  файл (обычно .md) и коротко скажи об этом в content.
- Если пользователь явно НЕ просил сохранять — просто отвечай в чате,
  file-блок не нужен.

ЛОГИКА:
- ОДИН action-блок за ответ (browser/computer/file — выбирай тот, что
  нужен для текущего шага).
- Пока задача не выполнена — по одному блоку за ответ.
- Когда задача выполнена — верни {"blocks":[], "content":"итог"}.

КРИТИЧЕСКИ ВАЖНО:
- НИКОГДА не пиши в content глаголы прошедшего времени («открыл»,
  «нажал», «ввёл», «перешёл», «запустил», «кликнул», «открываю»,
  «нажимаю», «ввожу», «сохранил»). Если хочешь это сказать — значит нужно
  СЕЙЧАС отправить соответствующий блок.
- Финальный ответ-завершение: {"blocks":[], "content":"Готово"} или
  {"blocks":[], "content":"Задача выполнена"}.
- Если просто болтают — {"blocks":[], "content":"..."}.
- Если задача ещё НЕ выполнена — ты ОБЯЗАН отправить action-блок.
  Слова «фильтрую», «ищу», «анализирую», «смотрю» БЕЗ блока — это ошибка,
  система не увидит никакого действия и остановится.
- Финальный ответ принимается только если он содержит явный маркер
  завершения: «Готово», «Задача выполнена», «Вот ссылки:», «Не удалось»
  или реальные результаты (список, ссылки).
"""


COMPUTER_SYSTEM_PROMPT = """Ты — Zimple AI. СЕЙЧАС ТЫ УПРАВЛЯЕШЬ РАБОЧИМ СТОЛОМ.
Это режим выполнения задачи, а не разговор.

ТВОЙ ОТВЕТ — РОВНО ОДИН JSON-ОБЪЕКТ. Никаких пояснений, markdown, списков.
Схема: {"blocks":[...], "content":"итог", "done":true/false}

Поле "done":
  - true  — задача полностью выполнена, действий больше не будет
  - false — задача ещё в процессе, обязательно должен быть action-блок

В обычном чате (просто болтаем, отвечаем на вопрос) "done" можно
не указывать — это не агентская сессия.

ТЫ ПОЛУЧАЕШЬ НА КАЖДОМ ШАГЕ:
  - скриншот экрана,
  - список элементов активного окна: у каждого есть ID (el_N),
    тип, название и координаты центра.

ДЕЙСТВИЯ (ровно один блок за ответ):
- click        {"type":"computer","action":"click","target":"el_5","text":"Кликаю по X"}
- double_click {"type":"computer","action":"double_click","target":"el_5","text":"Двойной клик по X"}
- right_click  {"type":"computer","action":"right_click","x":512,"y":340,"text":"ПКМ по X"}
- type         {"type":"computer","action":"type","value":"текст","text":"Печатаю ..."}
- key          {"type":"computer","action":"key","keys":["ctrl","s"],"text":"Сохраняю"}
- scroll       {"type":"computer","action":"scroll","dy":500,"text":"Скроллю вниз"}
- wait         {"type":"computer","action":"wait","seconds":2,"text":"Жду загрузки"}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ГЛАВНОЕ ПРАВИЛО — target ОБЯЗАТЕЛЕН.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Для click / double_click ТЫ ОБЯЗАН указать target — ID из списка
элементов, который ты получил. Это выглядит так:

  ✅ ПРАВИЛЬНО:
     {"type":"computer","action":"double_click","target":"el_22",
      "text":"Двойной клик по ярлыку DeepSeek"}

  ❌ НЕПРАВИЛЬНО (будет отброшено системой):
     {"type":"computer","action":"click"}                    ← нет target!
     {"type":"computer","action":"click","text":"кликну"}    ← нет target!
     {"type":"computer","action":"click","target":"DeepSeek"} ← это не ID!

Текстовое название (Например: «DeepSeek», «New chat», «отправить») НЕ является
target. target — это строго «el_число» из списка ниже.

Если элемента с нужным ярлыком НЕТ в списке — значит его не видно
в текущем активном окне. Тогда отправь
  {"type":"computer","action":"scroll","dy":300}
или
  {"type":"computer","action":"wait","seconds":2}
или верни
  {"blocks":[], "content":"Не вижу нужный элемент: <причина>"}.

НИКОГДА не отправляй click без target.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ЕЩЁ ПРАВИЛА:
1. ОДИН action-блок за ответ.
2. Чтобы ввести текст в поле — сначала ОТДЕЛЬНЫМ ответом отправь click
   по этому полю (с target!), дождись нового состояния, и только потом
   отправь type.
3. НИКОГДА не пиши в content глаголы действия («открыл», «нажал», «ввёл»,
   «перешёл»). Если хочешь это сказать — значит надо отправить блок СЕЙЧАС.
4. «Ждать» без причины — запрещено. Если ждёшь загрузки — используй wait.
5. Когда задача РЕАЛЬНО выполнена — верни {"blocks":[], "content":"Готово"}.
6. НИКОГДА не завершай ответ словами-действиями («фильтрую», «ищу»,
   «проверяю», «жду»). Если ты это делаешь — отправь соответствующий блок
   (scroll / wait / click). Завершение — только «Готово», «Не удалось»
   или реальный результат.
   
reasoning и status можно добавлять к блоку действия — они показываются
в UI, но не выполняют действий.
"""


PROJECT_SYSTEM_PROMPT = """Ты — Zimple AI, работаешь ВНУТРИ ПРОЕКТА.

Проект — это папка с файлами. Список файлов ниже. Работай только с файлами.
Ни браузера, ни рабочего стола здесь нет.

ТВОЙ ОТВЕТ — РОВНО ОДИН JSON-ОБЪЕКТ. Никаких пояснений и markdown вокруг.

Схема: {"blocks":[...], "content":"итог"}

ТИПЫ БЛОКОВ:
1) reasoning: {"type":"reasoning","text":"...","delay":0}
2) status:    {"type":"status","text":"Анализирую файл","ok":true,"delay":0.5}
3) project:   ОДИН такой блок за ответ:
{
  "type":"project",
  "action":"write_file",
  "text":"Создаю index.html",
  "path":"index.html",
  "content":"...ПОЛНЫЙ ГОТОВЫЙ ТЕКСТ ФАЙЛА...",
  "delay":0.5
}

ДЕЙСТВИЯ:
- list_files   {"action":"list_files","text":"Смотрю файлы"}
- read_file    {"action":"read_file","path":"index.html","text":"Читаю index.html"}
- write_file   {"action":"write_file","path":"new.py","content":"...","text":"Создаю new.py"}
- patch_file   {"action":"patch_file","path":"main.py",
                "find":"старый фрагмент","replace":"новый фрагмент","count":1,
                "text":"Правлю main.py"}
- append_file  {"action":"append_file","path":"notes.txt","content":"...","text":"Дописываю notes.txt"}
- delete_file  {"action":"delete_file","path":"old.py","text":"Удаляю old.py"}

ПРАВИЛА:
1. ОДИН project-блок за ответ.
2. "content" в write_file / append_file — ГОТОВЫЙ ТЕКСТ ФАЙЛА, не описание.
3. "text" — короткое описание действия для UI.
4. Ответ НИКОГДА не может состоять только из reasoning.
5. Перед правкой файла — read_file, потом patch_file.
6. НЕ переписывай файл целиком ради одной строки — используй patch_file.
7. Пути — точно как в списке, относительные, через слэш.

=== СПИСОК ФАЙЛОВ ===
{files}
"""


MODE_CLASSIFY_SYSTEM_PROMPT = """Ты — классификатор режимов ассистента Zimple AI.
По ПОСЛЕДНЕМУ сообщению пользователя определи, что нужно сделать СЕЙЧАС:

- "browser"  — открыть сайт, найти что-то в интернете, зайти на страницу,
               собрать информацию с сайтов, оформить заказ на сайте и т.п.
               Это работа с РЕАЛЬНЫМ браузером (Chromium).
- "computer" — управлять уже ОТКРЫТЫМИ ПРИЛОЖЕНИЯМИ РАБОЧЕГО СТОЛА Windows
               (НЕ браузером): кликать по окнам, печатать в программах,
               работать с проводником, ярлыками на рабочем столе, локально
               установленными программами.
- "chat"     — обычный разговор, вопрос, объяснение, написание/сохранение
               текста или файла, работа с файлами проекта — действие в
               интернете или на рабочем столе не требуется.

Важно: "открой сайт X" или "погугли Y" — это "browser", а не "computer",
даже если там есть слово «открой». "computer" — только когда явно речь
о рабочем столе, окне, ярлыке или локальном приложении Windows.

Ответь РОВНО одним словом: browser, computer или chat.
Никаких пояснений, кавычек и знаков препинания.
"""


# ============================================================
#  DEVICES
# ============================================================
DEVICES: dict = {}
DEVICE_LOCK = threading.Lock()
_LEGACY_MIGRATED = False


def _device_file(dev_id: str) -> Path:
    return DEVICES_DIR / f"{dev_id}.json"


def _project_files_root(dev_id: str, pid: str) -> Path:
    return DEVICES_DIR / dev_id / "projects" / pid / "files"


def _valid_dev_id(s) -> bool:
    if not s or not isinstance(s, str):
        return False
    return bool(re.match(r"^[a-zA-Z0-9_-]{8,64}$", s))


def _clean_title(t) -> str:
    if t is None:
        return "Новый чат"
    t = str(t)
    out = []
    for ch in t:
        if ch in ("\n", "\r", "\t"):
            out.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Co", "Cs"):
            continue
        out.append(ch)
    t = "".join(out)
    t = " ".join(t.split())
    if len(t) > 60:
        t = t[:57].rstrip() + "..."
    return t or "Новый чат"


def _try_migrate_legacy(state: dict) -> None:
    global _LEGACY_MIGRATED
    if _LEGACY_MIGRATED:
        return
    _LEGACY_MIGRATED = True
    if LEGACY_MARKER.exists() or not DATA_FILE.exists():
        return
    try:
        existing = list(DEVICES_DIR.glob("*.json"))
    except Exception:
        existing = []
    if existing:
        return
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    chats = data.get("chats") or {}
    order = data.get("chat_order") or []
    if not chats:
        return
    for cid, chat in chats.items():
        if not isinstance(chat, dict):
            continue
        chat["title"] = _clean_title(chat.get("title"))
        state["chats"][cid] = chat
    for cid in order:
        if cid in state["chats"] and cid not in state["chat_order"]:
            state["chat_order"].append(cid)
    for cid in state["chats"]:
        if cid not in state["chat_order"]:
            state["chat_order"].append(cid)
    try:
        LEGACY_MARKER.write_text(
            f"Migrated at {datetime.now().isoformat()}\n", encoding="utf-8"
        )
    except Exception:
        pass


DEFAULT_LABELS = ["важно", "работа", "проект"]


def _blank_device_state() -> dict:
    return {
        "chats": {},
        "chat_order": [],
        "projects": {},
        "project_order": [],
        "labels": list(DEFAULT_LABELS),
        "loaded": True,
    }


def _load_device(dev_id: str) -> dict:
    with DEVICE_LOCK:
        cached = DEVICES.get(dev_id)
        if cached and cached.get("loaded"):
            return cached

    state = _blank_device_state()
    path = _device_file(dev_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            chats = data.get("chats") or {}
            order = data.get("chat_order") or []
            projects = data.get("projects") or {}
            porder = data.get("project_order") or []
            labels = data.get("labels")
            if isinstance(labels, list):
                clean = [str(x).strip() for x in labels if str(x).strip()]
                # базовые метки всегда доступны
                for base in DEFAULT_LABELS:
                    if base not in clean:
                        clean.append(base)
                state["labels"] = clean
            for cid, chat in chats.items():
                if not isinstance(chat, dict):
                    continue
                chat["title"] = _clean_title(chat.get("title"))
                state["chats"][cid] = chat
            for cid in order:
                if cid in state["chats"] and cid not in state["chat_order"]:
                    state["chat_order"].append(cid)
            for cid in state["chats"]:
                if cid not in state["chat_order"] and not state["chats"][cid].get("project_id"):
                    state["chat_order"].append(cid)
            for pid, proj in projects.items():
                if isinstance(proj, dict):
                    state["projects"][pid] = proj
            for pid in porder:
                if pid in state["projects"] and pid not in state["project_order"]:
                    state["project_order"].append(pid)
            for pid in state["projects"]:
                if pid not in state["project_order"]:
                    state["project_order"].append(pid)
        except Exception as e:
            print(f"[zimple] load {dev_id} failed:", e)
    else:
        _try_migrate_legacy(state)

    with DEVICE_LOCK:
        DEVICES[dev_id] = state
    return state


def _save_device(dev_id: str) -> None:
    with DEVICE_LOCK:
        state = DEVICES.get(dev_id)
    if not state:
        return
    payload = {
        "saved_at": datetime.now().isoformat(),
        "chat_order": state["chat_order"],
        "chats": state["chats"],
        "project_order": state.get("project_order", []),
        "projects": state.get("projects", {}),
        "labels": state.get("labels", list(DEFAULT_LABELS)),
    }
    try:
        path = _device_file(dev_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
    except Exception as e:
        print(f"[zimple] save {dev_id} failed:", e)


def _save_all_devices() -> None:
    with DEVICE_LOCK:
        ids = list(DEVICES.keys())
    for dev_id in ids:
        _save_device(dev_id)


def _save_data() -> None:
    if has_request_context():
        dev_id = getattr(g, "device_id", None)
        if dev_id:
            _save_device(dev_id)
            return
    _save_all_devices()


def _dev() -> dict:
    return _load_device(g.device_id)


LOCAL_ID_FILE = DEVICES_DIR / "_local_device.txt"


def _local_device_id() -> str:
    """Постоянный id для локального клиента (десктопное окно / localhost).

    Раньше id жил только в cookie; десктопная оболочка их не хранит, поэтому
    на каждый запуск (а иногда и на каждый запрос) создавался новый «девайс»
    и чаты «пропадали». Теперь id лежит в файле рядом с данными.
    """
    try:
        if LOCAL_ID_FILE.exists():
            val = LOCAL_ID_FILE.read_text(encoding="utf-8").strip()
            if _valid_dev_id(val):
                return val
    except Exception:
        pass

    new_id = uuid.uuid4().hex[:16]

    # Подхватываем самые свежие существующие данные, чтобы старые чаты
    # не потерялись после обновления.
    try:
        files = [f for f in DEVICES_DIR.glob("*.json") if f.is_file()]
        if files:
            newest = max(files, key=lambda f: f.stat().st_mtime)
            new_id = newest.stem
    except Exception:
        pass

    try:
        LOCAL_ID_FILE.write_text(new_id, encoding="utf-8")
    except Exception as e:
        print("[zimple] cannot persist local device id:", e)
    return new_id


def _is_local_request() -> bool:
    addr = (request.remote_addr or "").strip()
    return addr in ("127.0.0.1", "::1", "localhost", "")


@app.before_request
def _setup_device_ctx():
    # 1) заголовок от фронтенда (localStorage) — самый надёжный источник
    dev = request.headers.get("X-Zimple-Device")
    if _valid_dev_id(dev):
        g.device_id = dev
        g.new_device = False
        return

    # 2) cookie
    dev = request.cookies.get("zimple_device")
    if _valid_dev_id(dev):
        g.device_id = dev
        g.new_device = False
        return

    # 3) локальный клиент — постоянный id из файла
    if _is_local_request():
        g.device_id = _local_device_id()
        g.new_device = True
        return

    g.device_id = uuid.uuid4().hex[:16]
    g.new_device = True


@app.after_request
def _persist_device_cookie(resp):
    if getattr(g, "device_id", None):
        resp.set_cookie(
            "zimple_device", g.device_id,
            max_age=60 * 60 * 24 * 365 * 5, samesite="Lax",
        )
    return resp


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


atexit.register(_save_data)
atexit.register(browser_agent.close_browser)
atexit.register(computer_agent.shutdown)


def _classify(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    return "file"


def _clamp_delay(v) -> float:
    try:
        d = float(v)
    except (TypeError, ValueError):
        return 0.0
    if d < 0:
        return 0.0
    if d > 600:
        return 600.0
    return round(d, 2)


# ============================================================
#  DEBUG LOG
# ============================================================
def _dump_debug(payload: dict, fname: str = "last_llm.json"):
    try:
        path = DEBUG_DIR / fname
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as e:
        print("[zimple] debug dump failed:", e)


# ============================================================
#  LM
# ============================================================
def _call_lm(messages: list, max_tokens: int = 8192, temperature: float = 0.4) -> str:
    payload = json.dumps({
        "model": LM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        LM_STUDIO_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=LM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _extract_json(raw: str):
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    candidates = []
    for oc, cc in (("{", "}"), ("[", "]")):
        i = text.find(oc)
        j = text.rfind(cc)
        if i != -1 and j != -1 and j > i:
            candidates.append(text[i:j + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            pass
    decoder = json.JSONDecoder()
    items = []
    idx = 0
    n = len(text)
    while idx < n:
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(text[start:])
            items.append(obj)
            idx = start + end
        except Exception:
            idx = start + 1
    if items:
        return items
    return None


def _normalize_parsed(parsed):
    if parsed is None:
        return {"blocks": [], "content": "", "done": None}

    def _get_done(obj):
        d = obj.get("done")
        if isinstance(d, bool):
            return d
        if isinstance(d, str):
            if d.strip().lower() in ("true", "yes", "1", "да"):
                return True
            if d.strip().lower() in ("false", "no", "0", "нет"):
                return False
        return None

    if isinstance(parsed, dict):
        if "type" in parsed and "blocks" not in parsed and "content" not in parsed:
            return {"blocks": [parsed], "content": "", "done": None}
        return {
            "blocks": parsed.get("blocks") or [],
            "content": parsed.get("content") or "",
            "done": _get_done(parsed),
        }

    if isinstance(parsed, list):
        blocks = []
        content_parts = []
        done_seen = None
        for item in parsed:
            if not isinstance(item, dict):
                continue
            has_blocks = isinstance(item.get("blocks"), list)
            has_content = isinstance(item.get("content"), str)
            has_type = "type" in item
            if has_type and not has_blocks and not has_content:
                blocks.append(item)
            else:
                if has_blocks:
                    blocks.extend(item["blocks"])
                if has_content and item["content"].strip():
                    content_parts.append(item["content"].strip())
                d = _get_done(item)
                if d is not None:
                    done_seen = d
        return {
            "blocks": blocks,
            "content": "\n".join(content_parts).strip(),
            "done": done_seen,
        }

    return {"blocks": [], "content": "", "done": None}


def _raw_to_history(raw: str, blocks: list, content: str) -> str:
    clean_blocks = []
    for b in blocks:
        t = b.get("type")
        if t == "reasoning":
            continue
        if t == "status":
            clean_blocks.append({"type": "status", "text": b.get("text", "")})
        elif t == "browser":
            item = {"type": "browser", "action": b.get("action"),
                    "text": b.get("text", "")}
            for k in ("url", "selector", "value", "key"):
                if b.get(k):
                    item[k] = b[k]
            clean_blocks.append(item)
        elif t == "computer":
            item = {"type": "computer", "action": b.get("action"),
                    "text": b.get("text", "")}
            for k in ("target", "value", "keys"):
                if b.get(k):
                    item[k] = b[k]
            clean_blocks.append(item)
        elif t == "project":
            item = {"type": "project", "action": b.get("action"),
                    "path": b.get("path", ""), "text": b.get("text", "")}
            clean_blocks.append(item)
        elif t == "file":
            item = {"type": "file", "action": b.get("action"),
                    "path": b.get("path", ""), "text": b.get("text", "")}
            clean_blocks.append(item)

    if not clean_blocks and not content:
        return "(ПРЕДЫДУЩАЯ ПОПЫТКА: только reasoning, без действий.)"

    out = {"blocks": clean_blocks}
    if content:
        out["content"] = content
    try:
        return json.dumps(out, ensure_ascii=False)
    except Exception:
        return raw or ""


def _choose_system_prompt(project_ctx: str, agent_mode: str = None) -> str:
    if project_ctx:
        base = PROJECT_SYSTEM_PROMPT.replace("{files}", project_ctx)
    elif agent_mode == "computer":
        base = COMPUTER_SYSTEM_PROMPT
    else:
        base = CHAT_SYSTEM_PROMPT
    return base + _memory_prompt_section()


def _build_lm_messages(chat: dict, project_ctx: str = None,
                       extra_hint: str = None, agent_mode: str = None):
    sys_prompt = _choose_system_prompt(project_ctx, agent_mode)
    msgs = [{"role": "system", "content": sys_prompt}]

    for m in chat["messages"][-HISTORY_LIMIT:]:
        if m.get("hidden"):
            continue
        role = m.get("role")
        if role == "user":
            text = (m.get("content") or "").strip()
            atts = m.get("attachments") or []
            images = [a for a in atts if isinstance(a, dict)
                      and a.get("type") == "image" and a.get("url")]
            others = [a for a in atts if isinstance(a, dict) and a.get("type") != "image"]

            if others:
                names = ", ".join(a.get("filename", "") for a in others if a.get("filename"))
                if names:
                    suffix = f"[Прикреплено: {names}]"
                    text = (text + "\n" + suffix).strip() if text else suffix

            if images:
                parts = []
                if text:
                    parts.append({"type": "text", "text": text})
                else:
                    parts.append({"type": "text", "text": "Опиши изображение."})
                for a in images:
                    data_url = _read_image_as_data_url(a.get("url"))
                    if data_url:
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        })
                if len(parts) == 1:
                    msgs.append({"role": "user", "content": parts[0]["text"]})
                else:
                    msgs.append({"role": "user", "content": parts})
            else:
                if text:
                    msgs.append({"role": "user", "content": text})

        elif role == "assistant":
            steps = m.get("_agent_steps")
            if steps:
                for s in steps:
                    r = s.get("role")
                    c = s.get("content") or ""
                    if r in ("user", "assistant") and c:
                        msgs.append({"role": r, "content": c})
            else:
                hist = _raw_to_history("", m.get("blocks") or [],
                                       (m.get("content") or "").strip())
                if hist:
                    msgs.append({"role": "assistant", "content": hist})

    if extra_hint:
        msgs.append({"role": "user", "content": extra_hint})
    return msgs


def _read_image_as_data_url(url: str):
    if not url or not isinstance(url, str) or not url.startswith("/uploads/"):
        return None
    name = url[len("/uploads/"):]
    if "/" in name or "\\" in name or ".." in name:
        return None
    path = UPLOAD_DIR / name
    try:
        if not path.exists() or not path.is_file():
            return None
        if path.stat().st_size > 12 * 1024 * 1024:
            return None
    except Exception:
        return None
    ext = path.suffix.lower()
    if ext not in IMAGE_EXTS:
        return None
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml", ".avif": "image/avif", ".heic": "image/heic",
    }.get(ext, "image/png")
    try:
        data = path.read_bytes()
    except Exception:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _validate_generated(parsed: dict, allow_project: bool = False,
                        allow_computer: bool = True) -> tuple:
    blocks = []
    raw_blocks = parsed.get("blocks") or []
    if isinstance(raw_blocks, list):
        for b in raw_blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            delay = _clamp_delay(b.get("delay", 0))

            if t == "reasoning":
                text = (b.get("text") or "").strip()
                if text:
                    blocks.append({"type": "reasoning", "text": text, "delay": delay})

            elif t == "status":
                text = (b.get("text") or "").strip()
                if text:
                    ok = b.get("ok")
                    ok = True if ok is None else bool(ok)
                    blocks.append({"type": "status", "text": text, "ok": ok, "delay": delay})

            elif t == "browser" and not allow_project:
                action = (b.get("action") or "").strip().lower()
                if not action:
                    continue
                # Браузер идентифицирует элементы CSS-селектором, а не
                # target/x,y (это поля для computer-режима/рабочего стола).
                # Раньше здесь ошибочно проверялось наличие target/x,y —
                # из-за этого ЛЮБОЙ browser-клик с корректным selector'ом
                # всё равно отбрасывался, и агент зацикливался на retry.
                if action in ("click", "double_click", "right_click", "type"):
                    has_selector = bool((b.get("selector") or "").strip())
                    if not has_selector:
                        print(f"[zimple] reject browser-block without "
                              f"selector: {b}")
                        continue
                block = {
                    "type": "browser",
                    "text": (b.get("text") or action).strip(),
                    "action": action,
                    "delay": delay,
                }
                for k in ("url", "selector", "value", "key", "wait_until"):
                    if b.get(k):
                        block[k] = b[k]
                if "dy" in b:
                    try:
                        block["dy"] = int(b["dy"])
                    except Exception:
                        pass
                if "seconds" in b:
                    try:
                        block["seconds"] = float(b["seconds"])
                    except Exception:
                        pass
                blocks.append(block)

            elif t == "computer" and not allow_project and allow_computer:
                action = (b.get("action") or "").strip().lower()
                if not action:
                    continue
                block = {
                    "type": "computer",
                    "text": (b.get("text") or action).strip(),
                    "action": action,
                    "delay": delay,
                }
                if b.get("target"):
                    block["target"] = str(b["target"])
                if b.get("value") is not None:
                    block["value"] = str(b["value"])
                if b.get("keys"):
                    keys = b["keys"]
                    if isinstance(keys, str):
                        keys = [k.strip() for k in keys.split(",") if k.strip()]
                    block["keys"] = keys
                for k in ("x", "y", "x1", "y1", "x2", "y2", "dy"):
                    if k in b and b[k] is not None:
                        try:
                            block[k] = int(b[k])
                        except Exception:
                            pass
                if "seconds" in b:
                    try:
                        block["seconds"] = float(b["seconds"])
                    except Exception:
                        pass
                blocks.append(block)

            elif t == "project" and allow_project:
                action = (b.get("action") or "").strip().lower()
                if not action:
                    continue
                block = {
                    "type": "project",
                    "action": action,
                    "text": (b.get("text") or action).strip(),
                    "delay": delay,
                }
                for k in ("path", "content", "find", "replace"):
                    if b.get(k) is not None:
                        block[k] = b[k]
                if "count" in b:
                    try:
                        block["count"] = int(b["count"])
                    except Exception:
                        pass
                blocks.append(block)

            elif t == "file" and not allow_project:
                # Запись файла напрямую на диск пользователя (вне
                # песочницы проекта) — доступно только в обычном чате.
                action = (b.get("action") or "").strip().lower()
                if action not in ("write_file", "append_file"):
                    print(f"[zimple] reject file-block with bad action: {b}")
                    continue
                path = (b.get("path") or "").strip()
                if not path:
                    print(f"[zimple] reject file-block without path: {b}")
                    continue
                block = {
                    "type": "file",
                    "action": action,
                    "text": (b.get("text") or action).strip(),
                    "path": path,
                    "content": b.get("content") if b.get("content") is not None else "",
                    "delay": delay,
                }
                blocks.append(block)

            elif t in ("image", "video"):
                url = (b.get("url") or "").strip()
                if url.startswith("/uploads/"):
                    blocks.append({
                        "type": t, "url": url,
                        "filename": (b.get("filename") or "").strip(),
                        "delay": delay,
                    })

    content = (parsed.get("content") or "").strip()
    return blocks, content


def _parse_manual_block(b: dict):
    if not isinstance(b, dict):
        return None
    t = b.get("type")
    delay = _clamp_delay(b.get("delay", 0))

    if t == "reasoning":
        text = (b.get("text") or "").strip()
        return {"type": "reasoning", "text": text, "delay": delay} if text else None

    if t == "status":
        text = (b.get("text") or "").strip()
        if not text:
            return None
        ok = b.get("ok")
        ok = True if ok is None else bool(ok)
        return {"type": "status", "text": text, "ok": ok, "delay": delay}

    if t == "browser":
        action = (b.get("action") or "").strip().lower()
        if not action:
            return None
        block = {
            "type": "browser",
            "text": (b.get("text") or action).strip(),
            "action": action,
            "delay": delay,
        }
        if b.get("screenshot"):
            block["screenshot"] = b["screenshot"]
        for k in ("url", "selector", "value", "key", "wait_until"):
            if b.get(k):
                block[k] = b[k]
        if "dy" in b:
            try:
                block["dy"] = int(b["dy"])
            except Exception:
                pass
        if "seconds" in b:
            try:
                block["seconds"] = float(b["seconds"])
            except Exception:
                pass
        return block

    if t == "computer":
        action = (b.get("action") or "").strip().lower()
        if not action:
            return None
        block = {
            "type": "computer",
            "text": (b.get("text") or action).strip(),
            "action": action,
            "delay": delay,
        }
        if b.get("screenshot"):
            block["screenshot"] = b["screenshot"]
        if b.get("target"):
            block["target"] = str(b["target"])
        if b.get("value") is not None:
            block["value"] = str(b["value"])
        if b.get("keys"):
            keys = b["keys"]
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split(",") if k.strip()]
            block["keys"] = keys
        for k in ("x", "y", "x1", "y1", "x2", "y2", "dy"):
            if k in b and b[k] is not None:
                try:
                    block[k] = int(b[k])
                except Exception:
                    pass
        if "seconds" in b:
            try:
                block["seconds"] = float(b["seconds"])
            except Exception:
                pass
        return block

    if t == "project":
        action = (b.get("action") or "").strip().lower()
        if not action:
            return None
        block = {
            "type": "project",
            "action": action,
            "text": (b.get("text") or action).strip(),
            "delay": delay,
        }
        for k in ("path", "content", "find", "replace"):
            if b.get(k) is not None:
                block[k] = b[k]
        if "count" in b:
            try:
                block["count"] = int(b["count"])
            except Exception:
                pass
        if b.get("result"):
            block["result"] = b["result"]
        return block

    if t == "file":
        action = (b.get("action") or "").strip().lower()
        if action not in ("write_file", "append_file"):
            return None
        path = (b.get("path") or "").strip()
        if not path:
            return None
        block = {
            "type": "file",
            "action": action,
            "text": (b.get("text") or action).strip(),
            "path": path,
            "content": b.get("content") if b.get("content") is not None else "",
            "delay": delay,
        }
        if b.get("result"):
            block["result"] = b["result"]
        return block

    if t == "memory":
        action = (b.get("action") or "append").strip().lower()
        if action not in ("append", "replace", "clear"):
            return None
        text = (b.get("text") or "").strip()
        if action != "clear" and not text:
            return None
        block = {
            "type": "memory",
            "action": action,
            "text": text,
            "delay": delay,
        }
        if b.get("result"):
            block["result"] = b["result"]
        return block

    if t in ("image", "video"):
        url = (b.get("url") or "").strip()
        if url.startswith("/uploads/"):
            return {
                "type": t, "url": url,
                "filename": (b.get("filename") or "").strip(),
                "delay": delay,
            }
    return None


def _apply_memory_blocks(blocks: list) -> None:
    """Memory-блоки исполняются сразу на сервере — фронтенду нечего делать."""
    for b in blocks or []:
        if not isinstance(b, dict) or b.get("type") != "memory":
            continue
        action = b.get("action") or "append"
        try:
            if action == "append":
                append_memory(b.get("text") or "")
            elif action == "replace":
                write_memory(b.get("text") or "")
            elif action == "clear":
                write_memory("")
            b["result"] = {"ok": True}
            if not b.get("text"):
                b["text"] = "Обновляю память"
            print("[zimple] memory block applied:", action)
        except Exception as e:
            b["result"] = {"ok": False, "error": str(e)[:200]}


def _project_context_string(dev: dict, chat: dict):
    pid = chat.get("project_id")
    if not pid:
        return None
    proj = (dev.get("projects") or {}).get(pid)
    if not proj:
        return None
    root = _project_files_root(g.device_id, pid)
    root.mkdir(parents=True, exist_ok=True)
    files = projects_agent.list_files(root)
    if files:
        flist = "\n".join(f"- {f['path']} ({f['size']} Б)" for f in files)
    else:
        flist = "(пусто — файлов пока нет, создай через write_file)"
    return f"Проект: {proj.get('name', '')}\n\nФайлы:\n{flist}"


# ---------- определение «врёт про действия» ----------
_ACTION_CLAIM_RE = re.compile(
    r"\b("
    r"открыл|открыла|открыто|открываю|"
    r"закрыл|закрыла|закрыто|закрываю|"
    r"нажал|нажала|нажимаю|"
    r"ввёл|ввел|ввела|ввожу|"
    r"напечатал|напечатала|печатаю|"
    r"перешёл|перешел|перехожу|"
    r"запустил|запустила|запускаю|"
    r"кликнул|кликнула|кликаю|"
    r"переключился|переключилась|"
    r"отправил|отправила|отправляю|"
    r"жду|ожидаю|"
    r"проверяю|проверил|"
    r"ищу|нашёл|нашел"
    r")\b",
    re.IGNORECASE,
)


def _detect_computer_intent(user_text: str) -> bool:
    """Резервная эвристика на случай, если LM недоступна для классификации
    режима (см. _classify_agent_mode). Сама по себе она слишком грубая —
    например «открой сайт X» тоже содержит «открой» — поэтому в обычной
    работе используется только как fallback, а не основной механизм."""
    if not user_text:
        return False
    t = user_text.lower()
    keywords = [
        "рабочем столе", "рабочий стол", "ярлык",
        "запусти ", "закрой ",
        "кликни", "нажми", "щёлкни", "щелкни",
        "напечатай", "введи в поле", "набери ",
        "на пк", "на компьютере", "на винде", "на windows",
        "проводник", "блокнот", "калькулятор",
        "двойным кликом", "двойной клик",
    ]
    for kw in keywords:
        if kw in t:
            return True
    return False


def _classify_agent_mode(user_text: str) -> str:
    """Спрашивает у LLM, какой режим нужен для последнего сообщения
    пользователя: "browser", "computer" или "chat". Заменяет прежнюю
    жёсткую эвристику по ключевым словам, которая путала «открой сайт»
    (нужен браузер) с «открой ярлык» (нужен рабочий стол) — она вызывала
    ложное включение управления ПК, когда пользователь на самом деле
    просил открыть браузер."""
    text = (user_text or "").strip()
    if not text:
        return "chat"
    try:
        raw = _call_lm(
            [
                {"role": "system", "content": MODE_CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": text[:2000]},
            ],
            max_tokens=8,
            temperature=0.0,
        )
    except Exception as e:
        print("[zimple] mode classify failed, falling back to heuristic:", e)
        return "computer" if _detect_computer_intent(text) else "chat"

    low = (raw or "").strip().lower()
    for mode in ("browser", "computer", "chat"):
        if mode in low:
            return mode
    print(f"[zimple] mode classify returned unexpected {raw!r}, "
          f"falling back to heuristic")
    return "computer" if _detect_computer_intent(text) else "chat"


import hashlib

def _screenshot_hash(path_or_url):
    """MD5 файла скриншота. Используется для детекта «действие ничего не изменило»."""
    if not path_or_url:
        return None
    try:
        name = path_or_url.rsplit("/", 1)[-1]
        p = UPLOAD_DIR / name
        if not p.exists() or not p.is_file():
            return None
        return hashlib.md5(p.read_bytes()).hexdigest()
    except Exception:
        return None

def _lm_and_parse(chat: dict, project_ctx: str = None,
                  allow_project: bool = False, extra_hint: str = None,
                  agent_mode: str = None):
    messages = _build_lm_messages(chat, project_ctx, extra_hint, agent_mode)
    try:
        raw = _call_lm(messages)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        print("[zimple] LM HTTP error:", e.code, detail)
        _dump_debug({
            "ts": datetime.now().isoformat(),
            "model": LM_MODEL,
            "agent_mode": agent_mode,
            "messages": messages,
            "http_error": {"code": e.code, "detail": detail[:1500]},
        })
        return None, None, (jsonify({"error": "lm_http_error", "code": e.code,
                                     "detail": detail[:500]}), 502)
    except urllib.error.URLError as e:
        print("[zimple] LM connection error:", e)
        return None, None, (jsonify({
            "error": "lm_unreachable",
            "detail": "Не удалось подключиться к LM Studio на localhost:1234.",
        }), 502)
    except Exception as e:
        print("[zimple] LM error:", e)
        return None, None, (jsonify({"error": "lm_error", "detail": str(e)}), 502)

    parsed = _normalize_parsed(_extract_json(raw))
    blocks, content = _validate_generated(
        parsed, allow_project=allow_project, allow_computer=not allow_project,
    )

    _dump_debug({
        "ts": datetime.now().isoformat(),
        "model": LM_MODEL,
        "agent_mode": agent_mode,
        "extra_hint_present": bool(extra_hint),
        "messages": messages,
        "raw_response": raw,
        "parsed_blocks": blocks,
        "parsed_content": content,
    })

    print(f"[zimple] LM -> blocks={len(blocks)} content_len={len(content)} mode={agent_mode}")
    for b in blocks:
        print(f"     block: {b.get('type')} action={b.get('action')} "
              f"target={b.get('target')} text={b.get('text','')[:60]!r}")
    if content:
        print(f"     content: {content[:120]!r}")

    if not content and not blocks:
        looks_like_json = raw and ('"type"' in raw or '"blocks"' in raw)
        if looks_like_json:
            return None, None, (jsonify({
                "error": "bad_json",
                "detail": "Модель вернула JSON, который не удалось разобрать.",
                "raw": (raw or "")[:400],
            }), 502)
        content = (raw or "").strip()
        if not content:
            return None, None, (jsonify({"error": "empty_generation",
                                         "raw": (raw or "")[:400]}), 502)

    return raw, {"blocks": blocks, "content": content}, None


def _is_agent_continuation(chat: dict) -> bool:
    """Возвращает True, если последнее assistant-сообщение содержало
    browser/computer action-блок — значит мы внутри агентского цикла."""
    for m in reversed(chat.get("messages", [])):
        if m.get("role") != "assistant":
            continue
        blocks = m.get("blocks") or []
        for b in blocks:
            if b.get("type") in ("browser", "computer"):
                return True
        return False
    return False

def _lm_and_parse_with_retry(chat: dict, project_ctx: str = None,
                             allow_project: bool = False,
                             agent_mode: str = None,
                             initial_hint: str = None,
                             require_done: bool = False):
    """require_done=True — мы внутри агентского цикла. Тогда ответ БЕЗ
    action-блока и БЕЗ done=true считается невалидным."""
    last_err = None
    for attempt in range(MAX_LLM_RETRIES + 1):
        extra = None
        if attempt == 0 and initial_hint:
            extra = initial_hint
        elif attempt > 0:
            if require_done and agent_mode == "computer":
                extra = (
                    "СТОП. Твой предыдущий ответ не содержал action-блока "
                    "и не был помечен как завершённый.\n"
                    "Сейчас выбери одно из двух:\n"
                    '  1) Если задача ещё не выполнена — верни РОВНО ОДИН '
                    'computer-блок и "done": false. Пример:\n'
                    '     {"blocks":[{"type":"computer","action":"click",'
                    '"target":"el_5","text":"..."}], "content":"", "done":false}\n'
                    '  2) Если задача реально выполнена — верни '
                    '{"blocks":[], "content":"итог", "done":true}.\n'
                    "Никаких слов-действий («фильтрую», «открываю», «ищу») "
                    "в content — они не выполняют действий."
                )
            elif require_done:
                extra = (
                    "СТОП. Твой предыдущий ответ не содержал action-блока "
                    "и не был помечен как завершённый.\n"
                    "Сейчас либо верни РОВНО ОДИН action-блок "
                    'с "done": false, либо заверши задачу с "done": true.'
                )
            elif allow_project:
                extra = (
                    "СТОП. Нужен ОДИН project-блок или "
                    '{"blocks":[], "content":"Готово", "done":true}.'
                )
            else:
                extra = (
                    "СТОП. Предыдущий ответ не содержал action-блока. "
                    "Отправь РОВНО ОДИН блок (browser/computer) или, если "
                    'задача завершена, верни {"blocks":[], "content":"Готово", '
                    '"done":true}.'
                )
            print(f"[zimple] retry {attempt} (agent_mode={agent_mode}, "
                  f"require_done={require_done})")

        raw, parsed, err = _lm_and_parse(
            chat, project_ctx, allow_project, extra, agent_mode,
        )
        if err:
            last_err = err
            break

        blocks = parsed["blocks"]
        content = parsed["content"]
        done_flag = parsed.get("done")
        has_action = any(b.get("type") in ("browser", "project", "computer", "file")
                         for b in blocks)

        # 1) Есть action-блок — принимаем, что бы ни было в content.
        if has_action:
            # Но если done=True и есть action — это противоречие,
            # оставляем done=False, чтобы цикл продолжился.
            if done_flag is True:
                print("[zimple] warning: done=true with action blocks, "
                      "treating as done=false")
                parsed["done"] = False
            return raw, parsed, None

        # 2) Action-блоков нет. Решаем судьбу по require_done.
        if require_done:
            if done_flag is True:
                return raw, parsed, None
            # done не выставлен или false, а действий нет — ретрай.
            print(f"[zimple] retry: no action and done={done_flag}, "
                  f"content={content[:80]!r}")
            continue

        # 3) Обычный чат/проект. Если есть content — принимаем.
        if content:
            return raw, parsed, None

    if last_err:
        return None, None, last_err
    return None, None, (jsonify({
        "error": "empty_generation",
        "detail": "Модель не смогла сформировать валидный ответ после "
                  "нескольких попыток. Возможно, стоит взять модель "
                  "крупнее (Qwen2.5-Coder 32B, Gemma 4 26B).",
    }), 502)


def _infer_agent_mode(target_msg: dict) -> str:
    blocks = target_msg.get("blocks") or []
    for b in reversed(blocks):
        t = b.get("type")
        if t == "computer":
            return "computer"
        if t in ("browser", "project", "file"):
            return None
    return None


# ============================================================
#  ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/uploads/<path:name>")
def serve_upload(name):
    return send_from_directory(UPLOAD_DIR, name)


@app.route("/api/debug/last", methods=["GET"])
def debug_last():
    path = DEBUG_DIR / "last_llm.json"
    if not path.exists():
        return jsonify({"error": "no debug log yet"}), 404
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models", methods=["GET"])
def list_models():
    """Список моделей, реально загруженных в LM Studio (эндпоинт,
    совместимый с OpenAI: GET /v1/models). Раньше в UI был захардкожен
    список несуществующих "Zimple-v2..." моделей."""
    models_url = LM_STUDIO_URL.replace("/chat/completions", "/models")
    try:
        req = urllib.request.Request(models_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        if ids:
            return jsonify(ids)
    except Exception as e:
        print("[zimple] /api/models: LM Studio unreachable:", e)
    # LM Studio недоступна или ничего не загружено — отдаём хотя бы
    # настроенную по умолчанию модель, чтобы UI не остался пустым.
    return jsonify([LM_MODEL])


@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    ext = Path(f.filename).suffix.lower()
    stored = uuid.uuid4().hex + ext
    dest = UPLOAD_DIR / stored
    try:
        f.save(dest)
    except Exception as e:
        print("[zimple] upload failed:", e)
        return jsonify({"error": "save failed"}), 500
    return jsonify({
        "url": f"/uploads/{stored}",
        "kind": _classify(f.filename),
        "filename": f.filename,
    })


@app.route("/api/chats", methods=["GET"])
def list_chats():
    dev = _dev()
    out = []
    for cid in dev["chat_order"]:
        c = dev["chats"].get(cid)
        if not c:
            continue
        if c.get("project_id"):
            continue
        out.append({
            "id": c.get("id"),
            "title": _clean_title(c.get("title")),
            "created": c.get("created"),
            "pinned": bool(c.get("pinned")),
            "label": c.get("label"),
        })
    # закреплённые чаты — наверх, порядок внутри групп сохраняем
    out.sort(key=lambda c: 0 if c.get("pinned") else 1)
    return jsonify(out)


@app.route("/api/chats", methods=["POST"])
def create_chat():
    data = request.get_json(silent=True) or {}
    dev = _dev()
    cid = uuid.uuid4().hex[:8]
    chat = {
        "id": cid,
        "title": _clean_title(data.get("title", "Новый чат")),
        "created": datetime.now().isoformat(),
        "messages": [],
    }
    dev["chats"][cid] = chat
    dev["chat_order"].insert(0, cid)
    _save_data()
    return jsonify(chat)


@app.route("/api/chats/<cid>", methods=["GET"])
def get_chat(cid):
    dev = _dev()
    if cid not in dev["chats"]:
        return jsonify({"error": "not found"}), 404
    c = dev["chats"][cid]
    c["title"] = _clean_title(c.get("title"))
    return jsonify(c)


@app.route("/api/chats/<cid>", methods=["PATCH"])
def patch_chat(cid):
    """Закрепление чата, метка и переименование."""
    dev = _dev()
    chat = dev["chats"].get(cid)
    if not chat:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}

    if "pinned" in data:
        chat["pinned"] = bool(data.get("pinned"))
    if "label" in data:
        label = data.get("label")
        if label in (None, "", False):
            chat["label"] = None
        else:
            label = str(label).strip()[:24]
            chat["label"] = label or None
            if label and label not in dev.setdefault("labels", list(DEFAULT_LABELS)):
                dev["labels"].append(label)
    if "title" in data:
        chat["title"] = _clean_title(data.get("title"))

    _save_data()
    return jsonify({
        "id": chat.get("id"),
        "title": _clean_title(chat.get("title")),
        "pinned": bool(chat.get("pinned")),
        "label": chat.get("label"),
    })


@app.route("/api/memory", methods=["GET"])
def memory_get():
    return jsonify({"text": read_memory(), "path": str(MEMORY_FILE)})


@app.route("/api/memory", methods=["POST"])
def memory_post():
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "replace").strip().lower()
    text = data.get("text") or ""
    if action == "append":
        result = append_memory(text)
    elif action == "clear":
        result = write_memory("")
    else:
        result = write_memory(text)
    return jsonify({"ok": True, "text": result, "path": str(MEMORY_FILE)})


@app.route("/api/device", methods=["GET"])
def get_device():
    """Фронтенд запоминает этот id в localStorage и шлёт в X-Zimple-Device."""
    return jsonify({"device_id": g.device_id})


@app.route("/api/labels", methods=["GET"])
def get_labels():
    dev = _dev()
    return jsonify(dev.setdefault("labels", list(DEFAULT_LABELS)))


@app.route("/api/labels", methods=["POST"])
def add_label():
    dev = _dev()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:24]
    if not name:
        return jsonify({"error": "empty"}), 400
    labels = dev.setdefault("labels", list(DEFAULT_LABELS))
    if name not in labels:
        labels.append(name)
        _save_data()
    return jsonify(labels)


@app.route("/api/labels/<path:name>", methods=["DELETE"])
def remove_label(name):
    dev = _dev()
    labels = dev.setdefault("labels", list(DEFAULT_LABELS))
    if name in labels:
        labels.remove(name)
    for c in dev["chats"].values():
        if c.get("label") == name:
            c["label"] = None
    _save_data()
    return jsonify(labels)


@app.route("/api/chats/<cid>", methods=["DELETE"])
def delete_chat(cid):
    dev = _dev()
    dev["chats"].pop(cid, None)
    if cid in dev["chat_order"]:
        dev["chat_order"].remove(cid)
    _save_data()
    return jsonify({"ok": True})


@app.route("/api/chats/<cid>/messages/<mid>", methods=["PATCH"])
def update_message(cid, mid):
    dev = _dev()
    if cid not in dev["chats"]:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    for m in dev["chats"][cid]["messages"]:
        if m.get("id") == mid:
            if "liked" in data:
                liked = data["liked"]
                if liked not in ("up", "down", None):
                    return jsonify({"error": "invalid liked"}), 400
                m["liked"] = liked
            _save_data()
            return jsonify(m)
    return jsonify({"error": "not found"}), 404


@app.route("/api/chats/<cid>/messages", methods=["POST"])
def add_message(cid):
    dev = _dev()
    if cid not in dev["chats"]:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    role = data.get("role", "user")
    if role not in ("user", "assistant"):
        role = "user"

    blocks = []
    if role == "assistant":
        raw = data.get("blocks") or []
        if isinstance(raw, list):
            for b in raw:
                blk = _parse_manual_block(b)
                if blk:
                    blocks.append(blk)

    attachments = []
    if role == "user":
        raw = data.get("attachments") or []
        if isinstance(raw, list):
            for a in raw:
                if not isinstance(a, dict):
                    continue
                url = (a.get("url") or "").strip()
                if not url.startswith("/uploads/"):
                    continue
                t = a.get("type")
                if t not in ("image", "video"):
                    t = _classify(a.get("filename") or url)
                    if t == "file":
                        continue
                attachments.append({
                    "type": t, "url": url,
                    "filename": (a.get("filename") or "").strip(),
                })

    if role == "user" and not (content or attachments):
        return jsonify({"error": "empty"}), 400
    if role == "assistant" and not (content or blocks):
        return jsonify({"error": "empty"}), 400

    msg = {
        "id": uuid.uuid4().hex[:8],
        "role": role,
        "content": content,
        "model": data.get("model") if role == "assistant" else None,
        "blocks": blocks,
        "attachments": attachments,
        "liked": None,
        "ts": datetime.now().isoformat(),
    }
    chat = dev["chats"][cid]
    chat["messages"].append(msg)

    if chat.get("title") == "Новый чат" and msg["role"] == "user":
        raw_title = content[:60] if content else (
            attachments[0]["filename"][:60] if attachments else "Новый чат"
        )
        chat["title"] = _clean_title(raw_title)

    _save_data()
    return jsonify(msg)


@app.route("/api/chats/<cid>/generate", methods=["POST"])
def generate_response(cid):
    dev = _dev()
    if cid not in dev["chats"]:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    display_model = (data.get("model") or "").strip() or "Zimple"
    chat = dev["chats"][cid]
    project_ctx = _project_context_string(dev, chat)
    allow_project = bool(chat.get("project_id"))

    agent_mode = None
    initial_hint = None
    if not allow_project:
        last_user = None
        for m in reversed(chat["messages"]):
            if m.get("role") == "user":
                last_user = m
                break
        if last_user:
            classified = _classify_agent_mode(last_user.get("content") or "")
            print(f"[zimple] classified mode = {classified!r}")
            if classified == "computer":
                agent_mode = "computer"
            elif classified == "browser":
                # CHAT_SYSTEM_PROMPT уже объясняет модели browser-блоки,
                # отдельного системного промпта не нужно — просто явно
                # подталкиваем её начать именно с браузера.
                initial_hint = (
                    "[Определён режим: РАБОТА С БРАУЗЕРОМ] Для этой задачи "
                    "нужен реальный браузер. Начни с ОДНОГО browser-блока "
                    "(обычно goto)."
                )

    if agent_mode == "computer":
        # авто-включение управления ПК (пользователь уже явно попросил)
        if not computer_agent.get_enabled():
            computer_agent.set_enabled(True)
            print("[zimple] auto-enabled computer control")

        # снимаем начальное состояние и подсовываем модели сразу
        st = computer_agent.get_state()
        active = st.get("active_window") or {}
        elements = st.get("elements") or []
        el_lines = []
        for el in elements[:120]:
            el_lines.append(
                f'  - [{el["id"]}] {el["type"]} "{el["name"]}" '
                f'@ ({el["cx"]},{el["cy"]})'
            )
        el_block = "\n".join(el_lines) if el_lines else "  (нет элементов)"
        initial_hint = (
            "[НАЧАЛЬНОЕ СОСТОЯНИЕ РАБОЧЕГО СТОЛА — используй его сразу, "
            "не проси скриншот отдельно]\n"
            f"Активное окно: {active.get('title', '—')} "
            f"({active.get('class', '')})\n\n"
            f"Интерактивные элементы:\n{el_block}\n\n"
            "Начинай выполнять задачу. Отправь РОВНО ОДИН computer-блок."
        )
        print(f"[zimple] initial state: {len(elements)} elements, "
              f"window={active.get('title','—')!r}")

    raw, parsed, err = _lm_and_parse_with_retry(
        chat, project_ctx, allow_project,
        agent_mode=agent_mode, initial_hint=initial_hint,
        require_done=False,   # первый ответ — не заставляем завершаться
    )
    if err:
        return err

    blocks = parsed["blocks"]
    content = parsed["content"]
    _apply_memory_blocks(blocks)
    done_flag = parsed.get("done")
    print(f"[zimple] first response: blocks={len(blocks)} "
          f"done={done_flag} content={content[:60]!r}")
    history_text = _raw_to_history(raw or "", blocks, content)

    msg = {
        "id": uuid.uuid4().hex[:8],
        "role": "assistant",
        "content": content,
        "model": display_model,
        "blocks": blocks,
        "attachments": [],
        "liked": None,
        "_agent_steps": [{"role": "assistant", "content": history_text}],
        "_agent_step_count": 0,
        "ts": datetime.now().isoformat(),
    }
    chat["messages"].append(msg)
    _save_data()
    return jsonify(msg)


@app.route("/api/chats/<cid>/generate/continue", methods=["POST"])
def generate_continue(cid):
    dev = _dev()
    if cid not in dev["chats"]:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True) or {}
    mid = data.get("mid")
    if not mid:
        return jsonify({"error": "mid required"}), 400

    chat = dev["chats"][cid]

    target = None
    for m in chat["messages"]:
        if m.get("id") == mid:
            target = m
            break
    if not target:
        return jsonify({"error": "message not found"}), 404

    step_count = int(target.get("_agent_step_count") or 0)
    if step_count >= MAX_AGENT_STEPS:
        return jsonify({"done": True, "blocks": [], "content": ""})

    blocks = target.get("blocks") or []
    has_browser = any(b.get("type") == "browser" for b in blocks)
    has_project = any(b.get("type") == "project" for b in blocks)
    has_computer = any(b.get("type") == "computer" for b in blocks)

    hints = []

    if has_browser:
        st = browser_agent.get_state(max_text=1500)
        url = st.get("url") or "—"
        title = st.get("title") or "—"
        text = st.get("text") or ""
        elements = st.get("interactive") or []
        el_lines = []
        for el in elements[:80]:
            tag = el.get("tag") or el.get("type") or "?"
            eid = el.get("id"); name = el.get("name")
            cls = el.get("cls"); etext = el.get("text"); frame = el.get("frame")
            bits = [tag]
            if eid:
                bits.append("#" + str(eid))
            elif name:
                bits.append("[name=" + str(name) + "]")
            elif cls:
                bits.append("." + str(cls))
            desc = " ".join(bits)
            if etext:
                desc += f' text="{etext}"'
            if frame and frame != "main":
                desc += f" [{frame}]"
            el_lines.append("  - " + desc)
        el_block = "\n".join(el_lines) if el_lines else "  (нет элементов)"
        hint_text = (
            "[Состояние браузера]\n"
            f"URL: {url}\nЗаголовок: {title}\n"
            f"Видимый текст:\n{text}\n\n"
            f"Интерактивные элементы:\n{el_block}"
        )

        dls = browser_agent.get_downloads(clear=True)
        if dls:
            lines = []
            for d in dls[-5:]:
                if d.get("ok") and d.get("path"):
                    lines.append(f"  - {d.get('filename', '')} → {d['path']}")
                else:
                    lines.append(f"  - ошибка скачивания: {d.get('error', '')}")
            hint_text += (
                "\n\n[Скачанные файлы]\n" + "\n".join(lines) +
                "\nЕсли файл скачан — сообщи пользователю полный путь "
                "к нему в content."
            )
        hints.append(hint_text)

    if has_computer:
        st = computer_agent.get_state()
        active = st.get("active_window") or {}
        elements = st.get("elements") or []
        el_lines = []
        for el in elements[:120]:
            eid = el.get("id", "")
            etype = el.get("type", "")
            name = el.get("name", "")
            cx = el.get("cx"); cy = el.get("cy")
            el_lines.append(f'  - [{eid}] {etype} "{name}" @ ({cx},{cy})')
        el_block = "\n".join(el_lines) if el_lines else "  (нет элементов)"
        hints.append(
            "[Состояние рабочего стола]\n"
            f"Активное окно: {active.get('title', '—')} "
            f"({active.get('class', '')})\n\n"
            f"Интерактивные элементы:\n{el_block}"
        )
        print(f"[zimple] computer state sent to LM: "
              f"{len(elements)} elements, window={active.get('title','—')!r}")

    if has_project:
        last_proj = None
        for b in reversed(blocks):
            if b.get("type") == "project":
                last_proj = b
                break
        if last_proj is not None:
            result = last_proj.get("result") or {"ok": False, "error": "no result"}
            try:
                result_str = json.dumps(result, ensure_ascii=False, indent=2)
            except Exception:
                result_str = str(result)
            if len(result_str) > 5000:
                result_str = result_str[:5000] + "\n... [усечено]"
            hints.append("[Результат операции с файлом]\n" + result_str)

    hints.append(
        "Продолжай. Если задача не завершена — верни ОДИН новый блок "
        "(computer/browser/project). Если задача выполнена — верни "
        '{"blocks": [], "content": "Готово"} без глаголов действия.'
    )
    state_hint = "\n\n".join(hints)

    steps = target.setdefault("_agent_steps", [])
    steps.append({"role": "user", "content": state_hint})

    project_ctx = _project_context_string(dev, chat)
    allow_project = bool(chat.get("project_id"))

    agent_mode = None if allow_project else _infer_agent_mode(target)
    if agent_mode:
        print(f"[zimple] agent_mode = {agent_mode}")

    raw, parsed, err = _lm_and_parse_with_retry(
        chat, project_ctx, allow_project,
        agent_mode=agent_mode,
        require_done=True,   # ← ВОТ ЭТО ГЛАВНОЕ
    )

    if err:
        steps.pop()
        _save_data()
        return err

    target["_agent_step_count"] = step_count + 1

    new_blocks = parsed["blocks"]
    new_content = parsed["content"]
    _apply_memory_blocks(new_blocks)

    target_blocks = target.setdefault("blocks", [])
    target_blocks.extend(new_blocks)
    if new_content:
        target["content"] = new_content

    history_text = _raw_to_history(raw or "", new_blocks, new_content)
    steps.append({"role": "assistant", "content": history_text})
    _save_data()

    has_next = any(b.get("type") in ("browser", "project", "computer", "file")
                   for b in new_blocks)

    return jsonify({
        "blocks": new_blocks,
        "content": new_content,
        "done": not has_next,
    })


# ---------- BROWSER ----------
@app.route("/api/browser/status", methods=["GET"])
def browser_status():
    return jsonify({"available": browser_agent.is_available()})


@app.route("/api/browser/exec", methods=["POST"])
def browser_exec():
    data = request.get_json(silent=True) or {}
    action = data.get("action") or {}
    result = browser_agent.execute(action)

    cid = data.get("cid"); mid = data.get("mid"); bidx = data.get("bidx")
    if result.get("screenshot") and cid and mid is not None and bidx is not None:
        dev = _dev()
        chat = dev["chats"].get(cid)
        if chat:
            for m in chat["messages"]:
                if m.get("id") == mid:
                    blocks = m.get("blocks") or []
                    try:
                        i = int(bidx)
                        if 0 <= i < len(blocks):
                            blocks[i]["screenshot"] = result["screenshot"]
                            if result.get("downloads"):
                                blocks[i]["downloads"] = result["downloads"]
                            _save_data()
                    except Exception:
                        pass
                    break
    return jsonify(result)


@app.route("/api/browser/close", methods=["POST"])
def browser_close():
    browser_agent.close_browser()
    return jsonify({"ok": True})


# ---------- COMPUTER ----------
@app.route("/api/computer/status", methods=["GET"])
def computer_status():
    return jsonify({
        "available": computer_agent.is_available(),
        "a11y": computer_agent.has_a11y(),
        "enabled": computer_agent.get_enabled(),
    })


@app.route("/api/computer/toggle", methods=["POST"])
def computer_toggle():
    data = request.get_json(silent=True) or {}
    enabled = computer_agent.set_enabled(bool(data.get("enabled")))
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/computer/state", methods=["GET"])
def computer_state():
    return jsonify(computer_agent.get_state())


@app.route("/api/computer/exec", methods=["POST"])
def computer_exec():
    data = request.get_json(silent=True) or {}
    action = data.get("action") or {}
    print(f"[zimple] /api/computer/exec action={action}")
    result = computer_agent.execute(action)
    print(f"[zimple] computer result ok={result.get('ok')} "
          f"err={result.get('error')} shot={bool(result.get('screenshot'))}")

    cid = data.get("cid"); mid = data.get("mid"); bidx = data.get("bidx")
    if result.get("screenshot") and cid and mid is not None and bidx is not None:
        dev = _dev()
        chat = dev["chats"].get(cid)
        if chat:
            for m in chat["messages"]:
                if m.get("id") == mid:
                    blocks = m.get("blocks") or []
                    try:
                        i = int(bidx)
                        if 0 <= i < len(blocks):
                            blocks[i]["screenshot"] = result["screenshot"]
                            if result.get("elements"):
                                blocks[i]["elements"] = result["elements"]
                            _save_data()
                    except Exception:
                        pass
                    break
    return jsonify(result)


# ---------- LOCAL FILES (запись вне песочницы проекта) ----------
@app.route("/api/local/exec", methods=["POST"])
def local_exec():
    data = request.get_json(silent=True) or {}
    action = data.get("action") or {}
    kind = (action.get("action") or "").strip().lower()
    path = action.get("path")
    content = action.get("content") or ""

    if kind not in ("write_file", "append_file"):
        result = {"ok": False, "error": f"unknown action: {kind}"}
    else:
        saved_path, err = local_files_agent.write_text(
            path, content, append=(kind == "append_file"),
        )
        if err:
            result = {"ok": False, "action": kind, "error": err}
        else:
            result = {"ok": True, "action": kind, "path": saved_path}

    cid = data.get("cid"); mid = data.get("mid"); bidx = data.get("bidx")
    if cid and mid is not None and bidx is not None:
        dev = _dev()
        chat = dev["chats"].get(cid)
        if chat:
            for m in chat["messages"]:
                if m.get("id") == mid:
                    blocks = m.get("blocks") or []
                    try:
                        i = int(bidx)
                        if 0 <= i < len(blocks):
                            blocks[i]["result"] = result
                            _save_data()
                    except Exception:
                        pass
                    break
    return jsonify(result)


# ---------- PROJECTS ----------
@app.route("/api/projects", methods=["GET"])
def list_projects():
    dev = _dev()
    out = []
    for pid in dev.get("project_order", []):
        p = (dev.get("projects") or {}).get(pid)
        if not p:
            continue
        root = _project_files_root(g.device_id, pid)
        try:
            count = sum(1 for _ in root.rglob("*") if _.is_file()) if root.exists() else 0
        except Exception:
            count = 0
        out.append({
            "id": p.get("id"), "name": p.get("name"),
            "created": p.get("created"), "chat_id": p.get("chat_id"),
            "files_count": count,
        })
    return jsonify(out)


@app.route("/api/projects", methods=["POST"])
def create_project():
    dev = _dev()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "Новый проект").strip()[:80] or "Новый проект"

    pid = uuid.uuid4().hex[:8]
    cid = uuid.uuid4().hex[:8]

    chat = {
        "id": cid, "title": name,
        "created": datetime.now().isoformat(),
        "messages": [], "project_id": pid,
    }
    dev["chats"][cid] = chat

    proj = {
        "id": pid, "name": name,
        "created": datetime.now().isoformat(),
        "chat_id": cid,
    }
    dev.setdefault("projects", {})[pid] = proj
    dev.setdefault("project_order", []).insert(0, pid)

    _project_files_root(g.device_id, pid).mkdir(parents=True, exist_ok=True)
    _save_data()
    return jsonify(proj)


@app.route("/api/projects/<pid>", methods=["GET"])
def get_project(pid):
    dev = _dev()
    proj = (dev.get("projects") or {}).get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404
    root = _project_files_root(g.device_id, pid)
    root.mkdir(parents=True, exist_ok=True)
    files = projects_agent.list_files(root)
    return jsonify({
        "id": proj.get("id"), "name": proj.get("name"),
        "created": proj.get("created"), "chat_id": proj.get("chat_id"),
        "files": files,
    })


@app.route("/api/projects/<pid>", methods=["DELETE"])
def delete_project(pid):
    dev = _dev()
    proj = (dev.get("projects") or {}).pop(pid, None)
    if pid in dev.get("project_order", []):
        dev["project_order"].remove(pid)
    if proj and proj.get("chat_id"):
        cid = proj["chat_id"]
        dev["chats"].pop(cid, None)
        if cid in dev["chat_order"]:
            dev["chat_order"].remove(cid)
    try:
        root = _project_files_root(g.device_id, pid).parent
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
    except Exception:
        pass
    _save_data()
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/upload", methods=["POST"])
def project_upload(pid):
    dev = _dev()
    proj = (dev.get("projects") or {}).get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404

    root = _project_files_root(g.device_id, pid)
    root.mkdir(parents=True, exist_ok=True)

    files = request.files.getlist("files") or []
    if not files:
        single = request.files.get("file")
        if single:
            files = [single]
    if not files:
        return jsonify({"error": "no files"}), 400

    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        rel = (getattr(f, "filename", "") or "").replace("\\", "/")
        rel = projects_agent._safe_relpath(rel)
        if not rel:
            continue
        dest = root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            f.save(dest)
            saved.append(rel)
        except Exception as e:
            print("[zimple] project upload failed:", e)

    _save_data()
    return jsonify({"ok": True, "saved": saved})


@app.route("/api/projects/<pid>/file", methods=["GET"])
def project_get_file(pid):
    try:
        dev = _dev()
        proj = (dev.get("projects") or {}).get(pid)
        if not proj:
            return jsonify({"error": "project not found"}), 404

        rel = (request.args.get("path") or "").strip()
        rel = projects_agent._safe_relpath(rel)
        if not rel:
            return jsonify({"error": "empty path"}), 400

        root = _project_files_root(g.device_id, pid)
        target = root / rel

        if not target.exists() or not target.is_file():
            return jsonify({"error": "file not found"}), 404

        raw = request.args.get("raw")
        if raw == "1":
            return send_file(str(target))

        ext = target.suffix.lower()
        kind = "text"
        if ext in IMAGE_EXTS:
            kind = "image"
        elif ext in VIDEO_EXTS:
            kind = "video"
        elif ext in TEXT_EXTS:
            kind = "text"
        else:
            try:
                target.read_text(encoding="utf-8")
                kind = "text"
            except Exception:
                kind = "binary"

        size = 0
        try:
            size = target.stat().st_size
        except Exception:
            pass

        if kind == "text":
            text, err = projects_agent.read_text(root, rel)
            if err:
                return jsonify({"error": err, "path": rel}), 400
            return jsonify({
                "path": rel, "kind": "text", "ext": ext,
                "size": size, "content": text or "",
            })

        return jsonify({
            "path": rel, "kind": kind, "ext": ext, "size": size,
            "url": f"/api/projects/{pid}/file?path={rel}&raw=1",
        })
    except Exception as e:
        print("[zimple] project_get_file error:", e)
        return jsonify({"error": "internal error: " + str(e)}), 500


@app.route("/api/projects/<pid>/file", methods=["POST"])
def project_write_file(pid):
    dev = _dev()
    proj = (dev.get("projects") or {}).get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True) or {}
    rel = (data.get("path") or "").strip()
    content = data.get("content")
    if content is None:
        return jsonify({"error": "content required"}), 400

    root = _project_files_root(g.device_id, pid)
    root.mkdir(parents=True, exist_ok=True)
    err = projects_agent.write_text(root, rel, content)
    if err:
        return jsonify({"error": err}), 400
    _save_data()
    return jsonify({"ok": True, "path": projects_agent._safe_relpath(rel)})


@app.route("/api/projects/<pid>/exec", methods=["POST"])
def project_exec(pid):
    dev = _dev()
    proj = (dev.get("projects") or {}).get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True) or {}
    action = data.get("action") or {}
    kind = (action.get("action") or "").strip().lower()

    root = _project_files_root(g.device_id, pid)
    root.mkdir(parents=True, exist_ok=True)

    result = {"ok": True, "action": kind}

    if kind == "list_files":
        result["files"] = projects_agent.list_files(root)
    elif kind == "read_file":
        rel = action.get("path")
        text, err = projects_agent.read_text(root, rel)
        if err:
            result = {"ok": False, "action": kind, "error": err}
        else:
            result["path"] = projects_agent._safe_relpath(rel)
            result["content"] = text
            if len(text) > 20000:
                result["content"] = text[:20000] + "\n... [усечено]"
                result["truncated"] = True
    elif kind == "write_file":
        err = projects_agent.write_text(root, action.get("path"),
                                         action.get("content") or "")
        if err:
            result = {"ok": False, "action": kind, "error": err}
        else:
            result["path"] = projects_agent._safe_relpath(action.get("path"))
            result["written"] = True
    elif kind == "patch_file":
        info, err = projects_agent.patch_text(
            root, action.get("path"),
            action.get("find") or "", action.get("replace") or "",
            action.get("count", 1),
        )
        if err:
            result = {"ok": False, "action": kind, "error": err}
        else:
            result["path"] = projects_agent._safe_relpath(action.get("path"))
            result["replacements"] = info.get("replacements")
            result["occurrences_before"] = info.get("occurrences_before")
    elif kind == "append_file":
        err = projects_agent.append_text(root, action.get("path"),
                                          action.get("content") or "")
        if err:
            result = {"ok": False, "action": kind, "error": err}
        else:
            result["path"] = projects_agent._safe_relpath(action.get("path"))
            result["appended"] = True
    elif kind == "delete_file":
        err = projects_agent.delete_file(root, action.get("path"))
        if err:
            result = {"ok": False, "action": kind, "error": err}
        else:
            result["path"] = projects_agent._safe_relpath(action.get("path"))
            result["deleted"] = True
    else:
        result = {"ok": False, "action": kind, "error": f"unknown action: {kind}"}

    cid = data.get("cid"); mid = data.get("mid"); bidx = data.get("bidx")
    if cid and mid is not None and bidx is not None:
        chat = dev["chats"].get(cid)
        if chat:
            for m in chat["messages"]:
                if m.get("id") == mid:
                    blocks = m.get("blocks") or []
                    try:
                        i = int(bidx)
                        if 0 <= i < len(blocks):
                            blocks[i]["result"] = result
                            _save_data()
                    except Exception:
                        pass
                    break
    return jsonify(result)


# ============================================================
#  Serve
# ============================================================
def _serve():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def _run_headless():
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    lan_ip = _get_lan_ip()
    no_window = ("--no-window" in sys.argv) or os.environ.get("ZIMPLE_NO_WINDOW") == "1"

    print()
    print("  Zimple AI")
    print("  " + "─" * 46)
    print(f"  Локально:      http://127.0.0.1:{PORT}")
    print(f"  В Wi-Fi-сети:  http://{lan_ip}:{PORT}")
    print(f"  Модель LM:     {LM_MODEL}")
    print(f"  Debug dump:    {DEBUG_DIR / 'last_llm.json'}")
    print("  " + "─" * 46)
    print()

    threading.Thread(target=_serve, daemon=True).start()

    try:
        if no_window:
            _run_headless()
        else:
            try:
                import webview
                webview.create_window(
                    "Zimple AI", f"http://127.0.0.1:{PORT}",
                    width=1200, height=920,
                    min_size=(900, 680), background_color="#ffffff",
                )
                webview.start()
            except ImportError:
                print("  pywebview не установлен — открываю в браузере.")
                webbrowser.open(f"http://127.0.0.1:{PORT}")
                _run_headless()
    finally:
        _save_data()
        browser_agent.close_browser()
        computer_agent.shutdown()