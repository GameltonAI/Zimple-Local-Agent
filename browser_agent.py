"""Управление реальным браузером через Playwright.

Все операции выполняются в выделенном worker-потоке, потому что sync API
Playwright привязан к потоку, который его создал. Flask обрабатывает запросы
в разных потоках, поэтому нужна очередь команд.
"""
import queue
import threading
import time
import uuid
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    try:
        from playwright.sync_api import TimeoutError as PWTimeoutError
    except ImportError:
        PWTimeoutError = None
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PWTimeoutError = None

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Куда складываем файлы, скачанные агентом из браузера
DOWNLOAD_DIR = Path.home() / "Downloads" / "ZimpleAI"

# Последние скачанные файлы (путь на диске + исходное имя и url)
_downloads: list = []
_downloads_lock = threading.Lock()


def _register_download(dl):
    """Сохраняет download-объект Playwright на диск и запоминает путь."""
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        name = dl.suggested_filename or (uuid.uuid4().hex + ".bin")
    except Exception:
        name = uuid.uuid4().hex + ".bin"
    target = DOWNLOAD_DIR / name
    n = 1
    while target.exists():
        target = DOWNLOAD_DIR / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    try:
        dl.save_as(str(target))
    except Exception as e:
        item = {"ok": False, "error": str(e)[:200], "filename": name, "path": None}
        with _downloads_lock:
            _downloads.append(item)
        return item
    try:
        url = dl.url
    except Exception:
        url = ""
    item = {
        "ok": True,
        "filename": name,
        "path": str(target),
        "url": url,
        "size": target.stat().st_size if target.exists() else 0,
    }
    with _downloads_lock:
        _downloads.append(item)
    print("[browser] downloaded:", item["path"])
    return item


def get_downloads(clear: bool = False):
    with _downloads_lock:
        items = list(_downloads)
        if clear:
            _downloads.clear()
    return items

_cmd_queue: "queue.Queue" = queue.Queue()
_worker_thread = None
_worker_lock = threading.Lock()


def is_available() -> bool:
    return PLAYWRIGHT_AVAILABLE


def _ensure_worker():
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker, daemon=True)
            _worker_thread.start()


def _do_action(page, action: dict):
    kind = (action.get("action") or "").strip().lower()

    if kind == "goto":
        url = (action.get("url") or "").strip()
        if not url:
            raise ValueError("url обязателен для goto")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        wait_until = action.get("wait_until") or "domcontentloaded"
        page.goto(url, wait_until=wait_until, timeout=25000)

    elif kind == "click":
        sel = (action.get("selector") or "").strip()
        if not sel:
            raise ValueError("selector обязателен для click")
        page.click(sel, timeout=8000)

    elif kind == "type":
        sel = (action.get("selector") or "").strip()
        val = action.get("value") or ""
        if not sel:
            raise ValueError("selector обязателен для type")
        page.fill(sel, val, timeout=8000)

    elif kind == "press":
        key = action.get("key") or "Enter"
        page.keyboard.press(key)

    elif kind == "scroll":
        try:
            dy = int(action.get("dy") or 600)
        except Exception:
            dy = 600
        page.mouse.wheel(0, dy)

    elif kind == "back":
        page.go_back(timeout=10000)

    elif kind == "wait":
        try:
            sec = float(action.get("seconds") or 1)
        except Exception:
            sec = 1.0
        time.sleep(max(0.0, min(sec, 10.0)))

    elif kind == "download":
        # Клик по ссылке/кнопке, который приводит к скачиванию файла.
        sel = (action.get("selector") or "").strip()
        url = (action.get("url") or "").strip()
        if not sel and not url:
            raise ValueError("нужен selector или url для download")
        if url:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            try:
                with page.expect_download(timeout=60000) as dl_info:
                    page.evaluate(
                        "(u) => { const a = document.createElement('a');"
                        " a.href = u; a.download = ''; document.body.appendChild(a);"
                        " a.click(); a.remove(); }", url
                    )
                _register_download(dl_info.value)
            except Exception:
                # некоторые ссылки открывают файл напрямую — пробуем goto
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
        else:
            with page.expect_download(timeout=60000) as dl_info:
                page.click(sel, timeout=8000)
            _register_download(dl_info.value)

    elif kind == "screenshot":
        pass

    else:
        raise ValueError(f"неизвестное действие: {kind}")


def _worker():
    """Единственный поток, который общается с Playwright."""
    pw = None
    browser = None
    context = None
    page = None

    def start_browser():
        nonlocal pw, browser, context, page
        if page is not None and not page.is_closed():
            return
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            context.on("page", lambda pg: pg.on("download", _register_download))
            page.on("download", _register_download)
        except Exception:
            pass

    def stop_browser():
        nonlocal pw, browser, context, page
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass
        pw = browser = context = page = None

    def sync_page_after_action(before_pages):
        """Если в результате действия открылись новые вкладки — переключаемся на последнюю."""
        nonlocal page
        try:
            if not context:
                return
            current_pages = context.pages
        except Exception:
            return
        try:
            new_pages = [p for p in current_pages if p not in before_pages]
        except Exception:
            new_pages = []

        if not new_pages:
            # Если текущая страница закрылась — переключаемся на любую живую
            try:
                if page is None or page.is_closed():
                    if current_pages:
                        page = current_pages[-1]
                        page.bring_to_front()
            except Exception:
                pass
            return

        target = new_pages[-1]
        try:
            target.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            try:
                target.wait_for_load_state("load", timeout=3000)
            except Exception:
                pass
        try:
            target.bring_to_front()
        except Exception:
            pass
        page = target

    while True:
        cmd = _cmd_queue.get()
        if cmd is None:
            break

        kind, args, holder = cmd

        try:
            if kind == "close":
                stop_browser()
                holder["result"] = {"ok": True}
                continue

            if not PLAYWRIGHT_AVAILABLE:
                holder["result"] = {
                    "ok": False,
                    "error": "Playwright не установлен. Выполни: pip install playwright && playwright install chromium",
                    "screenshot": None,
                }
                continue

            try:
                start_browser()
            except Exception as e:
                holder["result"] = {
                    "ok": False,
                    "error": "browser launch: " + str(e),
                    "screenshot": None,
                }
                continue

            if kind == "exec":
                action = args
                err = None
                with _downloads_lock:
                    dl_before = len(_downloads)

                before_pages = []
                try:
                    before_pages = list(context.pages) if context else []
                except Exception:
                    before_pages = []

                try:
                    _do_action(page, action)
                except Exception as e:
                    if PWTimeoutError and isinstance(e, PWTimeoutError):
                        err = "timeout: " + str(e)[:180]
                    else:
                        err = str(e)[:220]

                # Переключаемся на новую вкладку, если она открылась
                sync_page_after_action(before_pages)

                shot = None
                try:
                    name = uuid.uuid4().hex + ".png"
                    path = UPLOAD_DIR / name
                    page.screenshot(path=str(path), full_page=False)
                    shot = f"/uploads/{name}"
                except Exception as e:
                    if not err:
                        err = "screenshot failed: " + str(e)[:180]

                with _downloads_lock:
                    new_downloads = list(_downloads[dl_before:])

                holder["result"] = {
                    "ok": err is None, "error": err, "screenshot": shot,
                    "downloads": new_downloads,
                }

            elif kind == "state":
                try:
                    url = page.url
                except Exception:
                    url = None
                try:
                    title = page.title()
                except Exception:
                    title = None

                max_text = 1500
                if isinstance(args, dict):
                    try:
                        max_text = int(args.get("max_text") or max_text)
                    except Exception:
                        pass

                # Собираем текст со ВСЕХ фреймов, не только с главного
                parts = []
                frames_info = []
                try:
                    frames = page.frames
                except Exception:
                    frames = []

                for i, fr in enumerate(frames):
                    try:
                        fr_text = fr.evaluate(
                            "() => document.body ? document.body.innerText : ''"
                        )
                    except Exception:
                        fr_text = ""
                    if not isinstance(fr_text, str):
                        fr_text = ""
                    fr_text = fr_text.strip()

                    try:
                        fr_url = fr.url
                    except Exception:
                        fr_url = ""

                    is_main = (fr == page.main_frame)
                    label = "MAIN" if is_main else f"IFRAME#{i}"

                    frames_info.append(f"{label}: {fr_url}")
                    if fr_text:
                        parts.append(f"--- {label} ({fr_url}) ---\n{fr_text}")

                combined = "\n\n".join(parts)
                if len(combined) > max_text:
                    combined = combined[:max_text] + "\n... [усечено]"

                # Плюс интерактивные элементы со всех фреймов (см. фикс №2)
                interactive = _get_interactive_elements_all_frames(page)

                with _downloads_lock:
                    dls = list(_downloads[-5:])

                holder["result"] = {
                    "url": url,
                    "title": title,
                    "text": combined,
                    "frames": frames_info,
                    "interactive": interactive,
                    "downloads": dls,
                }

            else:
                holder["result"] = {"ok": False, "error": f"unknown cmd {kind}"}

        except Exception as e:
            holder["result"] = {"ok": False, "error": str(e), "screenshot": None}
        finally:
            holder["done"].set()


def _submit(kind, args, timeout=90):
    _ensure_worker()
    holder = {"done": threading.Event(), "result": None}
    _cmd_queue.put((kind, args, holder))
    if not holder["done"].wait(timeout=timeout):
        return {"ok": False, "error": "timeout waiting for browser worker",
                "screenshot": None, "url": None, "title": None, "text": ""}
    return holder["result"] or {}

def _get_interactive_elements_all_frames(page, limit_per_frame=80):
    """Собирает интерактивные элементы со всех фреймов."""
    out = []
    try:
        frames = page.frames
    except Exception:
        return out

    for i, fr in enumerate(frames):
        try:
            items = fr.evaluate("""
                (limit) => {
                    const out = [];
                    const sels = 'a, button, input, textarea, select, ' +
                                 '[role=button], [role=link], [role=tab], ' +
                                 '[onclick], [contenteditable=true], ' +
                                 'canvas[tabindex], [class*=btn], [class*=button]';
                    const nodes = document.querySelectorAll(sels);
                    let n = 0;
                    for (const el of nodes) {
                        if (n >= limit) break;
                        const r = el.getBoundingClientRect();
                        if (r.width < 4 || r.height < 4) continue;
                        const style = getComputedStyle(el);
                        if (style.visibility === 'hidden' ||
                            style.display === 'none' ||
                            style.pointerEvents === 'none') continue;
                        const txt = (el.innerText || el.value ||
                                     el.placeholder ||
                                     el.getAttribute('aria-label') ||
                                     el.getAttribute('title') || '')
                                    .trim().replace(/\\s+/g,' ').slice(0, 70);
                        out.push({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || undefined,
                            name: el.getAttribute('name') || undefined,
                            type: el.getAttribute('type') || undefined,
                            cls: (typeof el.className === 'string'
                                  ? el.className.split(/\\s+/).slice(0,3).join('.')
                                  : undefined),
                            text: txt || undefined,
                        });
                        n++;
                    }
                    return out;
                }
            """, limit_per_frame)
        except Exception:
            items = []

        if not isinstance(items, list):
            continue

        is_main = (fr == page.main_frame)
        label = "main" if is_main else f"iframe{i}"
        for el in items:
            if isinstance(el, dict):
                el["frame"] = label
                out.append(el)

    return out

def _find_frame_for_selector(page, sel, timeout=1500):
    """Ищет фрейм, в котором есть селектор. Возвращает Frame или None."""
    # сначала главный
    try:
        page.locator(sel).first.wait_for(state="attached", timeout=timeout)
        return page.main_frame
    except Exception:
        pass
    # потом остальные
    for fr in page.frames:
        if fr == page.main_frame:
            continue
        try:
            fr.locator(sel).first.wait_for(state="attached", timeout=timeout)
            return fr
        except Exception:
            continue
    return None

def execute(action: dict) -> dict:
    if not isinstance(action, dict):
        return {"ok": False, "error": "action must be dict", "screenshot": None}
    return _submit("exec", action)


def get_state(max_text: int = 1500) -> dict:
    result = _submit("state", {"max_text": max_text})
    if not isinstance(result, dict) or "url" not in result:
        return {"url": None, "title": None, "text": ""}
    return result


def close_browser():
    try:
        _submit("close", None, timeout=15)
    except Exception:
        pass