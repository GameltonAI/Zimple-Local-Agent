"""Запись файлов вне песочницы проекта — в любое место на диске пользователя
(рабочий стол, документы, загрузки, произвольная папка).

Используется, когда пользователь в ОБЫЧНОМ ЧАТЕ просит что-то "сохранить",
"положить на рабочий стол", "сделать файлом" и т.п., а не работать внутри
конкретного проекта.

Разрешено писать куда угодно ВНУТРИ домашней папки пользователя. Явно
запрещено:
  - выходить за пределы домашней папки (например в C:\\Windows, /etc и т.п.),
  - писать в папки автозапуска / конфиги учёток и ключей (Startup, .ssh,
    .aws, .gnupg, autostart, LaunchAgents и т.п.) — это типичные цели для
    атаки через prompt injection на странице, которую агент открыл в
    браузере,
  - создавать файлы с расширениями, которые ОС может исполнить или
    подхватить сама (.exe, .dll, .bat, .ps1, .lnk, .reg, ...).

Это НЕ полноценная песочница (в отличие от projects_agent) — предполагается,
что пользователь доверяет агенту так же, как уже доверяет ему управление
браузером и рабочим столом. Ограничения здесь — щит именно от случайной
или спровоцированной записи в чувствительные системные места, а не от
самого пользователя.
"""
from pathlib import Path

MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 МБ на файл

HOME = Path.home()

WELL_KNOWN = {
    "desktop": HOME / "Desktop",
    "documents": HOME / "Documents",
    "downloads": HOME / "Downloads",
    "home": HOME,
}

# Папки, запись в которые запрещена, даже если они лежат внутри HOME.
_SENSITIVE_SEGMENTS = {
    ".ssh", ".aws", ".gnupg", ".gpg", ".docker", ".kube", ".azure",
    "startup", "start menu", "autostart", "launchagents", "launchdaemons",
    "systemd", "cron.d",
}

# Расширения, которые ОС может исполнить/подхватить сама — их создавать
# через этот канал нельзя (используйте песочницу проекта, если нужно
# писать код).
_BLOCKED_EXTS = {
    ".exe", ".dll", ".sys", ".scr", ".com", ".bat", ".cmd", ".ps1", ".psm1",
    ".vbs", ".vbe", ".jse", ".wsf", ".wsh", ".msi", ".msc", ".jar",
    ".lnk", ".reg", ".cpl", ".apk", ".app", ".gadget", ".pif",
}


def _finish(target: Path):
    try:
        target = target.resolve()
        home = HOME.resolve()
    except Exception as e:
        return None, str(e)

    try:
        target.relative_to(home)
    except Exception:
        return None, "путь должен быть внутри домашней папки пользователя"

    for seg in target.parts:
        if seg.lower() in _SENSITIVE_SEGMENTS:
            return None, "запись в эту папку запрещена"

    if target.suffix.lower() in _BLOCKED_EXTS:
        return None, f"расширение '{target.suffix}' запрещено для этого канала"

    return target, None


def resolve_path(raw_path: str):
    """path поддерживает алиасы 'desktop:report.md', 'documents:notes.txt',
    'downloads:data.csv', 'home:foo/bar.txt', абсолютные пути и '~'.
    Простое имя файла без алиаса и без '/' кладём в Documents."""
    if not raw_path or not isinstance(raw_path, str):
        return None, "empty path"
    p = raw_path.strip().replace("\\", "/")
    if not p:
        return None, "empty path"

    low = p.lower()
    for alias, root in WELL_KNOWN.items():
        prefix = alias + ":"
        if low.startswith(prefix):
            rel = p[len(prefix):].lstrip("/")
            if not rel:
                return None, "путь после алиаса пуст"
            return _finish(root / rel)

    expanded = Path(p).expanduser()
    if expanded.is_absolute():
        return _finish(expanded)

    # относительный путь без алиаса и указания папки — Documents по умолчанию
    return _finish(WELL_KNOWN["documents"] / p)


def write_text(raw_path: str, content, append: bool = False):
    target, err = resolve_path(raw_path)
    if err:
        return None, err
    if content is None:
        content = ""
    content = str(content)
    if len(content.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        return None, f"содержимое слишком большое (лимит {MAX_FILE_BYTES} байт)"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return None, str(e)
    return str(target), None
