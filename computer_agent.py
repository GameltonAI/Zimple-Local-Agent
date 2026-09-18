"""Управление рабочим столом.

Архитектура как в browser_agent.py: единственный worker-поток общается
с ОС, а Flask кидает команды через очередь.
"""
import ctypes
import queue
import threading
import time
import uuid
from pathlib import Path

# --- DPI awareness (Windows) --------------------------------------------------
if hasattr(ctypes, "windll"):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    import mss
    _MSS_OK = True
except ImportError:
    _MSS_OK = False

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.03
    _PYAUTOGUI_OK = True
except ImportError:
    _PYAUTOGUI_OK = False

_UIA_OK = False
_UIA_INIT_CLS = None
try:
    import uiautomation as auto
    try:
        from uiautomation import UIAutomationInitializerInThread as _UIA_INIT_CLS
    except ImportError:
        # в старых версиях uiautomation контекст-менеджера нет —
        # придётся инициализировать COM вручную
        _UIA_INIT_CLS = None
    _UIA_OK = True
except ImportError:
    _UIA_OK = False

try:
    import pyperclip
    _CLIP_OK = True
except ImportError:
    _CLIP_OK = False

try:
    from PIL import Image, ImageDraw, ImageGrab
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

_cmd_queue: "queue.Queue" = queue.Queue()
_worker_thread = None
_worker_lock = threading.Lock()

_state_lock = threading.Lock()
_enabled = True  # default — управление включено

_STATE_FILE = Path(__file__).resolve().parent / "debug" / "computer_enabled.txt"


def _load_enabled_state():
    global _enabled
    try:
        if _STATE_FILE.exists():
            _enabled = _STATE_FILE.read_text().strip() == "1"
        else:
            _enabled = True
            _save_enabled_state()
    except Exception:
        _enabled = True


def _save_enabled_state():
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        _STATE_FILE.write_text("1" if _enabled else "0")
    except Exception as e:
        print("[computer] save enabled state failed:", repr(e))

_element_cache: dict = {}
MAX_TREE_ITEMS = 250

_OWN_WINDOW_TITLE = "Zimple AI"

_last_action_info = {"rect": None, "click": None, "label": None}


def is_available() -> bool:
    return _MSS_OK and _PYAUTOGUI_OK


def has_a11y() -> bool:
    return _UIA_OK


def set_enabled(v: bool) -> bool:
    global _enabled
    with _state_lock:
        _enabled = bool(v)
    _save_enabled_state()
    return _enabled


def get_enabled() -> bool:
    with _state_lock:
        return _enabled


def _ensure_worker():
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker, daemon=True)
            _worker_thread.start()


# ---------- управление своим окном (ctypes, без pywin32) ----------
_user32 = None
if hasattr(ctypes, "windll"):
    try:
        _user32 = ctypes.windll.user32
    except Exception:
        _user32 = None


def _find_own_windows():
    if _user32 is None:
        return []
    hwnds = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    def cb(hwnd, _):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == _OWN_WINDOW_TITLE:
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(EnumWindowsProc(cb), 0)
    except Exception:
        pass
    return hwnds


# Окно Zimple AI больше НЕ сворачивается перед действиями и скриншотами:
# постоянное сворачивание/разворачивание мешало работе. Вместо этого при
# сборе состояния мы просто игнорируем собственное окно и берём верхнее
# чужое окно из Z-порядка.
MINIMIZE_OWN_WINDOW = False


def _minimize_own():
    if not MINIMIZE_OWN_WINDOW:
        return []
    hwnds = _find_own_windows()
    for h in hwnds:
        try:
            _user32.ShowWindow(h, 6)  # SW_MINIMIZE
        except Exception:
            pass
    return hwnds


def _top_foreign_window():
    """Верхнее видимое окно, которое НЕ принадлежит Zimple AI."""
    if _user32 is None:
        return 0
    found = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    def cb(hwnd, _):
        if found:
            return False
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            if _user32.IsIconic(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if not title or title == _OWN_WINDOW_TITLE:
                return True
            found.append(hwnd)
            return False
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(EnumWindowsProc(cb), 0)
    except Exception:
        pass
    return found[0] if found else 0


def _restore_own(hwnds):
    if not hwnds or _user32 is None:
        return
    for h in hwnds:
        try:
            _user32.ShowWindow(h, 9)  # SW_RESTORE
        except Exception:
            pass


# ---------- скриншоты ----------
def _take_screenshot():
    name = uuid.uuid4().hex + ".png"
    path = UPLOAD_DIR / name

    if _MSS_OK:
        try:
            with mss.mss() as sct:
                sct.shot(mon=1, output=str(path))
            if path.exists() and path.stat().st_size > 0:
                return f"/uploads/{name}"
            print("[computer] mss ran but file is empty")
        except Exception as e:
            print("[computer] mss failed:", repr(e))

    if _PIL_OK:
        try:
            img = ImageGrab.grab()
            img.save(str(path))
            if path.exists() and path.stat().st_size > 0:
                return f"/uploads/{name}"
            print("[computer] PIL grab ran but file is empty")
        except Exception as e:
            print("[computer] PIL ImageGrab failed:", repr(e))

    print("[computer] no screenshot method worked")
    return None


def _annotate_shot(url):
    if not _PIL_OK or not url:
        return
    name = url.rsplit("/", 1)[-1]
    path = UPLOAD_DIR / name
    if not path.exists():
        return
    info = _last_action_info
    rect = info.get("rect")
    click = info.get("click")
    if not rect and not click:
        return
    try:
        img = Image.open(str(path)).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")

        if rect:
            x1, y1, x2, y2 = rect
            draw.rectangle(
                [(x1, y1), (x2, y2)],
                fill=(255, 210, 60, 55),
                outline=(255, 170, 20, 255),
                width=3,
            )

        if click:
            cx, cy = click
            r = 18
            draw.ellipse(
                [(cx - r, cy - r), (cx + r, cy + r)],
                outline=(220, 40, 40, 255), width=4,
            )
            r2 = 6
            draw.ellipse(
                [(cx - r2, cy - r2), (cx + r2, cy + r2)],
                fill=(220, 40, 40, 255),
            )
            draw.line(
                [(cx, cy), (cx + 26, cy + 26)],
                fill=(220, 40, 40, 230), width=4,
            )
            draw.polygon(
                [(cx + 26, cy + 26), (cx + 18, cy + 20), (cx + 20, cy + 18)],
                fill=(220, 40, 40, 230),
            )

        img.save(str(path))
    except Exception as e:
        print("[computer] annotate failed:", repr(e))


# ---------- a11y-дерево ----------
def _walk(ctrl, elements, depth=0):
    if len(elements) >= MAX_TREE_ITEMS or depth > 18:
        return
    try:
        children = ctrl.GetChildren()
    except Exception:
        return

    for c in children:
        if len(elements) >= MAX_TREE_ITEMS:
            return
        try:
            ctype = c.ControlTypeName or ""
        except Exception:
            ctype = ""

        interesting = ctype in {
            "ButtonControl", "EditControl", "ListItemControl",
            "MenuItemControl", "TabItemControl", "CheckBoxControl",
            "RadioButtonControl", "ComboBoxControl", "HyperlinkControl",
            "TreeItemControl", "SplitButtonControl", "TextControl",
        }

        try:
            r = c.BoundingRectangle
            x, y = r.left, r.top
            w, h = r.right - r.left, r.bottom - r.top
        except Exception:
            x = y = w = h = 0

        if interesting and w > 4 and h > 4:
            try:
                name = (c.Name or "").strip().replace("\n", " ").replace("\r", " ")
            except Exception:
                name = ""
            name = name[:80]
            eid = f"el_{len(elements)}"
            entry = {
                "id": eid,
                "type": ctype.replace("Control", "").lower() or "element",
                "name": name,
                "cx": x + w // 2,
                "cy": y + h // 2,
                "rect": [x, y, x + w, y + h],
            }
            elements.append(entry)
            _element_cache[eid] = c

        _walk(c, elements, depth + 1)


def _collect_state():
    # Окно Zimple AI не сворачиваем. Если активным оказалось наше же окно —
    # просто берём следующее чужое окно из Z-порядка.
    _element_cache.clear()
    shot = _take_screenshot()
    _annotate_shot(shot)
    active = {"title": "", "class": "", "hwnd": 0}
    elements = []

    if _UIA_OK:
        try:
            win = auto.GetForegroundControl()
            own_win = False
            try:
                own_win = bool(win) and (win.Name or "") == _OWN_WINDOW_TITLE
            except Exception:
                own_win = False
            if own_win:
                hwnd = _top_foreign_window()
                if hwnd:
                    try:
                        win = auto.ControlFromHandle(hwnd)
                    except Exception:
                        pass
            if win:
                try:
                    active = {
                        "title": win.Name or "",
                        "class": win.ClassName or "",
                        "hwnd": win.NativeWindowHandle or 0,
                    }
                except Exception:
                    pass
                _walk(win, elements)
        except Exception as e:
            print("[computer] a11y failed:", repr(e))

    print(f"[computer] state: window={active.get('title','')!r} "
          f"elements={len(elements)}")
    return {"screenshot": shot, "active_window": active, "elements": elements}


# ---------- ввод текста ----------
def _type_text(text: str):
    if not text:
        return
    if _CLIP_OK:
        try:
            old = ""
            try:
                old = pyperclip.paste()
            except Exception:
                pass
            pyperclip.copy(text)
            time.sleep(0.08)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.08)
            try:
                pyperclip.copy(old)
            except Exception:
                pass
            return
        except Exception as e:
            print("[computer] clipboard type failed:", repr(e))
    try:
        pyautogui.write(text, interval=0.02)
    except Exception as e:
        raise RuntimeError("не удалось ввести текст: " + str(e))


def _focus_element(el):
    try:
        el.SetFocus()
        time.sleep(0.12)
        return True
    except Exception:
        return False


# ---------- действия ----------
def _do_action(action: dict):
    global _last_action_info
    kind = (action.get("action") or "").strip().lower()
    print(f"[computer] action={kind} args={ {k: v for k, v in action.items() if k != 'action'} }")

    if kind in ("click", "double_click"):
        target = action.get("target")
        el = _element_cache.get(target) if target else None
        rect = None

        if el is not None:
            try:
                r = el.BoundingRectangle
                rect = (r.left, r.top, r.right, r.bottom)
                _last_action_info["rect"] = rect
                _last_action_info["click"] = (
                    (r.left + r.right) // 2,
                    (r.top + r.bottom) // 2,
                )
            except Exception:
                pass

            _focus_element(el)
            try:
                if kind == "double_click":
                    el.DoubleClick()
                else:
                    el.Click()
                print(f"[computer] {kind} via uiautomation OK (target={target})")
                return
            except Exception as e:
                print(f"[computer] uia {kind} failed: {e!r}")

        x = action.get("x")
        y = action.get("y")
        if (x is None or y is None) and rect:
            x = (rect[0] + rect[2]) // 2
            y = (rect[1] + rect[3]) // 2
        if x is None or y is None:
            raise ValueError(f"{kind}: нужен target или x/y")
        _last_action_info["click"] = (int(x), int(y))
        if kind == "double_click":
            pyautogui.doubleClick(int(x), int(y))
        else:
            pyautogui.click(int(x), int(y))
        print(f"[computer] {kind} via pyautogui OK at ({x},{y})")

    elif kind == "right_click":
        x = action.get("x")
        y = action.get("y")
        if x is None or y is None:
            raise ValueError("right_click: нужны x/y")
        _last_action_info["click"] = (int(x), int(y))
        pyautogui.rightClick(int(x), int(y))

    elif kind == "type":
        # ВАЖНО: это управление РАБОЧИМ СТОЛОМ (pyautogui/pyperclip), а не
        # браузером — здесь нет объекта page/selector. Поле уже должно быть
        # сфокусировано предыдущим click-действием (см. COMPUTER_SYSTEM_PROMPT).
        val = action.get("value")
        if val is None:
            raise ValueError("value обязателен для type")
        print(f"[computer] type: val={val!r} (len={len(val)})")
        _type_text(val)

    elif kind == "key":
        keys = action.get("keys")
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        if not keys:
            raise ValueError("key: нужен keys")
        pyautogui.hotkey(*keys)

    elif kind == "scroll":
        try:
            dy = int(action.get("dy") or 500)
        except Exception:
            dy = 500
        x = action.get("x")
        y = action.get("y")
        if x is not None and y is not None:
            pyautogui.moveTo(int(x), int(y))
        pyautogui.scroll(dy)

    elif kind == "move":
        x = action.get("x")
        y = action.get("y")
        if x is None or y is None:
            raise ValueError("move: нужны x/y")
        _last_action_info["click"] = (int(x), int(y))
        pyautogui.moveTo(int(x), int(y), duration=0.15)

    elif kind == "drag":
        x1 = action.get("x1")
        y1 = action.get("y1")
        x2 = action.get("x2")
        y2 = action.get("y2")
        if None in (x1, y1, x2, y2):
            raise ValueError("drag: нужны x1/y1/x2/y2")
        _last_action_info["click"] = (int(x2), int(y2))
        pyautogui.moveTo(int(x1), int(y1))
        pyautogui.dragTo(int(x2), int(y2), duration=0.4, button="left")

    elif kind == "wait":
        try:
            sec = float(action.get("seconds") or 1)
        except Exception:
            sec = 1.0
        time.sleep(max(0.0, min(sec, 15.0)))

    elif kind == "screenshot":
        pass

    else:
        raise ValueError(f"неизвестное действие: {kind}")


# ---------- worker ----------
def _worker_loop():
    global _last_action_info
    while True:
        cmd = _cmd_queue.get()
        if cmd is None:
            break
        kind, args, holder = cmd

        try:
            if not is_available():
                msg = (
                    "Не установлены зависимости. Нужно: "
                    "pip install mss pyautogui uiautomation pyperclip Pillow"
                )
                print("[computer]", msg)
                holder["result"] = {"ok": False, "error": msg, "screenshot": None}
                continue

            if kind == "exec":
                if not get_enabled():
                    holder["result"] = {
                        "ok": False, "error": "disabled", "screenshot": None,
                    }
                    continue

                _last_action_info = {"rect": None, "click": None, "label": None}

                own_hwnds = _minimize_own()
                if own_hwnds:
                    time.sleep(0.5)

                err = None
                try:
                    _do_action(args or {})
                except Exception as e:
                    err = str(e)[:250]
                    print("[computer] action error:", repr(e))

                time.sleep(0.5)

                if err is None:
                    state = _collect_state()
                    holder["result"] = {
                        "ok": True,
                        "error": None,
                        "screenshot": state["screenshot"],
                        "active_window": state["active_window"],
                        "elements": state["elements"],
                    }
                else:
                    shot = _take_screenshot()
                    _annotate_shot(shot)
                    holder["result"] = {
                        "ok": False,
                        "error": err,
                        "screenshot": shot,
                    }

                _restore_own(own_hwnds)

            elif kind == "state":
                holder["result"] = _collect_state()

            else:
                holder["result"] = {"ok": False, "error": f"unknown cmd {kind}"}

        except Exception as e:
            print("[computer] worker exception:", repr(e))
            holder["result"] = {"ok": False, "error": str(e), "screenshot": None}
        finally:
            holder["done"].set()


def _worker():
    """Точка входа потока. COM обязан быть инициализирован именно здесь —
    CoInitialize в одном потоке не действует на другой."""
    if _UIA_OK:
        if _UIA_INIT_CLS is not None:
            try:
                with _UIA_INIT_CLS(debug=False):
                    _worker_loop()
                return
            except Exception as e:
                print("[computer] UIAutomationInitializerInThread failed:", repr(e))
        else:
            # старые версии uiautomation: инициализируем COM вручную
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # APARTMENTTHREADED
                print("[computer] COM initialized via CoInitializeEx (fallback)")
            except Exception as e:
                print("[computer] CoInitializeEx failed:", repr(e))
    _worker_loop()


def _submit(kind, args, timeout=120):
    _ensure_worker()
    holder = {"done": threading.Event(), "result": None}
    _cmd_queue.put((kind, args, holder))
    if not holder["done"].wait(timeout=timeout):
        return {"ok": False, "error": "timeout waiting for computer worker",
                "screenshot": None, "elements": []}
    return holder["result"] or {}


def execute(action: dict) -> dict:
    if not isinstance(action, dict):
        return {"ok": False, "error": "action must be dict", "screenshot": None}
    return _submit("exec", action)


def get_state() -> dict:
    return _submit("state", None)

_load_enabled_state()

def shutdown():
    pass