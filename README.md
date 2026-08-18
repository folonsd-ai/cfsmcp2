# cfsmcp2

Версия **0.2.13**

> **Если ты ИИ-агент** и тебе нужно установить или обновить cfsmcp2, перейди к [AGENT-INSTALL.md](AGENT-INSTALL.md) и следуй инструкциям оттуда. Текущий файл — обзор для человека.

**Один MCP-сервер — на любое число конфигураций и расширений 1С.**  
В клиенте (Cursor, Claude Code, VS Code и др.) один endpoint; контексты добавляются в веб-UI, без нового MCP-сервера на каждую базу.

Индекс из Hierarchical-выгрузки (`dump`) или «Отчёта по конфигурации» (`report`): FTS, semantic-поиск, BSL, связи, СКД. Эмбеддинги — LM Studio + zvec. UI и Streamable HTTP MCP на одном порту **8559**.

Репозиторий: https://github.com/folonsd-ai/cfsmcp2 · архитектура: [ARCHITECTURE.md](ARCHITECTURE.md)

## Как попросить агента поставить cfsmcp2

Установка — протокол, который выполняет сам ИИ-агент. Откройте проект в Cursor и отправьте:

> Установи cfsmcp2 из `https://github.com/folonsd-ai/cfsmcp2` по `AGENT-INSTALL.md`.

Если репозиторий уже открыт в Cursor:

> Установи / обнови cfsmcp2 по `AGENT-INSTALL.md`.

Агент сам клонирует (если нужно), проверяет Docker и LM Studio. Если Docker нет — предложит [Docker Desktop](https://docs.docker.com/get-docker/) (на Linux — Engine + Compose) или native (venv). Если на `127.0.0.1:1234` нет ответа — предложит [LM Studio](https://lmstudio.ai/) (Local Server, порт 1234). Дальше: предложит точки подключения (`MOUNT_POINTS` + volumes), health, MCP. Выгрузку 1С указывают через UI «Точка подключения» — без zip на сервер.

Если нужен именно локальный venv без контейнера:

> Установи cfsmcp2 по `AGENT-INSTALL.md`, способ native.

### Fallback без агента (рекомендуется: Docker)

```bash
git clone --depth 1 https://github.com/folonsd-ai/cfsmcp2.git
cd cfsmcp2
```

1. В `docker-compose.yml` задайте volumes выгрузок и `MOUNT_POINTS` (пути **хоста** → `/mnt/…` в контейнере). Монтируйте **родителя** выгрузки (или каталог выше), не сам каталог с `Configuration.xml` — Обзор в UI не поднимается выше точки:
   ```yaml
   environment:
     MOUNT_POINTS: erp=/mnt/erp
   volumes:
     - ./data:/data
     - W:/1c/projects:/mnt/erp   # родитель; внутри — папки выгрузок
     # Linux/macOS: /home/user/1c:/mnt/erp
   ```
2. Таймзона контейнера (`TZ`, POSIX). Без неё логи и UI идут по UTC. По умолчанию — смещение **хоста** (`UTC-3` = UTC+3); другое — явно:
   ```bash
   # .env в корне (не коммитится):  TZ=UTC-3
   TZ=UTC-5 docker compose up -d --build   # Linux/macOS, UTC+5
   ```
   Windows PowerShell: `$env:TZ = "UTC-3"; docker compose up -d --build`
3. Запуск:
   ```bash
   docker compose up -d --build
   ```

Дальше: UI http://127.0.0.1:8559/ → LM Studio на хосте (`host.docker.internal:1234` из контейнера) → MCP `http://127.0.0.1:8559/mcp/` → в UI **«Точка подключения»** → Обзор внутри `/mnt/…`.

## Почему Docker (рекомендуемый способ)

| | Docker + точки | Native (venv) |
|---|---|---|
| Hierarchical-выгрузка | Volume + `MOUNT_POINTS` — без zip | Абсолютный путь / «Обзор…» на машине |
| Изоляция | Отдельный контейнер, данные в `./data` | Процесс Python на хосте |
| Обзор каталога | Browse внутри точки (`/mnt/…`) | Нативный диалог (tkinter, localhost) |
| LM Studio | `http://host.docker.internal:1234` | `http://127.0.0.1:1234` |
| Несколько выгрузок | Несколько named mounts (`erp`, `ut11`, `dumps`) | Несколько локальных путей |

Native — запасной вариант (нет Docker или нужна отладка на хосте). Без `MOUNT_POINTS` в Docker большая выгрузка зайдёт только upload zip/txt.

## Требования

- **Docker** (рекомендуется) или Python 3.13+ — Windows, macOS, Linux
- [LM Studio](https://lmstudio.ai/) Local Server с embedding-моделью (по умолчанию `text-embedding-multilingual-e5-small`)

## Точки подключения (`MOUNT_POINTS`)

Именованные корни, видимые процессу в контейнере. Формат: `id=/mnt/path` или `id=/mnt/path|Label`, разделители `,` / `;`.

- В compose: **volume** `хост:контейнер` и та же запись в `MOUNT_POINTS`.
- **Корень точки — не выгрузка.** Bind-mount — верхняя граница дерева: UI «Обзор» ходит только вниз от `/mnt/…`. В volume указывают родителя (или каталог выше); каталог с `Configuration.xml` выбирают уже в UI. Если смонтировать саму выгрузку, соседние деревья и смена источника без правки compose недоступны.
- UI (runtime=docker): выбор точки + browse (каталоги и `.txt`); путь в сущности — **контейнерный** (`/mnt/erp/...`), не путь хоста.
- `import-path` разрешает только пути **внутри** доступных точек.
- `RUNTIME_MODE=auto|docker|native` — автодетект Docker; override при необходимости.
- Если старый host-путь пропал после переезда — в «Источник» подсказки по имени папки.

Подробнее: [AGENT-INSTALL.md](AGENT-INSTALL.md), [ARCHITECTURE.md](ARCHITECTURE.md).

## Источники

| `source_mode` | Что это | Как лучше загрузить |
|---|---|---|
| `dump` | Hierarchical-выгрузка (`Configuration.xml` в корне) | **Точка подключения** (Docker) или локальный путь (native) — без zip |
| `report` | «Отчёт по конфигурации» (`.txt`) | Точка / путь к файлу или upload `.txt` |

Детект: в корне есть `Configuration.xml` → `dump`; явный `.txt` → `report`.

| `source_location` | Поведение |
|---|---|
| `path` | Абсолютный путь на машине **процесса**; **не копируется**; удаление сущности источник не трогает |
| `upload` | Копия в `./data` |

Профиль ingest задаётся при создании. По умолчанию **полная загрузка** (BSL full+chunks, справка, формы, СКД, макеты). Пресеты UI: «Модули» (meta+BSL signatures с calls), «Только метаданные». Глубина BSL: `signatures_only` / `signatures` / `full`. Смена профиля = новая сущность.

## MCP

Канон: `http://127.0.0.1:8559/mcp/` (со слэшем). Cursor — `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "cfsmcp2": {
      "url": "http://127.0.0.1:8559/mcp/"
    }
  }
}
```

Другие клиенты и переподключение — [AGENT-INSTALL.md](AGENT-INSTALL.md).

Если пользователь назвал конфигурацию — искать **только в ней**, без обхода всех `list_contexts`.

| Группа | Tools |
|---|---|
| Контексты | `list_contexts`, `list_context_groups` |
| Поиск | `search_metadata`, `search_under`, `semantic_search`, `helpsearch` (справка), `get_required_fields` |
| Объект | `get_object` (L2 default), `get_links`, `find_usages` (summary), `get_object_dossier` (L2), `trace_impact` (summary), `trace_call_chain` (BSL calls), `get_query_path` (JOIN-путь), `compare_objects` |
| BSL (`dump` + `bsl`) | `list_code_modules` (**`q` обязателен**, ≥2 символа), `list_methods`, `find_methods`, `get_method`, `get_module_structure`, `find_code_references`, `queue_method_reembed` |
| Формы | `search_forms`, `queue_managed_form_reembed` |
| СКД (`dump` + `dcs`) | `get_skd` |
| Статус | `get_indexing_status` |

`get_*` — имя **одной** сущности, не `tag:…`. Группа тегов — `context=tag:Имя` только в search-tools.

## UI (кратко)

Точка подключения (Docker) / локальный путь (native) или upload; бейджи Отчёт/Выгрузка и CF/CFE; один выключатель строки (`enabled`); копия ready-сущности; теги; статистика; ru/en. Настройки: вкладка **Tools** — список MCP tools с параметрами; в «Источнике» — «Сохранить» путь без перезагрузки; при создании — имя и комментарий.

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-or-later).

Copyright (C) 2026 folonsd-ai.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

If you modify cfsmcp2 and let others use it over a network (UI or MCP), AGPL requires that they can get the corresponding source. The canonical source is https://github.com/folonsd-ai/cfsmcp2 .
