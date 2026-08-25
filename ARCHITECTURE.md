# ARCHITECTURE — cfsmcp2

Версия документа: **1.5** · дата: **2026-08-25** · продукт **0.2.18**

---

## 1. Назначение и границы

**cfsmcp2** — MCP-сервер и веб-UI для поиска по метаданным и коду конфигураций и расширений **1С:Предприятие 8.3**. Одна **сущность** (контекст MCP) = одна конфигурация (CF) или расширение (CFE), построенная из одного источника в одном из двух режимов: текстовый отчёт (`report`) или Hierarchical-выгрузка в файлы (`dump`).

| Аспект | cfsmcp2 |
|---|---|
| Порт / MCP-имя | **8559** / **`cfsmcp2`** |
| Источники | `source_mode`: `report` \| `dump`; BSL из той же Hierarchical-выгрузки |
| Профиль ingest | JSON на сущность: metadata, BSL, СКД, макеты, формы, exclude |
| Свойства объектов (dump) | Полное снятие `<Properties>` из XML |
| Path-режим | `source_location=path` без копирования; в Docker — **точки** `MOUNT_POINTS` |
| MCP | **26 tools**: metadata, BSL, СКД, справка (`helpsearch`), формы (`search_forms`), usages/dossier/impact, `trace_call_chain`, `get_query_path`, `compare_objects`, `search_under`, `get_required_fields`, очереди reembed |

**Граница v1:** upload или путь, видимый процессу; **нет** вызова платформы 1С для выгрузки, **нет** аутентификации MCP, **нет** слияния CF+CFE в один индекс.

Рекомендуемый runtime — **Docker** с `MOUNT_POINTS` + volumes (см. §3.4). Native (venv) — запасной путь.

---

## 2. Стек и runtime

```
┌─────────────────────────────────────────────────────────────┐
│  Browser UI (index.html, i18n.js, ide-console.css, JSZip, Chart.js) │
│  GET /  ·  /static/                                         │
├─────────────────────────────────────────────────────────────┤
│  FastAPI (app/main.py)  version 0.2.18                      │
│  /api/*  ·  /mcp/  (Streamable HTTP, FastMCP)               │
├──────────────┬──────────────────────┬───────────────────────┤
│  SQLite      │  zvec (per entity)   │  LM Studio :1234      │
│  objects,    │  FTS + HNSW vectors  │  embeddings           │
│  links,      │  data/zvec/e{id}__*  │  (runtime settings)   │
│  entities    │                      │                       │
└──────────────┴──────────────────────┴───────────────────────┘
         ./data/metadata  ·  ./data/dumps  ·  ./data/cfsmcp2.sqlite3
```

| Компонент | Реализация |
|---|---|
| HTTP / API | **FastAPI** + **uvicorn**, порт **8559** (`HOST`, `PORT`) |
| MCP | **FastMCP** (`app/mcp/server.py`), mount `/mcp`, transport `streamable-http`, имя **`cfsmcp2`** |
| Хранилище метаданных | **SQLite** (`DB_PATH`, default `./data/cfsmcp2.sqlite3`) |
| Полнотекст + семантика | **zvec** — коллекция на сущность и модель эмбеддинга |
| Эмбеддинги | **LM Studio** OpenAI-compatible API (`LM_STUDIO_URL`, workers 1/2/4/8/12/16) |
| Python | **3.13+** |
| Контейнер | **Docker** (`docker-compose.yml`): `./data:/data`, **`MOUNT_POINTS`** + bind-mounts выгрузок, LM Studio через `host.docker.internal:1234` — **рекомендуемый** способ |
| Native-run | `python app/main.py` из venv — запасной путь (локальный «Обзор…») |

Один процесс обслуживает UI, REST API и MCP на одном порту.

---

## 3. Режимы источника

### 3.1. `source_mode`

| `source_mode` | Источник | Что парсится |
|---|---|---|
| `report` | Текстовый «Отчёт по конфигурации» (`.txt`) | Дерево отчёта → objects/links; BSL/СКД/формы **недоступны** |
| `dump` | Hierarchical-выгрузка (каталог с `Configuration.xml`) | Metadata XML, опционально формы/СКД/макеты/BSL по профилю |

**Детект C1** (клиент и `import-path`):

- В корне каталога есть `Configuration.xml` → **`dump`** (отчёт рядом игнорируется).
- Явный выбор `.txt` → **`report`**.
- Каталог с отчётом, но без `Configuration.xml` → **`report`**.
- Гибрида «report + пакет выгрузки» **нет**.

### 3.2. `source_location`: upload vs path

| `source_location` | Как создаётся | Где лежит источник | Delete сущности |
|---|---|---|---|
| `upload` | `POST /api/entities/upload`, `upload-dump` | Копия в `./data`: `metadata/e{id}.txt` или `dumps/e{id}/` + `e{id}.zip` | Удаляются артефакты cfsmcp2 |
| `path` | `POST /api/entities/import-path` (UI: «Точка подключения» / «Локальный путь») | **Внешний** абсолютный путь процесса: `file_path` / `dumps_dir`, **без копирования** | Внешний файл/каталог **не трогается** (guard) |

**Report path:** `source_path` — абсолютный путь к `.txt`; parse читает файл с диска.

**Dump path:** `source_path` — абсолютный каталог с `Configuration.xml`; `dumps_dir` = этот каталог, `zip_path=''`.

**Обновление с `entity_id`:**

- `upload` / `upload-dump` — остаётся `source_location=upload`, заменяются серверные артефакты.
- `import-path` с `entity_id` — сущность **переводится** в `source_location=path`; старые `dumps/e{id}/`, `e{id}.zip`, audit удаляются.

На localhost UI умеет нативный выбор каталога: `POST /api/system/pick-path` (только loopback; в Docker отключён). В Docker — browse по точке: `GET /api/system/browse`.

### 3.3. `MOUNT_POINTS` и `PATH_ALLOWED_ROOTS`

**`MOUNT_POINTS`** (рекомендуемый способ в Docker): именованные точки `id=/path` или `id=/path|Label`, разделители `,` / `;`.

Пример: `erp=/mnt/erp,ut11=/mnt/ut11,dumps=/mnt/dumps`.

- В `docker-compose` те же пути должны быть **volumes** (host → `/mnt/…`).
- Host-путь volume — **родитель выгрузки** (или выше), не каталог с `Configuration.xml`. Точка — корень дерева в контейнере; UI browse не поднимается выше `/mnt/…`. Саму выгрузку выбирают в UI внутри точки.
- UI в runtime=docker показывает выбор точки + browse (каталоги и `.txt`).
- `import-path`: путь обязан лежать **внутри** одной из доступных точек (точка ∪ вложенные).
- Если точки заданы, но ни одна не смонтирована → 400.

**`PATH_ALLOWED_ROOTS`**: whitelist без имён; используется в native / если `MOUNT_POINTS` не активны. Пустой список = без ограничения (warning).

**`RUNTIME_MODE`**: `auto` (детект `/.dockerenv` / cgroup) | `docker` | `native`.

API: `GET /api/system/runtime`, `GET /api/system/browse?mount_id=&rel=`, `GET /api/system/suggest-path` (миграция: старый путь пропал → подсказки по имени папки), `GET /api/system/peek-source?source_path=` (имя/синоним/версия/тип из `Configuration.xml` или отчёта **без ingest** — для превью перед загрузкой).

### 3.4. Docker vs native-run

| Сценарий | Рекомендация |
|---|---|
| Типовая установка / выгрузки на хосте | **Docker** + volumes + **`MOUNT_POINTS`**; UI — точки |
| Несколько конфигураций / дисков | Несколько named mounts (`erp`, `ut11`, `dumps`) |
| Нет Docker / отладка на хосте | Native + локальный путь / Обзор |
| Только маленький upload zip/txt | Docker или native без mounts |

В сущности `source_path` всегда путь **процесса** (`/mnt/erp/...` в Docker), не путь хоста.

---

## 4. Пайплайн

### 4.1. Статусы сущности

```
uploaded → parsing → parsed → indexing → ready
                ↘ parse_error          ↘ index_error
```

| Статус | Смысл |
|---|---|
| `uploaded` | Файл/zip принят, parse ещё не начат или в очереди |
| `parsing` | Идёт parse; прогресс в **процентах**: `indexed_count` = 0..99, `index_target` = 100, плюс `parse_added` / `parse_changed` / … |
| `parsed` | SQLite заполнен; запускается auto-reindex |
| `indexing` | Эмбеддинги + zvec (`indexed_count` / `index_target`) |
| `ready` | Доступна в MCP (`list_contexts` — только enabled + ready) |
| `parse_error` / `index_error` | Ошибка с `error_message` |

После успешного parse вызывается **auto reindex** (`pipeline.reindex_entity`).

Во время parse `indexed_count`/`index_target` несут **проценты** фазы (0..99/100) по этапам (report: по байтам файла; dump: metadata → assets → справка → BSL); UI показывает «N% · N objs · N links». После перехода в `indexing` они снова означают счётчик объектов.

При старте процесса сущности в статусе `indexing` считаются прерванными: `recover_interrupted_indexing()` продолжает оставшиеся эмбеддинги (`resume=True`), без полного пересчёта.

**«Обновить»:** повторный upload того же типа с `entity_id` **или** `import-path`; режим и **ingest_profile не меняются**; merge по `path` + `content_hash`. Для dump/report из каталога (path / точка монтирования) файлы с тем же `mtime`+`size` **не перечитываются** (`dump_file_state` + `objects.source_rel`); если все stamp совпали — дерево dump не обходится; связи живущих файлов сохраняются; неизменённый `ready` не запускает reindex. Zip-upload по-прежнему полный разбор (каталог переписывается, даты новые).

**Копия:** `POST /api/entities/{id}/copy` — только `ready`; новая сущность с другим именем, теги копируются.

### 4.2. Ветвление parse

**Report** (`_parse_report_entity`):

1. Декодирование txt (UTF-16LE BOM / UTF-8 / cp1251).
2. Streaming узлов отчёта → objects/links батчами в SQLite.
3. `exclude_name_substrings` (каскадно, регистронезависимо).
4. Auto reindex.

**Dump** (`_parse_dump_entity`):

```
Configuration.xml + object XML (dump_parser)
        ↓
[если профиль] формы / СКД / XML-макеты (dump_assets_parser)
        ↓
[если профиль help] справка **/Ext/Help/*.html (help_parser) → kind=Help
        ↓
[если профиль bsl]     BSL (*.bsl)          → методы; CALLS при signatures|full (не signatures_only)
[если профиль ordinary_forms] Ext/Form.bin  → UTF-8 модуль (form_bin.py; zlib fallback) → методы
        ↓
merge / delete orphans → parsed → reindex
```

Порядок в dump: **metadata → assets (forms/СКД/макеты) → справка → BSL/Form.bin** — в одном parse-проходе, без отдельного шага «загрузить модули».

Обычные (неуправляемые) формы (`Ext/Form.bin`) — отдельный флаг профиля `ordinary_forms` (**по умолчанию on**). Флаг `bsl` означает только `*.bsl`.

Индексация BSL при parse определяется **только** флагами профиля `bsl`/`ordinary_forms`, не runtime-toggle строки.

### 4.3. Reindex

- Эмбеддинги через LM Studio; batch/workers из settings (workers 1/2/4/8/12/16).
- zvec: FTS + HNSW cosine; коллекция `data/zvec/e{id}__{model}`; запись батчами ≤1000 docs.
- Incremental merge; overlap LM embed волны N+1 с записью zvec волны N; `bsl_embed_gen` для повторного embed методов.
- При сбое — fallback full rebuild; после рестарта процесса — resume оставшихся `embed_done=0`.
- `bsl_embed_mode`: `meta` / `body` / `chunks` (при `signatures` / `signatures_only` → всегда `meta`).
- В verbose-логе индексация **сворачивается** (~10 с): `index:progress` с `objects`, `passages`, `last=` (для методов — `ParentPath · MethodName`).
- Parse и reindex логируют **разбивку времени по фазам** (`setup`/`embed`/`zvec` для индекса; этапы для parse) строкой `… timing entity=N …`.

---

## 5. Модель данных

### 5.1. Entity (таблица `entities`)

| Поле | Описание |
|---|---|
| `source_mode` | `report` \| `dump` |
| `source_location` | `upload` \| `path` |
| `source_path` | Строка пути для UI и «Обновить» |
| `ingest_profile` | JSON профиля (см. §5.4) |
| `entity_type` | `configuration` \| `extension` (авто из метаданных) |
| `file_path` | Путь к txt (report) |
| `dumps_dir` | Распакованная выгрузка или внешний каталог (path) |
| `zip_path` | Zip-исходник (upload-dump); пуст в path-режиме |
| `status`, `enabled`, `bsl_enabled` | Пайплайн и MCP; **`enabled` ≡ `bsl_enabled`** (один toggle) |
| `bsl_embed_gen` | Поколение embed методов (очередь `queue_method_reembed`) |
| `model`, tags, parse_*, index_* | Модель эмбеддинга, теги, прогресс parse/index |

CFE-детект: непустой `ConfigurationExtensionPurpose` или `ObjectBelonging` корня `Adopted`/`Borrowed`. Тег `ConfigurationExtensionCompatibilityMode` **не** достаточен (он есть и у обычных CF).

### 5.2. Objects / links / tags

**`objects`:** `path`, `kind`, `name`, `synonym`, `comment`, `belong`, `props_json`, `content_hash`, `parse_gen`, `source_rel` (относительный файл dump), …

**`dump_file_state`:** снимок `rel_path` → `mtime_ns`+`size` для skip parse.

**`links`:** `link_type` — `type`, `movements`, `based_on`, `composition`, `calls`, …

**`tags`:** группировка контекстов; поиск `context=tag:Имя`.

### 5.3. Kinds и RU-канон путей

| Сущность | Пример пути |
|---|---|
| Объект метаданных | `Справочники.Номенклатура` |
| Реквизит | `Справочники.X.Реквизиты.Y` |
| ТЧ / реквизит ТЧ | `….ТабличныеЧасти.Z` / `….ТабличныеЧасти.Z.Реквизиты.W` |
| Значение перечисления | `Перечисления.X.ЗначенияПеречисления.Y` |
| Измерение/ресурс регистра | `Регистры.X.Измерения.Y` |
| Форма | `….Формы.Имя` / `ОбщиеФормы.Имя` |
| Макет / СКД | `….Макеты.Имя` |
| BSL-метод | `….Методы.{Module\|Form\|…}.{ИмяМетода}` |
| Общая картинка | `ОбщиеКартинки.X` |

**Kinds (dump, по профилю):**

| kind | Источник |
|---|---|
| `Catalog`, `Document`, … | Metadata XML |
| `Procedure`, `Function` | BSL |
| `ManagedForm` | `Form.xml` |
| `DataCompositionSchema` | `Template.xml` (корень `DataCompositionSchema`) |
| `SpreadsheetTemplate` | `Template.xml` (корень не СКД) |
| `Help` | Пользовательская справка `**/Ext/Help/*.html` (каждый раздел h2/h3 — отдельный record; `props.owner` — объект-владелец) |

Dump **богаче** report по `props_json` (полные XML-Properties); паритет по **путям и связям** для `get_object` / `get_links`.

### 5.4. Ingest profile (`ingest_profile`)

Задаётся **при создании**, на «Обновить» не меняется.

| Ключ | Default | Назначение |
|---|---|---|
| `metadata` | **on** | Объекты, реквизиты, типы, связи, CommonPictures |
| `bsl` | **on** | `*.bsl`; только для `dump` (Form.bin — отдельный флаг `ordinary_forms`) |
| `ordinary_forms` | **on** | Текст модулей обычных форм (`**/Ext/Form.bin`) → методы BSL; только для `dump` |
| `help` | **on** | Пользовательская справка `**/Ext/Help/*.html` → kind=Help (`helpsearch`); только для `dump` |
| `dcs` | **on** | СКД → индекс + `get_skd` |
| `spreadsheet_templates` | **on** | XML-макеты (не СКД) |
| `managed_forms` | **on** | `Form.xml` |
| `exclude_name_substrings` | `АрхивныеАлгоритмы`, `РегламентированныйОтчет`, `Драйвер`, `КомпонентаИнтеграции` | Каскадное исключение по подстроке **имени** |
| `bsl_load_mode` | `full` | `signatures_only` / `signatures` / `full` (legacy `code`→`full`) |
| `bsl_embed_mode` | `chunks` | `meta` / `body` / `chunks` |

**UI-пресеты** (диалог загрузки dump): **Полная загрузка** = default выше; **Модули** — meta+BSL `signatures` (с calls); **Только метаданные** — без BSL. Глубина BSL: `signatures_only` (без calls) / `signatures` (с calls) / `full`.

Для `report`: опции BSL/СКД/формы/макетов игнорируются (нет выгрузки).

---

## 6. MCP tools

**Всего: 26 tools** (`app/mcp/server.py`).

Контракт: metadata/code tools стабильны; новые — аддитивно. Формы и XML-макеты — через `get_object`; СКД — отдельный `get_skd`.

Инструкции сервера (FastMCP `instructions`) запрещают fan-out: если пользователь назвал контекст — искать **только в нём**, не обходить все имена из `list_contexts`.

### 6.1. Metadata и контексты

| Tool | Назначение |
|---|---|
| `list_contexts` | Enabled + **ready** контексты с tags |
| `list_context_groups` | Теги для `context=tag:…` в search |
| `search_metadata` | FTS; prefix-expand длинных токенов; SQL LIKE fallback; ranking (kind/coverage/diversity при `tag:`; интенты, домены, Help). **`literal=true`**: mid-string SQL (name/path/synonym; comment при `path_prefix` или длинном query), без FTS |
| `find_by_guid` | UUID → path/kind (`objects.guid` после reparse; иначе ConfigDumpInfo.xml на диске). Только dump |
| `search_under` | FTS в поддереве `parent_path` (после нахождения корня документа/справочника) |
| `semantic_search` | Hybrid zvec + FTS (RRF) + тот же ranking; `path_prefix`, `compact` |
| `helpsearch` | Поиск по **пользовательской справке** (`**/Ext/Help/*.html`, kind=Help): хит — раздел (`comment` = текст, `props.owner` — объект-владелец); гибрид vector+FTS; опция `owner` — сузить до объекта |
| `get_required_fields` | FillChecking + `МассивОбязательных*` + `МассивНепроверяемыхРеквизитов` + реквизиты по хоз.операции |
| `get_object` | Карточка; `detail_level` L3 / **L2** (default, attrs+ТЧ+movements) / L0 (полный props) |
| `get_links` | Входящие/исходящие связи |
| `get_query_path` | Карта **JOIN-пути** source→target (+ `fallbacks`): атрибуты/колонки ТЧ со связью типов + готовые методы `*ТекстЗапроса`; отличает «нет реквизита» от «нет данных» |

У search-hits: `comment` (часто бизнес-правила), при неоднозначных `ТитулN` — `title_candidates`.

### 6.2. Аналитика

| Tool | Назначение |
|---|---|
| `find_usages` | Входящие: `type`, `movements`, `based_on`, `composition`, `calls`; default **summary** (owner+count), `expand=paths` |
| `get_object_dossier` | Паспорт: default L2 attrs+ТЧ+movements + usage counts; L0 / `expand=paths` — полная детализация |
| `trace_impact` | BFS по links, depth 1..5, `down`/`up`; default summary owners; CALLS при BSL-ingest (и signatures) |
| `trace_call_chain` | BFS по resolved BSL `calls` (default on; выкл. `experimental_call_chain`); не замена `trace_impact` |
| `compare_objects` | Лёгкое сравнение двух корней: реквизиты / ТЧ / движения / заголовки Help — для «в чём разница между A и B» |
| `get_indexing_status` | **Все** сущности (не только ready); для dump: `index_stale` при drift `Configuration.xml` / `ConfigDumpInfo.xml` |

### 6.3. Code (dump, профиль `bsl`, контекст enabled)

| Tool | Назначение |
|---|---|
| `list_code_modules` | Модули с `methods_count`; **обязателен `q`** (≥2 символа; полный список не отдаётся) |
| `list_methods` | Список методов, фильтры |
| `find_methods` | Exact/prefix → при &lt;3 hits **fuzzy** (zvec RRF); **`literal=true`**: mid-string name/path/signature, без fuzzy |
| `get_method` | Метод; `body` из индекса (`full`) или с диска; при файле новее stamp — `index_stale=true`, body с диска |
| `get_module_structure` | Методы, `#Область`, экспорты, номера строк |
| `find_code_references` | Подстрока в телах: индекс при `full`, иначе grep `.bsl`; arg **`identifier`**; **`literal=true`** предпочитает диск |
| `queue_method_reembed` | Пометить методы сущности на повторный embed (`bsl_embed_gen`) |

### 6.4. СКД и формы (dump)

| Tool | Назначение |
|---|---|
| `get_skd(context, path)` | `data_sets`, `fields`, `parameters`, `settings_variants` |
| `search_forms` | Поиск управляемых форм |
| `queue_managed_form_reembed` | Повторный embed форм после смены текста/кода |

### 6.5. Нормализация входа

Tools `get_object`, `find_usages`, `trace_impact`, `get_object_dossier` принимают RU/EN формы и refs: `СправочникСсылка.X`, `CatalogRef.X`, `Catalogs.X`, вложенность `Attributes↔Реквизиты`, `Forms↔Формы`, … — ядро `app/services/refs.py`.

### 6.6. Workflow для агента

```
[контекст неизвестен] list_contexts / list_context_groups → одно имя
[контекст известен]   сразу search/get в этом имени (без fan-out)

search_metadata | semantic_search   (context может быть tag:…; compact=true)
  → после FTS miss по точному id/фрагменту: literal=true
  → UUID из XML / ConfigDumpInfo → find_by_guid
  → после корня объекта: search_under(parent_path) / path_prefix
  → «обязательно ли поле?» get_required_fields
  → get_object | get_object_dossier  (detail_level=L2)
  → find_usages | trace_impact       (summary=true; expand=paths при необходимости)
  → [dump+профиль help] helpsearch   («как заполнить/настроить», owner=…)
  → «в чём разница между A и B» → compare_objects
  → «как получить данные X из A» → get_query_path(source, target, fallbacks)
     (context = имя сущности из hit, не tag:)
  → [dump+BSL] list_code_modules(q=…) → find_methods → get_method
     (find_methods/find_code_references: literal=true; get_method: index_stale)
  → [dump] search_forms / get_skd
```

`get_*` tools **не принимают** `tag:…` — только имя одной сущности.

---

## 7. UI (кратко)

Одностраничное приложение: `app/static/index.html`, `i18n.js` (ru/en), стили `ide-console.css` (ключ темы: `cfsmcp2-theme`).

| Элемент | Поведение |
|---|---|
| Способ доставки | **Локальный путь** (import-path) или upload; для каталога upload → allowlist → zip (DEFLATE level 1) → upload-dump |
| Диалог профиля | Перед созданием из каталога; metadata+BSL+**справка** on; СКД/макеты/формы/Form.bin off; поля **имени** и **комментария** (автоподстановка по `peek-source` перед загрузкой) |
| Таблица | Бейджи **Отчёт**/**Выгрузка**, **CF**/**CFE**; `source_path`; метки профиля; parse-прогресс в %; status-ring |
| MCP run | **Один** play-toggle на строку (`enabled`); отдельного BSL-toggle **нет** |
| BSL | Счётчик методов + глубина (`signatures_only` / `signatures` / `full`) в профиле / подсказке; обычные формы — флаг `ordinary_forms` |
| Tags | OR-фильтр; batch enable/disable |
| Копия | Готовую сущность можно скопировать под новым именем |
| «Источник конфигурации» | Editable path + **«Сохранить»** (PATCH `source_path` **без ingest** — смена пути/точки) + «Загрузить»; DESIGNER/ibcmd **disabled**; «Загрузить на MCP» = refresh upload-flow |
| Настройки | Вкладки **Общие** / **Tools**: список MCP tools (`GET /api/settings/tools`, `lang=ru|en`) — имя, описание, параметры (тип, обязательность, дефолт) |
| Poll | Интервал из настроек (по умолчанию 2 с); `GET /api/entities`, `/api/tags`, `/api/health`, `/api/settings/models`, `/api/system/runtime`, `/api/system/browse` в app-лог не пишутся |
| Тикер | Быстрая панель у лого: MCP / slow / errors за **последние 20 мин**; индекс занят — сейчас |
| Статистика | События + лог (`minimal` / `errors` / `verbose`); клик по строке — полный текст; выгрузка `/api/stats/log.txt` (`source=ram\|db\|all`). Persist: таблица `app_log` (`error` / `slow` / `info`), настройки `slow_request_ms` / `app_log_retain_days` / `app_log_max_rows`. Slow/error MCP: `args` JSON + `result_summary` для offline-replay |

---

## 8. Безопасность dump zip

Реализация: `app/services/dump_zip.py`, лимиты — `app/core/config.py`.

| Угроза | Защита |
|---|---|
| Path traversal | Только относительные пути; запрет `..` и абсолютных; результат внутри `dumps/e{id}/` |
| Zip bomb | `zip_max_entries` (200k), `zip_max_uncompressed_bytes` (20 ГБ), `zip_max_file_bytes` (2 ГБ), `zip_max_compression_ratio` (2000) |
| Лишние файлы | Извлекается **только allowlist** профиля: `*.xml` (кроме `**/Help/**`), `*.bsl` при `bsl`, `**/Ext/Form.bin` при `ordinary_forms`, `**/Help/*.html`/`*.htm` при `help`; бинарники картинок, `Template.bin`, HTML вне справки — пропуск |
| Кодировки имён | UTF-8 / cp866 / cp437 |

Превышение лимита → **400** (`ZipSafetyError`) с понятным сообщением.

Клиент (JS) фильтрует каталог по тому же allowlist **до** упаковки zip. Path-режим zip не использует.

`pick-path` — только если клиент **loopback**.

---

## 9. Вне текущего контура

| Функция | Почему не сейчас |
|---|---|
| Вызов **DESIGNER** / **ibcmd** для выгрузки | Нужны платформа 1С, строка ИБ, безопасный subprocess; в UI только каркас |
| **`bsl_scope_members`** | Нужна внешняя база платформенных методов |
| **Cross-encoder rerank** | zvec RRF уже даёт hybrid search |
| **Chat-LLM** (`answer_metadata_question`) | Отдельный продуктовый контур |
| **`compare_base_and_extension`** | Нужны две сущности + diff-модель |
| **XSD** | Нет источника XSD в пайплайне |
| **`get_form`** (дерево формы) | Сейчас: `ManagedForm` + `get_object` / `search_forms` |
| **Persisted directory handle** (File System Access API) | UX «Обновить» без повторного диалога |
| Бинарные MXL (`Template.bin`) | Не разбираются |
| Аутентификация MCP | Нет |
| Reindex через MCP | Только REST/UI (очереди reembed — да: `queue_*_reembed`) |

---

## 10. Операционка

### 10.1. Каталоги данных (`./data`)

| Путь | Содержимое |
|---|---|
| `metadata/` | `e{id}.txt`, `e{id}.dump.audit.jsonl`, pending BSL zips |
| `dumps/` | `e{id}/` (распакованный dump), `e{id}.zip` |
| `cfsmcp2.sqlite3` | SQLite (в т.ч. `app_settings`) |
| `zvec/` | Векторные коллекции |

Каталог `data/` в git не хранится (кроме `.gitkeep`). Docker: volume `./data:/data`; env `METADATA_DIR`, `DUMPS_DIR`, `DB_PATH`, `ZVEC_DIR`.

### 10.2. Переменные окружения (основные)

| Env | Default | Назначение |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind |
| `PORT` | `8559` | HTTP |
| `PATH_ALLOWED_ROOTS` | (пусто) | Корни для `import-path` (если нет `MOUNT_POINTS`) |
| `MOUNT_POINTS` | (пусто) | Именованные точки `id=/mnt/...` (Docker) |
| `RUNTIME_MODE` | `auto` | `auto` \| `docker` \| `native` |
| `LM_STUDIO_URL` | `http://127.0.0.1:1234` | Эмбеддинги |
| `TZ` | смещение хоста; запас `UTC-3` | POSIX-таймзона процесса в Docker (логи, `datetime.now()`, UI). `UTC-3` = UTC+3. Имена вроде `Europe/Moscow` без пакета tzdata не работают; tzdata в образ не кладём |
| `EMBEDDING_WORKERS` | `2` | Параллелизм reindex (1/2/4/8/12/16) |
| `ZIP_MAX_*` | см. config | Лимиты zip (опционально переопределить) |

Живые настройки UI (URL LM Studio, модель, workers, poll, `log_level`, окно статистики) — таблица `app_settings`, не только env.

### 10.3. Health и MCP URL

| Endpoint | Ответ |
|---|---|
| `GET /api/health` | `{"status":"ok","service":"cfsmcp2"}` |
| MCP | `http://<host>:8559/mcp/` (Streamable HTTP; `/mcp` → `/mcp/` middleware) |

### 10.4. Запуск

```bash
# Рекомендуется: Docker + MOUNT_POINTS (см. docker-compose.yml)
docker compose up -d --build

# Запасной путь: native
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
.venv\Scripts\python app\main.py
```

Подробный протокол: [AGENT-INSTALL.md](AGENT-INSTALL.md). Краткий старт: [README.md](README.md).

### 10.5. Ключевые модули

| Модуль | Роль |
|---|---|
| `app/main.py` | FastAPI, static, MCP mount, resume indexing |
| `app/api/entities.py` | upload, upload-dump, import-path, copy, CRUD, reindex, PATCH `source_path` |
| `app/api/system.py` | pick-path / browse / runtime / suggest-path / peek-source |
| `app/api/settings.py` | Настройки, db info, список MCP tools (`GET /api/settings/tools`, `lang=ru`) |
| `app/services/mounts.py` | MOUNT_POINTS, детект Docker, browse, миграция |
| `app/api/stats.py` | снимок статистики, persist-лог (list/clear/export), выгрузка RAM+БД |
| `app/services/pipeline.py` | parse (metadata → assets → справка → BSL), reindex, resume, delete guard, clone, прогресс в % и тайминги фаз |
| `app/services/parser.py` | Report txt |
| `app/services/dump_parser.py` | Metadata XML dump |
| `app/services/dump_assets_parser.py` | Forms, СКД, XML-макеты |
| `app/services/help_parser.py` | Пользовательская справка `**/Ext/Help/*.html` → kind=Help |
| `app/services/form_bin.py` | Модуль обычной формы из `Ext/Form.bin` (UTF-8 blob, как cfsmcp) |
| `app/services/bsl_parser.py` | BSL (`*.bsl`), Form.bin (по флагам профиля), CALLS |
| `app/services/dump_zip.py` | Безопасная распаковка (allowlist по профилю) |
| `app/services/search.py` | FTS, semantic, `help_search`, compare, query-path, required fields |
| `app/services/ranking.py` | Ранжирование: интенты (RLS/график/HTTP/политика/настройки), домены, Help-owner, stem-recall |
| `app/services/payload.py` | Компактные MCP payload'ы: summary, пагинация, dossier, query-path |
| `app/services/usage_stats.py` | События, уровни лога, rollup индексации, UI-poll исключения; hooks → `app_log` |
| `app/services/app_log.py` | Persist error/slow/info в SQLite (`app_log`), prune по дням/max_rows |
| `app/services/phase_timing.py` | PhaseTimer: `timing_ms` / `bottleneck` / `hint` для search/usages/dossier |
| `app/mcp/server.py` | 26 MCP tools |
| `app/mcp/tool_i18n_ru.py` | RU-строки tools для UI настроек (агентские тексты — EN) |
| `app/core/config.py` | Settings, лимиты zip, path roots |

---

## Связанные документы

- [README.md](README.md) — быстрый старт (локально и Docker)
- [AGENT-INSTALL.md](AGENT-INSTALL.md) — установка для ИИ-агента
