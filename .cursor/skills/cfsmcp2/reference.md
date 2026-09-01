# cfsmcp2 — справочник MCP tools (26)

Контекст (`context`): точное имя сущности из `list_contexts` или `tag:Имя` **только**
в search-tools при явном запросе группы.

## Контексты

| Tool | Назначение |
|------|------------|
| `list_contexts` | Enabled + ready контексты, tags, profile (bsl, lean, source_mode). **Только** если пользователь не назвал контекст |
| `list_context_groups` | Теги для `context=tag:…` |

## Поиск

| Tool | Назначение |
|------|------------|
| `search_metadata` | FTS по метаданным; `literal` = mid-string с path_prefix; `path_prefix` сузить поддерево |
| `search_under` | FTS внутри `parent_path` (после нахождения корня объекта) |
| `semantic_search` | Hybrid vector+FTS (RRF); нужен LM Studio |
| `helpsearch` | Пользовательская справка (kind=Help); `owner` — сузить до объекта |
| `get_required_fields` | FillChecking, обязательные/непроверяемые реквизиты, по хоз.операции |
| `find_by_guid` | UUID → path (dump; index или ConfigDumpInfo.xml) |

## Объект и аналитика

| Tool | Назначение |
|------|------------|
| `get_object` | Карточка; `detail_level` L3 / **L2** (default) / L0 |
| `get_links` | Входящие/исходящие связи |
| `find_usages` | Входящие: type, movements, based_on, composition, calls; default **summary** |
| `get_object_dossier` | Паспорт объекта + usage counts; L2 default |
| `trace_impact` | BFS по links, depth 1..5; default summary |
| `trace_call_chain` | BFS по BSL calls (не metadata links) |
| `get_query_path` | JOIN-путь source→target, fallbacks, готовые `*ТекстЗапроса` |
| `compare_objects` | Сравнение двух корней: реквизиты, ТЧ, movements, Help |

## BSL (dump + profile bsl)

| Tool | Назначение |
|------|------------|
| `list_code_modules` | Модули с `methods_count`; **`q` обязателен** (≥2 символа) |
| `list_methods` | Список методов; фильтры; корень коллекции → `parent_too_broad` |
| `find_methods` | Exact/prefix → fuzzy; `literal` = mid-string name/path/signature |
| `get_method` | Тело/сигнатура; `index_stale` → body с диска |
| `get_module_structure` | Методы, `#Область`, экспорты, номера строк |
| `find_code_references` | Подстрока в телах; arg `identifier`; `literal` → предпочитает диск |
| `queue_method_reembed` | Очередь повторного embed методов |

## Формы и СКД (dump)

| Tool | Назначение |
|------|------------|
| `search_forms` | Поиск управляемых форм |
| `get_skd` | data_sets, fields, parameters, settings_variants |
| `queue_managed_form_reembed` | Повторный embed форм |

## Статус

| Tool | Назначение |
|------|------------|
| `get_indexing_status` | Все сущности; для dump: `index_stale` при drift Configuration.xml |

## Нормализация путей

Tools `get_object`, `find_usages`, `trace_impact`, `get_object_dossier` принимают
RU/EN формы: `СправочникСсылка.X`, `CatalogRef.X`, `Catalogs.X`, вложенность
`Attributes↔Реквизиты`, `Forms↔Формы`, …

## source_mode и profile

| Режим | Доступно |
|-------|----------|
| `report` | Метаданные, links, search; **нет** BSL, СКД, forms, helpsearch из dump |
| `dump` + full profile | Все группы tools |
| `dump` + lean / bsl=false | Метаданные, links, trace_impact (без calls); **нет** code-tools |

Движения регистра в metadata-only: `find_usages(path=Регистры…, link_type=movements)`
или `movements` в `get_object` L2 / dossier.
