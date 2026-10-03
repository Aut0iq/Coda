"""Cookies YouTube для роликов 18+: разбор присланного текста.

Приложение присылает содержимое cookies.txt (Netscape) — файлом или вставкой.
При вставке табы нередко превращаются в пробелы, поэтому строки чиним.
"""

from __future__ import annotations

LOGIN_COOKIES = ("SID", "__Secure-3PSID", "LOGIN_INFO")

HELP = (
    "Нужен экспорт cookies.txt (формат Netscape) с youtube.com из браузера, где ты вошёл "
    "в аккаунт — например, расширением «Get cookies.txt LOCALLY». Лучше второстепенный "
    "аккаунт. Cookies используются только для роликов 18+."
)


def parse(text: str) -> tuple[list[str] | None, str]:
    """-> (строки youtube.com для сохранения, '') или (None, причина отказа)."""
    fixed = []
    for line in text.splitlines():
        if line.strip() and not line.startswith("#") and "\t" not in line:
            parts = line.split()
            if len(parts) >= 7:
                line = "\t".join(parts[:6] + [" ".join(parts[6:])])
        fixed.append(line)

    lines = [l for l in fixed if l.strip() and not l.startswith("#")]
    yt = []
    for l in lines:
        cols = l.split("\t")
        if len(cols) != 7 or ".youtube.com" not in cols[0]:
            continue
        if cols[5].startswith("ST-"):      # служебные, к входу отношения не имеют
            continue
        yt.append(l)
    if not yt:
        return None, "Это не похоже на cookies YouTube в формате Netscape. " + HELP
    if not any(l.split("\t")[5] in LOGIN_COOKIES for l in yt):
        return None, ("В тексте нет cookies входа (SID / LOGIN_INFO): экспорт сделан без входа "
                      "в YouTube или текст обрезался. Нужны строки с SID, __Secure-3PSID и LOGIN_INFO.")
    return yt, ""


def render(lines: list[str]) -> str:
    return "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"
