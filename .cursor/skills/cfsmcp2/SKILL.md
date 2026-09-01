---
name: cfsmcp2
description: >-
  Install and use the cfsmcp2 MCP server for 1C configuration metadata, BSL code,
  links, SKD, and help search over hierarchical dumps or configuration reports.
  Use when the user mentions cfsmcp2, 1C metadata search, configuration dump
  index, MCP port 8559, or asks to explore 1C structure without live database
  access.
---

# cfsmcp2 — MCP по метаданным и коду 1С

## Что это

**cfsmcp2** — MCP-сервер и индекс локальной выгрузки или «Отчёта по конфигурации»
1С. Порт **8559**, endpoint `http://127.0.0.1:8559/mcp/`, имя **`cfsmcp2`**.

Один endpoint — много контекстов (CF/CFE); контексты добавляются в веб-UI
(http://127.0.0.1:8559/), не через новый MCP на каждую конфигурацию.

**Граница:** cfsmcp2 отвечает на **«как устроена система»** — метаданные, связи,
модули, СКД, справка. **Не** читает живую ИБ (записи, суммы, движения). Для
живых данных — MCP **livedata1c**, если доступен.

## Установить skill

Skill — правила поведения агента при вызове MCP tools. **Не поднимает** сервер
и **не** подключает MCP в клиенте (это — [AGENT-INSTALL.md](../../../AGENT-INSTALL.md)).

### Когда skill уже есть

Workspace — **клон cfsmcp2** (есть `docker-compose.yml`, `.cursor/skills/cfsmcp2/`):
ничего копировать не нужно. Агент читает `.cursor/skills/cfsmcp2/SKILL.md`.

### Куда класть (Cursor)

| Область | Путь | Когда |
|---------|------|-------|
| **Проект** | `<workspace>/.cursor/skills/cfsmcp2/` | Skill нужен только в этом репозитории |
| **Глобально** | Linux/macOS: `~/.cursor/skills/cfsmcp2/` | Skill во всех проектах на машине |
| | Windows: `%USERPROFILE%\.cursor\skills\cfsmcp2\` | |

В каталоге должны лежать `SKILL.md` и `reference.md`.

**Не** класть в `~/.cursor/skills-cursor/` — это служебный каталог Cursor.

### Способ 1 — команда агенту (рекомендуется)

Откройте **целевой** проект в Cursor и отправьте:

> Установи skill cfsmcp2 из `https://github.com/folonsd-ai/cfsmcp2`

Агент: прочитает этот `SKILL.md`, клонирует/обновит repo (во временную папку
или рядом), скопирует `.cursor/skills/cfsmcp2/` в выбранное место, **не затирая**
другие skills.

### Способ 2 — вручную

```bash
# 1. Взять skill из репозитория (один раз или для обновления)
git clone --depth 1 https://github.com/folonsd-ai/cfsmcp2.git /tmp/cfsmcp2
# или: cd cfsmcp2 && git pull

# 2a. В конкретный проект (Linux / macOS / Git Bash)
mkdir -p /path/to/your-project/.cursor/skills
cp -r /tmp/cfsmcp2/.cursor/skills/cfsmcp2 /path/to/your-project/.cursor/skills/

# 2b. Глобально (Linux / macOS)
mkdir -p ~/.cursor/skills
cp -r /tmp/cfsmcp2/.cursor/skills/cfsmcp2 ~/.cursor/skills/
```

Windows PowerShell (в проект):

```powershell
git clone --depth 1 https://github.com/folonsd-ai/cfsmcp2.git $env:TEMP\cfsmcp2
New-Item -ItemType Directory -Force -Path ".\.cursor\skills" | Out-Null
Copy-Item -Recurse -Force "$env:TEMP\cfsmcp2\.cursor\skills\cfsmcp2" ".\.cursor\skills\cfsmcp2"
```

Глобально на Windows:

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.cursor\skills" | Out-Null
Copy-Item -Recurse -Force "$env:TEMP\cfsmcp2\.cursor\skills\cfsmcp2" "$env:USERPROFILE\.cursor\skills\cfsmcp2"
```

### Обновление skill

Повторите копирование из свежего клона (способ 2) или команду агенту с `git pull`
в исходном репозитории. Перезапуск Cursor обычно не нужен; новый чат подхватит
обновлённый `SKILL.md`.

### Проверка

- Файл существует: `<целевой-путь>/.cursor/skills/cfsmcp2/SKILL.md`
- В новом чате Cursor агент может вызвать skill по имени **cfsmcp2** или по
  запросу про метаданные 1С через cfsmcp2.
- Для реальных MCP-вызовов сервер всё равно должен быть online (следующий раздел).

### Skill + MCP (полная связка)

1. **Skill** (этот раздел) — как вызывать tools.
2. **MCP-сервер** — [AGENT-INSTALL.md](../../../AGENT-INSTALL.md): Docker, UI,
   контексты `ready`.
3. **MCP в клиенте** — `http://127.0.0.1:8559/mcp/` в `.cursor/mcp.json`.

Команда «всё сразу» агенту:

> Установи skill cfsmcp2 из `https://github.com/folonsd-ai/cfsmcp2` и подними
> MCP-сервер по `AGENT-INSTALL.md`.

## Установить MCP-сервер

Skill ≠ работающий MCP. Сервер поднимается отдельно — полный протокол в
[AGENT-INSTALL.md](../../../AGENT-INSTALL.md) в корне репозитория.

Кратко:

1. Docker (по умолчанию) или native (venv) — см. AGENT-INSTALL.
2. Health: `GET http://127.0.0.1:8559/api/health` → `{"status":"ok",…}`.
3. MCP в клиенте (Cursor — merge, не затирать другие серверы):

```json
{
  "mcpServers": {
    "cfsmcp2": {
      "url": "http://127.0.0.1:8559/mcp/"
    }
  }
}
```

4. UI → добавить сущность → «Точка подключения» / локальный путь → дождаться
   `ready`.
5. LM Studio (`:1234`) — для `semantic_search`; без него работают FTS и карточки.

Команда пользователю для установки сервера:

> Установи cfsmcp2 из `https://github.com/folonsd-ai/cfsmcp2` по `AGENT-INSTALL.md`.

## Preflight перед работой с tools

1. MCP подключён и health OK.
2. `list_contexts` — только **enabled + ready** (если пользователь **не** назвал
   контекст).
3. Смотреть `profile` в ответе:
   - `source_mode=report` или `lean=true` / `bsl=false` — **нет** BSL, call graph,
     СКД, help из dump.
   - `source_mode=dump` + `bsl=true` — полный набор code-tools.
4. При сомнении в актуальности индекса: `get_indexing_status` → `index_stale`.

## Scoping контекста (обязательно)

- Пользователь **назвал** конфигурацию/расширение → **только** это имя во всех
  вызовах; **не** вызывать `list_contexts` и **не** повторять запрос по всем
  контекстам (fan-out).
- `get_*` tools — имя **одной** сущности, **никогда** `tag:…`.
- Поиск по группе тегов — только `context=tag:Имя` в **search**-tools и только
  если пользователь просил группу.
- `context` в `get_*` бери из поля `context` hit'а, не подставляй другое имя.

## Workflow

```
[контекст неизвестен]  list_contexts | list_context_groups → одно имя
[контекст известен]    сразу search/get в этом имени

search_metadata (FTS) → при слабом результате semantic_search
  → get_object | get_object_dossier (detail_level=L2 по умолчанию)
  → find_usages | trace_impact (summary=true; expand=paths при необходимости)
```

Остановись, когда ответ получен — не строй длинную цепочку без нужды.

## Маршруты по типу задачи

| Задача | Маршрут |
|--------|---------|
| Найти объект по имени | `search_metadata` → `get_object` |
| Смысл / синоним / «похожее» | `semantic_search` (если LM Studio online) |
| Точный идентификатор, FTS промах | retry без `literal`, или `literal=true` **с** `path_prefix` объекта |
| После корня документа/справочника | `search_under(parent_path=…)` или `path_prefix` |
| UUID из XML | `find_by_guid` (только dump) |
| Обязательность поля | `get_required_fields` |
| Связи / кто ссылается | `find_usages` (summary) → `get_links` |
| Impact по метаданным | `trace_impact` — type, movements, based_on, composition |
| Цепочка вызовов BSL | `trace_call_chain` (не путать с impact) |
| JOIN-путь между объектами | `get_query_path(source, target)` |
| Разница двух объектов | `compare_objects` |
| How-to / справка | `helpsearch` → `get_object` по `props.owner` |
| BSL: найти процедуру | `find_methods` → `get_method` |
| BSL: текст в модуле | `find_code_references(identifier, parent_path=модуль)` |
| Список модулей | `list_code_modules(q=…)` — **q обязателен**, ≥2 символа |
| СКД | `get_skd`; при `skd_hint` в search — вызвать по кандидату |
| Формы | `search_forms` → `get_object` на путь формы |

Полная таблица 26 tools — [reference.md](reference.md).

## Поиск: правила

- **FTS первым** для точных имён; `semantic_search` — если FTS пустой/слабый
  или запрос на естественном языке.
- `literal=true` — **только** один идентификатор (без пробелов) **и** конкретный
  `path_prefix` / `parent_path` объекта. Не на фразу, не на корень коллекции
  (`Документы`, `ОбщиеМодули`, …) → refused (`too_broad`).
- `find_methods` / `find_code_references`: корень коллекции как `parent_path` →
  `parent_too_broad`. Сначала сузь до модуля или объекта из prior hit.
- `compact=true` на search-tools; `get_object` / dossier — L2 default (attrs+ТЧ+
  movements); L0 только для полного `props`.
- Читай поле `comment` в hit'ах — там часто бизнес-правила.

## BSL и профиль ingest

- Code-tools (`find_methods`, `get_method`, `trace_call_chain`, …) — только
  **dump** с `bsl=true` в профиле.
- `get_method` / `find_code_references` при `index_stale` читают `.bsl` с диска.
- `trace_call_chain` — BFS по BSL `calls`; для регистров/d movements →
  `trace_impact` / `find_usages(link_type=movements)`.
- Если chain пуст — следуй `hint` / `next_tool` в ответе (обычно `trace_impact`).

## Чего НЕ делать

- Fan-out одного запроса по всем контекстам из `list_contexts`.
- `literal=true` на естественной фразе или без сужения path.
- `get_*` с `tag:…`.
- `trace_call_chain` вместо `trace_impact` для движений регистров.
- `find_methods` / BSL-tools в контексте `report` или `lean`.
- Искать «сколько документов в базе» — это livedata1c, не cfsmcp2.
- Чинить offline LM Studio правкой исходников cfsmcp2.

## Дополнительно

- Установка сервера: [AGENT-INSTALL.md](../../../AGENT-INSTALL.md)
- Архитектура и workflow: [ARCHITECTURE.md](../../../ARCHITECTURE.md) §6
- Tools (детали): [reference.md](reference.md)
