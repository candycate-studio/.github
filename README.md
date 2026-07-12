# .github

Это `.github` репозиторий организации [candycate-studio](https://github.com/candycate-studio):
профиль (`profile/README.md`), community-health файлы и workflow'ы динамических виджетов.

## Виджеты профиля

| Файл | Что генерирует | Workflow |
|---|---|---|
| `profile/assets/org-activity.svg` | Heatmap git-активности (52×7) — агрегат коммитов по всем репозиториям Org | `.github/workflows/org-activity.yml` |
| `profile/assets/metrics.svg` | Карточка [lowlighter/metrics](https://github.com/lowlighter/metrics) — активность, языки, репозитории | `.github/workflows/metrics.yml` |

Скрипт heatmap'а — `scripts/org_activity.py` (Python 3, только stdlib).

## Требуемый секрет

Оба workflow'а используют **`ORG_METRICS_TOKEN`** — Personal Access Token (classic) со
скоупами `repo` + `read:org`. Без него оба workflow'а отрабатывают как **зелёный no-op**:
`org_activity.py` тихо завершается при пустом токене, а шаг `metrics` пропускается guard'ом.

Добавить: Settings → Secrets and variables → Actions → New secret с именем `ORG_METRICS_TOKEN`
(на уровне организации или репозитория).
