"""Общая подготовка: пути и окружение ДО импорта main (движок читает env при загрузке)."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="hub-test-"))
os.environ.setdefault("HUB_DATA", str(_TMP / "data"))
os.environ.setdefault("MUSIC_DIR", str(_TMP / "music"))
# downloader читает их при импорте — раньше, чем main выставит значения по умолчанию
os.environ["COOKIE_FILE"] = str(_TMP / "data" / "cookies.txt")
os.environ["LIBRARY_INDEX"] = str(_TMP / "data" / "library.json")
os.environ.setdefault("MB_TMP_DIR", str(_TMP / "music" / ".incoming"))
os.environ["HUB_NO_BACKGROUND"] = "1"
os.environ["HUB_TOKEN"] = "test-token"
for k in ("PROXY_URL", "PROXY2_URL", "PROXY_URLS"):
    os.environ.pop(k, None)
