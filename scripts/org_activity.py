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
import random
import sys
import time
import urllib.error
import urllib.request
import zlib
from collections import deque

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


# ---- змейка (охотится за активностью и растёт, съедая её) ------------------------

BASE_LEN = 4              # сегментов до первой еды (индекс 0 — голова)
MAX_LEN_SPARSE = 20       # потолок роста на пустой сетке — рост видно, манёвра хватает
MAX_LEN_DENSE = 6         # на забитой длинная змейка замуровывает сама себя
DENSE_FOOD = 40           # граница «сетка забита» (клеток активности)
STEP_SEC = 0.06           # секунд на шаг (темп); длительность = шаги * это, с зажимом
DUR_MIN, DUR_MAX = 10.0, 30.0
WIGGLE_P = 0.3            # шанс случайного поворота на прямом участке (иначе скучно)
SNAKE_COLOR = "#d8285a"
LEAD = 4                  # клеток заезда/выезда ЗА кадром — петля замыкается невидимо


def _pct(v):
    """Компактное число для CSS — без хвостовых нулей."""
    r = round(v, 3)
    return str(int(r)) if r == int(r) else f"{r:g}"


def _seed_of(levels):
    """Стабильный seed из самих данных: одинаковая сетка → одинаковый путь.

    Без этого рандом менял бы SVG на каждом прогоне, и Action коммитил бы
    пустой дифф ежедневно. hash() не годится — он рандомизирован между запусками.
    """
    return zlib.crc32(repr(levels).encode("utf-8"))


def _max_len(food):
    """Потолок роста — по плотности сетки, а не одной константой на все случаи.

    Замер (свип по 4 сидам): при 60% плотности потолок 20 доедает 44% активности,
    потолок 6 — 90%; на разреженной (наш случай: ~22 клетки) оба дают 100%. Поэтому
    пока сетка пустая — растим длинно и это видно, а как забьётся — держим коротко,
    иначе змейка в 7 строк высотой сама себя замуровывает.
    """
    return MAX_LEN_SPARSE if food <= DENSE_FOOD else MAX_LEN_DENSE


def _in_grid(cell):
    return 0 <= cell[0] < COLS and 0 <= cell[1] < ROWS


def _neighbors(cell):
    c, r = cell
    return ((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1))


def _can_reach(start, goal, blocked):
    """BFS по свободным клеткам: дотянется ли голова до goal, минуя blocked."""
    seen = {start}
    q = deque([start])
    while q:
        for n in _neighbors(q.popleft()):
            if n == goal:
                return True
            if _in_grid(n) and n not in blocked and n not in seen:
                seen.add(n)
                q.append(n)
    return False


def _is_safe(nxt, body, body_len, grows):
    """Ход безопасен, если после него голова всё ещё видит свой хвост.

    Классическая эвристика змейки: пока путь до хвоста есть — змейка не заперта.
    Без неё на сетке высотой всего 7 клеток растущая змейка замуровывает себя почти
    сразу (проверено тестом: доедала 10 клеток из 29).
    """
    nb = deque(body)
    nb.append(nxt)
    limit = body_len + 1 if grows else body_len
    while len(nb) > limit:
        nb.popleft()
    tail = nb[0]
    blocked = set(nb)
    blocked.discard(tail)          # хвост уползёт — он не препятствие
    return _can_reach(nxt, tail, blocked)


def snake_path(levels, rng):
    """Путь головы: заезд из-за кадра → жадный обход ВСЕЙ активности → выезд за кадр.

    Правила как в оригинальной игре:
      * цель хода — ближайшая (манхэттен) недоеденная клетка, ничьи рвём рандомом;
      * идём лесенкой (случайная ось из тех, что не сошлись), а на ПРЯМЫХ участках
        ещё и виляем с шансом WIGGLE_P — иначе движение к еде на одной строке
        выродилось бы в скучную прямую;
      * **в себя не ходим**: занятые телом клетки запрещены, тело растёт на клетку
        за каждую съеденную (хвост не подтягивается в этот ход);
      * съедаем всё, на что наступили по дороге.

    Возвращает (cells, eaten_at) — путь и {клетка: индекс шага, на котором её съели}.
    """
    start_row = ROWS // 2
    cells = [(-i, start_row) for i in range(LEAD, 0, -1)]
    cur = cells[-1]
    remaining = {(c, r) for c in range(COLS) for r in range(ROWS) if levels[c][r] > 0}
    eaten_at = {}
    body = deque(cells)          # клетки, занятые змейкой (голова — последняя)
    body_len = BASE_LEN
    max_len = _max_len(len(remaining))
    guard = 40 * COLS * ROWS     # страховка от вечного цикла, если змейка заперлась
    stall = 0                    # подряд недостижимых целей — после 3 уходим с поля

    def free(cell):
        return _in_grid(cell) and cell not in body

    def step_once(target, wiggle, budget_left):
        """Один безопасный шаг к цели. False — ходить совсем некуда.

        Используется и на охоте, и на выезде: наивный «иди вправо» на выезде
        упирался в собственное тело, если змейка доела, свернувшись клубком.
        """
        nonlocal cur, body_len
        c, r = cur
        tc, tr = target
        toward = []
        if c != tc:
            toward.append((c + (1 if tc > c else -1), r))
        if r != tr:
            toward.append((c, r + (1 if tr > r else -1)))
        rng.shuffle(toward)

        prefer = list(toward)
        # Виляем только когда ход к цели прямой (одна ось) — иначе он и так лесенкой.
        if wiggle and len(toward) == 1 and budget_left > 8 and rng.random() < WIGGLE_P:
            side = [(c, r - 1), (c, r + 1)] if r == tr else [(c - 1, r), (c + 1, r)]
            rng.shuffle(side)
            prefer = side + prefer
        prefer = [x for x in prefer if free(x)]
        others = [x for x in _neighbors(cur) if free(x) and x not in prefer]
        rng.shuffle(others)

        pick = None
        for x in prefer + others:          # сначала ходы, после которых видим хвост
            if _is_safe(x, body, body_len, x in remaining):
                pick = x
                break
        if pick is None:                   # безопасных нет — хотя бы не в себя
            pick = (prefer + others)[0] if (prefer + others) else None
        if pick is None:
            return False

        cur = pick
        cells.append(cur)
        body.append(cur)
        if cur in remaining:               # съели клетку — растём на одну
            eaten_at[cur] = len(cells) - 1
            remaining.discard(cur)
            body_len = min(max_len, body_len + 1)
        while len(body) > body_len:
            body.popleft()
        return True

    while remaining and guard > 0 and stall < 3:
        target = min(
            remaining,
            key=lambda f: (abs(f[0] - cur[0]) + abs(f[1] - cur[1]), rng.random()),
        )
        budget = 6 * (abs(target[0] - cur[0]) + abs(target[1] - cur[1])) + 40
        ate_before = len(eaten_at)
        while cur != target and budget > 0 and guard > 0:
            budget -= 1
            guard -= 1
            if not step_once(target, True, budget):   # замуровалась — охота окончена
                guard = 0
                break
        # Прогресс меряем съеденным, а не достижением конкретной цели: в плотной каше
        # цель может быть недостижима, но змейка по дороге всё равно ест.
        stall = 0 if (cur == target or len(eaten_at) > ate_before) else stall + 1

    # Выезд. Уводим за край не только голову, но и ВЕСЬ хвост: сегмент k отстаёт на k
    # шагов, поэтому если гнать только голову, на 100% хвост ещё в сетке и петля
    # 100%->0% выглядит рывком.
    total_len = min(max_len, BASE_LEN + len(eaten_at))

    # 1) К правому краю — тем же безопасным шагом: змейка могла доесть, свернувшись
    #    клубком, и «иди вправо» напролом упирается в собственное тело.
    edge_budget = 8 * COLS + 80
    while _in_grid(cur) and cur[0] < COLS - 1 and edge_budget > 0:
        edge_budget -= 1
        if not step_once((COLS - 1, cur[1]), False, edge_budget):
            break

    # 2) Прямо за кадр: off-grid клетки телом не заняты, коллизий там нет.
    while cur[0] < COLS + total_len + 1:
        cur = (cur[0] + 1, cur[1])
        cells.append(cur)
        body.append(cur)
        while len(body) > body_len:
            body.popleft()
    return cells, eaten_at


def render_svg(levels):
    width = GX + COLS * C + 4
    height = GY + ROWS * C + 4
    size = C - PAD

    rng = random.Random(_seed_of(levels))
    path, eaten_at = snake_path(levels, rng)
    steps = max(1, len(path) - 1)
    duration = min(DUR_MAX, max(DUR_MIN, steps * STEP_SEC))
    step_pct = 100.0 / steps
    step_sec = duration / steps

    dur = _pct(duration)
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
            idx = eaten_at.get((col, row))
            if idx is None:      # змейка не добралась — клетка просто горит, без анимации
                parts.append(
                    f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{size}" height="{size}" '
                    f'rx="2.5" fill="{fill}"/>'
                )
                continue
            # Непустая клетка гаснет ровно в тот момент, когда её проходит голова.
            eat_pct = idx * step_pct
            regrow_at = min(100.0, eat_pct + step_pct * 0.8)
            name = f"e{col}_{row}"
            css.append(
                f"@keyframes {name}{{0%,{_pct(eat_pct)}%{{fill:{fill};opacity:1}}"
                f"{_pct(regrow_at)}%,100%{{fill:{RAMP[0]};opacity:.2}}}}"
            )
            parts.append(
                f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{size}" height="{size}" '
                f'rx="2.5" fill="{fill}" class="c" style="animation-name:{name}"/>'
            )

    for row, label in sorted(DAY_LABELS.items()):
        baseline = GY + row * C + (C - PAD) - 2.5
        parts.append(f'<text x="3" y="{_fmt(baseline)}" font-size="9" fill="#8b95a1">{label}</text>')

    # Рост: +1 сегмент за каждую съеденную клетку (как в оригинальной игре).
    # Сегмент k >= BASE_LEN «рождается» в момент, когда съедена (k - BASE_LEN)-я клетка,
    # и появляется у хвоста — там, где змейка в этот миг и удлиняется.
    eat_order = sorted(eaten_at.values())
    food_n = sum(1 for c in range(COLS) for r in range(ROWS) if levels[c][r] > 0)
    total_len = min(_max_len(food_n), BASE_LEN + len(eat_order))

    # Змейка живёт строго в поле клеток: заезд/выезд за кадром обрезаются клипом,
    # иначе на старте она едет по зоне подписей дней.
    parts.append(
        f'<clipPath id="gclip"><rect x="{GX}" y="{GY}" '
        f'width="{(COLS - 1) * C + size}" height="{(ROWS - 1) * C + size}"/></clipPath>'
    )
    parts.append('<g clip-path="url(#gclip)">')

    # Хвост рисуем первым, голову — последней (поверх остальных сегментов).
    for k in range(total_len - 1, -1, -1):
        op = 1.0 if k == 0 else max(0.5, 0.92 - k * 0.015)
        rx = size / 2 if k == 0 else 3
        if k < BASE_LEN:
            style = f"animation-delay:{k * step_sec:.3f}s;opacity:{_pct(op)}"
        else:
            born = eat_order[k - BASE_LEN] * step_pct
            css.append(
                f"@keyframes g{k}{{0%,{_pct(born)}%{{opacity:0}}"
                f"{_pct(min(100.0, born + step_pct * 0.5))}%,100%{{opacity:{_pct(op)}}}}}"
            )
            # Две анимации: путь (со сдвигом хвоста) и рождение (по абсолютному времени).
            style = f"animation-name:snk,g{k};animation-delay:{k * step_sec:.3f}s,0s"
        parts.append(
            f'<rect class="sg" style="{style}" x="0" y="0" '
            f'width="{size}" height="{size}" rx="{_fmt(rx)}"/>'
        )
    parts.append("</g>")

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
