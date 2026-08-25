"""FTS query expansion and hit ranking (kind boost, diversity, token coverage)."""

from __future__ import annotations

import re
from typing import Any

# Prefix expand only longer stems so «нулев» does not explode recall.
MIN_PREFIX_TOKEN_LEN = 6
BOOST_KINDS = {
    "Document": 1.2,
    "Catalog": 1.2,
    "Report": 1.15,
    "DataCompositionSchema": 0.9,
    "CommonModule": 0.6,
}
# Prefer metadata roots (Документы.X / Отчеты.X) over nested Attribute hits.
BOOST_ROOT_OBJECT = 0.45
# Exact query token == root Catalog/Document/Report name (Gap 2 vs Attribute homonyms).
BOOST_EXACT_ROOT_NAME = 1.35
BOOST_ALIAS_HIT = 1.6
BOOST_REPORT_INTENT_ROOT = 0.55
# U15: nested СКД under an Отчеты.X is valuable for «собрать данные» NL —
# instead of the -0.85 nest penalty, give it a positive boost.
BOOST_REPORT_INTENT_SCHEMA = 0.4
BOOST_HOWTO_INFO_REGISTER = 0.35
BOOST_HOWTO_QUERY_TEXT_METHOD = 0.4
BOOST_HOWTO_FALLBACK = 0.25
# U10: пользовательская справка (kind=Help) поднимается только на how-to /
# fallback запросах; в обычном поиске её буст нулевой.
BOOST_HELP_HOWTO = 0.8
# U23: object that owns a Help hit in the pool (Help.Документы.X → Документы.X).
BOOST_HELP_OWNER = 1.1
# U25: rights/settings constraint how-to («не мог редактировать», «запретить»).
BOOST_SETTINGS_KIND = {
    "Constant": 1.35,
    "FunctionalOption": 1.25,
    "Role": 1.0,
}
BOOST_SETTINGS_PANEL = 0.7  # Обработки/формы «Настройка…»
# U26: junk / deep nest on user how-to.
PENALTY_ATTACHED_FILES = -1.4
PENALTY_DELETED_PREFIX = -0.7
PENALTY_JUNK_KIND = -0.55  # EnumValue / deep Attribute on how-to
PENALTY_COMMON_PICTURE = -0.5
# U24: cross-domain noise (trade how-to → HR/военкомат и т.п.).
PENALTY_DOMAIN_NOISE = -1.25
# Domain-collapse: demote dense payroll / invoice / finance-lease clusters when
# the query points elsewhere (reverse of root-boost — press the noisy island).
PENALTY_DOMAIN_CLUSTER = -2.15
# U27: Help/owner from unrelated product island (ВЕТИС on plain MRP, …).
PENALTY_HELP_OFFDOMAIN = -1.15
# U28–U35: class intents + policy / stem-root ranking.
BOOST_RLS_HIT = 1.55
BOOST_PROD_SCHEDULE_HIT = 1.15
BOOST_INTEGRATION_HIT = 1.15
BOOST_DOMAIN_STEM_HIT = 1.05
BOOST_POLICY_META = 1.45
BOOST_STEM_RECALL_ROOT = 1.3
# U36: quoted / typed metadata names + query-JOIN / pricing / reconcile.
BOOST_NAMED_ENTITY = 1.5
BOOST_QUERY_JOIN_REGISTER = 1.05
BOOST_PRICING_HIT = 1.15
BOOST_PRICING_FX_HIT = 1.55
BOOST_RECONCILE_REPORT = 1.1
BOOST_RECONCILE_PNL = 1.45
BOOST_BUDGET_EXEC_HIT = 1.35
BOOST_REPAIR_OVERDUE_HIT = 1.35
PENALTY_HELP_WHEN_STEM_INTENT = -1.55
PENALTY_HELP_WHEN_QUERY_JOIN = -1.45
PENALTY_PRICING_FX_NOISE = -1.25
PENALTY_RECONCILE_BUDGET_OSV = -0.9
PENALTY_BUDGET_EXEC_NOISE = -1.5
PENALTY_REPAIR_OVERDUE_NOISE = -1.55
PENALTY_RLS_DOC_NOISE = -2.0
PENALTY_POLICY_DOC_NOISE = -1.1
# U39: universal multi-token coverage / weak-singleton / kind-prior (no domain packs).
BOOST_MULTI_TOKEN_COVER = 1.25
BOOST_KIND_PRIOR = 0.95
PENALTY_WEAK_SINGLETON = -1.35
PENALTY_HOWTO_POLICY_NOISE = -1.05
PENALTY_HOWTO_ZERO_COVER = -2.6
PENALTY_RICH_SINGLETON = -1.2
PENALTY_CAMEL_TAIL = -1.15
MIN_CONTENT_TOKEN_LEN = 4
MIN_STRONG_ROOTS_SKIP_HELP = 2
MIN_RICH_QUERY_TOKENS = 5
# Cap SQL stem-recall pairs (each pair ≈ one tree scan on ERP).
MAX_INTENT_STEM_SPECS = 6
# Stop stem SQL once this many root hits are in the pool.
MIN_STEM_ROOT_HITS = 2
# U35: limit expensive Document / HTTP trees per query.
MAX_DOCUMENT_STEM_SPECS = 3
MAX_HTTP_STEM_SPECS = 2
MAX_NAMED_ENTITY_SPECS = 6
# U41: hard cap on mid-string / stem LIKE scans per query (prefix lane first).
MAX_STEM_LIKE_SCANS = 3
STEM_RECALL_BUDGET_MS = 250.0
# Outer wall-clock ceiling for the whole ``sql_recall`` span (search_metadata /
# semantic_search). Nested above class-C ``STEM_RECALL_BUDGET_MS`` — not a
# replacement: stem packs still stop on their own 250ms cap; this stops
# token/help/report/stem together from running tens of seconds. Keep
# SQL_RECALL_SPAN_BUDGET_MS > STEM_RECALL_BUDGET_MS so C can finish and
# indexed/help lanes still get a slice. Flags stay separate:
# ``stem_recall_budget_hit`` (C) vs ``sql_recall_budget_hit`` (span).
SQL_RECALL_SPAN_BUDGET_MS = 500.0
# Prefer cheaper trees before Документы.* in stem LIKE order.
_STEM_PREFIX_COST: dict[str, int] = {
    "Константы.": 0,
    "ФункциональныеОпции.": 0,
    "Роли.": 1,
    "РегламентныеЗадания.": 1,
    "РегистрыСведений.": 2,
    "РегистрыНакопления.": 2,
    "Справочники.": 3,
    "Отчеты.": 3,
    "Обработки.": 4,
    "HTTPСервисы.": 4,
    "ОбщиеМодули.": 5,
    "Документы.": 6,
}
_STEM_ROOT_KINDS = frozenset(
    {
        "Document",
        "Catalog",
        "CommonModule",
        "Role",
        "Constant",
        "FunctionalOption",
        "HTTPService",
        "InformationRegister",
        "AccumulationRegister",
        "DataProcessor",
        "ExchangePlan",
        "Report",
    }
)
_ROOT_KINDS = frozenset({"Document", "Catalog", "Report"})
_SETTINGS_ROOT_KINDS = frozenset({"Constant", "FunctionalOption", "Role"})
_REPORT_PATH_PREFIXES = ("Отчеты.", "Reports.")
# Primary report tree used to scope alias-term SQL scans.
REPORT_PATH_PREFIX_RU = "Отчеты."
# Meta roots under Help.<owner> (U23).
_HELP_OWNER_KIND = {
    "Документы": "Document",
    "Справочники": "Catalog",
    "Отчеты": "Report",
    "Обработки": "DataProcessor",
    "РегистрыСведений": "InformationRegister",
    "РегистрыНакопления": "AccumulationRegister",
    "ПланыВидовХарактеристик": "ChartOfCharacteristicTypes",
    "ПланыОбмена": "ExchangePlan",
    "ОбщиеФормы": "CommonForm",
    "Подсистемы": "Subsystem",
    "HTTPСервисы": "HTTPService",
    "ОбщиеМодули": "CommonModule",
    "Роли": "Role",
    "Константы": "Constant",
    "ФункциональныеОпции": "FunctionalOption",
}
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Only real FTS operators — NOT NL punctuation «()?» which blocked U41 lexical OR.
_HAS_FTS_OP = re.compile(r"[*]|\bAND\b|\bOR\b|\bNOT\b", re.IGNORECASE)
_HOWTO_QUERY_RE = re.compile(r"запросом|получить", re.IGNORECASE)
# U36: «напишите запрос / свяжите регистр / соединив» — AccumReg first, demote Help.
_QUERY_JOIN_INTENT_RE = re.compile(
    r"напишите\s+запрос|напиши\s+запрос|составьте\s+запрос|составить\s+запрос|"
    r"текст\s*запроса|"
    r"свяжите\s+регистр|связ\w*\s+регистр|"
    r"соедини\w*\s+регистр|объедини\w*\s+регистр|"
    r"какие\s+поля\s+(?:использовать|взять)|поля\s+для\s+расч|"
    r"как\s+корректно\s+учесть|уч[её]сть\s+уже\s+зарезерв",
    re.IGNORECASE,
)
# U36: price + FX auto-recalc (курс/валюта) — separate from series pricing.
_PRICING_FX_INTENT_RE = re.compile(
    r"цен\w*.{0,48}(?:курс|доллар|валют)|"
    r"(?:курс|доллар|валют).{0,48}цен|"
    r"пересчитыва\w*.{0,48}курс|"
    r"(?:автомат\w*|сам\w*).{0,40}пересчит\w*.{0,40}(?:курс|валют|доллар|цен)|"
    r"цен\w*.{0,40}(?:автомат\w*|пересчит\w*).{0,40}(?:курс|валют|доллар)",
    re.IGNORECASE,
)
# U36: price + series / shelf life (not FX).
_PRICING_SERIES_INTENT_RE = re.compile(
    r"цен\w*.{0,48}(?:сери|срок\w*\s+годност|годност)|"
    r"(?:сери|срок\w*\s+годност).{0,48}цен|"
    r"отдельн\w*\s+цен\w*.{0,36}(?:сери|годност)",
    re.IGNORECASE,
)
# Legacy union used by skip-Help / stem-or-domain gates.
_PRICING_INTENT_RE = re.compile(
    r"цен\w*.{0,48}(?:курс|автомат|пересчит|доллар|валют|сери|срок\w*\s+годност|годност)|"
    r"(?:курс|доллар|валют|сери|срок\w*\s+годност).{0,48}цен|"
    r"отдельн\w*\s+цен\w*.{0,36}(?:сери|годност)|"
    r"пересчитыва\w*\s+при\s+изменении\s+курса",
    re.IGNORECASE,
)
# U36: ОПиУ / P&L vs ОСВ reconciliation.
_RECONCILE_REPORT_RE = re.compile(
    r"(?:не\s+совпада|расхожд|почему).{0,100}"
    r"(?:прибыл|убытк|опиу|осв|оборотно)|"
    r"(?:прибыл|убытк|опиу).{0,100}"
    r"(?:осв|оборотно|не\s+совпада|расхожд)|"
    r"(?:осв|оборотно[\s\-]*салд).{0,100}"
    r"(?:прибыл|убытк|не\s+совпада|расхожд)",
    re.IGNORECASE,
)
# U38 pack2 R12: budget execution chain limit → claims → fact → remainder.
_BUDGET_EXEC_INTENT_RE = re.compile(
    r"бюджет\w*.{0,100}(?:лимит|исполнен|стать\w*\s+бюджет)|"
    r"(?:лимит|исполнен).{0,100}бюджет|"
    r"стать\w*\s+бюджет|"
    r"лимит\s*[→\->].{0,60}заявк|"
    r"исполнен\w*\s+бюджет",
    re.IGNORECASE,
)
# U38 pack2 R18: overdue / warranty repair + norms / deadlines.
_REPAIR_OVERDUE_INTENT_RE = re.compile(
    r"(?:ремонт|заказ[\-\u2011\u2013\u2014]?\s*наряд).{0,100}"
    r"(?:просроч|срок|норматив|гарант)|"
    r"(?:просроч|срок|норматив|гарант).{0,100}"
    r"(?:ремонт|заказ[\-\u2011\u2013\u2014]?\s*наряд)|"
    r"гарантийн\w*\s+ремонт|"
    r"норматив\w*\s+срок\w*\s+ремонт|"
    r"заказ[\-\u2011\u2013\u2014]?\s*наряд\w*\s+на\s+ремонт",
    re.IGNORECASE,
)
_ARROW_CHAIN_QUOTE_RE = re.compile(r"[→⟶]|--+>|=>")
_BUDGET_LIMIT_RE = re.compile(
    r"ЛимитыПоДаннымБюджетирования|ЛимитыРасхода|ПравилаЛимитов|КонтрольЛимитов",
    re.IGNORECASE,
)
_BUDGET_CLAIM_OR_FACT_RE = re.compile(
    r"ЗаявкаНаРасходован|СтатьиБюджет|ПравилаПолученияФакта|ПравилаЛимитов",
    re.IGNORECASE,
)
_REPAIR_ORDER_RE = re.compile(r"ЗаказНаРемонт", re.IGNORECASE)
_REPAIR_SCHEDULE_RE = re.compile(
    r"ПланРемонтов|РемонтыРабочих|Норматив\w*Срок|Фактическ\w*Срок|"
    r"ПланированиеРемонт",
    re.IGNORECASE,
)
_BUDGET_EXEC_NOISE_RE = re.compile(
    r"АнализЖурналаРегистрации|ПроизводственныеЗатраты|НачальныеОстаткиНЗП|"
    r"ПеремещениеВДругоеПодразделение",
    re.IGNORECASE,
)
_REPAIR_OVERDUE_NOISE_RE = re.compile(
    r"ПросроченныеЗадачи|ПросроченныеДействия|Документооборот|"
    r"РегламентированныйОтчет|РемонтОбуви",
    re.IGNORECASE,
)
# Quoted metadata names «…» / "…" (3–80 chars).
_NAMED_ENTITY_QUOTED_RE = re.compile(r"[«\"]([^»\"\n]{3,80})[»\"]")
_NAMED_ENTITY_TYPED_RE = re.compile(
    r"(регистр(?:ы)?\s+накопления|регистр(?:ы)?\s+сведений|регистр(?:ы)?|"
    r"справочник(?:и)?|документ(?:ы)?|отч[её]т(?:ы)?|обработк\w*)"
    r"\s+[«\"]([^»\"\n]{3,80})[»\"]",
    re.IGNORECASE,
)
_PHRASE_STOPWORDS = frozenset(
    {
        "на",
        "по",
        "с",
        "и",
        "в",
        "от",
        "для",
        "к",
        "о",
        "об",
        "со",
        "из",
        "у",
        "а",
        "или",
    }
)
# U20: «получить X из A, иначе из B» — coalesce/fallback intent.
_FALLBACK_QUERY_RE = re.compile(
    r"иначе|если\s+нет|fallback|coalesce|либо\s+из|или\s+из|в\s+случае\s+отсутствия",
    re.IGNORECASE,
)
# U10: пользовательская справка отвечает на «как заполнить/настроить/провести…».
_HELP_QUERY_RE = re.compile(
    r"как\s+заполн|как\s+настро|как\s+пользовать|как\s+работать|как\s+провес|"
    r"как\s+оформ|как\s+создат|как\s+удалить|как\s+изменит|как\s+добавить|"
    r"инструкци|руководств|справка|помощь|памятк",
    re.IGNORECASE,
)
# U23: mass user FAQ («Как…?», «Можно ли…?», «Где…?») — help-first + owner boost.
_USER_HOWTO_RE = re.compile(
    r"(?:^|\s)(?:как|можно\s+ли|где(?:\s+ввести|\s+указать)?|какие|какой|какая)\b|"
    r"как\s+(?:заполн|настро|оформ|сделать|ввест|указать|назнач|зарегистрир|"
    r"посмотр|сформир|начисл|вывест)|"
    r"можно\s+ли\s+(?:сделать|использовать|зарегистрир|назнач)|"
    r"опишите|объясните|в\s+чём\s+разница|реализовать\s+проверку",
    re.IGNORECASE,
)
# U25: constraint / rights / «оператор не может».
_SETTINGS_CONSTRAINT_RE = re.compile(
    r"не\s+мог(?:ли|ут|у)?|не\s+редактир|запрет\w*|запрещ|"
    r"не\s+имел\w*\s+права|только\s+определенн|"
    r"огранич\w*\s+(?:прав|скид|цен|ручн)|"
    r"оператор\w*.*(?:не\s+|только)|"
    r"менеджер\w*.*(?:только|не\s+мог)|"
    r"можно\s+ли\s+сделать\s+так",
    re.IGNORECASE,
)
# U28: RLS / record-level access.
_RLS_INTENT_RE = re.compile(
    r"\brls\b|ограничен\w*\s+доступа|прав\w*\s+доступа|"
    r"на\s+уровне\s+записей|шаблон\w*\s+ограничен|"
    r"доступ\w*\s+к\s+данн|роль\w*.*склад|склад.*роль",
    re.IGNORECASE,
)
# U29: production schedule / equipment availability.
_PROD_SCHEDULE_INTENT_RE = re.compile(
    r"график\w*\s+производ|расписан\w*\s+производ|"
    r"доступност\w*\s+оборудован|загрузк\w*\s+оборудован|"
    r"рабоч\w*\s+центр|смен\w*.*производ|производ\w*.*смен",
    re.IGNORECASE,
)
# U30: external integration / HTTP / WMS.
_INTEGRATION_INTENT_RE = re.compile(
    r"http[-\s]?сервис|http\s*service|\bwms\b|интеграц\w*.*(?:внешн|wms|http)|"
    r"внешн\w*\s+wms|обмен\w*\s+с\s+wms|esb|web[-\s]?сервис",
    re.IGNORECASE,
)
# U33: «включить/настроить учёт/схему/опцию» — ответ в ФО/константах.
_ACCOUNTING_POLICY_RE = re.compile(
    r"(?:включить|настро\w*|использовать)\w*.{0,48}"
    r"(?:схем|уч[её]т|опци|функциональн)|"
    r"(?:схем\w*\s+(?:склад|ордер|уч[её]т)|ордерн\w*\s+схем)|"
    r"(?:уч[её]т\w*\s+(?:по\s+)?сери|себестоим\w*.{0,36}(?:сери|фифо|партион))|"
    r"фифо.{0,48}(?:сери|годност|себестоим)|"
    r"перекр[её]стн\w*\s+списан",
    re.IGNORECASE,
)
# Document Help noise under policy / costing intents.
_POLICY_DOC_NOISE_RE = re.compile(
    r"Списан|Хранен|Выкуп\w*Хранит|ОтчетОСписании|СписаниеОтгружен",
    re.IGNORECASE,
)
# U24: soft domain markers (mass disambiguation, not per-FAQ aliases).
_DOMAIN_TRADE_RE = re.compile(
    r"цен[аыу]|скидк|наценк|лояльн|дисконт|продаж|закуп|номенклатур|"
    r"клиент|партнер|поставщик|прайс|соглашен\w*\s+с\s+клиен",
    re.IGNORECASE,
)
_DOMAIN_ZUP_RE = re.compile(
    r"зарплат|сотрудник|отпуск|кадр|фсс|больнич|командиров|"
    r"начислен|удержан|алимент|табел|штатн",
    re.IGNORECASE,
)
_DOMAIN_PLAN_RE = re.compile(
    r"планирован|сезонн\w*\s+коэффициент|товарн\w*\s+категор|план\s+продаж|план\s+закуп",
    re.IGNORECASE,
)
_DOMAIN_WH_RE = re.compile(
    r"склад|ордерн|приходн\w*\s+ордер|расходн\w*\s+ордер|зон\w*\s+(?:приём|прием|хранен|отгруз)|ячеист",
    re.IGNORECASE,
)
_DOMAIN_PROD_RE = re.compile(
    r"производ|спецификац|выпуск\w*\s+продук|этап\w*\s+производ|"
    r"давальч|переработк|брак|отход|ресурсн|"
    r"трудозатрат|загрузк\w*\s+смен|смен\w*.*загрузк|неравномерн\w*.*загрузк",
    re.IGNORECASE,
)
# Labor-load / shift imbalance — NOT bare «брак» / «производственн* участок».
# Exact-inject of ПлановыеТрудозатраты only when this fires (bug-4 fix regression).
_DOMAIN_LABOR_LOAD_RE = re.compile(
    r"трудозатрат|"
    r"загрузк\w*\s+смен|смен\w*.*загрузк|"
    r"неравномерн\w*.*(?:загрузк|коэффициент|распредел)|"
    r"коэффициент\w*\s+неравномерн|"
    r"перекос\w*\s+распределен\w*\s+трудозатрат",
    re.IGNORECASE,
)
_DOMAIN_COST_RE = re.compile(
    r"себестоим|фифо|постатейн|калькуляц|партионн|"
    r"накладн\w*\s+расход|распределен\w*\s+накладн|маржинальн",
    re.IGNORECASE,
)
# Counterparty / AR-AP advances (not payroll НДФЛ «аванс»).
_DOMAIN_SETTLEMENT_RE = re.compile(
    r"аванс\w*.*(?:зачет|просроч|завис|контрагент|договор)|"
    r"(?:зачет|просроч|завис|незакрыт\w*)\w*.*аванс|"
    r"частичн\w*\s+зачет",
    re.IGNORECASE,
)
# Overhead phrase (adj.) — not «накладная» the document.
_DOMAIN_OVERHEAD_RE = re.compile(
    r"накладн\w*\s+расход|распределен\w*\s+накладн",
    re.IGNORECASE,
)
# Warehouse operating cost (rent/staff/capital by area) — not ФСБУ 25 lease.
_DOMAIN_WH_OPS_COST_RE = re.compile(
    r"(?:склад|хранен).*(?:аренда|площад|капитал|кладовщик)|"
    r"(?:аренда|площад|капитал|кладовщик).*(?:склад|хранен)|"
    r"стоимость\w*\s+хранен",
    re.IGNORECASE,
)
# Product returns (customer/supplier) — not «возвратная тара».
# Do NOT list «причин» here: «причины корректировок возврат, ошибка…» is an
# enumeration of VAT-correction reasons, not a goods-return subject (batch5).
_DOMAIN_GOODS_RETURN_RE = re.compile(
    r"возврат\w*.*(?:товар|номенклатур|клиент|продаж|реализац|канал|поставщик)|"
    r"(?:товар|номенклатур|клиент|продаж|реализац|канал|поставщик)\w*.*возврат|"
    r"доля\w*\s+возврат|свод\w*\s+по\s+возврат|"
    r"заявк\w*\s+на\s+возврат",
    re.IGNORECASE,
)
# After «причины/варианты/например» a short token list is an enumeration, not
# the query subject — blocks exact-inject on a single list item (batch5 P2).
_ENUM_CONTEXT_RE = re.compile(
    r"(?:причин\w*|вариант\w*|например|в\s+том\s+числе)\s+"
    r"[\w\-]+(?:\s*[,;/]\s*|\s+)[\w\-]+",
    re.IGNORECASE,
)
# Noise path/name stems when query domain does NOT include ZUP.
_TRADE_NOISE_RE = re.compile(
    r"КабинетСотрудника|Военкомат|ДляВоенкомата|СведенияОПриеме|"
    r"ИзвещениеОПриеме|ДанныеДляОформленияНаРаботу|"
    r"СЗВК|ПФР(?![а-яА-Я])",
    re.IGNORECASE,
)
# Noise when query looks ZUP and not trade/warehouse-stock.
_ZUP_NOISE_RE = re.compile(
    r"ЯндексМаркет|ВводОстатковТоваров|ПоступлениеТоваровНаСклад|"
    r"ТоварыНаСкладах|Маркетплейс",
    re.IGNORECASE,
)
# Dense payroll attribute / TC flood (dozens of clones in ERP dumps).
_PAYROLL_FLOOD_RE = re.compile(
    r"ТабличныеЧасти\.НДФЛ|"
    r"ЗачтеноАвансовыхПлатежей|"
    r"ЗачетАвансаНДФЛ|"
    r"КоэффициентыРаспределения(?:Пособия|СреднегоЗаработка|ДенежногоСодержания)|"
    r"ВыплатаБывшимСотрудникам|"
    r"ОплатаДнейУходаЗаДетьми",
    re.IGNORECASE,
)
# Invoice/waybill «накладная» sense (collides with overhead adj.).
_INVOICE_NAKLAD_RE = re.compile(
    r"ПоЗаказамНакладным|НакладнаяВозврат|ЗаказамИНакладным|"
    r"КОформлениюНакладных|ТранспортнаяНакладная|"
    r"НакладныеКлиент|ЗаказыНакладные",
    re.IGNORECASE,
)
# Returnable packaging island (collides with product «возврат»).
_RETURNABLE_PACKAGE_RE = re.compile(
    r"Возвратн\w*Тар|"
    r"Переданн\w*Возвратн|"
    r"Принят\w*Возвратн|"
    r"Распоряжен\w*НаОтгрузкуИВозврат|"
    r"Залогов\w*(?:Тар|Упаков)",
    re.IGNORECASE,
)
# ФСБУ 25 / finance-lease accounting island.
_FINANCE_LEASE_RE = re.compile(
    r"Кадастров|"
    r"Выкупн\w*(?:Стоимост|Аренд)|"
    r"Ликвидационн\w*Стоимост|"
    r"ИнвестицииВАренду|"
    r"СтоимостьПредметовАренды|"
    r"Предмет\w*Аренд|"
    r"Негарантированн|"
    r"Договор(?:а|ов)?Аренды|"
    r"(?:Взаиморасчет|Остатк)\w*ПоАренд|"
    r"ПоАренде|"
    r"Арендованн\w*ОС|"
    r"Финансов\w*Аренд|"
    r"УсловияДоговоровАренды|"
    r"ЗаключениеДоговораАренды|"
    r"ИзменениеУсловийДоговораАренды",
    re.IGNORECASE,
)
# U27: product islands that pollute Help-first when not asked.
_OFFDOMAIN_ISLANDS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    # (query must match to KEEP, path noise)
    (re.compile(r"ветис|меркур|всд", re.I), re.compile(r"ВЕТИС|ВетИС|ВСД")),
    (re.compile(r"яндекс", re.I), re.compile(r"ЯндексКасс|ЯндексМаркет")),
    (re.compile(r"гис\s*мт|честн\w*\s+знак|исмп|маркиров", re.I), re.compile(r"ИСМП|ГИСМТ|Честн\w*Знак")),
    (re.compile(r"документооборот|1сdo|бэд", re.I), re.compile(r"Документооборот|БЭД")),
)
_REPORT_INTENT_RE = re.compile(
    r"(?<!под)отч[её]т|отчетность|ведомост|скд|аналитик|свод|выгруз|"
    r"собрать\s+данн|за\s+период|в\s+разрезе|дебитор",
    re.IGNORECASE,
)
# Report-intent without bare «ведомост» — for ЗУП/payroll context where
# «ведомость» means a payroll sheet, not Отчеты.ВедомостьПоОС.
_REPORT_INTENT_NO_VEDOMOST_RE = re.compile(
    r"(?<!под)отч[её]т|отчетность|скд|аналитик|свод|выгруз|"
    r"собрать\s+данн|за\s+период|в\s+разрезе|дебитор",
    re.IGNORECASE,
)
# ЗУП / payroll markers: «ведомость» + сотрудник/начисление ≠ ОС/НМА-отчёт.
_SALARY_CONTEXT_RE = re.compile(
    r"зарплат|сотрудник|начислен|зуп|"
    r"ведомост\w*.*сотрудник|сотрудник.*ведомост|"
    r"начислен\w*.*сотрудник|сотрудник.*начислен|"
    r"ведомост\w*\s+по\s+зарплат",
    re.IGNORECASE,
)
# NL «поступление товаров от поставщика» → ПриобретениеТоваровУслуг
# (не ПоступлениеТоваровОтКлиента — омонимия «от»).
_PURCHASE_FROM_SUPPLIER_RE = re.compile(
    r"поступлен\w*\s+(?:товаров?\s+)?(?:и\s+услуг\s+)?от\s+поставщик|"
    r"оформить\s+поступлен\w*.*поставщик|"
    r"приобретен\w*\s+(?:товаров?\s+)?(?:и\s+услуг\s+)?(?:от\s+)?поставщик|"
    r"поступлен\w*\s+от\s+поставщик",
    re.IGNORECASE,
)

# U14: NL stems / phrases → ERP report names (maintainable alias map).
# Keys are casefold; token keys match if a query token startswith the key
# (len>=4) or equals it. Phrase keys are space-joined stems all present in query.
_ALIAS_TOKEN_MAP: dict[str, tuple[str, ...]] = {
    "дебиторск": (
        "ДебиторскаяЗадолженность",
        "ЗадолженностьКлиентов",
        "ВедомостьРасчетовСКлиентами",
    ),
    "дебиторка": (
        "ДебиторскаяЗадолженность",
        "ЗадолженностьКлиентов",
    ),
}
_ALIAS_PHRASE_MAP: dict[str, tuple[str, ...]] = {
    "задолженность клиент": (
        "ЗадолженностьКлиентов",
        "ВедомостьРасчетовСКлиентами",
        "ДебиторскаяЗадолженность",
    ),
    "по срокам": (
        "ЗадолженностьКлиентов",
        "ДебиторскаяЗадолженность",
    ),
}
# Document aliases (NL → Document name). NOT report aliases — SQL scopes them
# to Документы.*, and they must not trigger looks_like_report_query.
DOCUMENT_PATH_PREFIX_RU = "Документы."
_DOC_ALIAS_NAMES_PURCHASE = ("ПриобретениеТоваровУслуг",)


def query_tokens(query: str) -> list[str]:
    return [t.casefold() for t in _TOKEN_RE.findall(query or "") if t]


def query_tokens_cased(query: str) -> list[str]:
    """Letter tokens with original casing (for SQLite LIKE / Cyrillic CamelCase)."""
    return [t for t in _TOKEN_RE.findall(query or "") if t]


def _token_matches_alias_key(token: str, key: str) -> bool:
    if not token or not key:
        return False
    if token == key:
        return True
    # Stem prefix: «дебиторская» / «дебиторской» → дебиторск
    if len(key) >= 4 and token.startswith(key):
        return True
    return False


def alias_terms_for_query(query: str) -> list[str]:
    """Extra search terms from synonym/alias map (report discovery, U14)."""
    tokens = query_tokens(query)
    if not tokens:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        for key, terms in _ALIAS_TOKEN_MAP.items():
            if _token_matches_alias_key(tok, key):
                for term in terms:
                    cf = term.casefold()
                    if cf not in seen:
                        seen.add(cf)
                        found.append(term)
    q = " ".join(tokens)
    for phrase, terms in _ALIAS_PHRASE_MAP.items():
        parts = phrase.split()
        if parts and all(any(p in t or t.startswith(p) for t in tokens) for p in parts):
            for term in terms:
                cf = term.casefold()
                if cf not in seen:
                    seen.add(cf)
                    found.append(term)
        elif phrase in q:
            for term in terms:
                cf = term.casefold()
                if cf not in seen:
                    seen.add(cf)
                    found.append(term)
    return found


def document_alias_terms_for_query(query: str) -> list[str]:
    """NL → Document names (purchase-from-supplier and similar).

    Kept separate from report aliases so SQL can scope to ``Документы.*`` and
    so these terms do not flip ``looks_like_report_query``.
    """
    raw = query or ""
    found: list[str] = []
    seen: set[str] = set()
    if _PURCHASE_FROM_SUPPLIER_RE.search(raw):
        for term in _DOC_ALIAS_NAMES_PURCHASE:
            cf = term.casefold()
            if cf not in seen:
                seen.add(cf)
                found.append(term)
    return found


def all_alias_terms_for_query(query: str) -> list[str]:
    """Report + document alias terms (FTS OR expansion / hit boost)."""
    seen: set[str] = set()
    out: list[str] = []
    for term in alias_terms_for_query(query) + document_alias_terms_for_query(query):
        cf = term.casefold()
        if cf not in seen:
            seen.add(cf)
            out.append(term)
    return out


def looks_like_salary_context(query: str) -> bool:
    """ЗУП / payroll NL («ведомость» + сотрудник/начисление)."""
    return bool(_SALARY_CONTEXT_RE.search(query or ""))


def looks_like_report_query(query: str) -> bool:
    """True when NL intent is report discovery (or report-alias map fired).

    In payroll context bare «ведомост» does not count — it is a salary sheet,
    not ``Отчеты.ВедомостьПоОС``.
    """
    raw = (query or "").strip()
    if not raw:
        return False
    if looks_like_salary_context(raw):
        if _REPORT_INTENT_NO_VEDOMOST_RE.search(raw):
            return True
        return bool(alias_terms_for_query(raw))
    if _REPORT_INTENT_RE.search(raw):
        return True
    return bool(alias_terms_for_query(raw))


def looks_like_howto_query(query: str) -> bool:
    """Light U17: «как запросом получить …»."""
    return bool(_HOWTO_QUERY_RE.search(query or ""))


def looks_like_user_howto_query(query: str) -> bool:
    """U23: mass FAQ how-to («Как…?», «Можно ли…?», «Где…?»).

    Broader than ``looks_like_howto_query`` (BSL «запросом получить») and
    ``_HELP_QUERY_RE`` (fill/setup verbs). Drives help-first merge + owner boost.
    """
    raw = query or ""
    if not raw.strip():
        return False
    return bool(
        _USER_HOWTO_RE.search(raw)
        or _HELP_QUERY_RE.search(raw)
        or looks_like_howto_query(raw)
        or looks_like_fallback_query(raw)
        or looks_like_query_join_intent(raw)
    )


def looks_like_settings_constraint_query(query: str) -> bool:
    """U25: rights / «не мог редактировать» / ограничить перечень скидок."""
    return bool(_SETTINGS_CONSTRAINT_RE.search(query or ""))


def looks_like_rls_query(query: str) -> bool:
    """U28: RLS / record-level access how-to."""
    return bool(_RLS_INTENT_RE.search(query or ""))


def looks_like_production_schedule_query(query: str) -> bool:
    """U29: production schedule / equipment availability."""
    return bool(_PROD_SCHEDULE_INTENT_RE.search(query or ""))


def looks_like_integration_http_query(query: str) -> bool:
    """U30: HTTP service / WMS / external integration."""
    return bool(_INTEGRATION_INTENT_RE.search(query or ""))


def looks_like_accounting_policy_query(query: str) -> bool:
    """U33: enable/configure accounting feature (FO / Constant class)."""
    return bool(_ACCOUNTING_POLICY_RE.search(query or ""))


def looks_like_compare_query(query: str) -> bool:
    """«В чём разница между A и B» — hint to compare_objects."""
    return bool(
        re.search(
            r"в\s+чём\s+разница|чем\s+отлича|разница\s+между|отличие\s+между|"
            r"сравн\w*\s+(?:два|документ|объект)",
            query or "",
            re.IGNORECASE,
        )
    )


def looks_like_query_join_intent(query: str) -> bool:
    """U36: write a query / JOIN registers / which fields."""
    return bool(_QUERY_JOIN_INTENT_RE.search(query or ""))


def looks_like_pricing_intent(query: str) -> bool:
    """U36: prices + FX auto-recalc or series / shelf-life pricing."""
    return bool(_PRICING_INTENT_RE.search(query or ""))


def looks_like_pricing_fx_intent(query: str) -> bool:
    """U37: price auto-recalc driven by FX / currency rate."""
    return bool(_PRICING_FX_INTENT_RE.search(query or ""))


def looks_like_pricing_series_intent(query: str) -> bool:
    """U37: separate prices for series / shelf life."""
    return bool(_PRICING_SERIES_INTENT_RE.search(query or ""))


def looks_like_report_reconcile_intent(query: str) -> bool:
    """U36: P&L / ОПиУ vs ОСВ mismatch."""
    return bool(_RECONCILE_REPORT_RE.search(query or ""))


def looks_like_budget_exec_intent(query: str) -> bool:
    """U38: budget execution report — limit → claims → fact → remainder."""
    return bool(_BUDGET_EXEC_INTENT_RE.search(query or ""))


def looks_like_repair_overdue_intent(query: str) -> bool:
    """U38: overdue / warranty repair orders + norms / deadlines."""
    return bool(_REPAIR_OVERDUE_INTENT_RE.search(query or ""))


_RECONCILE_OSV_RE = re.compile(r"ОборотноСальд", re.IGNORECASE)
_RECONCILE_PNL_RE = re.compile(
    r"АнализФинансовых|ФинансовыйРезультат|Прибыл\w*ИУбыт|ДоходыИРасходы|"
    r"ВаловаяПрибыль|ОтчетОФинансовых",
    re.IGNORECASE,
)
_PRICING_FX_ANCHOR_RE = re.compile(
    r"ОбновлениеЦен|КурсыВалют|Пересчитыв\w*Цен|ПересчитыватьЦены",
    re.IGNORECASE,
)
_PRICING_FX_NOISE_RE = re.compile(
    r"ПроцентИзменен|Ставк\w*НДС|НДС|ВыборПроцента",
    re.IGNORECASE,
)


def reconcile_sides_covered(hits: list[dict[str, Any]]) -> tuple[bool, bool]:
    """Whether hit pool has ОСВ-side and ОПиУ/финрезультат-side roots."""
    osv = pnl = False
    for h in hits:
        path = str(h.get("path") or "")
        name = str(h.get("name") or "")
        hay = f"{path} {name}"
        if _RECONCILE_OSV_RE.search(hay):
            osv = True
        if _RECONCILE_PNL_RE.search(hay):
            pnl = True
        if osv and pnl:
            break
    return osv, pnl


def pricing_fx_anchor_hit(hits: list[dict[str, Any]]) -> bool:
    """True if FX-pricing recall found job/rate/auto-recalc anchor."""
    for h in hits:
        hay = f"{h.get('path') or ''} {h.get('name') or ''}"
        if _PRICING_FX_ANCHOR_RE.search(hay):
            return True
    return False


def budget_exec_chain_covered(hits: list[dict[str, Any]]) -> bool:
    """U38: limit side + claim/fact/articles side for budget execution."""
    limit = claim_or_fact = False
    for h in hits:
        hay = f"{h.get('path') or ''} {h.get('name') or ''}"
        if _BUDGET_LIMIT_RE.search(hay):
            limit = True
        if _BUDGET_CLAIM_OR_FACT_RE.search(hay):
            claim_or_fact = True
        if limit and claim_or_fact:
            return True
    return False


def repair_overdue_anchor_hit(hits: list[dict[str, Any]]) -> bool:
    """U38: ЗаказНаРемонт plus schedule/norm/plan register."""
    order = schedule = False
    for h in hits:
        hay = f"{h.get('path') or ''} {h.get('name') or ''}"
        if _REPAIR_ORDER_RE.search(hay):
            order = True
        if _REPAIR_SCHEDULE_RE.search(hay):
            schedule = True
        if order and schedule:
            return True
    return False


def phrase_to_metadata_name_candidates(phrase: str) -> list[str]:
    """NL phrase → CamelCase name candidates (Заказы на производство → …)."""
    raw = (phrase or "").strip().replace("ё", "е").replace("Ё", "Е")
    if not raw:
        return []
    # Normalize hyphen variants in заказ-наряд before tokenization.
    raw = re.sub(r"[\u2011\u2013\u2014\-]+", "-", raw)
    words = _TOKEN_RE.findall(raw)
    if not words:
        return []
    # Keep connectors as CamelCase segments (ЗаказыНаПроизводство).
    with_conn: list[str] = []
    for w in words:
        cf = w.casefold()
        if cf in _PHRASE_STOPWORDS:
            if with_conn:
                with_conn.append(w[:1].upper() + w[1:].casefold())
            continue
        with_conn.append(w[:1].upper() + w[1:].casefold())
    significant = [
        w[:1].upper() + w[1:].casefold()
        for w in words
        if w.casefold() not in _PHRASE_STOPWORDS
    ]
    out: list[str] = []
    for cand in ("".join(with_conn), "".join(significant)):
        if len(cand) < 4:
            continue
        out.append(cand)
        # Stem for LIKE recall (ЗаказыНаПроизводство → ЗаказыНаПроизвод).
        if len(cand) > 12:
            out.append(cand[:12])
        elif len(cand) > 8:
            out.append(cand[: max(8, len(cand) - 2)])
    # Soft synonyms from phrase tokens (not FAQ aliases).
    wcf = [w.casefold() for w in words]
    if any(w.startswith("расчет") for w in wcf) and any(
        "контрагент" in w for w in wcf
    ):
        out.extend(
            (
                "РасчетыСКлиентами",
                "ВзаиморасчетыСКонтрагентами",
                "РасчетыСПоставщиками",
            )
        )
    if any(w.startswith("договор") for w in wcf):
        out.append("ДоговорыКонтрагентов")
    if any("график" in w for w in wcf) and any(
        w.startswith("оплат") for w in wcf
    ):
        out.extend(("ГрафикОплатыКлиентам", "ГрафикиОплаты"))
    # «Заказ-наряд на ремонт» / «заказ наряд на ремонт» → ERP Документы.ЗаказНаРемонт
    if any(w.startswith("ремонт") for w in wcf) and (
        any(w.startswith("наряд") for w in wcf)
        or any(w.startswith("заказ") for w in wcf)
    ):
        out.insert(0, "ЗаказНаРемонт")
    if any(w.startswith("стать") for w in wcf) and any(
        w.startswith("бюджет") for w in wcf
    ):
        out.insert(0, "СтатьиБюджетов")
    # Single inflected word: prefer morphology stem (пересортице → Пересорти).
    if len(significant) == 1:
        stemmed = nl_token_to_metadata_stem(words[0])
        if stemmed:
            out.insert(0, stemmed)
    return list(dict.fromkeys(out))


def _prefixes_for_typed_head(head: str) -> list[str]:
    h = (head or "").casefold()
    if "накопления" in h:
        return ["РегистрыНакопления."]
    if "сведений" in h:
        return ["РегистрыСведений."]
    if h.startswith("регистр"):
        # Ambiguous «регистр» — AccumReg first; RS only wastes stem budget.
        return ["РегистрыНакопления."]
    if h.startswith("справочник"):
        return ["Справочники."]
    if h.startswith("документ"):
        return ["Документы."]
    if h.startswith("отч"):
        return ["Отчеты."]
    if h.startswith("обработк"):
        return ["Обработки."]
    return []


def _phrase_word_count(phrase: str) -> int:
    return len(_TOKEN_RE.findall(phrase or ""))


def extract_named_entity_specs(query: str) -> list[tuple[str, str]]:
    """U36/U40: ``(path_prefix, stem)`` from typed/quoted metadata names.

    Not a FAQ alias map — stems come from the user's wording («Заказы на производство»).
    Bare topic quotes («пересортице») are booked before typed fan-out so long
    how-tos with several «регистр/документ …» still recall the subject term.
    """
    q = query or ""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(prefix: str, stem: str) -> None:
        if len(out) >= MAX_NAMED_ENTITY_SPECS:
            return
        stem = (stem or "").strip()
        if len(stem) < 4:
            return
        key = (prefix, stem)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    typed_spans: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _NAMED_ENTITY_TYPED_RE.finditer(q)
    ]

    def _add_lean(prefixes: list[str], phrase: str, *, single_word_prefs: bool) -> None:
        cands = phrase_to_metadata_name_candidates(phrase)
        if not cands or not prefixes:
            return
        primary = cands[0]
        prefs = prefixes
        if single_word_prefs and _phrase_word_count(phrase) <= 1:
            prefs = ["Документы.", "РегистрыНакопления."]
        for pref in prefs:
            add(pref, primary)
        # Multi-word: one shorter twin for LIKE (ОстаткиПоСериям → ОстаткиПоСер).
        if _phrase_word_count(phrase) >= 2 and len(cands) > 1 and cands[1] != primary:
            add(prefs[0], cands[1])

    # 1) Bare quotes first (topic emphasis not covered by typed spans).
    for m in _NAMED_ENTITY_QUOTED_RE.finditer(q):
        if any(s <= m.start() and m.end() <= e for s, e in typed_spans):
            continue
        phrase = m.group(1)
        if _ARROW_CHAIN_QUOTE_RE.search(phrase):
            continue
        _add_lean(
            [
                "Документы.",
                "РегистрыНакопления.",
                "РегистрыСведений.",
                "Справочники.",
            ],
            phrase,
            single_word_prefs=True,
        )

    # 2) Typed «регистр/документ …» — one prefix family, lean candidates.
    for m in _NAMED_ENTITY_TYPED_RE.finditer(q):
        phrase = m.group(2)
        if _ARROW_CHAIN_QUOTE_RE.search(phrase):
            continue
        prefixes = _prefixes_for_typed_head(m.group(1))
        _add_lean(prefixes, phrase, single_word_prefs=False)

    return out


def named_entity_name_list(query: str) -> list[str]:
    """Unique CamelCase candidates for ``search_by_exact_names``."""
    names: list[str] = []
    seen: set[str] = set()
    q = query or ""
    phrases: list[str] = []
    for m in _NAMED_ENTITY_TYPED_RE.finditer(q):
        ph = m.group(2)
        if not _ARROW_CHAIN_QUOTE_RE.search(ph):
            phrases.append(ph)
    for m in _NAMED_ENTITY_QUOTED_RE.finditer(q):
        ph = m.group(1)
        if not _ARROW_CHAIN_QUOTE_RE.search(ph):
            phrases.append(ph)
    for ph in phrases:
        for cand in phrase_to_metadata_name_candidates(ph):
            # Prefer full CamelCase over truncated stems for exact match.
            if cand in seen or len(cand) < 6:
                continue
            # Skip short stems that are truncations (same prefix as a longer name).
            if any(n.startswith(cand) and n != cand for n in names):
                continue
            seen.add(cand)
            names.append(cand)
    names.sort(key=lambda n: (-len(n), n))
    return names[:12]


def term_only_in_enumeration(query: str, stem: str) -> bool:
    """True when ``stem`` appears once and only inside a reasons/variants list.

    Class-level guard for exact-inject over-fire (production→labor_load,
    goods_return on «причины … возврат, ошибка», …): a single comma/space
    list item after «причины/варианты/например» is not the query subject.
    """
    raw = query or ""
    stem_cf = (stem or "").casefold().replace("ё", "е")
    if len(stem_cf) < 4:
        return False
    qcf = raw.casefold().replace("ё", "е")
    matches = [m.start() for m in re.finditer(re.escape(stem_cf), qcf)]
    if len(matches) != 1:
        return False
    pos = matches[0]
    for m in _ENUM_CONTEXT_RE.finditer(raw):
        if m.start() <= pos < m.end():
            return True
    return False


def query_domains(query: str) -> frozenset[str]:
    """U24: soft topic tags for cross-domain noise penalties."""
    raw = query or ""
    found: set[str] = set()
    if _DOMAIN_TRADE_RE.search(raw):
        found.add("trade")
    if _DOMAIN_ZUP_RE.search(raw):
        found.add("zup")
    if _DOMAIN_PLAN_RE.search(raw):
        found.add("plan")
    if _DOMAIN_WH_RE.search(raw):
        found.add("warehouse")
    if _DOMAIN_PROD_RE.search(raw):
        found.add("production")
    if _DOMAIN_LABOR_LOAD_RE.search(raw):
        found.add("labor_load")
    if _DOMAIN_COST_RE.search(raw):
        found.add("costing")
    if _DOMAIN_SETTLEMENT_RE.search(raw):
        if not term_only_in_enumeration(raw, "аванс"):
            found.add("settlement")
    if _DOMAIN_OVERHEAD_RE.search(raw):
        found.add("overhead")
    if _DOMAIN_WH_OPS_COST_RE.search(raw):
        if not term_only_in_enumeration(raw, "аренда"):
            found.add("warehouse_ops")
    if _DOMAIN_GOODS_RETURN_RE.search(raw):
        if not term_only_in_enumeration(raw, "возврат"):
            found.add("goods_return")
    return frozenset(found)


def zup_is_incidental(query: str) -> bool:
    """True when «зарплата» is a cost line, not the ZUP subject of the query."""
    domains = query_domains(query)
    if "zup" not in domains:
        return False
    return bool(
        domains & {
            "warehouse",
            "warehouse_ops",
            "production",
            "trade",
            "settlement",
            "overhead",
            "costing",
        }
    )


def intent_stem_recall_specs(query: str) -> list[tuple[str, str]]:
    """U28–U33: ``(path_prefix, stem)`` pairs for scoped SQL recall.

    Prefer small trees first (Роли/ФО/HTTPСервисы) before Документы/ОбщиеМодули.
    Cap at ``MAX_INTENT_STEM_SPECS``; Document/HTTP trees further limited.
    """
    q = query or ""
    qcf = q.casefold()
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    doc_n = 0
    http_n = 0

    def add(prefix: str, stem: str) -> None:
        nonlocal doc_n, http_n
        if len(out) >= MAX_INTENT_STEM_SPECS:
            return
        if prefix.startswith("Документы."):
            if doc_n >= MAX_DOCUMENT_STEM_SPECS:
                return
            doc_n += 1
        if prefix.startswith("HTTPСервисы."):
            if http_n >= MAX_HTTP_STEM_SPECS:
                return
            http_n += 1
        key = (prefix, stem)
        if key not in seen:
            seen.add(key)
            out.append(key)

    if looks_like_rls_query(q):
        add("Роли.", "Ограничен")
        add("Константы.", "ОграничиватьДоступ")
        add("ФункциональныеОпции.", "ОграничиватьДоступ")

    if looks_like_production_schedule_query(q):
        add("Справочники.", "РабочиеЦентры")
        add("РегистрыСведений.", "ГрафикЭтап")
        add("Обработки.", "ПланированиеГрафика")
        add("Отчеты.", "ГрафикПроизвод")

    if looks_like_integration_http_query(q):
        # Cheap HTTP tree only (+ one ESB processor); skip ОбщиеМодули scan.
        add("HTTPСервисы.", "HTTP")
        add("Обработки.", "ESB")

    # U31/U36 domain packs BEFORE bare policy «Использовать» (that floods FO).
    if "давальч" in qcf or (
        "переработ" in qcf
        and ("заказчик" in qcf or "сырь" in qcf or "материал" in qcf)
    ):
        add("ОбщиеМодули.", "Давальческ")
        add("Документы.", "Давальц")
        add("Документы.", "ЗаказДавальца")
        add("ФункциональныеОпции.", "Давальческ")
        add("Роли.", "Давальческ")

    if (
        "ордерн" in qcf
        or re.search(r"зон\w*\s+(?:приём|прием|хранен|отгруз)", qcf)
        or ("ордер" in qcf and "склад" in qcf)
    ):
        add("ФункциональныеОпции.", "ИспользоватьОрдерн")
        add("Документы.", "ПриходныйОрдерНаТовары")
        add("Документы.", "РасходныйОрдерНаТовары")
        add("Справочники.", "СкладскиеПомещ")

    if looks_like_pricing_fx_intent(q):
        # FX auto-recalc first — not manual price-list / % change Help.
        add("РегламентныеЗадания.", "ОбновлениеЦен")
        add("РегистрыСведений.", "КурсыВалют")
        add("Константы.", "Пересчитыв")
        add("ФункциональныеОпции.", "Пересчитыв")
        add("ОбщиеКартинки.", "Пересчитыв")
        add("РегистрыСведений.", "ЦеныНоменклатуры")
        add("Документы.", "УстановкаЦен")
    elif looks_like_pricing_series_intent(q) or looks_like_pricing_intent(q):
        add("РегистрыСведений.", "ЦеныНоменклатуры")
        add("Документы.", "УстановкаЦен")
        add("Константы.", "Пересчитыв")
        add("Обработки.", "ПрайсЛист")

    if looks_like_report_reconcile_intent(q):
        # Both sides of the pair — ОПиУ/финрезультат before flooding ОСВ variants.
        add("Отчеты.", "АнализФинансовых")
        add("Отчеты.", "ФинансовыйРезультат")
        add("Отчеты.", "ОборотноСальдовая")
        add("Отчеты.", "ВаловаяПрибыль")

    if looks_like_budget_exec_intent(q):
        # Cheap trees only — Документы.* name-prefix was 1–2s/stem on ERP;
        # Document roots come from exact-name counter-recall below.
        add("РегистрыНакопления.", "ЛимитыПоДаннымБюджетирования")
        add("Справочники.", "СтатьиБюджетов")
        add("Справочники.", "ПравилаПолученияФакта")
        add("Справочники.", "ПравилаЛимитов")

    if looks_like_repair_overdue_intent(q):
        # Real ERP names; quoted «Нормативы сроков» often missing as roots.
        add("Документы.", "ЗаказНаРемонт")
        add("РегистрыСведений.", "ПланРемонтов")
        add("РегистрыСведений.", "РемонтыРабочих")
        add("Обработки.", "ПланированиеРемонт")
        add("РегистрыСведений.", "НормативыСроков")
        add("РегистрыНакопления.", "ФактическиеСроки")

    if looks_like_query_join_intent(q):
        if "резерв" in qcf:
            add("РегистрыНакопления.", "РезервыТоваров")
            add("РегистрыНакопления.", "ТоварыКОтгрузке")
        if "ресурсн" in qcf or "спецификац" in qcf:
            add("Справочники.", "Ресурсн")
        if "потреб" in qcf and "материал" in qcf:
            add("РегистрыНакопления.", "ЗаказыНаПроизвод")
            add("РегистрыСведений.", "ЗаказыНаПроизвод")
            add("Отчеты.", "Потреблен")

    # U33: FO/Constant for «включить схему / учёт» — specific stems only when
    # domain packs above did not already fill the list.
    if looks_like_accounting_policy_query(q):
        if "ордерн" in qcf or ("ордер" in qcf and "схем" in qcf):
            add("ФункциональныеОпции.", "ИспользоватьОрдерн")
            add("Константы.", "ИспользоватьОрдерн")
        if (
            "фифо" in qcf
            or "сери" in qcf
            or ("себестоим" in qcf and "партион" in qcf)
        ):
            add("ФункциональныеОпции.", "УчитыватьСебестоимостьПоСериям")
            add("Константы.", "УчитыватьСебестоимостьПоСериям")
        domain_busy = bool(out) or "давальч" in qcf or looks_like_pricing_intent(q)
        if not out and not domain_busy:
            add("ФункциональныеОпции.", "Использовать")
            add("Константы.", "Использовать")

    if "фифо" in qcf or (
        "себестоим" in qcf
        and ("сери" in qcf or ("срок" in qcf and "годност" in qcf))
    ):
        add("ФункциональныеОпции.", "УчитыватьСебестоимостьПоСериям")
        add("Константы.", "УчитыватьСебестоимостьПоСериям")

    # U36/U40: quoted/typed names first (explicit), then distinctive NL fillers.
    for pref, stem in extract_named_entity_specs(q):
        add(pref, stem)

    # Domain tags drive penalties + exact-name counter-recall in search.py
    # (``_domain_counter_exact_hits``). Do not add Document/Report stem LIKE
    # here — that was the cold sql_recall spike.

    if looks_like_user_howto_query(q) or looks_like_query_join_intent(q):
        for pref, stem in distinctive_nl_stem_specs(q):
            add(pref, stem)

    return out[:MAX_INTENT_STEM_SPECS]


def is_policy_meta_kind(kind: str) -> bool:
    """Constant / FO including report-index Russian collection kinds."""
    return kind in {
        "Constant",
        "Константы",
        "FunctionalOption",
        "ФункциональныеОпции",
        "Role",
        "Роли",
    }


def looks_like_fallback_query(query: str) -> bool:
    """U20: query implies coalesce/fallback intent («из ТТН, иначе из фмРейс»)."""
    return bool(_FALLBACK_QUERY_RE.search(query or ""))


def stem_spec_cost_key(pref_stem: tuple[str, str]) -> tuple:
    """Sort key: cheaper metadata trees before Документы.*."""
    pref, stem = pref_stem
    return (_STEM_PREFIX_COST.get(pref, 9), -len(stem or ""), pref, stem or "")


def lexical_name_prefixes(query: str) -> list[str]:
    """U41: CamelCase / morphology prefixes for cheap name-index recall + FTS OR.

    Not a FAQ map — derived from quotes and distinctive NL tokens only.
    Prefer short topic stems (bare quotes / distinctive) before long typed
    CamelCase names so «пересортице» is not crowded out of the cap.
    Verbish NL (рассчитывает / скользящую) is dropped — those stems made
    ``search_by_name_prefixes`` multi-second on large Docker volumes.
    """
    q = query or ""
    if not (
        looks_like_user_howto_query(q)
        or looks_like_query_join_intent(q)
        or extract_named_entity_specs(q)
    ):
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        n = (name or "").strip()
        if len(n) < 5 or not _is_lexical_sql_stem(n):
            return
        key = n.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(n)

    # 1) Distinctive NL + named-entity stems (short, high signal).
    for _, stem in distinctive_nl_stem_specs(q):
        add(stem)
    for _, stem in extract_named_entity_specs(q):
        add(stem)
    # 2) Full CamelCase candidates (may be long).
    for n in named_entity_name_list(q):
        add(n)
        if len(n) > 10:
            add(n[: max(8, len(n) - 2)])
    return out[:8]


def lexical_name_prefixes_for_sql(query: str, *, max_n: int = 2) -> list[str]:
    """Stricter cap for SQLite name-prefix lane (Docker ERP tax per stem)."""
    return lexical_name_prefixes(query)[: max(1, min(int(max_n), 4))]


def expand_fts_query(query: str) -> str:
    """Append ``*`` to long tokens; OR-in alias report/document names (U14+P0).

    U41: also OR lexical CamelCase prefixes so hybrid FTS hits ERP names
    without waiting for SQL stem LIKE.

    Leaves queries with existing FTS operators unchanged. Tokens shorter than
    ``MIN_PREFIX_TOKEN_LEN`` (e.g. «нулев») are not prefix-expanded.
    """
    raw = (query or "").strip()
    if not raw or _HAS_FTS_OP.search(raw):
        return raw
    parts: list[str] = []
    changed = False
    for tok in raw.split():
        letters = _TOKEN_RE.findall(tok)
        core = letters[0] if letters else tok
        if len(core) >= MIN_PREFIX_TOKEN_LEN and not tok.endswith("*"):
            parts.append(tok + "*")
            changed = True
        else:
            parts.append(tok)
    base = " ".join(parts) if changed else raw
    extras: list[str] = []
    q_cf = raw.casefold()
    for a in all_alias_terms_for_query(raw):
        if a.casefold() in q_cf:
            continue
        extras.append(a + "*" if len(a) >= MIN_PREFIX_TOKEN_LEN else a)
    # Lexical CamelCase stems: always OR — NL «пересортице» must not block
    # FTS term Пересорти* (substring skip would drop the useful form).
    for a in lexical_name_prefixes(raw):
        extras.append(a + "*" if len(a) >= MIN_PREFIX_TOKEN_LEN else a)
    # Dedupe while preserving order.
    extras = list(dict.fromkeys(extras))[:6]
    if not extras:
        return base
    return f"({base}) OR " + " OR ".join(extras)


def _token_in_hay(token: str, hay: str) -> bool:
    if not token:
        return False
    if token in hay:
        return True
    if len(token) >= MIN_PREFIX_TOKEN_LEN:
        stem = token[: max(MIN_PREFIX_TOKEN_LEN, len(token) - 2)]
        if stem in hay:
            return True
    return False


def token_coverage(tokens: list[str], hit: dict[str, Any]) -> float:
    """Share of query tokens found as substrings in name/synonym/path.

    More specific longer names with full overlap rank above a universal prefix
    match (``ВводОстатковСПодотчетниками`` > ``ВводОстатков``).
    Long tokens also match a short stem so NL «подотчетным» hits «Подотчетниками».
    """
    if not tokens:
        return 0.0
    name = str(hit.get("name") or "").casefold()
    syn = str(hit.get("synonym") or "").casefold()
    path = str(hit.get("path") or "").casefold()
    hay = f"{name} {syn} {path}"
    scored = [t for t in tokens if len(t) >= MIN_PREFIX_TOKEN_LEN] or tokens
    hits_n = sum(1 for t in scored if _token_in_hay(t, hay))
    coverage = hits_n / len(scored)
    specificity = 0.0
    if hits_n == len(scored) and len(scored) > 1:
        specificity = 0.35 + min(len(name), 80) / 200.0
    elif coverage >= 0.5:
        specificity = coverage * 0.15
    return coverage + specificity


# Filler / grammar tokens — not used for multi-token coverage.
_CONTENT_STOPWORDS = frozenset(
    {
        "как",
        "где",
        "что",
        "какие",
        "какой",
        "какая",
        "какое",
        "для",
        "при",
        "или",
        "также",
        "через",
        "между",
        "после",
        "перед",
        "этот",
        "эта",
        "это",
        "эти",
        "том",
        "того",
        "чтобы",
        "когда",
        "если",
        "можно",
        "нужно",
        "есть",
        "быть",
        "был",
        "была",
        "были",
        "будет",
        "опишите",
        "объясните",
        "назовите",
        "получите",
        "получить",
        "найти",
        "смотрите",
        "смотреть",
        "используют",
        "использовать",
        "отвечают",
        "устроено",
        "настроено",
        "обеспечивают",
        "формирование",
        "детализацией",
        "разрезе",
        "учетом",
        "учётом",
        "случаев",
        "случае",
        "логику",
        "правила",
        "порядок",
        "этапов",
        "ошибки",
        "конфигурации",
        "системе",
        "erp",
        "1с",
        "на",
        "по",
        "с",
        "и",
        "в",
        "от",
        "к",
        "о",
        "об",
        "из",
        "у",
        "а",
        "но",
        "же",
        "ли",
        "не",
        "ни",
        "да",
        "нет",
        "всё",
        "все",
        "ещё",
        "еще",
        "уже",
        "только",
        "также",
        "со",
        "во",
        "кто",
        "чем",
        "чьи",
        "их",
        "его",
        "её",
        "ее",
        "мы",
        "вы",
        "они",
        "он",
        "она",
        "оно",
        # Query-construction / meta-kind words (U40) — not real domain content.
        "составьте",
        "составить",
        "напишите",
        "напиши",
        "запрос",
        "запроса",
        "запросом",
        "соедините",
        "соединить",
        "соединив",
        "объедините",
        "объединить",
        "объединив",
        "свяжите",
        "связать",
        "документы",
        "документ",
        "документом",
        "регистры",
        "регистр",
        "регистров",
        "справочники",
        "справочник",
        "отчеты",
        "отчёт",
        "отчет",
        "обработки",
        "обработка",
        "накопления",
        "сведений",
        "константы",
        "перечисления",
        "подсистемы",
        "выявления",
        "формирует",
        "формирование",
        "рассматривает",
        "данных",
        "данными",
        "например",
    }
)
# High-frequency stems: alone they must not own #1 on how-to discovery.
_WEAK_CONTENT_STEMS = (
    "обмен",
    "интеграц",
    "продаж",
    "распределен",
    "регламент",
    "склад",
    "сообщен",
    "статус",
    "задач",
    "настройк",
    "операц",
    "документ",
    "регистр",
    "справочник",
    "обработк",
    "подсистем",
    "модул",
    "процедур",
    "функци",
    # Ultra-generic NL stems: mid-LIKE under Документы.* is multi-second on ERP
    # and floods payroll/cost clones («коэффициент», «стоимость»).
    "коэффициент",
    "стоимость",
    "эффективн",
)
# How-to process words: long but non-subject — must not own distinctive recall.
_PROCESS_NL_STEMS = (
    "сопоставлен",
    "расхожден",
    "фактическ",
    "учетн",
    "расчет",
    "рассчит",  # рассчитывает — not a metadata name prefix
    "скольз",  # скользящую потребность → noise stem Скользящ
    "напиш",
    "состав",
    "опиш",
    "выяв",
    "избеж",
    "контрол",
    "смоделир",
    "детализац",
    "приоритет",
    "объём",
    "объем",
    "описан",
    "логик",
    "получен",
    "вывест",
    "сгруппир",
    "объедин",
    "соединен",
    "который",
    "которая",
    "которое",
    "которые",
    "вперед",
    "вперёд",
)
# Verb / participle tails after casefold (howto NL → false metadata stems).
_VERBISH_NL_TAIL_RE = re.compile(
    r"(?:ает|яет|ует|иет|ают|яют|ал[аи]?|ял[аи]?|ил[аи]?|"
    r"ать|ять|ить|еть|ающ|яющ|ующ|ющ|ящ\w*|ащ\w*)$"
)
_KIND_PRIOR_JOB_RE = re.compile(
    r"регламентн\w*\s+задан|задан\w*\s+регламент",
    re.IGNORECASE,
)
_KIND_PRIOR_CODE_RE = re.compile(
    r"общ\w*\s+модул|где\s+в\s+код|в\s+код[еа]|"
    r"процедур\w*|функци\w*\s+(?:общ|менеджер)|менеджер\w*\s+модул",
    re.IGNORECASE,
)
_KIND_PRIOR_HTTP_RE = re.compile(
    r"\bhttp\b|http-?сервис|план\w*\s+обмен|wms|esb",
    re.IGNORECASE,
)
_KIND_PRIOR_JOIN_META_RE = re.compile(
    r"соедини\w*|свяжи\w*|объедини\w*|напишите\s+запрос|напиши\s+запрос",
    re.IGNORECASE,
)
_POLICY_NOISE_KINDS = frozenset(
    {
        "FunctionalOption",
        "ФункциональныеОпции",
        "DefinedType",
        "ОпределяемыеТипы",
        "CommonPicture",
        "ОбщиеКартинки",
        "StyleItem",
        "ЭлементыСтиля",
    }
)


def query_content_tokens(query: str) -> list[str]:
    """Significant NL tokens for multi-token coverage (U39/U40)."""
    raw = (query or "").replace("ё", "е").replace("Ё", "Е")
    out: list[str] = []
    seen: set[str] = set()
    for t in query_tokens(raw):
        if len(t) < MIN_CONTENT_TOKEN_LEN:
            continue
        if t in _CONTENT_STOPWORDS or t in _PHRASE_STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _content_token_in_hay(token: str, hay: str) -> bool:
    """Match content token with a shorter stem than FTS prefix expand."""
    if not token or not hay:
        return False
    tok = token.replace("ё", "е")
    if tok in hay:
        return True
    if len(tok) >= MIN_CONTENT_TOKEN_LEN:
        stem = tok[: max(MIN_CONTENT_TOKEN_LEN, len(tok) - 2)]
        if stem in hay:
            return True
    return False


def hit_content_matches(query: str, hit: dict[str, Any]) -> list[str]:
    """Content tokens from the query that appear in the hit."""
    tokens = query_content_tokens(query)
    if not tokens:
        return []
    hay = (
        f"{hit.get('name') or ''} {hit.get('synonym') or ''} {hit.get('path') or ''}"
    ).casefold().replace("ё", "е")
    return [t for t in tokens if _content_token_in_hay(t, hay)]


def _is_weak_content_token(token: str) -> bool:
    t = (token or "").casefold().replace("ё", "е")
    return any(t.startswith(stem) or stem in t for stem in _WEAK_CONTENT_STEMS)


def _is_process_nl_token(token: str) -> bool:
    t = (token or "").casefold().replace("ё", "е")
    if any(t.startswith(stem) for stem in _PROCESS_NL_STEMS):
        return True
    # Conjugated verbs / participles → never path-prefix metadata stems.
    return bool(_VERBISH_NL_TAIL_RE.search(t))


def _is_lexical_sql_stem(stem: str) -> bool:
    """Keep only stems that can plausibly be ERP CamelCase name prefixes."""
    s = (stem or "").strip()
    if len(s) < 5:
        return False
    # Reject verbish after nl_token_to_metadata_stem (Рассчитывает, Скользящ…).
    if _is_process_nl_token(s) or _is_process_nl_token(s.casefold()):
        return False
    cf = s.casefold().replace("ё", "е")
    if _VERBISH_NL_TAIL_RE.search(cf):
        return False
    return True


def _quoted_span_blob(query: str) -> str:
    """Lowercase blob of all «…» / \"…\" spans for quote-priority ranking."""
    parts = [m.group(1) for m in _NAMED_ENTITY_QUOTED_RE.finditer(query or "")]
    return " ".join(parts).casefold().replace("ё", "е")


def _paren_span_blob(query: str) -> str:
    """Lowercase blob of (…) enumerations — often short register nouns."""
    parts = re.findall(r"\(([^)]{2,80})\)", query or "")
    return " ".join(parts).casefold().replace("ё", "е")


_INFLECTION_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "иям",
    "иях",
    "ого",
    "ему",
    "ими",
    "ыми",
    "ая",
    "ое",
    "ые",
    "ие",
    "ых",
    "их",
    "ой",
    "ий",
    "ый",
    "ою",
    "ею",
    "ом",
    "ем",
    "ам",
    "ям",
    "ах",
    "ях",
    "ую",
    "юю",
    "ии",
    "ов",
    "ев",
    "ей",
    "ью",
    "ья",
    "ье",
    "це",  # пересортице → пересорти
    "ии",
)


def nl_token_to_metadata_stem(token: str) -> str:
    """U40: NL token → CamelCase-ish stem for LIKE recall (no synonym map)."""
    t = (token or "").casefold().replace("ё", "е")
    if len(t) < 5:
        return ""
    for suf in sorted(_INFLECTION_SUFFIXES, key=len, reverse=True):
        if len(t) > len(suf) + 4 and t.endswith(suf):
            t = t[: -len(suf)]
            break
    if len(t) > 12:
        t = t[:12]
    if len(t) < 5:
        return ""
    return t[0].upper() + t[1:]


def distinctive_nl_stem_specs(query: str) -> list[tuple[str, str]]:
    """Longest non-weak content tokens → scoped stem pairs for SQL recall.

    Prefer tokens that appear inside quotes; skip process/how-to verbs so
    «пересортице» is not crowded out by сопоставления/фактическими.

    Token length floor is 5 (same as ``_is_lexical_sql_stem``) so short
    reference nouns («курсы», «цены» stems) are not dropped before scoping.
    Trees: Документы always; РегистрыНакопления for long stems (≥8);
    РегистрыСведений for shorter distinctive stems (5–7) — info/status
    registers without opening a third tree on every long stem.
    """
    q = query or ""
    q_blob = _quoted_span_blob(q)
    p_blob = _paren_span_blob(q)
    tokens = [
        t
        for t in query_content_tokens(q)
        if len(t) >= 5
        and not _is_weak_content_token(t)
        and not _is_process_nl_token(t)
    ]

    def _in_blob(t: str, blob: str) -> int:
        if not blob:
            return 0
        if t in blob or any(t[: max(4, len(t) - 2)] in blob for _ in (0,)):
            return 1
        for qw in _TOKEN_RE.findall(blob):
            if qw.startswith(t[:5]) or t.startswith(qw[:5]):
                return 1
        return 0

    def _tok_key(t: str) -> tuple[int, int, int]:
        # Quotes > parenthetical lists > length (topic nouns often in lists).
        return (_in_blob(t, q_blob), _in_blob(t, p_blob), len(t))

    tokens.sort(key=_tok_key, reverse=True)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    # Quotas: long stems fill Accum without starving short Info nouns
    # («курсы»/«статусы») that lose a pure longest-first race to adjectives.
    long_n = 0
    short_n = 0
    max_long = 2
    max_short = 2
    for tok in tokens:
        if long_n >= max_long and short_n >= max_short:
            break
        stem = nl_token_to_metadata_stem(tok)
        if not stem or not _is_lexical_sql_stem(stem):
            continue
        key = stem.casefold()
        if key in seen:
            continue
        if len(stem) >= 8:
            if long_n >= max_long:
                continue
            seen.add(key)
            out.append(("Документы.", stem))
            out.append(("РегистрыНакопления.", stem))
            long_n += 1
        else:
            # Band 5–7: short reference/status nouns → InfoRegister only.
            if short_n >= max_short:
                continue
            seen.add(key)
            out.append(("Документы.", stem))
            out.append(("РегистрыСведений.", stem))
            short_n += 1
    return out


def multi_token_coverage_boost(query: str, hit: dict[str, Any]) -> float:
    """U39/U40: ≥2 significant query tokens in name/path → strong boost."""
    matched = hit_content_matches(query, hit)
    if len(matched) >= 2:
        # Extra weight for more distinctive overlaps (cap +0.7).
        extra = min(0.35 * (len(matched) - 2), 0.7)
        return BOOST_MULTI_TOKEN_COVER + extra
    return 0.0


def weak_singleton_penalty(query: str, hit: dict[str, Any]) -> float:
    """U39: exactly one weak token match → demote (обмен/продаж/распределен…)."""
    if not looks_like_user_howto_query(query):
        return 0.0
    matched = hit_content_matches(query, hit)
    if len(matched) != 1:
        return 0.0
    if not _is_weak_content_token(matched[0]):
        return 0.0
    # Keep named-entity / stem-recall roots — they were explicitly recalled.
    if hit.get("_named_entity_recall") or hit.get("_stem_recall"):
        return 0.0
    return PENALTY_WEAK_SINGLETON


def rich_query_singleton_penalty(query: str, hit: dict[str, Any]) -> float:
    """U40: rich how-to (≥5 content tokens) + exactly one match → demote.

    Catches ``ИнвентаризацияНМА`` matching only «инвентаризация» while the
    query also asks about series/lots/goods — without domain-specific packs.
    """
    if not looks_like_user_howto_query(query):
        return 0.0
    tokens = query_content_tokens(query)
    if len(tokens) < MIN_RICH_QUERY_TOKENS:
        return 0.0
    matched = hit_content_matches(query, hit)
    if len(matched) != 1:
        return 0.0
    if hit.get("_named_entity_recall") or hit.get("_stem_recall"):
        return 0.0
    return PENALTY_RICH_SINGLETON


def howto_zero_coverage_penalty(query: str, hit: dict[str, Any]) -> float:
    """U40: how-to with several content tokens — demote hits matching none."""
    if not looks_like_user_howto_query(query):
        return 0.0
    tokens = query_content_tokens(query)
    if len(tokens) < 3:
        return 0.0
    if hit_content_matches(query, hit):
        return 0.0
    if hit.get("_named_entity_recall") or hit.get("_stem_recall"):
        return 0.0
    kind = str(hit.get("kind") or "")
    # Soft on Help only when help boost is the point of the query — still demote.
    if kind in _POLICY_NOISE_KINDS:
        return PENALTY_HOWTO_ZERO_COVER
    return PENALTY_HOWTO_ZERO_COVER


_CAMEL_SEG_RE = re.compile(
    r"[А-ЯA-Z]{2,}(?![а-яa-z])|[А-ЯA-Z][а-яa-z0-9]+|[а-яa-z0-9]+"
)


def _camel_segments(name: str) -> list[str]:
    return [m.group(0) for m in _CAMEL_SEG_RE.finditer(name or "") if m.group(0)]


def camel_tail_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U40: matched stem + unmatched CamelCase tail (НМА/ОС/…) → demote.

    ``ИнвентаризацияНМА`` vs query about goods series: «инвентаризация» matches,
    leftover ``НМА`` does not appear in the query.
    """
    if not looks_like_user_howto_query(query):
        return 0.0
    name = str(hit.get("name") or "")
    segs = _camel_segments(name)
    if len(segs) < 2:
        return 0.0
    matched = hit_content_matches(query, hit)
    if not matched:
        return 0.0
    q_blob = " ".join(query_content_tokens(query))
    unmatched = []
    for seg in segs:
        scf = seg.casefold().replace("ё", "е")
        if len(scf) < 2:
            continue
        if any(_content_token_in_hay(t, scf) or scf in t for t in matched):
            continue
        if any(_content_token_in_hay(t, scf) for t in query_content_tokens(query)):
            continue
        # Short ALLCAPS / 2–4 letter tails are domain markers (НМА, ОС, ГИСМ).
        if seg.isupper() and 2 <= len(seg) <= 5:
            unmatched.append(seg)
        elif len(scf) >= 5 and scf not in q_blob and not any(
            scf.startswith(t[:4]) for t in matched if len(t) >= 4
        ):
            unmatched.append(seg)
    if not unmatched:
        return 0.0
    if hit.get("_named_entity_recall") or hit.get("_stem_recall"):
        return 0.0
    return PENALTY_CAMEL_TAIL


def kind_prior_boost(query: str, hit: dict[str, Any]) -> float:
    """U39: grammar of the question → preferred metadata kinds (not business packs)."""
    q = query or ""
    kind = str(hit.get("kind") or "")
    path = str(hit.get("path") or "")
    b = 0.0
    if _KIND_PRIOR_JOB_RE.search(q):
        if kind in {"ScheduledJob", "РегламентныеЗадания"} or path.startswith(
            "РегламентныеЗадания."
        ):
            b = max(b, BOOST_KIND_PRIOR)
    if _KIND_PRIOR_CODE_RE.search(q):
        if kind in {"CommonModule", "ОбщиеМодули", "Procedure", "Function"}:
            b = max(b, BOOST_KIND_PRIOR)
        if kind == "Help":
            b -= 0.6
    if _KIND_PRIOR_HTTP_RE.search(q):
        if kind in {
            "HTTPService",
            "HTTPСервисы",
            "ExchangePlan",
            "ПланыОбмена",
        } or path.startswith(("HTTPСервисы.", "ПланыОбмена.")):
            b = max(b, BOOST_KIND_PRIOR)
    if _KIND_PRIOR_JOIN_META_RE.search(q):
        if kind in {
            "Document",
            "Документы",
            "AccumulationRegister",
            "InformationRegister",
            "РегистрыНакопления",
            "РегистрыСведений",
            "Catalog",
            "Справочники",
        } and path.count(".") == 1:
            # Only when the hit actually overlaps query content (U40).
            if len(hit_content_matches(query, hit)) >= 1:
                b = max(b, BOOST_KIND_PRIOR * 0.75)
    return b


def howto_policy_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U39: on user how-to, demote FO/DefinedType/pictures unless multi-token hit."""
    if not looks_like_user_howto_query(query):
        return 0.0
    kind = str(hit.get("kind") or "")
    if kind not in _POLICY_NOISE_KINDS:
        return 0.0
    if len(hit_content_matches(query, hit)) >= 2:
        return 0.0
    if looks_like_settings_constraint_query(query) or looks_like_rls_query(query):
        return 0.0
    if looks_like_accounting_policy_query(query) and kind in {
        "FunctionalOption",
        "ФункциональныеОпции",
        "Constant",
        "Константы",
    }:
        return 0.0
    return PENALTY_HOWTO_POLICY_NOISE


def is_strong_howto_root(query: str, hit: dict[str, Any]) -> bool:
    """Depth-2 root with ≥2 content-token matches (U39 skip-Help gate)."""
    path = str(hit.get("path") or "")
    if path.count(".") != 1:
        return False
    kind = str(hit.get("kind") or "")
    if kind == "Help":
        return False
    return len(hit_content_matches(query, hit)) >= 2


def strong_howto_root_count(query: str, hits: list[dict[str, Any]]) -> int:
    """How many strong roots are already in the pool."""
    return sum(1 for h in hits if is_strong_howto_root(query, h))


def should_skip_help_for_strong_pool(query: str, hits: list[dict[str, Any]]) -> bool:
    """U39: skip Help SQL when how-to pool already has enough strong roots."""
    if not looks_like_user_howto_query(query):
        return False
    return strong_howto_root_count(query, hits) >= MIN_STRONG_ROOTS_SKIP_HELP


def is_root_object(hit: dict[str, Any]) -> bool:
    """True for top-level Document/Catalog/Report paths like ``Отчеты.X``."""
    kind = str(hit.get("kind") or "")
    if kind not in _ROOT_KINDS:
        return False
    path = str(hit.get("path") or "")
    return bool(path) and path.count(".") == 1


def report_root_path(path: str) -> str | None:
    """``Отчеты.X.Макеты.Y`` → ``Отчеты.X``; non-report paths → None."""
    p = path or ""
    if not any(p.startswith(pref) for pref in _REPORT_PATH_PREFIXES):
        return None
    parts = [x for x in p.split(".") if x]
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def ensure_report_roots(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Surface root ``Отчеты.X`` when only nested report children matched (U13)."""
    if not hits:
        return hits
    existing = {str(h.get("path") or "") for h in hits}
    extra: list[dict[str, Any]] = []
    for h in hits:
        root = report_root_path(str(h.get("path") or ""))
        if not root or root in existing:
            continue
        name = root.split(".")[-1]
        extra.append(
            {
                "context": h.get("context"),
                "path": root,
                "kind": "Report",
                "belong": h.get("belong"),
                "name": name,
                "synonym": "",
                "comment": "",
                "score": float(h.get("score") or 0) * 0.95,
            }
        )
        existing.add(root)
    if not extra:
        return hits
    return list(hits) + extra


def alias_hit_boost(query: str, hit: dict[str, Any]) -> float:
    """Boost root hits whose name matches alias expansion terms for this query."""
    terms = all_alias_terms_for_query(query)
    if not terms:
        return 0.0
    name = str(hit.get("name") or "").casefold()
    kind = str(hit.get("kind") or "")
    for term in terms:
        t = term.casefold()
        if not t:
            continue
        if name == t:
            return BOOST_ALIAS_HIT
        # Root report/catalog/document whose name embeds the alias (CamelCase compound).
        if is_root_object(hit) and t in name:
            return BOOST_ALIAS_HIT
        if kind == "Report" and is_root_object(hit) and name in t:
            return BOOST_ALIAS_HIT
    return 0.0


def exact_root_name_boost(query: str, hit: dict[str, Any]) -> float:
    """Gap 2: root whose ``name`` equals the query (or a query token) beats Attribute.

    ``Справочники.Номенклатура`` vs ``….Реквизиты.Номенклатура`` — same FTS term,
    root must win. Full equality only (not substring) to avoid short false hits.
    """
    if not is_root_object(hit):
        return 0.0
    name = str(hit.get("name") or "").casefold().replace("ё", "е")
    if not name or len(name) < 3:
        return 0.0
    q = (query or "").strip().casefold().replace("ё", "е")
    if not q:
        return 0.0
    if name == q:
        return BOOST_EXACT_ROOT_NAME
    # Single-token or multi-token: any content token exact-equals the root name.
    for tok in query_tokens(q):
        if tok == name:
            return BOOST_EXACT_ROOT_NAME
    return 0.0


def purchase_client_confusion_penalty(query: str, hit: dict[str, Any]) -> float:
    """P0: demote ``ПоступлениеТоваровОтКлиента`` when NL is «от поставщика»."""
    if not document_alias_terms_for_query(query):
        return 0.0
    name = str(hit.get("name") or "")
    path = str(hit.get("path") or "")
    if "ПоступлениеТоваровОтКлиента" in name or "ПоступлениеТоваровОтКлиента" in path:
        return -2.0
    return 0.0


def help_owner_path(path: str) -> str | None:
    """``Help.Документы.X.Формы.Y`` → ``Документы.X``; non-help → None (U23)."""
    p = path or ""
    if not p.startswith("Help."):
        return None
    rest = p[5:]
    parts = [x for x in rest.split(".") if x]
    if len(parts) < 2:
        return None
    if parts[0] not in _HELP_OWNER_KIND:
        return None
    return f"{parts[0]}.{parts[1]}"


def help_owners_from_hits(hits: list[dict[str, Any]]) -> set[str]:
    """Unique owner object paths present via Help hits in the pool."""
    out: set[str] = set()
    for h in hits:
        if str(h.get("kind") or "") != "Help":
            # Also accept paths that already start with Help. even if kind wrong.
            op = help_owner_path(str(h.get("path") or ""))
            if op:
                out.add(op)
            continue
        op = help_owner_path(str(h.get("path") or ""))
        if op:
            out.add(op)
    return out


def ensure_help_owners(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """U23: surface owner Document/Catalog/… when Help sections matched."""
    if not hits:
        return hits
    existing = {str(h.get("path") or "") for h in hits}
    extra: list[dict[str, Any]] = []
    for owner in help_owners_from_hits(hits):
        if owner in existing:
            continue
        cat = owner.split(".", 1)[0]
        name = owner.split(".", 1)[-1]
        kind = _HELP_OWNER_KIND.get(cat, "Catalog")
        # Score from the best Help child for this owner.
        best = 0.0
        ctx = None
        for h in hits:
            if help_owner_path(str(h.get("path") or "")) == owner:
                best = max(best, float(h.get("score") or 0))
                ctx = ctx or h.get("context")
        extra.append(
            {
                "context": ctx,
                "path": owner,
                "kind": kind,
                "belong": None,
                "name": name,
                "synonym": "",
                "comment": "",
                "score": best * 0.97 if best else 0.5,
            }
        )
        existing.add(owner)
    if not extra:
        return hits
    return list(hits) + extra


def help_owner_hit_boost(hit: dict[str, Any], owners: set[str]) -> float:
    """Boost the metadata root that a Help hit pointed at (U23)."""
    if not owners:
        return 0.0
    path = str(hit.get("path") or "")
    if path in owners:
        return BOOST_HELP_OWNER
    return 0.0


def domain_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U24 + domain-collapse: demote dense off-topic clusters.

    Payroll/НДФЛ attributes and finance-lease enums dominate embedding
    neighborhoods for bare «аванс» / «аренда» / «коэффициент». When the query
    tags another domain, press that noisy island (inverse of root boost).
    """
    domains = query_domains(query)
    if not domains:
        # Still demote payroll flood on settlement-ish queries that only
        # matched via weak tokens — settlement tag may be empty if regex miss.
        hay0 = f"{hit.get('path') or ''} {hit.get('name') or ''}"
        if _PAYROLL_FLOOD_RE.search(hay0) and _DOMAIN_SETTLEMENT_RE.search(query or ""):
            return PENALTY_DOMAIN_CLUSTER
        return 0.0
    hay = f"{hit.get('path') or ''} {hit.get('name') or ''}"
    incidental_zup = zup_is_incidental(query)
    zup_focus = "zup" in domains and not incidental_zup

    if "trade" in domains and not zup_focus and _TRADE_NOISE_RE.search(hay):
        return PENALTY_DOMAIN_NOISE
    if zup_focus and "trade" not in domains and _ZUP_NOISE_RE.search(hay):
        return PENALTY_DOMAIN_NOISE

    # Settlement / production / costing without ZUP focus → payroll flood.
    if (
        not zup_focus
        and _PAYROLL_FLOOD_RE.search(hay)
        and (
            "settlement" in domains
            or "production" in domains
            or "overhead" in domains
            or "costing" in domains
            or "trade" in domains
            or incidental_zup
        )
    ):
        return PENALTY_DOMAIN_CLUSTER

    # «Накладные расходы» → demote invoice/waybill «накладная».
    if "overhead" in domains and _INVOICE_NAKLAD_RE.search(hay):
        if not re.search(r"Расход", hay, re.IGNORECASE):
            return PENALTY_DOMAIN_CLUSTER

    # Product returns → demote returnable-packaging («возвратная тара»).
    if "goods_return" in domains and _RETURNABLE_PACKAGE_RE.search(hay):
        return PENALTY_DOMAIN_CLUSTER

    # Rights Roles match «клиент/возврат» tokens but are not cost objects.
    kind = str(hit.get("kind") or "")
    path = str(hit.get("path") or "")
    if kind in {"Role", "Роли"} and (
        "overhead" in domains
        or "settlement" in domains
        or "warehouse_ops" in domains
        or "production" in domains
    ):
        return PENALTY_DOMAIN_NOISE

    # Help / card-payment noise on overhead costing NL.
    if "overhead" in domains and (
        kind == "Help" or path.startswith("Help.")
    ):
        return PENALTY_DOMAIN_NOISE

    # Warehouse ops cost → demote ФСБУ 25 finance-lease accounting.
    if (
        ("warehouse_ops" in domains or ("warehouse" in domains and "costing" in domains))
        and _FINANCE_LEASE_RE.search(hay)
    ):
        return PENALTY_DOMAIN_CLUSTER

    # Incidental «зарплата» as a cost line — demote ZUP settings/resources.
    if incidental_zup and re.search(
        r"Зарплат|Сотрудник|СреднемуЗаработку|НДФЛ",
        hay,
        re.IGNORECASE,
    ):
        return PENALTY_DOMAIN_NOISE

    # Generic cost enums drown warehouse-ops tops («ВидыСтоимости»).
    if "warehouse_ops" in domains and kind in {"EnumValue", "Enum", "Перечисления"}:
        return PENALTY_DOMAIN_NOISE

    return 0.0


def domain_counter_exact_specs(query: str) -> list[tuple[str, list[str]]]:
    """Exact metadata names to inject against dense off-topic clusters.

    Returns ``(path_prefix, names)`` for ``search_by_exact_names`` — indexed,
    no tree LIKE. Complements ``domain_noise_penalty`` (demote) with a cheap
    positive recall of the competing domain's roots.
    """
    domains = query_domains(query)
    out: list[tuple[str, list[str]]] = []
    if "settlement" in domains:
        out.append(
            (
                "РегистрыНакопления",
                [
                    "ЗачетыАвансовСчетамиФактурами",
                    "НДСАвансыВыданные",
                    "НДСАвансыПолученные",
                ],
            )
        )
    if "overhead" in domains or (
        "costing" in domains and "trade" in domains
    ):
        out.append(
            ("Справочники", ["ПравилаРаспределенияРасходов"])
        )
        out.append(("Документы", ["РаспределениеПрочихЗатрат"]))
        out.append(("Отчеты", ["ЭффективностьСделокСКлиентами"]))
    # Labor-load inject ONLY on labor_load (not bare production/брак/участок).
    if "labor_load" in domains and "zup" not in domains:
        out.append(
            (
                "РегистрыНакопления",
                ["ПлановыеТрудозатраты", "ТрудозатратыКОформлению"],
            )
        )
        out.append(
            (
                "РегистрыСведений",
                ["ПланированиеЗагрузкиВидовРабочихЦентров"],
            )
        )
    if "goods_return" in domains:
        out.append(
            (
                "Документы",
                [
                    "ВозвратТоваровОтКлиента",
                    "ЗаявкаНаВозвратТоваровОтКлиента",
                    "ВозвратТоваровПоставщику",
                ],
            )
        )
        out.append(
            (
                "Отчеты",
                [
                    "ВыручкаИСебестоимостьПродаж",
                ],
            )
        )
    if "warehouse_ops" in domains:
        out.append(("Справочники", ["Склады", "СкладскиеПомещения"]))
        out.append(("Документы", ["РаспределениеПрочихЗатрат"]))
    if looks_like_budget_exec_intent(query):
        out.append(
            (
                "Документы",
                [
                    "ЗаявкаНаРасходованиеДенежныхСредств",
                    "ЛимитыРасходаДенежныхСредств",
                ],
            )
        )
        out.append(
            (
                "Справочники",
                [
                    "СтатьиБюджетов",
                    "ПравилаПолученияФактаПоСтатьямБюджетов",
                ],
            )
        )
    # Schedule intent: exact roots (indexed) so we never pay stem name-prefix
    # scans — those were the marshrutnye-karty latency regression (batch5 P1).
    if looks_like_production_schedule_query(query):
        out.append(
            (
                "Справочники",
                ["РабочиеЦентры", "МаршрутныеКарты"],
            )
        )
        out.append(
            (
                "РегистрыСведений",
                ["ГрафикЭтаповПроизводства"],
            )
        )
    return out


def diversify_by_leaf_name(
    hits: list[dict[str, Any]],
    *,
    limit: int,
    max_per_leaf: int = 1,
) -> list[dict[str, Any]]:
    """After rerank: avoid top-N floods of the same Attribute/TC leaf name.

    ERP dumps clone ``ЗачтеноАвансовыхПлатежей`` across dozens of payroll docs;
    without this, demotion alone still fills top-6 with clones.
    """
    if limit <= 0 or len(hits) <= 1:
        return hits
    out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for h in hits:
        kind = str(h.get("kind") or "")
        path = str(h.get("path") or "")
        name = str(h.get("name") or "")
        leaf = name or (path.rsplit(".", 1)[-1] if path else "")
        deep = path.count(".") >= 3 or kind in {
            "Attribute",
            "TabularSection",
            "Dimension",
            "Resource",
            "EnumValue",
        }
        if deep and leaf:
            key = leaf.casefold()
            n = counts.get(key, 0)
            if n >= max_per_leaf:
                continue
            counts[key] = n + 1
        out.append(h)
        if len(out) >= int(limit):
            return out
    return out


def help_offdomain_penalty(query: str, hit: dict[str, Any]) -> float:
    """U27: demote Help/owners from product islands not mentioned in the query.

    E.g. ВЕТИС Help on plain MRP «заказ на производство» without ветИС/меркурий.
    """
    if not looks_like_user_howto_query(query):
        return 0.0
    kind = str(hit.get("kind") or "")
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    # Only Help hits and their promoted owners (roots we care about).
    is_help = kind == "Help" or path.startswith("Help.")
    is_owner_root = path.count(".") == 1 and kind in {
        "Document",
        "Catalog",
        "Report",
        "DataProcessor",
        "CommonModule",
        "ExchangePlan",
        "HTTPService",
        "InformationRegister",
    }
    if not (is_help or is_owner_root):
        return 0.0
    hay = f"{path} {name}"
    raw = query or ""
    for keep_re, noise_re in _OFFDOMAIN_ISLANDS:
        if noise_re.search(hay) and not keep_re.search(raw):
            return PENALTY_HELP_OFFDOMAIN
    return 0.0


def class_intent_boost(query: str, hit: dict[str, Any]) -> float:
    """U28–U33: boost matches for RLS / schedule / HTTP / policy / domain packs."""
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    kind = str(hit.get("kind") or "")
    hay = f"{path} {name}"
    b = 0.0

    if looks_like_rls_query(query):
        if kind in {"Role", "Роли"} or re.search(
            r"Ограничен\w*Доступ|Шаблон\w*Ограничен|RLS|ОграничиватьДоступ|"
            r"ПриЗаполненииОграниченияДоступа",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_RLS_HIT)
        elif is_policy_meta_kind(kind) and "Доступ" in name:
            b = max(b, BOOST_RLS_HIT * 0.85)

    if looks_like_production_schedule_query(query):
        if re.search(
            r"ГрафикПроизвод|ГрафикЭтап|Рабоч\w*Центр|Доступност\w*Оборудован|"
            r"ПланированиеГрафика",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_PROD_SCHEDULE_HIT)

    if looks_like_integration_http_query(query):
        if kind in {"HTTPService", "HTTPСервисы", "ExchangePlan", "ПланыОбмена"} or re.search(
            r"HTTPСервис|WMS|ESB|Интеграц\w*",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_INTEGRATION_HIT)

    if looks_like_accounting_policy_query(query):
        if is_policy_meta_kind(kind) and re.search(
            r"Использовать|Учитывать|Ведется|Ордерн|СебестоимостьПоСериям|Сери",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_POLICY_META)

    qcf = (query or "").casefold()
    if "давальч" in qcf or (
        "переработ" in qcf and ("заказчик" in qcf or "сырь" in qcf)
    ):
        if re.search(r"Давальческ|Давальц|Переработк|ОтчетПереработ", hay, re.IGNORECASE):
            b = max(b, BOOST_DOMAIN_STEM_HIT)
    if "ордерн" in qcf or ("ордер" in qcf and "склад" in qcf) or re.search(
        r"зон\w*\s+(?:приём|прием|хранен|отгруз)", qcf
    ):
        if re.search(
            r"ПриходныйОрдер|РасходныйОрдер|ОрдерНаПеремещ|СкладскиеПомещ|Ордерн|"
            r"ИспользоватьОрдерн",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_DOMAIN_STEM_HIT)
    if "фифо" in qcf or (
        "себестоим" in qcf and ("сери" in qcf or "годност" in qcf)
    ):
        if re.search(
            r"СебестоимостьПоСериям|УчитыватьСебестоимостьПоСериям|ФИФО|"
            r"РасчетСебестоимости",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_DOMAIN_STEM_HIT)

    if looks_like_pricing_fx_intent(query):
        if _PRICING_FX_ANCHOR_RE.search(hay):
            b = max(b, BOOST_PRICING_FX_HIT)
        elif re.search(
            r"ЦеныНоменклатуры|УстановкаЦен|ВидыЦен|КурсыВалют",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_PRICING_HIT)
    elif looks_like_pricing_intent(query):
        if re.search(
            r"ЦеныНоменклатуры|УстановкаЦен|Пересчитыв\w*Цен|ПрайсЛист|"
            r"ВидыЦен|ЦенаНоменклатуры",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_PRICING_HIT)

    if looks_like_report_reconcile_intent(query):
        if kind in {"Report", "Отчеты"} or path.startswith("Отчеты."):
            if _RECONCILE_PNL_RE.search(hay):
                b = max(b, BOOST_RECONCILE_PNL)
            elif _RECONCILE_OSV_RE.search(hay):
                b = max(b, BOOST_RECONCILE_REPORT)
            elif re.search(
                r"Прибыл|Убытк|БухгалтерскаяОтчетность|РасшифровкиБух",
                hay,
                re.IGNORECASE,
            ):
                b = max(b, BOOST_RECONCILE_REPORT * 0.85)

    if looks_like_budget_exec_intent(query):
        if _BUDGET_LIMIT_RE.search(hay) or _BUDGET_CLAIM_OR_FACT_RE.search(hay):
            b = max(b, BOOST_BUDGET_EXEC_HIT)
        elif re.search(
            r"Бюджетн\w*Отчет|Исполнен\w*Бюджет|СтатьиБюджет|Лимит",
            hay,
            re.IGNORECASE,
        ):
            b = max(b, BOOST_BUDGET_EXEC_HIT * 0.85)

    if looks_like_repair_overdue_intent(query):
        if _REPAIR_ORDER_RE.search(hay) or _REPAIR_SCHEDULE_RE.search(hay):
            b = max(b, BOOST_REPAIR_OVERDUE_HIT)
        elif re.search(r"Ремонт|Обслуживан", hay, re.IGNORECASE) and kind in {
            "Document",
            "Документы",
            "InformationRegister",
            "РегистрыСведений",
            "DataProcessor",
            "Обработки",
        }:
            b = max(b, BOOST_REPAIR_OVERDUE_HIT * 0.8)

    if looks_like_query_join_intent(query):
        if kind in {
            "AccumulationRegister",
            "InformationRegister",
            "РегистрыНакопления",
            "РегистрыСведений",
        } and path.count(".") == 1:
            b = max(b, BOOST_QUERY_JOIN_REGISTER)
        if re.search(r"Резерв|Ресурсн|ЗаказыНаПроизвод|Расчет\w*СКонтрагент", hay, re.I):
            b = max(b, BOOST_QUERY_JOIN_REGISTER)

    if hit.get("_named_entity_recall") and path.count(".") == 1:
        b = max(b, BOOST_NAMED_ENTITY)
    elif hit.get("_lexical_recall") and path.count(".") == 1:
        b = max(b, BOOST_NAMED_ENTITY * 0.9)

    return b


def stem_recall_boost(hit: dict[str, Any]) -> float:
    """U35: depth-2 roots from intent stem-recall outrank noisy Help."""
    if not (
        hit.get("_stem_recall")
        or hit.get("_named_entity_recall")
        or hit.get("_lexical_recall")
    ):
        return 0.0
    path = str(hit.get("path") or "")
    if path.count(".") != 1:
        return 0.0
    if hit.get("_named_entity_recall") or hit.get("_lexical_recall"):
        return BOOST_NAMED_ENTITY
    return BOOST_STEM_RECALL_ROOT


def _stem_or_domain_intent(query: str) -> bool:
    return bool(
        intent_stem_recall_specs(query)
        or looks_like_query_join_intent(query)
        or looks_like_pricing_intent(query)
        or looks_like_report_reconcile_intent(query)
        or looks_like_budget_exec_intent(query)
        or looks_like_repair_overdue_intent(query)
        or extract_named_entity_specs(query)
    )


def stem_intent_help_penalty(
    query: str,
    hit: dict[str, Any],
    *,
    stem_intent: bool | None = None,
) -> float:
    """U35/U36: when a class/domain stem pack fires, demote Help without class boost."""
    if str(hit.get("kind") or "") != "Help":
        return 0.0
    if stem_intent is None:
        stem_intent = _stem_or_domain_intent(query)
    if not stem_intent:
        return 0.0
    if class_intent_boost(query, hit) > 0:
        return 0.0
    # Help under matching owner tree still useful (e.g. Help.…Давальц…).
    path = str(hit.get("path") or "")
    if class_intent_boost(query, {"path": path, "name": path, "kind": "Document"}) > 0:
        return 0.0
    if looks_like_query_join_intent(query) or looks_like_pricing_intent(query):
        return PENALTY_HELP_WHEN_QUERY_JOIN
    if looks_like_budget_exec_intent(query) or looks_like_repair_overdue_intent(
        query
    ):
        return PENALTY_HELP_WHEN_QUERY_JOIN
    return PENALTY_HELP_WHEN_STEM_INTENT


def rls_doc_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U34: RLS answers live in Role/Const/FO — demote document/Help purchase noise."""
    if not looks_like_rls_query(query):
        return 0.0
    kind = str(hit.get("kind") or "")
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    hay = f"{path} {name}"
    if re.search(
        r"Ограничен|Доступ|RLS|Шаблон\w*Ограничен",
        hay,
        re.IGNORECASE,
    ):
        return 0.0
    if kind == "Help" and path.startswith("Help.Документы."):
        return PENALTY_RLS_DOC_NOISE
    if kind in {"Document", "Документы"}:
        return PENALTY_RLS_DOC_NOISE
    return 0.0


def policy_doc_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U33: policy how-to — demote списание/хранение docs drowning FO/Const."""
    if not looks_like_accounting_policy_query(query):
        return 0.0
    kind = str(hit.get("kind") or "")
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    hay = f"{path} {name}"
    if is_policy_meta_kind(kind):
        return 0.0
    if not _POLICY_DOC_NOISE_RE.search(hay):
        return 0.0
    if kind == "Help" or kind in {"Document", "Документы"}:
        return PENALTY_POLICY_DOC_NOISE
    return 0.0


def reconcile_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U36/U37: ОПиУ↔ОСВ — demote non-report / budget ОСВ / payroll noise."""
    if not looks_like_report_reconcile_intent(query):
        return 0.0
    kind = str(hit.get("kind") or "")
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    hay = f"{path} {name}"
    if kind in {"Report", "Отчеты"} or path.startswith("Отчеты."):
        if _RECONCILE_PNL_RE.search(hay):
            return 0.0
        if _RECONCILE_OSV_RE.search(hay):
            if re.search(r"Бюджет", hay, re.IGNORECASE):
                return PENALTY_RECONCILE_BUDGET_OSV
            return 0.0
        return -0.3
    if re.search(
        r"Военкомат|ПФР|КабинетСотрудника|НалогНаПрибыльУбытки|"
        r"ПодпискиНаРассылки",
        hay,
        re.IGNORECASE,
    ):
        return -1.6
    if kind not in {"Report", "Отчеты", "Help"}:
        return -0.85
    return 0.0


def pricing_fx_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U37: FX price-recalc — demote manual %/НДС Help without rate anchors."""
    if not looks_like_pricing_fx_intent(query):
        return 0.0
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    hay = f"{path} {name}"
    if _PRICING_FX_ANCHOR_RE.search(hay) or re.search(
        r"КурсыВалют|ОбновлениеЦен", hay, re.IGNORECASE
    ):
        return 0.0
    kind = str(hit.get("kind") or "")
    if kind == "Help" and _PRICING_FX_NOISE_RE.search(hay):
        return PENALTY_PRICING_FX_NOISE
    if kind == "Help" and re.search(r"ПрайсЛист|УстановкаЦен", hay, re.I):
        if not re.search(r"Курс|Валют|Автомат|Пересчитыв", hay, re.I):
            return PENALTY_PRICING_FX_NOISE * 0.85
    if "ПрайсЛист" in path and not re.search(r"Курс|Валют", hay, re.I):
        return -0.7
    return 0.0


def budget_exec_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U38: demote journal/cost noise drowning budget limit→claim chain."""
    if not looks_like_budget_exec_intent(query):
        return 0.0
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    hay = f"{path} {name}"
    if _BUDGET_LIMIT_RE.search(hay) or _BUDGET_CLAIM_OR_FACT_RE.search(hay):
        return 0.0
    if _BUDGET_EXEC_NOISE_RE.search(hay):
        return PENALTY_BUDGET_EXEC_NOISE
    kind = str(hit.get("kind") or "")
    if kind == "Help" and "Бюджет" not in hay and "Лимит" not in hay:
        return PENALTY_BUDGET_EXEC_NOISE * 0.7
    if re.search(r"Подразделен", hay, re.I) and "Бюджет" not in hay:
        return -0.85
    return 0.0


def repair_overdue_noise_penalty(query: str, hit: dict[str, Any]) -> float:
    """U38: demote ПросроченныеЗадачи / print-form overdue without repair."""
    if not looks_like_repair_overdue_intent(query):
        return 0.0
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    hay = f"{path} {name}"
    if _REPAIR_ORDER_RE.search(hay) or _REPAIR_SCHEDULE_RE.search(hay):
        return 0.0
    if _REPAIR_OVERDUE_NOISE_RE.search(hay):
        return PENALTY_REPAIR_OVERDUE_NOISE
    if re.search(r"Просрочен", hay, re.I) and "Ремонт" not in hay:
        return PENALTY_REPAIR_OVERDUE_NOISE
    return 0.0


def settings_intent_boost(query: str, hit: dict[str, Any]) -> float:
    """U25: Константы / ФО / Роли / панели «Настройка» on constraint how-to."""
    if not looks_like_settings_constraint_query(query):
        return 0.0
    kind = str(hit.get("kind") or "")
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    b = BOOST_SETTINGS_KIND.get(kind, 0.0)
    if kind in _SETTINGS_ROOT_KINDS and path.count(".") == 1:
        b = max(b, BOOST_SETTINGS_KIND.get(kind, 0.0))
    if ("Настройк" in name or "Настройк" in path) and kind in {
        "DataProcessor",
        "CommonForm",
        "ManagedForm",
    }:
        b = max(b, BOOST_SETTINGS_PANEL)
    if "Ограничен" in name and kind == "Constant":
        b = max(b, BOOST_SETTINGS_KIND["Constant"] + 0.2)
    # Prefer price/discount settings; demote false stem hits (Центр, …).
    qcf = (query or "").casefold()
    if kind == "Constant" and "цен" in qcf:
        if re.search(r"Ценов|Допустим\w*Цен|ЦенПродаж|Ручн\w*Скид|Ограничен\w*Скид", name):
            b += 0.6
        elif re.search(r"Центр|Оценк", name):
            b -= 1.0
    # Soft demote unrelated document roots (settings answers live in Const/FO/Role).
    if kind == "Document" and not re.search(
        r"Цен|Скидк|Соглашен|Прав|Настройк", name, re.IGNORECASE
    ):
        b -= 0.9
    return b


def junk_nest_penalty(query: str, hit: dict[str, Any]) -> float:
    """U26: attached-files / Удалить* / deep attrs / pictures on user how-to."""
    if not looks_like_user_howto_query(query):
        # Still demote attached-files everywhere — they never answer discovery.
        path0 = str(hit.get("path") or "")
        name0 = str(hit.get("name") or "")
        if "ПрисоединенныеФайлы" in path0 or name0.endswith("ПрисоединенныеФайлы"):
            return PENALTY_ATTACHED_FILES
        return 0.0
    path = str(hit.get("path") or "")
    name = str(hit.get("name") or "")
    kind = str(hit.get("kind") or "")
    pen = 0.0
    if "ПрисоединенныеФайлы" in path or name.endswith("ПрисоединенныеФайлы"):
        pen += PENALTY_ATTACHED_FILES
    if name.startswith("Удалить") or ".Удалить" in path:
        pen += PENALTY_DELETED_PREFIX
    if kind == "CommonPicture":
        pen += PENALTY_COMMON_PICTURE
    if kind in {"EnumValue", "Attribute", "TabularSection", "Dimension", "Resource"}:
        if path.count(".") >= 2:
            pen += PENALTY_JUNK_KIND
    return pen


def howto_query_boost(query: str, hit: dict[str, Any]) -> float:
    """U17+U20+U36: boost Info/Accum register roots / ТекстЗапроса methods."""
    join = looks_like_query_join_intent(query)
    if not (
        looks_like_howto_query(query)
        or looks_like_fallback_query(query)
        or join
    ):
        return 0.0
    kind = str(hit.get("kind") or "")
    name = str(hit.get("name") or "")
    path = str(hit.get("path") or "")
    base = 0.0
    if kind == "InformationRegister" and path.count(".") == 1:
        base = BOOST_HOWTO_INFO_REGISTER
    if join and kind in {
        "AccumulationRegister",
        "InformationRegister",
        "РегистрыНакопления",
        "РегистрыСведений",
    } and path.count(".") == 1:
        base = max(base, BOOST_QUERY_JOIN_REGISTER)
    if kind in {"Procedure", "Function"} and (
        "ТекстЗапроса" in name or "ДанныеПеревозк" in name
    ):
        base = BOOST_HOWTO_QUERY_TEXT_METHOD
    if base and looks_like_fallback_query(query):
        base += BOOST_HOWTO_FALLBACK
    return base


def help_query_boost(query: str, hit: dict[str, Any]) -> float:
    """U10+U23: kind=Help поднимается на user how-to / fallback, иначе 0."""
    if str(hit.get("kind") or "") != "Help":
        return 0.0
    if not looks_like_user_howto_query(query):
        return 0.0
    return BOOST_HELP_HOWTO


def rerank_hits(
    hits: list[dict[str, Any]],
    query: str,
    entities_by_name: dict[str, dict[str, Any]] | None = None,
    *,
    tag: bool = False,
) -> list[dict[str, Any]]:
    if not hits:
        return hits
    tokens = query_tokens(query)
    entities_by_name = entities_by_name or {}
    max_score = max(float(h.get("score") or 0) for h in hits) or 1.0
    report_intent = looks_like_report_query(query)
    help_owners = help_owners_from_hits(hits)
    user_howto = looks_like_user_howto_query(query)
    stem_intent = _stem_or_domain_intent(query)
    best_cfg = 0.0
    if tag:
        for h in hits:
            ctx = str(h.get("context") or "")
            et = (entities_by_name.get(ctx) or {}).get("entity_type") or "configuration"
            if et != "extension":
                best_cfg = max(best_cfg, float(h.get("score") or 0))

    scored: list[tuple[float, dict[str, Any]]] = []
    for h in hits:
        raw = float(h.get("score") or 0)
        cov = token_coverage(tokens, h)
        kind = str(h.get("kind") or "")
        kind_b = BOOST_KINDS.get(kind, 0.0)
        root_b = BOOST_ROOT_OBJECT if is_root_object(h) else 0.0
        # U26: on user how-to, prefer roots a bit more.
        if user_howto and is_root_object(h):
            root_b += 0.15
        alias_b = alias_hit_boost(query, h)
        exact_root_b = exact_root_name_boost(query, h)
        # Synonym-resolved root reports count as strong conceptual coverage (U14).
        if alias_b and kind == "Report" and is_root_object(h):
            cov = max(cov, 0.9)
        if exact_root_b:
            cov = max(cov, 0.95)
        # Prefer root Report over nested children when NL looks like report discovery.
        if report_intent and kind == "Report" and is_root_object(h):
            root_b += BOOST_REPORT_INTENT_ROOT
        nest_pen = 0.0
        schema_b = 0.0
        if report_intent and report_root_path(str(h.get("path") or "")) and not is_root_object(h):
            if kind == "DataCompositionSchema":
                schema_b = BOOST_REPORT_INTENT_SCHEMA
            else:
                nest_pen = -0.85
        howto_b = howto_query_boost(query, h)
        help_b = help_query_boost(query, h)
        owner_b = help_owner_hit_boost(h, help_owners)
        settings_b = settings_intent_boost(query, h)
        class_b = class_intent_boost(query, h)
        stem_b = stem_recall_boost(h)
        domain_pen = domain_noise_penalty(query, h)
        offdomain_pen = help_offdomain_penalty(query, h)
        junk_pen = junk_nest_penalty(query, h)
        purchase_pen = purchase_client_confusion_penalty(query, h)
        stem_help_pen = stem_intent_help_penalty(query, h, stem_intent=stem_intent)
        rls_pen = rls_doc_noise_penalty(query, h)
        policy_pen = policy_doc_noise_penalty(query, h)
        reconcile_pen = reconcile_noise_penalty(query, h)
        pricing_fx_pen = pricing_fx_noise_penalty(query, h)
        budget_pen = budget_exec_noise_penalty(query, h)
        repair_pen = repair_overdue_noise_penalty(query, h)
        multi_b = multi_token_coverage_boost(query, h)
        # RLS / policy how-to: do not let warehouse/doc token overlap rescue noise docs.
        if rls_pen < 0 or policy_pen < 0:
            multi_b = 0.0
            cov = min(cov, 0.12)
            kind_b = min(kind_b, 0.15)
            root_b = min(root_b, 0.1)
        # Domain-collapse clusters: «стоимость/площади/аренда» tokens must not
        # rescue payroll/lease clones via multi-token coverage.
        if domain_pen <= PENALTY_DOMAIN_CLUSTER + 0.01:
            multi_b = 0.0
            cov = min(cov, 0.1)
            kind_b = min(kind_b, 0.2)
            root_b = min(root_b, 0.15)
        weak_pen = weak_singleton_penalty(query, h)
        rich_sing_pen = rich_query_singleton_penalty(query, h)
        zero_cover_pen = howto_zero_coverage_penalty(query, h)
        camel_pen = camel_tail_noise_penalty(query, h)
        kind_prior_b = kind_prior_boost(query, h)
        policy_noise_pen = howto_policy_noise_penalty(query, h)
        ext = 0.0
        if tag:
            ctx = str(h.get("context") or "")
            et = (entities_by_name.get(ctx) or {}).get("entity_type") or "configuration"
            if et == "extension" and (best_cfg <= 0 or raw <= best_cfg * 1.15):
                ext = -0.8
        adj = (
            cov * 4.0
            + kind_b
            + root_b
            + alias_b
            + exact_root_b
            + howto_b
            + help_b
            + owner_b
            + settings_b
            + class_b
            + stem_b
            + multi_b
            + kind_prior_b
            + purchase_pen
            + domain_pen
            + offdomain_pen
            + junk_pen
            + stem_help_pen
            + rls_pen
            + policy_pen
            + reconcile_pen
            + pricing_fx_pen
            + budget_pen
            + repair_pen
            + weak_pen
            + rich_sing_pen
            + zero_cover_pen
            + camel_pen
            + policy_noise_pen
            + nest_pen
            + schema_b
            + (raw / max_score)
            + ext
        )
        scored.append((adj, h))
    scored.sort(
        key=lambda t: (
            -t[0],
            -float(t[1].get("score") or 0),
            t[1].get("path") or "",
        )
    )
    return [h for _, h in scored]


def diversify_by_context(
    hits: list[dict[str, Any]],
    *,
    limit: int,
    tag: bool,
    n_contexts: int,
) -> list[dict[str, Any]]:
    """Keep top-N from flooding a single extension when searching ``tag:``."""
    if not tag or n_contexts <= 1 or len(hits) <= 2:
        return hits
    cap = max(3, (int(limit) + max(n_contexts, 1) - 1) // max(n_contexts, 1))
    cap = min(max(cap, 3), max(3, int(limit) // 2) if int(limit) >= 6 else cap)
    per: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    for h in hits:
        ctx = str(h.get("context") or "")
        if per.get(ctx, 0) < cap:
            out.append(h)
            per[ctx] = per.get(ctx, 0) + 1
        else:
            overflow.append(h)
    out.extend(overflow)
    return out
