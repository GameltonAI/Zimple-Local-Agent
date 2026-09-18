"""Работа с файлами проектов. Файлы хранятся на диске, путь — относительно
корня проекта. Все операции защищены от выхода за пределы корня.
"""
import shutil
from pathlib import Path

MAX_FILE_BYTES = 3 * 1024 * 1024   # 3 МБ на файл — граница для read/write
MAX_LIST_ITEMS = 2000


def _safe_relpath(p) -> str:
    """Нормализует относительный путь, отбрасывает пустые и опасные сегменты."""
    if not p:
        return ""
    p = str(p).replace("\\", "/").strip()
    while p.startswith("/"):
        p = p[1:]
    parts = []
    for chunk in p.split("/"):
        chunk = chunk.strip()
        if chunk in ("", ".", ".."):
            continue
        parts.append(chunk)
    return "/".join(parts)


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def list_files(root: Path) -> list:
    out = []
    if not root.exists():
        return out
    count = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        try:
            size = p.stat().st_size
        except Exception:
            size = 0
        out.append({
            "path": rel,
            "size": size,
            "ext": p.suffix.lower(),
            "name": p.name,
        })
        count += 1
        if count >= MAX_LIST_ITEMS:
            break
    return out


def read_text(root: Path, relpath: str):
    rel = _safe_relpath(relpath)
    if not rel:
        return None, "empty path"
    target = root / rel
    if not _inside(root, target) or not target.is_file():
        return None, "not found"
    try:
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            return None, f"file too big ({size} bytes, limit {MAX_FILE_BYTES})"
        data = target.read_bytes()
    except Exception as e:
        return None, str(e)
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1"), None
        except Exception as e:
            return None, str(e)


def write_text(root: Path, relpath: str, content: str):
    rel = _safe_relpath(relpath)
    if not rel:
        return "empty path"
    if content is None:
        content = ""
    if len(content.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        return "content too big"
    target = root / rel
    if not _inside(root, target):
        return "path outside project"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        return str(e)
    return None


def patch_text(root: Path, relpath: str, find: str, replace: str, count=1):
    """Заменяет find на replace. count=1 — первое вхождение,
    count=0 или None — все. Возвращает (info, err)."""
    rel = _safe_relpath(relpath)
    if not rel:
        return None, "empty path"
    if not find:
        return None, "find is empty"
    target = root / rel
    if not _inside(root, target) or not target.is_file():
        return None, "not found"
    try:
        src = target.read_text(encoding="utf-8")
    except Exception as e:
        return None, str(e)

    occurrences = src.count(find)
    if occurrences == 0:
        return None, "find not found in file"

    try:
        cnt = int(count)
    except Exception:
        cnt = 1

    if cnt <= 0:
        dst = src.replace(find, replace)
        n = occurrences
    else:
        dst = src.replace(find, replace, cnt)
        n = min(cnt, occurrences)

    try:
        target.write_text(dst, encoding="utf-8")
    except Exception as e:
        return None, str(e)
    return {"replacements": n, "occurrences_before": occurrences}, None


def append_text(root: Path, relpath: str, content: str):
    rel = _safe_relpath(relpath)
    if not rel:
        return "empty path"
    if content is None:
        content = ""
    target = root / rel
    if not _inside(root, target):
        return "path outside project"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return str(e)
    return None


def delete_file(root: Path, relpath: str):
    rel = _safe_relpath(relpath)
    if not rel:
        return "empty path"
    target = root / rel
    if not _inside(root, target) or not target.exists():
        return "not found"
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as e:
        return str(e)
    return None