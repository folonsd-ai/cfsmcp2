"""Russian UI strings for MCP tools (settings page). Agent-facing MCP stays English."""

from __future__ import annotations

_CTX = (
    "Точное имя сущности (конфигурация/расширение) из запроса пользователя или "
    "list_contexts, ЛИБО tag:ИмяТега из list_context_groups (только если пользователь "
    "просил искать группу по тегу). Если контекст уже назван — передайте только это имя; "
    "не повторяйте один и тот же запрос по другим контекстам. "
    "Пример: РасширениеКонтурЛогистика или tag:КА2"
)
_CTX_SINGLE = (
    "Точное имя одной сущности (из запроса, list_contexts или поля context в хите). "
    "Не передавайте tag:… сюда. Не подменяйте контекст на другой, чем в вопросе."
)

# name -> {title, description, parameters: {param: description}}
TOOLS_RU: dict[str, dict] = {
    "list_contexts": {
        "title": "Список контекстов метаданных",
        "description": (
            "Список включённых готовых контекстов (имя, теги, счётчики, profile).\n\n"
            "Используйте ТОЛЬКО если пользователь ещё не назвал контекст.\n"
            "Если контекст уже назван (например РасширениеКонтурЛогистика) — "
            "пропустите этот tool и ищите по этому имени напрямую; "
            "не перебирайте все возвращённые контексты.\n"
            "profile.lean=true / profile.bsl=false — только метаданные "
            "(нет find_methods / trace_call_chain). "
            "Движения регистра: find_usages(..., link_type=movements)."
        ),
        "parameters": {},
    },
    "list_context_groups": {
        "title": "Список групп контекстов по тегу",
        "description": (
            "Список тегов, группирующих готовые контексты. Нужен только если "
            "пользователь просил группу по тегу или имя контекста неизвестно. "
            "Дальше ищите с context=tag:Имя — без ручного цикла по контекстам."
        ),
        "parameters": {},
    },
    "search_metadata": {
        "title": "Полнотекстовый поиск метаданных",
        "description": (
            "Полнотекстовый поиск в ОДНОМ контексте (или одной группе tag:).\n\n"
            "Передавайте контекст из запроса пользователя точно. Не вызывайте "
            "повторно для каждого контекста из list_contexts с одним запросом. "
            "tag:… — только при поиске по группе тега.\n"
            "После нахождения родителя предпочитайте path_prefix / search_under.\n"
            "literal=true без path_prefix — только точное имя/путь, не фраза.\n"
            "Корень коллекции как path_prefix (Документы, Отчеты, …) — "
            "path_prefix_too_broad; нужен путь конкретного объекта."
        ),
        "parameters": {
            "context": _CTX,
            "query": "Полнотекстовый поисковый запрос",
            "kind": "Необязательный фильтр вида, напр. Catalog, Document",
            "include_borrowed": "Включать заимствованные объекты",
            "limit": "Размер страницы",
            "offset": "Смещение для постраничной выдачи",
            "path_prefix": (
                "Путь объекта, не корень коллекции "
                "(Документы.Имя, не Документы)"
            ),
            "compact": "Компактные хиты (path/kind/name/synonym/comment/score)",
            "literal": (
                "Без path_prefix — точное name/path; mid-string только с путём "
                "объекта. Корень коллекции → path_prefix_too_broad. "
                "Один идентификатор, не фраза."
            ),
        },
    },
    "search_under": {
        "title": "Поиск под родительским путём",
        "description": (
            "FTS (+ SQL) поиск только среди потомков parent_path. "
            "Предпочтительнее глобального поиска."
        ),
        "parameters": {
            "context": _CTX,
            "parent_path": "Путь родительского объекта, напр. Документы.КонтурЛогистика_ДФ_Титул1",
            "query": "Запрос внутри поддерева родителя",
            "kind": "Необязательный фильтр вида",
            "include_borrowed": "Включать заимствованные объекты",
            "limit": "Размер страницы",
            "compact": "Компактные хиты",
        },
    },
    "semantic_search": {
        "title": "Семантический / гибридный поиск",
        "description": (
            "Гибридный векторный + FTS поиск в ОДНОМ контексте (или группе tag:).\n\n"
            "Те же правила области, что у search_metadata: только названный контекст, "
            "без fan-out. Используйте после FTS, когда вопрос концептуальный, "
            "а не точный идентификатор."
        ),
        "parameters": {
            "context": _CTX,
            "query": "Запрос на естественном языке",
            "top_n": "Число результатов",
            "path_prefix": "Только пути под этим префиксом",
            "compact": "Компактные хиты",
        },
    },
    "helpsearch": {
        "title": "Поиск по файлам справки конфигурации",
        "description": (
            "Поиск по пользовательской справке (Ext/Help/*.html) конфигурации.\n\n"
            "Каждый хит — раздел справки: comment = текст раздела, props.owner = "
            "объект-владелец. Для вопросов «как пользоваться / как заполнять»; "
            "дополняйте get_object(owner), чтобы открыть карточку объекта."
        ),
        "parameters": {
            "context": _CTX,
            "query": "Вопрос на естественном языке о работе с конфигурацией",
            "limit": "Число результатов",
            "owner": "Необязательный путь объекта-владельца, напр. Документы.ТранспортнаяНакладная",
            "compact": "Компактные хиты",
        },
    },
    "get_required_fields": {
        "title": "Обязательные поля / FillChecking",
        "description": (
            "Ответ на «обязательно ли?» через FillChecking, МассивОбязательных* и "
            "код, снимающий проверки (МассивНепроверяемыхРеквизитов / хоз.операция)."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Путь документа/справочника/реквизита, напр. Документы.…_Титул1",
        },
    },
    "get_object": {
        "title": "Карточка объекта метаданных",
        "description": (
            "Карточка объекта. По умолчанию L2 достаточно для «какие поля заполнять». "
            "Нужно имя одной сущности (не tag:). "
            "Для счётчиков использования и топа владельцев — get_object_dossier."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Полный путь объекта, напр. Catalogs.X.Attributes.Y или русский путь",
            "detail_level": "L3=path/kind/name/synonym; L2=реквизиты+ТЧ+движения (по умолч.); L0=полные props",
        },
    },
    "get_links": {
        "title": "Связи метаданных",
        "description": (
            "Входящие/исходящие связи объекта. Нужно имя одной сущности (не tag:)."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "object": "Путь объекта",
            "direction": "Направление связей",
        },
    },
    "get_query_path": {
        "title": "Карта JOIN-пути source→target (с запасными A→B)",
        "description": (
            "Карта JOIN-пути: как получить поля `target` для данного `source`.\n\n"
            "Показывает реквизиты / колонки ТЧ со связью типов source↔target "
            "(поля для запроса связанных данных), плюс готовые методы *ТекстЗапроса*, "
            "где упоминаются оба объекта. Для сценариев «получить X из A, иначе из B» "
            "с fallbacks: карта покрывает каждую пару и отличает «нет такого реквизита» "
            "(пустой список join) от «нет данных» (поле есть, значение может быть пустым — "
            "обрабатывайте через ЕСТЬNULL/ВЫБОР)."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "source": "Путь исходного объекта, который уже есть (напр. РеализацияТоваровУслуг)",
            "target": "Путь целевого объекта, откуда нужны данные (напр. ТранспортнаяНакладная)",
            "fallbacks": "Запасные цели, если данных нет в `target` (напр. [\"фмРейс\"])",
            "limit": "Макс. число готовых методов запроса",
        },
    },
    "find_usages": {
        "title": "Где используется объект",
        "description": (
            "Где объект используется: входящие ссылки по нормализованным refs.\n\n"
            "По умолчанию summary группирует по владельцу (Документы.X) + count; "
            "DefinedType исключается.\n"
            "Учитывает ссылки типов, движения, based_on, composition и вызовы BSL.\n"
            "Документы движений регистра: path=регистр, link_type=movements."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Путь объекта (RU или EN форма)",
            "link_type": "Фильтр типа связи; None = все. movements — кто пишет в регистр",
            "summary": "Агрегация до объектов-владельцев + счётчики (по умолч.). False или expand=paths — полные пути.",
            "expand": "expand=paths возвращает полный список from_path (с пагинацией)",
            "limit": "Размер страницы",
            "offset": "Смещение для постраничной выдачи",
        },
    },
    "get_object_dossier": {
        "title": "Досье объекта",
        "description": (
            "Паспорт объекта. По умолчанию L2: реквизиты (имя+тип), табличные части,\n"
            "движения, счётчики использования + топ владельцев — без XML props "
            "и дампов путей."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Путь объекта (RU или EN форма)",
            "include_props": "Принудительно L0 с полными свойствами (по умолч. False)",
            "detail_level": "L3 карточка; L2 сводка реквизиты+ТЧ+движения+usage (по умолч.); L0 полный",
            "summary": "Агрегировать usages до владельцев (по умолч. True)",
            "expand": "expand=paths сохраняет полные пути usages",
            "limit": "Размер страницы usages",
            "offset": "Смещение usages",
        },
    },
    "trace_impact": {
        "title": "Трассировка влияния по связям",
        "description": (
            "Рекурсивный анализ влияния по связям (BFS с защитой от циклов).\n\n"
            "По умолчанию summary: путь владельца + count. В полном режиме строка: "
            "{path, link_type, depth}."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Путь объекта (RU или EN форма)",
            "direction": "down = кто использует объект (входящие); up = на что ссылается объект (исходящие)",
            "depth": "Макс. глубина (1..5)",
            "limit": "Макс. число строк / владельцев",
            "summary": "Агрегация до объектов-владельцев + счётчики (по умолч. True)",
            "expand": "expand=paths возвращает полные пути",
            "offset": "Смещение для постраничной выдачи",
        },
    },
    "trace_call_chain": {
        "title": "Цепочка вызовов BSL",
        "description": (
            "BFS только по рёбрам calls (по умолч. вкл.).\n\n"
            "Не для «кто пишет в регистр», типов и движений — это find_usages / "
            "trace_impact. Пустая цепочка → hint/next_tool; не повторять этот tool "
            "с другой depth."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Путь метода, напр. ОбщиеМодули.X.Методы.Module.Foo",
            "direction": "callees = кого вызывает; callers = кто вызывает; both",
            "depth": "Макс. глубина (1..5)",
            "limit": "Макс. узлов на направление",
        },
    },
    "list_code_modules": {
        "title": "Список модулей с методами BSL",
        "description": (
            "Список кодовых модулей по фильтру q (родители методов BSL со счётчиками). "
            "Если путь объекта/модуля уже известен — list_methods(parent_path=…): "
            "этот tool сканирует широко и на больших конфигурациях медленный. "
            "Полный список без q не поддерживается."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "kind": "Необязательный фильтр вида модуля, напр. CommonModule, Catalog, Document",
            "q": "Обязательный фильтр по имени модуля (≥2 символа). Префикс всегда; подстрока только если задан kind. Не вместо list_methods, если путь уже есть.",
        },
    },
    "list_methods": {
        "title": "Список методов BSL",
        "description": (
            "Список методов BSL контекста (опционально в рамках модуля). "
            "q — только подстрока, без fuzzy/NL; для запроса по теме/интенту — find_methods."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "parent_path": (
                "Путь модуля из хита (ОбщиеМодули.Имя). "
                "Не корень коллекции — parent_too_broad."
            ),
            "q": "Подстрочный фильтр по имени/сигнатуре/доке/пути",
            "export_only": "Только экспортные методы",
            "limit": "Макс. строк (<=200)",
        },
    },
    "find_methods": {
        "title": "Поиск методов BSL (точное, затем нечёткое)",
        "description": (
            "Поиск методов: сначала точная подстрока; если <3 хитов — "
            "семантический (нечёткий) поиск. "
            "parent_path — только путь из хита; корень коллекции (ОбщиеМодули, Документы) → "
            "parent_too_broad; при parent_not_found — search_metadata, "
            "не повтор find_methods с другим угаданным путём. "
            "Термины из имени метода перевешивают совпадения только в параметрах/сигнатуре — "
            "при «глаголе» в имени сужайте parent_path. "
            "literal=true — mid-string только с parent_path; без него exact/prefix."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "query": "Имя метода / сигнатура / намерение",
            "parent_path": (
                "Путь модуля/объекта из хита (ОбщиеМодули.Имя). "
                "Не корень коллекции — parent_too_broad."
            ),
            "export_only": "Только экспортные методы",
            "limit": "Макс. строк (<=200)",
            "literal": "Mid-string name/path/signature только с parent_path; без него — exact/prefix. Не фраза.",
        },
    },
    "get_method": {
        "title": "Детали метода BSL",
        "description": (
            "Метод BSL с сигнатурой/докой; тело из индекса (code/full) или с диска "
            "по source_rel. Если файл выгрузки новее индекса — body с диска и index_stale=true."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Путь метода, напр. ОбщиеМодули.Х.Методы.Module.ИмяМетода",
        },
    },
    "get_module_structure": {
        "title": "Структура модуля BSL",
        "description": (
            "Структура кодового модуля: методы (строка/область/сигнатура), области, экспорт."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "module_path": "Путь модуля, напр. ОбщиеМодули.ИмяМодуля",
        },
    },
    "find_code_references": {
        "title": "Поиск упоминаний имени в телах методов",
        "description": (
            "Где имя/идентификатор встречается в телах методов "
            "(индекс code/full или файлы выгрузки). "
            "Нужен конкретный parent_path из хита (ОбщиеМодули.ИмяМодуля); "
            "корень коллекции (ОбщиеМодули, Обработки, Документы) и поиск без parent "
            "на большом дампе отклоняются. "
            "literal=true — предпочитать файлы на диске (свежий источник)."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "identifier": "Идентификатор для поиска в телах методов BSL",
            "parent_path": "Модуль/объект из хита (не корень коллекции ОбщиеМодули/Обработки)",
            "limit": "Макс. число хитов",
            "offset": "Смещение для постраничной выдачи",
            "literal": "Искать в файлах выгрузки; с конкретным parent_path модуля/объекта.",
        },
    },
    "get_skd": {
        "title": "Структура схемы компоновки данных",
        "description": (
            "Структурированная выборка СКД: наборы данных, поля, параметры, варианты настроек."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path": "Путь к объекту макета СКД, напр. …Макеты.ИмяСКД",
        },
    },
    "compare_objects": {
        "title": "Сравнить два объекта метаданных",
        "description": (
            "Лёгкое сравнение двух корней: реквизиты / ТЧ / движения / заголовки Help.\n\n"
            "Для «в чём разница между A и B». Diff по именам; типы и FillChecking — "
            "через get_object; макеты отчётов — get_skd."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "path_a": "Путь первого объекта",
            "path_b": "Путь второго объекта",
        },
    },
    "search_forms": {
        "title": "Поиск управляемых форм",
        "description": (
            "Поиск ManagedForm по имени/пути и структуре формы (элементы, команды, "
            "реквизиты из props_json). После обновления текста эмбеддингов — "
            "queue_managed_form_reembed."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "query": "Имя формы или текст элемента/команды/реквизита",
            "path_prefix": "Необязательный префикс владельца, напр. Документы.ЗаказКлиента",
            "limit": "Максимум строк",
            "compact": "Компактные хиты",
        },
    },
    "queue_managed_form_reembed": {
        "title": "Переэмбед только ManagedForm",
        "description": (
            "Пометить строки ManagedForm на re-embed и продолжить индексацию. "
            "Не переразбирает Form.xml и не трогает другие виды."
        ),
        "parameters": {"context": _CTX_SINGLE},
    },
    "queue_method_reembed": {
        "title": "Переэмбед только методы (Procedure/Function)",
        "description": (
            "Пометить строки Procedure/Function на re-embed после обновления "
            "meta_text (сигнатура/параметры). Не переразбирает BSL."
        ),
        "parameters": {"context": _CTX_SINGLE},
    },
    "find_by_guid": {
        "title": "Найти метаданные по GUID",
        "description": (
            "Резолв UUID метаданных (атрибут uuid= / ConfigDumpInfo id=) → path/kind.\n\n"
            "Только dump. Пока objects.guid пуст (до reparse) — поиск в ConfigDumpInfo.xml. "
            "Дальше обычно get_object по path."
        ),
        "parameters": {
            "context": _CTX_SINGLE,
            "guid": "UUID (скобки и суффикс .N модуля допустимы)",
            "limit": "Макс. число хитов",
        },
    },
    "get_indexing_status": {
        "title": "Статус индексации всех сущностей",
        "description": (
            "Статус ВСЕХ сущностей (не только ready/enabled, в отличие от list_contexts). "
            "Для dump: index_stale=true, если Configuration.xml / ConfigDumpInfo.xml "
            "новее снимка индекса — нужен refresh."
        ),
        "parameters": {
            "context_ref": "Необязательное имя сущности или tag:Имя для фильтра",
        },
    },
}


def apply_tool_i18n_ru(
    *,
    name: str,
    title: str,
    description: str,
    parameters: list,
) -> tuple[str, str, list]:
    """Overlay RU title/description/param descriptions when available."""
    loc = TOOLS_RU.get(name)
    if not loc:
        return title, description, parameters
    title_out = str(loc.get("title") or title)
    desc_out = str(loc.get("description") or description)
    param_map = loc.get("parameters") or {}
    params_out = []
    for p in parameters:
        d = dict(p) if isinstance(p, dict) else p.model_dump()
        pname = d.get("name") or ""
        if pname in param_map and param_map[pname]:
            d["description"] = param_map[pname]
        params_out.append(d)
    return title_out, desc_out, params_out
