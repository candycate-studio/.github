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


# ---- змейка (snk-style: ползёт по сетке и «съедает» клетки) ----------------------

SNAKE_LEN = 5             # сегментов тела (индекс 0 — голова)
SNAKE_DURATION = 20.0     # секунд на полный проход сетки
SNAKE_COLOR = "#d8285a"
LEAD = 4                  # клеток заезда/выезда ЗА кадром — петля замыкается невидимо


def _pct(v):
    """Компактное число для CSS — без хвостовых нулей."""
    r = round(v, 3)
    return str(int(r)) if r == int(r) else f"{r:g}"


def snake_path():
    """Путь головы: заезд из-за левого края → серпантин по всем клеткам
    (колонка сверху-вниз, следующая снизу-вверх) → выезд за правый край.

    Оба конца пути лежат вне viewBox, поэтому «прыжок» петли 100%→0% зритель
    не видит, а съеденные клетки успевают отрасти обратно за кадром.
    """
    cells = [(-i, 0) for i in range(LEAD, 0, -1)]
    last_row = 0
    for col in range(COLS):
        rows = list(range(ROWS)) if col % 2 == 0 else list(range(ROWS - 1, -1, -1))
        cells.extend((col, row) for row in rows)
        last_row = rows[-1]
    cells.extend((COLS - 1 + i, last_row) for i in range(1, LEAD + 1))
    return cells


def render_svg(levels):
    width = GX + COLS * C + 4
    height = GY + ROWS * C + 4
    size = C - PAD

    path = snake_path()
    steps = len(path) - 1
    step_pct = 100.0 / steps
    step_sec = SNAKE_DURATION / steps
    idx_of = {cell: i for i, cell in enumerate(path) if 0 <= cell[0] < COLS}

    dur = _pct(SNAKE_DURATION)
    css = [
        f".c{{animation-duration:{dur}s;animation-timing-function:linear;"
        f"animation-iteration-count:infinite}}",
        f".sg{{transform-box:view-box;transform-origin:0 0;fill:{SNAKE_COLOR};"
        f"animation:snk {dur}s linear infinite}}",
    ]

    # Один @keyframes на всю змейку — сегменты расходятся через animation-delay.
    kf = []
    for i, (col, row) in enumerate(path):
        x, y = cell_xy(col, row)
        kf.append(f"{_pct(i * step_pct)}%{{transform:translate({_fmt(x)}px,{_fmt(y)}px)}}")
    css.append("@keyframes snk{" + "".join(kf) + "}")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="git-активность {ORG} за год: змейка съедает клетки коммитов">',
        f"<title>git-активность {ORG}</title>",
        "",  # место под <style> — заполняется ниже, когда css собран
    ]

    for col in range(COLS):
        for row in range(ROWS):
            x, y = cell_xy(col, row)
            level = levels[col][row]
            fill = RAMP[level]
            if level == 0:
                parts.append(
                    f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{size}" height="{size}" '
                    f'rx="2.5" fill="{fill}" opacity="0.20"/>'
                )
                continue
            # Непустая клетка гаснет ровно в тот момент, когда её проходит голова.
            eaten_at = idx_of[(col, row)] * step_pct
            regrow_at = min(100.0, eaten_at + step_pct * 0.8)
            name = f"e{col}_{row}"
            css.append(
                f"@keyframes {name}{{0%,{_pct(eaten_at)}%{{fill:{fill};opacity:1}}"
                f"{_pct(regrow_at)}%,100%{{fill:{RAMP[0]};opacity:.2}}}}"
            )
            parts.append(
                f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{size}" height="{size}" '
                f'rx="2.5" fill="{fill}" class="c" style="animation-name:{name}"/>'
            )

    for row, label in sorted(DAY_LABELS.items()):
        baseline = GY + row * C + (C - PAD) - 2.5
        parts.append(f'<text x="3" y="{_fmt(baseline)}" font-size="9" fill="#8b95a1">{label}</text>')

    # Хвост рисуем первым, голову — последней (поверх остальных сегментов).
    for k in range(SNAKE_LEN - 1, -1, -1):
        parts.append(
            f'<rect class="sg" style="animation-delay:{k * step_sec:.3f}s;'
            f'opacity:{_pct(round(1.0 - k * 0.16, 2))}" x="0" y="0" '
            f'width="{size}" height="{size}" rx="{_fmt(size / 2 if k == 0 else 3)}"/>'
        )

    css.append("@media(prefers-reduced-motion:reduce){.sg{display:none}.c{animation:none}}")
    parts[2] = "<style>" + "".join(css) + "</style>"

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
