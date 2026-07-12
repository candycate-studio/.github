#!/usr/bin/env python3
"""
org_activity.py — собирает агрегированную git-активность по всем репозиториям
организации candycate-studio и рисует SVG-heatmap (52 недели x 7 дней) в том же
стиле, что и плейсхолдер profile/assets/org-activity.svg.

Только stdlib: urllib.request, json, os, sys, time, math.

Без токена (GH_TOKEN пуст/не задан) скрипт печатает уведомление и тихо
завершается (exit 0) — это ожидаемо до того, как в репозиторий добавят
секрет ORG_METRICS_TOKEN, чтобы плановые прогоны Action оставались зелёными.
"""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

# ---- конфигурация -----------------------------------------------------------

ORG = os.environ.get("GH_ORG", "candycate-studio")
TOKEN = os.environ.get("GH_TOKEN", "").strip()

API_ROOT = "https://api.github.com"
USER_AGENT = "candycate-studio-org-activity-script"

# Геометрия сетки — совпадает с плейсхолдером profile/assets/org-activity.svg.
COLS, ROWS = 52, 7   # 52 недели x 7 дней (0=Вс..6=Сб — порядок GitHub stats API)
C, PAD = 13, 2        # шаг ячейки / внутренний паддинг
GX, GY = 30, 14        # отступ сетки от левого/верхнего края
RAMP = ["#8b95a1", "#a7e8d8", "#5fcdb5", "#35a08e", "#1f7a6b"]  # уровни 0..4
DAY_LABELS = {1: "Пн", 3: "Ср", 5: "Пт"}  # индексы дня недели: 0=Вс..6=Сб

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT_PATH = os.path.join(REPO_ROOT, "profile", "assets", "org-activity.svg")

RETRY_202 = 5          # попыток дождаться асинхронного расчёта статистики
RETRY_SLEEP_BASE = 3    # секунд перед первой повторной попыткой


# ---- HTTP-хелперы -------------------------------------------------------------

def _headers():
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _get(url):
    """GET-запрос. Возвращает (status, body_bytes, headers) даже при HTTP-ошибке."""
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _parse_next_link(link_header):
    """Достаёт rel="next" URL из заголовка Link (пагинация GitHub API)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        pieces = part.split(";")
        if len(pieces) < 2:
            continue
        url = pieces[0].strip().strip("<>")
        rel = pieces[1].strip()
        if rel == 'rel="next"':
            return url
    return None


def fetch_org_repos(org):
    """Все репозитории организации (все страницы), без archived."""
    repos = []
    url = f"{API_ROOT}/orgs/{org}/repos?per_page=100&type=all"
    while url:
        status, body, headers = _get(url)
        if status != 200:
            msg = body.decode("utf-8", "replace")[:300]
            raise RuntimeError(f"не удалось получить список репозиториев ({status}): {msg}")
        data = json.loads(body.decode("utf-8"))
        repos.extend(data)
        url = _parse_next_link(headers.get("Link"))
    return [r for r in repos if not r.get("archived")]


def fetch_commit_activity(full_name):
    """52 недели коммит-активности репозитория. None — если недоступно/пропущено."""
    url = f"{API_ROOT}/repos/{full_name}/stats/commit_activity"
    for attempt in range(RETRY_202):
        try:
            status, body, _resp_headers = _get(url)
        except urllib.error.URLError:
            return None
        if status == 200:
            try:
                data = json.loads(body.decode("utf-8"))
            except ValueError:
                return None
            return data if isinstance(data, list) else None
        if status == 202:
            # GitHub ещё считает статистику в фоне — подождём и повторим.
            time.sleep(RETRY_SLEEP_BASE + attempt * 2)
            continue
        # 403/404/409 (пустой репозиторий) и т.п. — пропускаем репозиторий молча.
        return None
    return None  # так и не досчиталось за отведённые попытки


# ---- агрегация ----------------------------------------------------------------

def build_grid(repos):
    """Возвращает (grid[col][row], scanned, skipped, total) — сумму коммитов
    по всем репозиториям, выровненную по неделе (метка week из GitHub API)."""
    weeks = {}  # week_ts -> [7 int]
    scanned, skipped = 0, 0

    for repo in repos:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        data = fetch_commit_activity(full_name)
        if not data:
            skipped += 1
            continue
        scanned += 1
        for entry in data:
            ts = entry.get("week")
            if ts is None:
                continue
            days = entry.get("days") or [0] * 7
            bucket = weeks.setdefault(ts, [0] * 7)
            for i in range(7):
                bucket[i] += days[i] if i < len(days) else 0

    ordered_ts = sorted(weeks.keys())[-COLS:]
    grid = [weeks[ts] for ts in ordered_ts]
    # Если истории меньше 52 недель — дополняем пустыми колонками слева (старые).
    while len(grid) < COLS:
        grid.insert(0, [0] * ROWS)

    return grid, scanned, skipped, len(repos)


def _level_of(v, q1, q2, q3):
    if v <= 0:
        return 0
    if v <= q1:
        return 1
    if v <= q2:
        return 2
    if v <= q3:
        return 3
    return 4


def compute_levels(grid):
    """5 уровней: 0 = пусто, 1..4 — по квартилям ненулевых значений
    (либо, если распределение плоское, относительно максимума)."""
    flat_nonzero = sorted(v for col in grid for v in col if v > 0)
    if not flat_nonzero:
        return [[0] * ROWS for _ in grid], 0

    n = len(flat_nonzero)

    def quartile(p):
        idx = min(n - 1, max(0, math.ceil(p * n) - 1))
        return flat_nonzero[idx]

    q1, q2, q3 = quartile(0.25), quartile(0.50), quartile(0.75)
    mx = flat_nonzero[-1]
    if q1 == q3:  # распределение почти плоское — считаем пороги от максимума
        q1 = max(1, math.ceil(mx * 0.25))
        q2 = max(1, math.ceil(mx * 0.50))
        q3 = max(1, math.ceil(mx * 0.75))

    levels = [[_level_of(v, q1, q2, q3) for v in col] for col in grid]
    return levels, mx


# ---- SVG ------------------------------------------------------------------------

def _fmt(v):
    """Компактное число: целое без .0, иначе один знак после запятой (как в плейсхолдере)."""
    r = round(v, 1)
    if r == int(r):
        return str(int(r))
    return f"{r:.1f}"


def cell_xy(col, row):
    return GX + col * C, GY + row * C


def cell_center(col, row):
    x, y = cell_xy(col, row)
    off = (C - PAD) / 2
    return x + off, y + off


def render_svg(levels):
    width = GX + COLS * C + 4
    height = GY + ROWS * C + 4
    size = C - PAD

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="git-активность {ORG} за год со змейкой">',
        f"<title>git-активность {ORG}</title>",
    ]

    for col in range(COLS):
        for row in range(ROWS):
            x, y = cell_xy(col, row)
            level = levels[col][row]
            fill = RAMP[level]
            opacity = ' opacity="0.20"' if level == 0 else ""
            parts.append(
                f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{size}" height="{size}" '
                f'rx="2.5" fill="{fill}"{opacity}/>'
            )

    # Статичная декоративная змейка поверх нескольких ячеек — не завязана на данные,
    # как и в исходном плейсхолдере.
    snake_rows = [3, 2, 3, 4, 3, 2, 3, 4]
    start_col = max(0, min(15, COLS - len(snake_rows)))
    points = [cell_center(start_col + i, r) for i, r in enumerate(snake_rows)]
    stroke_w = size + 1
    pts_str = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)
    parts.append(
        f'<polyline points="{pts_str}" fill="none" stroke="#d8285a" '
        f'stroke-width="{stroke_w}" stroke-linecap="round" stroke-linejoin="round" opacity="0.92"/>'
    )
    hx, hy = points[-1]
    head_r = stroke_w / 2 + 1
    parts.append(f'<circle cx="{_fmt(hx)}" cy="{_fmt(hy)}" r="{head_r:.1f}" fill="#d8285a"/>')

    for row, label in sorted(DAY_LABELS.items()):
        baseline = GY + row * C + (C - PAD) - 2.5
        parts.append(f'<text x="3" y="{_fmt(baseline)}" font-size="9" fill="#8b95a1">{label}</text>')

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ---- main -------------------------------------------------------------------------

def main():
    if not TOKEN:
        print("GH_TOKEN не задан — обновление org-activity пропущено (no-op).")
        sys.exit(0)

    try:
        repos = fetch_org_repos(ORG)
        grid, scanned, skipped, total = build_grid(repos)
        levels, max_cell = compute_levels(grid)
        total_commits = sum(sum(col) for col in grid)

        svg = render_svg(levels)
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        with open(OUTPUT_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(svg)
    except Exception as exc:  # верхний уровень: не роняем пайплайн без диагностики
        print(f"org-activity: ошибка обновления — {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"org-activity: репозиториев всего {total}, просканировано {scanned}, "
        f"пропущено {skipped}; коммитов за 52 недели: {total_commits}; "
        f"максимум в ячейке: {max_cell}."
    )


if __name__ == "__main__":
    main()
