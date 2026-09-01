from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path

import sqlite3
import zvec

from app.core.config import settings
from app.core.database import connect
from app.repositories import entities as ent_repo
from app.repositories import methods as method_repo  # noqa: F401 (import parity with cfsmcp)
from app.repositories import objects as obj_repo
from app.services import payload as payload_svc
from app.services import ranking as ranking_svc
from app.services import refs
from app.services.embeddings import EmbeddingClient
from app.services.help_parser import HELP_KIND
from app.services.phase_timing import PhaseTimer, attach_timing, bottleneck_hint
from app.services.pipeline import collection_path
from app.services.zvec_store import zvec_store

log = logging.getLogger("cfsmcp2.search")


class ZvecFilterFailed(Exception):
    """Parent-scoped zvec filter rejected; callers must not retry unscoped."""

    def __init__(self, parent: str, cause: BaseException):
        self.parent = parent
        self.cause = cause
        super().__init__(
            f"zvec path filter failed for parent_path={parent}: {cause}"
        )


# Short TTL cache for resolved MCP contexts (name → entity row).
_ENTITY_TTL_SEC = 5.0
_entity_cache: dict[str, tuple[float, dict]] = {}
_entity_cache_lock = threading.Lock()

# U28–U31 perf: stem SQL recall shared across FTS+semantic for the same query
# (otherwise each tool pays up to MAX_INTENT_STEM_SPECS scans on ERP).
_STEM_RECALL_TTL_SEC = 45.0
_stem_recall_cache: dict[tuple[int, str], tuple[float, list[dict]]] = {}
_stem_recall_lock = threading.Lock()

TAG_PREFIX = "tag:"

# Soft fan-out detector: same query → different contexts within window.
_FANOUT_WINDOW_SEC = 30.0
_FANOUT_RECENT: deque[tuple[float, str, str]] = deque(maxlen=200)
_FANOUT_LOCK = threading.Lock()

_REQUIRED_METHOD_NAMES = (
    "МассивОбязательныхРеквизитов",
    "МассивОбязательныхРеквизитовТЧ",
    "МассивУсловноОбязательныхРеквизитов",
    "МассивУсловноОбязательныхРеквизитовТЧ",
)
_RE_ADD_STRING = re.compile(r'\.Добавить\s*\(\s*"([^"]+)"\s*\)')
_RE_UNCHECKED_ADD = re.compile(
    r'МассивНепроверяемыхРеквизитов\s*\.\s*Добавить\s*\(\s*"([^"]+)"\s*\)',
    re.IGNORECASE,
)
_RE_TITLE_NUM = re.compile(r"(?i)титул\s*[_-]?\s*(\d+)\b")
_OP_FIELD_NEEDLES = (
    "ЗаполнитьИменаРеквизитовПоХозяйственнойОперации",
    "ИменаРеквизитовПоХозяйственнойОперации",
)

_COMPACT_HIT_KEYS = ("context", "path", "kind", "name", "synonym", "comment", "score")

# Unscoped body grep loads every method row / .bsl. Tiny fixtures only.
_UNSCOPED_BODY_SEARCH_MAX_METHODS = 500
_UNSCOPED_BODY_SEARCH_MAX_OBJECTS = 20_000

# U15: report-data NL queries («отчет по продажам за период в разрезе…») — tokens
# that only describe the request, not the report subject; used to pick the most
# specific non-stopword token for a scoped SQL recall of the Отчеты.* tree.
_REPORT_STOP_TOKENS = frozenset(
    "отчет отчета отчету отчеты отчетов отчёта отчёты отчётов отчетность "
    "скд данные данных данным сведения собрать собратьданные получить "
    "сформировать построить сформироватьотчет выгрузить выгрузка выгрузку "
    "период периода периоду периоде периоды периодов разрез разрезе разреза "
    "регион региона региону регионе регионы месяцев месяца месяце месяцы "
    "настроить настройки настройка показать показатели показателю "
    "колонку колонки таблицу таблицы список списка формы форму сформировать "
    "свод доля детализация срез унификация анализ"
    .split()
)


def _report_significant_tokens(query: str, max_n: int = 2) -> list[str]:
    """Non-stopword query tokens in order (report subject first), deduped.

    U15: for «отчет по продажам за период в разрезе номенклатуры» returns
    [«продажам», «номенклатуры»] (subject «продажи» precedes the dimension).
    """
    domains = ranking_svc.query_domains(query)
    goods_return = "goods_return" in domains
    budget = ranking_svc.looks_like_budget_exec_intent(query)
    out: list[str] = []
    seen: set[str] = set()
    tokens = ranking_svc.query_tokens_cased(query)
    if budget:
        # Prefer article/budget stems over generic «затрат/проект».
        preferred = [
            t
            for t in tokens
            if len(t) >= ranking_svc.MIN_PREFIX_TOKEN_LEN
            and (
                t.casefold().startswith("стать")
                or t.casefold().startswith("бюджет")
            )
        ]
        if preferred:
            tokens = preferred + [t for t in tokens if t not in preferred]
    for t in tokens:
        if len(t) < ranking_svc.MIN_PREFIX_TOKEN_LEN:
            continue
        cf = t.casefold()
        if cf in _REPORT_STOP_TOKENS or cf in seen:
            continue
        # Product-return NL: bare «возврат*» floods returnable-packaging reports.
        if goods_return and cf.startswith("возврат"):
            continue
        # Budget analytics: «категориям» hits HR qualification reports first.
        if budget and cf.startswith("категор"):
            continue
        seen.add(cf)
        out.append(t)
        if len(out) >= max_n:
            break
    return out


def invalidate_entity_cache(name: str | None = None) -> None:
    """Drop cached context lookup. Pass name or None to clear all."""
    with _entity_cache_lock:
        if name is None:
            _entity_cache.clear()
        else:
            _entity_cache.pop(name, None)
    if name is None:
        with _stem_recall_lock:
            _stem_recall_cache.clear()


def _norm_query(q: str) -> str:
    return " ".join((q or "").casefold().split())


def _note_fanout(query: str, context_names: list[str]) -> str | None:
    """Return a warning if the same query recently hit other contexts (agent fan-out)."""
    qn = _norm_query(query)
    if not qn or not context_names:
        return None
    now = time.time()
    others: set[str] = set()
    with _FANOUT_LOCK:
        while _FANOUT_RECENT and now - _FANOUT_RECENT[0][0] > _FANOUT_WINDOW_SEC:
            _FANOUT_RECENT.popleft()
        for _ts, prev_q, prev_ctx in _FANOUT_RECENT:
            if prev_q == qn and prev_ctx not in context_names:
                others.add(prev_ctx)
        for ctx in context_names:
            _FANOUT_RECENT.append((now, qn, ctx))
    if not others:
        return None
    shown = ", ".join(sorted(others)[:5])
    return (
        f"Possible fan-out: query recently used on other context(s): {shown}. "
        "If the user named one context, do not repeat the same query elsewhere."
    )


def _path_under(path: str, prefix: str) -> bool:
    p = path or ""
    pref = (prefix or "").strip().rstrip(".")
    if not pref:
        return True
    return p == pref or p.startswith(pref + ".")


def _compact_hit(hit: dict) -> dict:
    return {k: hit.get(k) for k in _COMPACT_HIT_KEYS if hit.get(k) not in (None, "")}


def _compact_results(payload: dict, *, compact: bool) -> dict:
    if not compact:
        return payload
    results = payload.get("results")
    if isinstance(results, list):
        payload = dict(payload)
        payload["results"] = [_compact_hit(h) if isinstance(h, dict) else h for h in results]
        payload["compact"] = True
    cands = payload.get("title_candidates")
    if isinstance(cands, list):
        payload = dict(payload)
        payload["title_candidates"] = [
            {k: c.get(k) for k in ("context", "path", "kind", "name", "synonym", "comment") if c.get(k)}
            if isinstance(c, dict)
            else c
            for c in cands
        ]
    return payload


def _fts_match_string(query: str) -> str:
    return ranking_svc.expand_fts_query(query)


def _query_fts(coll, query: str, *, topk: int, filter_expr: str | None, output_fields: list[str]):
    match = _fts_match_string(query)
    q = zvec.Query(field_name="text", fts=zvec.Fts(match_string=match))
    try:
        return list(
            coll.query(
                queries=q,
                topk=topk,
                filter=filter_expr,
                output_fields=output_fields,
            )
        )
    except Exception:
        if match != (query or "").strip():
            q = zvec.Query(field_name="text", fts=zvec.Fts(match_string=query))
            return list(
                coll.query(
                    queries=q,
                    topk=topk,
                    filter=filter_expr,
                    output_fields=output_fields,
                )
            )
        raise


def _sql_hit_dict(
    entity: dict,
    sh: dict,
    *,
    stem_recall: bool = False,
    named_entity_recall: bool = False,
    lexical_recall: bool = False,
) -> dict:
    hit = {
        "context": entity["name"],
        "path": sh.get("path"),
        "kind": sh.get("kind"),
        "belong": sh.get("belong"),
        "name": sh.get("name"),
        "synonym": sh.get("synonym"),
        "comment": sh.get("comment") or "",
        "score": float(sh.get("score") or 0),
    }
    if sh.get("fill_checking") is not None:
        hit["fill_checking"] = sh.get("fill_checking")
    if stem_recall:
        hit["_stem_recall"] = True
    if named_entity_recall:
        hit["_named_entity_recall"] = True
        hit["_stem_recall"] = True
    if lexical_recall:
        hit["_lexical_recall"] = True
        hit["_stem_recall"] = True
    return hit


def _domain_counter_exact_hits(
    conn,
    entity: dict,
    query: str,
    *,
    limit: int,
) -> list[dict]:
    """Indexed exact-name inject for domain-collapse counter-recall."""
    specs = ranking_svc.domain_counter_exact_specs(query)
    if not specs:
        return []
    eid = int(entity["id"])
    per = min(max(int(limit), 4), 8)
    rows: list[dict] = []
    seen: set[str] = set()
    for pref, names in specs:
        extra = obj_repo.search_by_exact_names(
            conn,
            eid,
            names,
            path_prefix=pref,
            include_borrowed=True,
            limit=per,
        )
        for sh in extra:
            p = str(sh.get("path") or "")
            if not p or p in seen:
                continue
            seen.add(p)
            hit = _sql_hit_dict(entity, sh, stem_recall=True)
            rows.append(hit)
    return rows


def _lexical_prefix_sql_hits(
    conn,
    entity: dict,
    query: str,
    *,
    limit: int,
    budget: _SqlRecallBudget | None = None,
) -> list[dict]:
    """U41: cheap name-prefix lane before stem mid-LIKE trees.

    At most 2 stems × 2 collections — each stem can cost multi-seconds on
    large Docker volumes; honor span budget between probes.
    """
    prefixes = ranking_svc.lexical_name_prefixes_for_sql(query, max_n=2)
    if not prefixes:
        return []
    eid = int(entity["id"])
    per = min(max(int(limit), 3), 6)
    rows: list[dict] = []
    seen: set[str] = set()
    for coll in ("Документы", "РегистрыНакопления"):
        if budget is not None and budget.exhausted():
            break
        for stem in prefixes:
            if budget is not None and budget.exhausted():
                break
            extra = obj_repo.search_by_name_prefixes(
                conn,
                eid,
                [stem],
                path_prefix=coll,
                include_borrowed=True,
                limit=per,
            )
            for sh in extra:
                p = str(sh.get("path") or "")
                if not p or p in seen:
                    continue
                seen.add(p)
                rows.append(_sql_hit_dict(entity, sh, lexical_recall=True))
                if len(rows) >= 8:
                    return rows
    return rows


def _count_stem_roots(hits: list[dict]) -> int:
    n = 0
    for h in hits:
        path = str(h.get("path") or "")
        if path.count(".") != 1:
            continue
        if h.get("_stem_recall") or h.get("_named_entity_recall") or h.get(
            "_lexical_recall"
        ):
            n += 1
    return n


def _named_entity_sql_hits(
    conn,
    entity: dict,
    query: str,
    *,
    limit: int,
    stem_like: bool = True,
) -> list[dict]:
    """U36: exact + stem recall for quoted/typed metadata names in the query."""
    specs = ranking_svc.extract_named_entity_specs(query)
    names = ranking_svc.named_entity_name_list(query)
    if not specs and not names:
        return []
    eid = int(entity["id"])
    rows: list[dict] = []
    seen: set[str] = set()
    per = min(max(int(limit), 4), 8)
    # U38: domain packs already recall real ERP roots; skip stem LIKE for
    # fictional quoted registers («Нормативы сроков ремонта») — those scans
    # dominated sql_recall (~3–5s) on pack2 R12/R18.
    domain_pack = ranking_svc.looks_like_budget_exec_intent(
        query
    ) or ranking_svc.looks_like_repair_overdue_intent(query)

    # Exact names under each prefix (indexed; cheap).
    prefixes = list(dict.fromkeys(pref for pref, _ in specs))
    if not prefixes:
        prefixes = [
            "РегистрыНакопления.",
            "РегистрыСведений.",
            "Документы.",
            "Справочники.",
        ]
    for pref in prefixes[:4]:
        if names:
            extra = obj_repo.search_by_exact_names(
                conn,
                eid,
                names[:6],
                path_prefix=pref.rstrip("."),
                include_borrowed=True,
                limit=per,
            )
            for sh in extra:
                p = str(sh.get("path") or "")
                if not p or p in seen:
                    continue
                seen.add(p)
                rows.append(
                    _sql_hit_dict(entity, sh, named_entity_recall=True)
                )

    if domain_pack or not stem_like:
        return rows

    # Stem roots for specs that exact-missed (cap scans; stop on roots).
    root_n = sum(1 for h in rows if str(h.get("path") or "").count(".") == 1)
    scans = 0
    for pref, stem in specs[: ranking_svc.MAX_NAMED_ENTITY_SPECS]:
        if root_n >= ranking_svc.MIN_STEM_ROOT_HITS:
            break
        if scans >= ranking_svc.MAX_STEM_LIKE_SCANS:
            break
        if any(stem.casefold() in str(p).casefold() for p in seen):
            continue
        extra = obj_repo.search_by_stem_roots(
            conn,
            eid,
            stem,
            path_prefix=pref,
            include_borrowed=True,
            limit=min(per, 6),
        )
        scans += 1
        for sh in extra:
            p = str(sh.get("path") or "")
            if not p or p in seen:
                continue
            seen.add(p)
            rows.append(_sql_hit_dict(entity, sh, named_entity_recall=True))
            if p.count(".") == 1:
                root_n += 1
    return rows


def _report_recall_blocked(query: str, hits: list[dict]) -> bool:
    """Skip report recall when a root Document already matches the subject and
    its name embeds «отчет» (авансовый отчет → Документы.АвансовыйОтчет).

    U15 guard: the recall leg must not steal the #1 spot from a document the
    query literally names («X отчет» = Документ.XОтчет).
    """
    toks = _report_significant_tokens(query, max_n=1)
    if not toks:
        return False
    t = toks[0].casefold()
    stem = t[: max(ranking_svc.MIN_PREFIX_TOKEN_LEN, len(t) - 2)]
    for h in hits:
        if str(h.get("kind") or "") != "Document" or not ranking_svc.is_root_object(h):
            continue
        nm = str(h.get("name") or "").casefold()
        if "отчет" in nm and (t in nm or stem in nm):
            return True
    return False


def _object_row_as_sql_hit(entity: dict, obj: dict) -> dict:
    return _sql_hit_dict(
        entity,
        {
            "path": obj.get("path"),
            "kind": obj.get("kind"),
            "belong": obj.get("belong"),
            "name": obj.get("name"),
            "synonym": obj.get("synonym"),
            "comment": "",
            "score": 1.0,
        },
    )


class _SqlRecallBudget:
    """Wall-clock ceiling for one ``sql_recall`` span.

    Nested above class-C ``STEM_RECALL_BUDGET_MS``: stem packs keep their own
    inner cap; this object is the outer stop for token/help/report/stem in the
    same span. Do not merge the two hit flags — diagnostics must show which
    budget fired (``stem_recall_budget_hit`` vs ``sql_recall_budget_hit``).
    """

    __slots__ = (
        "deadline",
        "sql_recall_budget_hit",
        "stem_recall_budget_hit",
        "unscoped_mid_like_skipped",
    )

    def __init__(self, budget_ms: float | None = None) -> None:
        ms = float(
            ranking_svc.SQL_RECALL_SPAN_BUDGET_MS
            if budget_ms is None
            else budget_ms
        )
        self.deadline = time.perf_counter() + max(0.0, ms) / 1000.0
        self.sql_recall_budget_hit = False
        self.stem_recall_budget_hit = False
        self.unscoped_mid_like_skipped = False

    def remaining_ms(self) -> float:
        return max(0.0, (self.deadline - time.perf_counter()) * 1000.0)

    def exhausted(self) -> bool:
        if time.perf_counter() >= self.deadline:
            self.sql_recall_budget_hit = True
            return True
        return False

    def attach_flags(self, payload: dict) -> None:
        if self.sql_recall_budget_hit:
            payload["sql_recall_budget_hit"] = True
        if self.stem_recall_budget_hit:
            payload["stem_recall_budget_hit"] = True
        if self.unscoped_mid_like_skipped:
            payload["sql_recall_skipped"] = "unscoped_mid_like_forbidden"


def _is_report_path_prefix(path_prefix: str | None) -> bool:
    """True when scope is the report collection tree (mode A only — no mid-LIKE)."""
    p = (path_prefix or "").strip().rstrip(".")
    if not p:
        return False
    head = p.split(".", 1)[0]
    return head in {"Отчеты", "Reports"}


def _pool_strong_for_token_sql(query: str, hits: list[dict]) -> bool:
    """Skip token mid-LIKE when hybrid/FTS already has a usable root pool."""
    if not hits:
        return False
    if _root_content_hits(query, hits) >= ranking_svc.MIN_STEM_ROOT_HITS:
        return True
    report_roots = 0
    any_roots = 0
    for h in hits:
        if not ranking_svc.is_root_object(h):
            continue
        any_roots += 1
        if str(h.get("kind") or "") == "Report":
            report_roots += 1
    if report_roots >= 1:
        return True
    return any_roots >= ranking_svc.MIN_STEM_ROOT_HITS


def _sql_identifier_hits(
    conn,
    entity: dict,
    query: str,
    *,
    include_borrowed: bool,
    limit: int,
) -> list[dict]:
    """Indexed exact path / name= for a single identifier. No unscoped %LIKE%.

    Does not filter ``kind``: a Procedure/Function with the same name can
    appear in ``search_metadata``. That is intentional (the tool may surface
    methods) and matches the old LIKE plan — not a regression to hide.
    """
    raw = (query or "").strip()
    if not raw:
        return []
    eid = int(entity["id"])
    lim = max(1, min(int(limit), 50))
    candidates = [raw]
    canonical = refs.normalize_path(raw)
    if canonical and canonical not in candidates:
        candidates.append(canonical)
    for path in candidates:
        obj = obj_repo.get_object(conn, eid, path)
        if obj:
            if not include_borrowed and obj.get("belong") not in (None, "", "Own"):
                continue
            return [_object_row_as_sql_hit(entity, obj)]
    name = raw.rsplit(".", 1)[-1]
    pref = None
    if "." in (canonical or raw):
        parts = (canonical or raw).split(".")
        if len(parts) >= 2:
            pref = ".".join(parts[:-1])
            name = parts[-1]
    rows = obj_repo.search_by_exact_names(
        conn,
        eid,
        [name],
        path_prefix=pref,
        include_borrowed=include_borrowed,
        limit=lim,
    )
    return [_sql_hit_dict(entity, sh) for sh in rows]


def _sql_token_hits(
    conn,
    entity: dict,
    query: str,
    *,
    kind: str | None,
    include_borrowed: bool,
    limit: int,
    path_prefix: str | None = None,
    fts_hit_count: int = 0,
    needs_report_roots: bool = False,
    pool_strong: bool = False,
    budget: _SqlRecallBudget | None = None,
) -> list[dict]:
    # Keep original CamelCase — SQLite LIKE is case-sensitive for Cyrillic, so
    # casefolded ``вводостатков`` misses ``ВводОстатковСПодотчетниками``.
    tokens = ranking_svc.query_tokens_cased(query)
    long_tokens = [t for t in tokens if len(t) >= ranking_svc.MIN_PREFIX_TOKEN_LEN]
    aliases = ranking_svc.alias_terms_for_query(query)
    doc_aliases = ranking_svc.document_alias_terms_for_query(query)
    if not long_tokens and not aliases and not doc_aliases:
        return []
    if budget is not None and budget.exhausted():
        return []
    rows: list = []
    # Policy (discussion/2026-08-20-sql-recall-policy-proposal.md §3/§9):
    #   A indexed — always OK
    #   B scoped mid-LIKE — path_prefix/kind, not report tree, pool weak
    #   C stem packs — separate budget (caller)
    #   D unscoped mid-LIKE — forbidden (never search_by_tokens without scope)
    scoped = bool((path_prefix or "").strip() or kind)
    help_kind = kind == HELP_KIND or str(path_prefix or "").startswith("Help")
    report_tree = _is_report_path_prefix(path_prefix)
    ident = (not scoped) and _looks_like_method_identifier(query)
    # Prefer caller pool_strong; fall back to empty-FTS heuristic only when
    # caller did not pass hits (legacy: fts_hit_count alone is not "pool strength").
    strong = bool(pool_strong)
    ident_hits: list = []
    if ident and not strong:
        # Indexed name= / path=. Unscoped %identifier% is class D — never.
        ident_hits = _sql_identifier_hits(
            conn,
            entity,
            query,
            include_borrowed=include_borrowed,
            limit=limit,
        )
    # Class D: unscoped mid-LIKE is banned even when FTS is empty.
    if long_tokens and not scoped and not ident:
        if budget is not None:
            budget.unscoped_mid_like_skipped = True
    allow_mid_like = (
        bool(long_tokens)
        and scoped
        and not report_tree  # report tree: mode A only (name-prefix / exact)
        and not strong
        and not ident
        and (budget is None or not budget.exhausted())
    )
    if allow_mid_like:
        # NL queries have many verbs/stopwords; AND of all of them kills recall.
        # Keep the two longest stems (e.g. подотчетным + остатки).
        use_tokens = long_tokens
        if len(use_tokens) > 2:
            use_tokens = sorted(use_tokens, key=lambda t: (-len(t), t.casefold()))[:2]
        # Help: one noun is enough; verbs double a Help-tree scan.
        if help_kind and len(use_tokens) > 1:
            use_tokens = [
                sorted(use_tokens, key=lambda t: (-len(t), t.casefold()))[0]
            ]
        sql_q = " ".join(use_tokens)
        rows = obj_repo.search_by_tokens(
            conn,
            int(entity["id"]),
            sql_q,
            kind=kind,
            include_borrowed=include_borrowed,
            path_prefix=path_prefix,
            limit=limit,
        )
    # U14: each alias term separately (OR recall) — AND of report names would miss.
    # Exact name under Отчеты.* (no comment LIKE — mid-string under the report tree
    # was multi-second on ERP when aliases fired).
    if aliases and (budget is None or not budget.exhausted()):
        alias_prefix = (path_prefix or "").strip().rstrip(".") or ranking_svc.REPORT_PATH_PREFIX_RU
        seen_paths = {r["path"] for r in rows if r["path"]}
        per_alias_limit = max(5, min(int(limit), 20))
        for term in aliases:
            if budget is not None and budget.exhausted():
                break
            if any(term.casefold() in str(p).casefold() for p in seen_paths):
                continue
            extra = obj_repo.search_by_exact_names(
                conn,
                int(entity["id"]),
                [term],
                path_prefix=alias_prefix,
                include_borrowed=include_borrowed,
                limit=per_alias_limit,
            )
            for r in extra:
                p = r["path"]
                if p and p not in seen_paths:
                    seen_paths.add(p)
                    rows.append(r)
    # P0: document aliases (purchase-from-supplier → ПриобретениеТоваровУслуг)
    # Exact Документы.<Name> / name= — must not comment-LIKE the Documents tree.
    if doc_aliases and (budget is None or not budget.exhausted()):
        explicit = (path_prefix or "").strip()
        skip_docs = bool(
            explicit
            and not explicit.startswith(ranking_svc.DOCUMENT_PATH_PREFIX_RU)
            and not explicit.startswith("Documents.")
        )
        if not skip_docs:
            doc_prefix = (
                (explicit.rstrip(".") + ".")
                if explicit
                else ranking_svc.DOCUMENT_PATH_PREFIX_RU
            )
            seen_paths = {r["path"] for r in rows if r["path"]}
            per_alias_limit = max(5, min(int(limit), 20))
            for term in doc_aliases:
                if budget is not None and budget.exhausted():
                    break
                if any(term.casefold() in str(p).casefold() for p in seen_paths):
                    continue
                extra = obj_repo.search_by_exact_names(
                    conn,
                    int(entity["id"]),
                    [term],
                    path_prefix=doc_prefix.rstrip("."),
                    include_borrowed=include_borrowed,
                    limit=per_alias_limit,
                )
                for r in extra:
                    p = r["path"]
                    if p and p not in seen_paths:
                        seen_paths.add(p)
                        rows.append(r)
    # U15: report-data NL without alias terms — indexed name-prefix under Отчеты.*
    # (mode A). Mid-LIKE under the report tree is out of the hot path (§9).
    if needs_report_roots and not aliases and (budget is None or not budget.exhausted()):
        # First significant token is the report subject («по продажам» → продажи);
        # dimension tokens (номенклатуры/региону) would outmatch it in the exact
        # name match and push misleading reports to the top.
        toks = _report_significant_tokens(query, max_n=1)
        if toks:
            rp = (path_prefix or "").strip().rstrip(".") or ranking_svc.REPORT_PATH_PREFIX_RU
            seen_paths = {r["path"] for r in rows if r["path"]}
            per_tok_limit = max(10, min(int(limit) * 4, 60))
            for tok in toks:
                if budget is not None and budget.exhausted():
                    break
                # Name-prefix under Отчеты.* (not mid-LIKE/path BETWEEN tax).
                stem = ranking_svc.nl_token_to_metadata_stem(tok) or tok
                for r in obj_repo.search_by_name_prefixes(
                    conn,
                    int(entity["id"]),
                    [stem],
                    path_prefix=rp,
                    include_borrowed=include_borrowed,
                    limit=per_tok_limit,
                ):
                    p = r["path"]
                    if p and p not in seen_paths:
                        seen_paths.add(p)
                        rows.append(r)
    sql_hits = [_sql_hit_dict(entity, sh) for sh in rows]
    if not ident_hits:
        return sql_hits
    seen = {str(h.get("path") or "") for h in ident_hits}
    extra = [h for h in sql_hits if str(h.get("path") or "") not in seen]
    return ident_hits + extra


def _merge_sql_hits(hits: list[dict], sql_hits: list[dict]) -> list[dict]:
    seen = {str(h.get("path") or "") for h in hits}
    for sh in sql_hits:
        p = str(sh.get("path") or "")
        if p and p not in seen:
            seen.add(p)
            hits.append(sh)
    return hits


def _intent_stem_sql_hits(
    conn,
    entity: dict,
    query: str,
    *,
    limit: int,
    prior_root_hits: int = 0,
    budget: _SqlRecallBudget | None = None,
) -> list[dict]:
    """Run capped stem-pack SQL once per (entity, query); reuse within TTL.

    Early-stops after ``MIN_STEM_ROOT_HITS`` root hits so we do not pay
    ~10s×N for every Document/CommonModule tree on ERP.

    U41: cheaper prefixes first, hard scan/time budget, skip when lexical
    lane already filled the root quota.

    Class-C ``STEM_RECALL_BUDGET_MS`` is an *inner* cap. When ``budget`` (span
    ``SQL_RECALL_SPAN_BUDGET_MS``) is passed, it is an *outer* ceiling — not a
    replacement for C. Hitting C sets ``stem_recall_budget_hit``; hitting the
    span sets ``sql_recall_budget_hit`` (separate flags).
    """
    eid = int(entity["id"])
    key = (eid, (query or "").casefold().strip())
    if not key[1]:
        return []
    if budget is not None and budget.exhausted():
        return []
    now = time.time()
    with _stem_recall_lock:
        cached = _stem_recall_cache.get(key)
        if cached and (now - cached[0]) < _STEM_RECALL_TTL_SEC:
            return [dict(h) for h in cached[1]]
        if len(_stem_recall_cache) > 64:
            dead = [
                k
                for k, (ts, _) in _stem_recall_cache.items()
                if now - ts >= _STEM_RECALL_TTL_SEC
            ]
            for k in dead:
                _stem_recall_cache.pop(k, None)

    specs = ranking_svc.intent_stem_recall_specs(query)
    # Cheaper trees before Документы.* (Document mid-LIKE is the ERP tax).
    specs = sorted(specs, key=ranking_svc.stem_spec_cost_key)
    rows: list[dict] = []
    root_n = int(prior_root_hits)
    reconcile = ranking_svc.looks_like_report_reconcile_intent(query)
    pricing_fx = ranking_svc.looks_like_pricing_fx_intent(query)
    budget_exec = ranking_svc.looks_like_budget_exec_intent(query)
    repair_overdue = ranking_svc.looks_like_repair_overdue_intent(query)
    # Chain packs may continue past MIN_STEM_ROOT_HITS; still always apply a
    # wall-clock budget (batch4: budget Document mid-LIKE was ~300ms+).
    chain_pack = reconcile or pricing_fx or budget_exec or repair_overdue
    if not chain_pack and root_n >= ranking_svc.MIN_STEM_ROOT_HITS:
        # Do not cache: skip is conditional on prior lexical/named hits.
        return []

    t0 = time.perf_counter()
    scans = 0
    max_scans = (
        ranking_svc.MAX_INTENT_STEM_SPECS
        if chain_pack
        else ranking_svc.MAX_STEM_LIKE_SCANS
    )
    for pref, stem in specs:
        if scans >= max_scans:
            break
        if budget is not None and budget.exhausted():
            break
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if elapsed_ms >= ranking_svc.STEM_RECALL_BUDGET_MS:
            if budget is not None:
                budget.stem_recall_budget_hit = True
            break
        # Root-only path/name LIKE — avoids ~10s Document-tree scans.
        extra = obj_repo.search_by_stem_roots(
            conn,
            eid,
            stem,
            path_prefix=pref,
            include_borrowed=True,
            limit=min(max(int(limit), 6), 10),
        )
        scans += 1
        if not extra:
            continue
        for sh in extra:
            hit = _sql_hit_dict(entity, sh, stem_recall=True)
            rows.append(hit)
            path = str(hit.get("path") or "")
            # Depth-2 roots only (Документы.X).
            if path.count(".") == 1:
                root_n += 1
        # U37: reconcile needs BOTH ОСВ and ОПиУ/финрезультат sides.
        if reconcile:
            osv, pnl = ranking_svc.reconcile_sides_covered(rows)
            if osv and pnl:
                break
            continue
        # U37: FX pricing — keep scanning until rate/job/auto-recalc anchor.
        if pricing_fx:
            if (
                ranking_svc.pricing_fx_anchor_hit(rows)
                and root_n >= ranking_svc.MIN_STEM_ROOT_HITS
            ):
                break
            continue
        # U38: budget execution — limit + claim/fact before early-stop.
        if budget_exec:
            if (
                ranking_svc.budget_exec_chain_covered(rows)
                and root_n >= ranking_svc.MIN_STEM_ROOT_HITS
            ):
                break
            continue
        # U38: repair overdue — order + schedule/plan register.
        if repair_overdue:
            if (
                ranking_svc.repair_overdue_anchor_hit(rows)
                and root_n >= ranking_svc.MIN_STEM_ROOT_HITS
            ):
                break
            continue
        if root_n >= ranking_svc.MIN_STEM_ROOT_HITS:
            break

    with _stem_recall_lock:
        _stem_recall_cache[key] = (now, rows)
    return [dict(h) for h in rows]


def _root_content_hits(query: str, hits: list[dict]) -> int:
    """How many depth-2 roots already cover ≥1 content token."""
    n = 0
    for h in hits:
        path = str(h.get("path") or "")
        if path.count(".") != 1:
            continue
        if ranking_svc.hit_content_matches(query, h):
            n += 1
    return n


def _apply_howto_help_recall(
    conn,
    entity: dict,
    query: str,
    hits: list[dict],
    *,
    path_prefix: str | None,
    limit: int,
    stem_recall: bool = True,
    budget: _SqlRecallBudget | None = None,
) -> list[dict]:
    """U23/U25/U28–U38: Help+owners; settings/RLS/policy/query-join skip Help.

    Settings / RLS / accounting-policy / query-JOIN / pricing / reconcile /
    budget-exec / repair-overdue questions are verb-heavy; Help SQL on those
    verbs pollutes the pool, so they use stem / named-entity recall instead of
    Help. Class intents also use scoped stem recall — cached so FTS+semantic
    do not double-pay within ``_STEM_RECALL_TTL_SEC``.
    Pass ``stem_recall=False`` from FTS (exact path); stems belong on semantic.
    """
    if budget is not None and budget.exhausted():
        return hits
    explicit = (path_prefix or "").strip()
    settings = ranking_svc.looks_like_settings_constraint_query(query)
    rls = ranking_svc.looks_like_rls_query(query)
    policy = ranking_svc.looks_like_accounting_policy_query(query)
    query_join = ranking_svc.looks_like_query_join_intent(query)
    pricing = ranking_svc.looks_like_pricing_intent(query)
    pricing_fx = ranking_svc.looks_like_pricing_fx_intent(query)
    reconcile = ranking_svc.looks_like_report_reconcile_intent(query)
    budget_exec = ranking_svc.looks_like_budget_exec_intent(query)
    repair_overdue = ranking_svc.looks_like_repair_overdue_intent(query)
    user_howto = ranking_svc.looks_like_user_howto_query(query)
    qcf = (query or "").casefold()
    order_pack = (
        "ордерн" in qcf
        or ("ордер" in qcf and "склад" in qcf)
        or bool(re.search(r"зон\w*\s+(?:приём|прием|хранен|отгруз)", qcf))
    )

    if settings and not explicit:
        stems: list[str] = []
        for needle, stem in (
            ("цен", "Ценов"),
            ("цен", "Допустим"),
            ("скидк", "Скидк"),
            ("наценк", "Скидк"),
            ("огранич", "Ограничен"),
            ("прав", "Прав"),
            ("редактир", "Ограничен"),
        ):
            if needle in qcf and stem not in stems:
                stems.append(stem)
        if not stems:
            stems = ["Ограничен"]
        for pref in ("Константы.", "ФункциональныеОпции.", "Роли."):
            if budget is not None and budget.exhausted():
                break
            for stem in stems[:3]:
                if budget is not None and budget.exhausted():
                    break
                extra = obj_repo.search_by_stem_roots(
                    conn,
                    int(entity["id"]),
                    stem,
                    path_prefix=pref,
                    include_borrowed=True,
                    limit=min(max(int(limit), 8), 15),
                )
                if extra:
                    hits = _merge_sql_hits(
                        hits,
                        [
                            _sql_hit_dict(entity, sh, stem_recall=True)
                            for sh in extra
                        ],
                    )
        return hits

    # Pool strength from hybrid/FTS *before* lexical SQL — if roots already
    # cover the query, skip the Docker-tax name-prefix lane (same early-stop
    # idea as stem packs below).
    pool_strong = (
        _root_content_hits(query, hits) >= ranking_svc.MIN_STEM_ROOT_HITS
        or _pool_strong_for_token_sql(query, hits)
    )

    # U41: cheap name-prefix lexical lane before named/stem LIKE.
    if (
        stem_recall
        and not explicit
        and not pool_strong
        and (budget is None or not budget.exhausted())
    ):
        lex_hits = _lexical_prefix_sql_hits(
            conn, entity, query, limit=limit, budget=budget
        )
        if lex_hits:
            hits = _merge_sql_hits(hits, lex_hits)
            pool_strong = (
                _root_content_hits(query, hits) >= ranking_svc.MIN_STEM_ROOT_HITS
                or _pool_strong_for_token_sql(query, hits)
            )

    # U36: always exact named-entity; stem LIKE only if pool still weak.
    if stem_recall and not explicit and (budget is None or not budget.exhausted()):
        named_hits = _named_entity_sql_hits(
            conn,
            entity,
            query,
            limit=limit,
            stem_like=not pool_strong,
        )
        if named_hits:
            hits = _merge_sql_hits(hits, named_hits)
            pool_strong = (
                _root_content_hits(query, hits) >= ranking_svc.MIN_STEM_ROOT_HITS
            )

    # Domain-collapse: exact-name counter-recall (indexed) even when hybrid
    # already has content-covered roots — those roots are often the wrong
    # island (payroll coef / lease) and would skip stem packs.
    domains = ranking_svc.query_domains(query)
    schedule_intent = ranking_svc.looks_like_production_schedule_query(query)
    force_domain = bool(
        domains
        & {
            "settlement",
            "overhead",
            "labor_load",
            "warehouse_ops",
            "goods_return",
        }
    ) or ranking_svc.looks_like_budget_exec_intent(query) or schedule_intent
    if (
        stem_recall
        and not explicit
        and force_domain
        and (budget is None or not budget.exhausted())
    ):
        exact_hits = _domain_counter_exact_hits(
            conn, entity, query, limit=limit
        )
        if exact_hits:
            hits = _merge_sql_hits(hits, exact_hits)
            # Exact inject already filled domain anchors — skip stem packs that
            # still pay ~1–2s/name-prefix under Catalog/Document on ERP Docker.
            if (
                ranking_svc.looks_like_budget_exec_intent(query)
                or "goods_return" in domains
                or schedule_intent
            ):
                pool_strong = True

    # U28–U35/U41: stem packs — skipped when lexical/hybrid already filled roots,
    # except chain packs (reconcile / pricing_fx / budget / repair): hybrid roots
    # are often the wrong island, and _intent_stem_sql_hits already continues
    # past MIN_STEM_ROOT_HITS until the pack anchor. Class C under outer span
    # budget (see _SqlRecallBudget / STEM_RECALL_BUDGET_MS).
    chain_pack = (
        reconcile
        or pricing_fx
        or budget_exec
        or repair_overdue
    )
    if (
        stem_recall
        and not explicit
        and (not pool_strong or chain_pack)
        and (budget is None or not budget.exhausted())
    ):
        stem_hits = _intent_stem_sql_hits(
            conn,
            entity,
            query,
            limit=limit,
            prior_root_hits=_count_stem_roots(hits),
            budget=budget,
        )
        if stem_hits:
            hits = _merge_sql_hits(hits, stem_hits)

    # Skip Help SQL when metadata intents own the answer; still promote owners
    # already present in the hybrid pool (Help → Отчеты.X / Документы.X).
    skip_help = (
        rls
        or policy
        or query_join
        or pricing
        or reconcile
        or budget_exec
        or repair_overdue
        or order_pack
        or ranking_svc.should_skip_help_for_strong_pool(query, hits)
    )
    if skip_help and not explicit:
        return ranking_svc.ensure_help_owners(hits)

    if not user_howto:
        return hits
    if explicit and not explicit.startswith("Help."):
        return ranking_svc.ensure_help_owners(hits)
    if budget is not None and budget.exhausted():
        return ranking_svc.ensure_help_owners(hits)
    help_prefix = explicit if explicit.startswith("Help.") else "Help."
    help_hits = _sql_token_hits(
        conn,
        entity,
        query,
        kind=HELP_KIND,
        include_borrowed=True,
        limit=min(max(int(limit), 10), 30),
        path_prefix=help_prefix,
        fts_hit_count=0,
        needs_report_roots=False,
        pool_strong=False,
        budget=budget,
    )
    if help_hits:
        hits = _merge_sql_hits(hits, help_hits)
    return ranking_svc.ensure_help_owners(hits)


def _attach_skd_and_compare_hints(
    out: dict,
    query: str,
    items: list[dict],
) -> None:
    """U32: after Report hits suggest get_skd; on compare-NL suggest compare_objects."""
    if not items:
        return
    report_roots: list[str] = []
    skd_paths: list[str] = []
    for h in items:
        kind = str(h.get("kind") or "")
        path = str(h.get("path") or "")
        if not path:
            continue
        if kind == "DataCompositionSchema":
            skd_paths.append(path)
        elif kind == "Report" and path.count(".") == 1:
            report_roots.append(path)
        elif ranking_svc.report_root_path(path):
            root = ranking_svc.report_root_path(path)
            if root and root not in report_roots:
                report_roots.append(root)
    if (
        ranking_svc.looks_like_report_query(query)
        or skd_paths
        or (report_roots and ranking_svc.looks_like_user_howto_query(query))
    ):
        cands = list(dict.fromkeys(skd_paths))[:5]
        if not cands:
            cands = [f"{p}.Макеты" for p in report_roots[:3]]
        if cands:
            out["skd_candidates"] = cands
            out["skd_hint"] = (
                "Report / analytics how-to: call get_skd(context, path) on a "
                "DataCompositionSchema under Отчеты.X.Макеты.* (see skd_candidates) "
                "to inspect fields (series, shelf life, analytics)."
            )
    if ranking_svc.looks_like_compare_query(query):
        roots = [
            str(h.get("path") or "")
            for h in items
            if ranking_svc.is_root_object(h)
            and str(h.get("kind") or "") in {"Document", "Catalog", "Report"}
        ][:4]
        out["compare_hint"] = (
            "Compare-two-objects intent: call compare_objects(context, path_a, path_b) "
            "with two Document/Catalog/Report roots from results."
        )
        if len(roots) >= 2:
            out["compare_candidates"] = roots[:2]


def _parse_title_num(query: str) -> str | None:
    m = _RE_TITLE_NUM.search(query or "")
    return m.group(1) if m else None


def _attach_title_candidates(
    out: dict,
    query: str,
    entities: list[dict],
    *,
    path_prefix: str | None,
) -> None:
    """When query mentions ТитулN, add Document roots as title_candidates."""
    if path_prefix:
        return
    num = _parse_title_num(query)
    if not num or not entities:
        return
    conn = connect(settings.db_path)
    try:
        cands: list[dict] = []
        for ent in entities:
            for row in obj_repo.list_title_document_candidates(
                conn, int(ent["id"]), num, limit=40
            ):
                cands.append(
                    {
                        "context": ent["name"],
                        **row,
                    }
                )
        if cands:
            out["title_candidates"] = cands
            out["title_candidates_hint"] = (
                f"Query mentions Титул{num}: pick one Document path, then "
                "search_under / get_required_fields on it (do not treat attribute "
                "hits named ТитулN as the document root)."
            )
    finally:
        conn.close()


def _enrich_comments(hits: list[dict], entities: list[dict]) -> None:
    """Attach object.comment from SQLite (already in FTS text, but not in zvec fields)."""
    if not hits or not entities:
        return
    by_name = {e["name"]: e for e in entities}
    # group paths per entity
    paths_by_eid: dict[int, list[str]] = {}
    for h in hits:
        ent = by_name.get(str(h.get("context") or ""))
        if not ent:
            continue
        eid = int(ent["id"])
        paths_by_eid.setdefault(eid, []).append(str(h.get("path") or ""))
    conn = connect(settings.db_path)
    try:
        comments: dict[tuple[str, str], str] = {}
        for ent in entities:
            eid = int(ent["id"])
            cmap = obj_repo.comments_by_paths(conn, eid, paths_by_eid.get(eid, []))
            for path, cmt in cmap.items():
                if cmt:
                    comments[(ent["name"], path)] = cmt
        for h in hits:
            key = (str(h.get("context") or ""), str(h.get("path") or ""))
            if key in comments and not h.get("comment"):
                h["comment"] = comments[key]
    finally:
        conn.close()


def _suggest_context_candidates(name: str, conn: sqlite3.Connection) -> list[str]:
    rows = ent_repo.list_ready_contexts(conn)
    names = [r["name"] for r in rows if r.get("name")]
    needle = (name or "").casefold()
    if not needle:
        return []
    exact_ci = [n for n in names if n.casefold() == needle]
    if exact_ci:
        return exact_ci
    ends = [n for n in names if n.casefold().endswith(needle)]
    if len(ends) == 1:
        return ends
    contains = [n for n in names if needle in n.casefold()]
    if len(contains) == 1:
        return contains
    # prefer longer overlap
    scored = sorted(
        ((n, n.casefold()) for n in names if needle in n.casefold() or n.casefold() in needle),
        key=lambda t: (-len(t[1]), t[0]),
    )
    return [n for n, _ in scored[:8]]


def parse_context_ref(raw: str) -> tuple[str, str]:
    """Return ('name', entity_name) or ('tag', tag_name)."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("Context must not be empty")
    if s.lower().startswith(TAG_PREFIX):
        tag = s[len(TAG_PREFIX) :].strip()
        if not tag:
            raise ValueError("Empty tag after 'tag:'. Example: tag:КА2")
        return "tag", tag
    return "name", s


def _open_collection(entity: dict):
    path = collection_path(entity["id"], entity["model"])
    if not path.exists():
        raise FileNotFoundError(
            f"Index for context '{entity['name']}' not found at {path}. Run reindex in UI."
        )
    return zvec_store.get(path, read_only=True)


def _dedupe_hits_by_path(hits: list[dict]) -> list[dict]:
    """Keep best (first) hit per path — chunk mode may return several docs for one method."""
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        path = str(h.get("path") or "")
        if path in seen:
            continue
        seen.add(path)
        out.append(h)
    return out


def _dedupe_hits_by_context_path(hits: list[dict]) -> list[dict]:
    """Keep best hit per (context, path) for federated search."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for h in hits:
        key = (str(h.get("context") or ""), str(h.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _filter_expr(
    *,
    kind: str | None,
    include_borrowed: bool,
    exclude_methods: bool = False,
    methods_only: bool = False,
    path_prefix: str | None = None,
) -> str | None:
    parts = []
    if methods_only:
        parts.append("(kind = 'Procedure' OR kind = 'Function')")
    elif kind:
        parts.append(f"kind = '{kind}'")
    if not include_borrowed:
        parts.append("belong = 'Own'")
    if exclude_methods and not methods_only:
        parts.append("kind != 'Procedure'")
        parts.append("kind != 'Function'")
    pref = (path_prefix or "").strip().rstrip(".")
    if pref:
        # zvec 0.6.0 native filter accepts LIKE (`path LIKE 'pref.%'`).
        # Python wrapper docs mention ``==``; the engine parse rejects ``==``
        # (probed 2026-08-18: hybrid query on a large methods collection).
        safe = pref.replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"path LIKE '{safe}.%'")
    return " AND ".join(parts) if parts else None


def _method_parent_from_path(path: str) -> str:
    """Owner object path from a method hit path (…Parent.Методы.Role.Name)."""
    p = (path or "").strip()
    if ".Методы." in p:
        return p.split(".Методы.", 1)[0]
    if "." in p:
        return p.rsplit(".", 1)[0]
    return p


def _method_hit_under_parent(path: str, parent_path: str | None) -> bool:
    """True if method path belongs to parent_path (no SQLite / props_json)."""
    pref = (parent_path or "").strip().rstrip(".")
    if not pref:
        return True
    p = (path or "").strip()
    if not p:
        return False
    if p.startswith(pref + ".Методы."):
        return True
    return _method_parent_from_path(p) == pref


# Collection roots (no object name): body grep under these is still ERP-wide I/O.
_COLLECTION_ROOT_NAMES = frozenset(
    {
        "Документы",
        "Documents",
        "Справочники",
        "Catalogs",
        "Отчеты",
        "Reports",
        "Обработки",
        "DataProcessors",
        "РегистрыСведений",
        "InformationRegisters",
        "РегистрыНакопления",
        "AccumulationRegisters",
        "РегистрыБухгалтерии",
        "AccountingRegisters",
        "РегистрыРасчета",
        "CalculationRegisters",
        "ОбщиеМодули",
        "CommonModules",
        "ОбщиеФормы",
        "CommonForms",
        "Роли",
        "Roles",
        "Константы",
        "Constants",
        "ФункциональныеОпции",
        "FunctionalOptions",
        "HTTPСервисы",
        "HTTPServices",
        "WebСервисы",
        "WebServices",
        "РегламентныеЗадания",
        "ScheduledJobs",
        "Перечисления",
        "Enums",
        "ПланыВидовХарактеристик",
        "ChartsOfCharacteristicTypes",
        "ПланыОбмена",
        "ExchangePlans",
        "БизнесПроцессы",
        "BusinessProcesses",
        "Задачи",
        "Tasks",
    }
)
_COLLECTION_ROOT_NAMES_CF = frozenset(n.casefold() for n in _COLLECTION_ROOT_NAMES)


def _is_collection_root_path(parent_path: str | None) -> bool:
    """True for ``Обработки`` / ``ОбщиеМодули`` (no object) — not ``ОбщиеМодули.X``."""
    pref = (parent_path or "").strip().rstrip(".")
    if not pref or "." in pref:
        return False
    return pref in _COLLECTION_ROOT_NAMES or pref.casefold() in _COLLECTION_ROOT_NAMES_CF


def _unscoped_body_search_blocked(entity: dict, parent_path: str | None) -> bool:
    """True when dump-wide method/body grep would scan a large context."""
    if (parent_path or "").strip():
        return False
    methods = int(entity.get("bsl_method_count") or 0)
    objects = int(entity.get("object_count") or 0)
    if methods > _UNSCOPED_BODY_SEARCH_MAX_METHODS:
        return True
    if methods == 0 and objects > _UNSCOPED_BODY_SEARCH_MAX_OBJECTS:
        return True
    return False


def _body_search_parent_too_broad(parent_path: str | None) -> bool:
    """Collection-level parent still greps thousands of .bsl files."""
    return _is_collection_root_path(parent_path)


def _looks_like_method_identifier(query: str) -> bool:
    """Single-token PascalCase / identifier — not a natural-language phrase."""
    q = (query or "").strip()
    if not q or any(c.isspace() for c in q):
        return False
    return all(c.isalnum() or c in "._" for c in q)


def _should_fuzzy_methods(query: str, exact_count: int) -> bool:
    """Fuzzy only when exact is empty, or weak natural-language exact (<3).

    Identifier queries with ≥1 exact hit skip semantic (avoids 100s zvec on BP).
    NL phrases with zero exact hits go straight to fuzzy (exact LIKE is skipped
    in ``list_methods`` for multi-word queries).
    """
    if exact_count >= 3:
        return False
    if exact_count >= 1 and _looks_like_method_identifier(query):
        return False
    return True


# Report roots are often missing from `objects` (only nested children indexed).
# kind=Report must not go into FTS/SQL filters — search the tree, then promote.
_REPORT_KIND_ALIASES = frozenset({"report", "отчет", "отчёт"})
_DEFAULT_REPORT_PATH_PREFIX = "Отчеты."


def _is_report_kind(kind: str | None) -> bool:
    if not kind:
        return False
    return kind.strip().casefold() in _REPORT_KIND_ALIASES


def _report_search_scope(
    kind: str | None, path_prefix: str | None
) -> tuple[str | None, str | None, bool]:
    """Remap kind=Report → no kind filter + report path_prefix + post-filter roots.

    Returns ``(search_kind, path_prefix, prefer_report_roots)``.
    """
    if not _is_report_kind(kind):
        return kind, path_prefix, False
    prefix = (path_prefix or "").strip() or None
    if not prefix:
        prefix = _DEFAULT_REPORT_PATH_PREFIX
    return None, prefix, True


def _prefer_report_roots(hits: list[dict], *, prefer: bool) -> list[dict]:
    """When caller asked for kind=Report, keep promoted/synthetic Report roots only."""
    if not prefer or not hits:
        return hits
    roots = [h for h in hits if str(h.get("kind") or "") == "Report"]
    return roots if roots else hits


def _bsl_enabled(entity: dict) -> bool:
    """Методы участвуют в поиске/code-tools, если контекст включён (единый toggle строки)."""
    if entity.get("enabled") is not None:
        return bool(entity["enabled"])
    return bool(entity["bsl_enabled"]) if entity.get("bsl_enabled") is not None else True


def _profile_bsl_on(entity: dict) -> bool:
    """Whether this context was ingested with BSL modules (profile.bsl)."""
    from app.repositories.entities import ingest_profile_of

    if str(entity.get("source_mode") or "").lower() == "report":
        return False
    profile = ingest_profile_of(entity)
    return profile.get("bsl") is not False


def _validate_ready_entity(entity: dict, context: str) -> dict:
    if not entity["enabled"]:
        raise ValueError(f"Context '{context}' is disabled. Enable it in the UI.")
    if entity["status"] != "ready":
        raise ValueError(
            f"Context '{context}' status is '{entity['status']}', need 'ready'. Run reindex."
        )
    return dict(entity)


def _doc_hit(d, *, context_name: str) -> dict:
    return {
        "context": context_name,
        "path": d.fields.get("path"),
        "kind": d.fields.get("kind"),
        "belong": d.fields.get("belong"),
        "name": d.fields.get("name"),
        "synonym": d.fields.get("synonym"),
        "score": d.score,
    }


def resolve_context(
    context: str,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Load a single ready+enabled entity by name. Rejects tag: refs (use for get_* tools)."""
    kind, value = parse_context_ref(context)
    if kind == "tag":
        raise ValueError(
            f"'{context}' is a tag group, not a single context. "
            "Use an entity name from list_contexts / list_context_groups "
            "or from a search hit's 'context' field. "
            "For group search pass tag:… to search_metadata / semantic_search."
        )
    return _resolve_context_by_name(value, conn)


def _resolve_context_by_name(
    name: str,
    conn: sqlite3.Connection | None = None,
) -> dict:
    now = time.monotonic()
    with _entity_cache_lock:
        hit = _entity_cache.get(name)
        if hit is not None and (now - hit[0]) < _ENTITY_TTL_SEC:
            return _validate_ready_entity(hit[1], name)

    own_conn = conn is None
    if own_conn:
        conn = connect(settings.db_path)
    assert conn is not None
    try:
        entity = ent_repo.get_entity_by_name(conn, name)
        resolved_name = name
        if not entity:
            cands = _suggest_context_candidates(name, conn)
            if len(cands) == 1:
                resolved_name = cands[0]
                entity = ent_repo.get_entity_by_name(conn, resolved_name)
            elif len(cands) > 1:
                raise ValueError(
                    f"Unknown context '{name}'. Candidates: {', '.join(cands)}. "
                    "Pass the exact name from list_contexts."
                )
            else:
                raise ValueError(
                    f"Unknown context '{name}'. Call list_contexts or list_context_groups "
                    f"(tag:Name for a group)."
                )
        if not entity:
            raise ValueError(
                f"Unknown context '{name}'. Call list_contexts or list_context_groups "
                f"(tag:Name for a group)."
            )
        validated = _validate_ready_entity(dict(entity), resolved_name)
        with _entity_cache_lock:
            _entity_cache[resolved_name] = (time.monotonic(), dict(validated))
            if resolved_name != name:
                _entity_cache[name] = (time.monotonic(), dict(validated))
        return validated
    finally:
        if own_conn:
            conn.close()


def resolve_context_group(
    context: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, str | None, list[dict]]:
    """Resolve name or tag:… to a list of ready entities.

    Returns (ref, tag_or_none, entities).
    """
    kind, value = parse_context_ref(context)
    own_conn = conn is None
    if own_conn:
        conn = connect(settings.db_path)
    assert conn is not None
    try:
        if kind == "name":
            ent = _resolve_context_by_name(value, conn)
            return context, None, [ent]
        rows = ent_repo.list_ready_entities_for_tag(conn, value)
        if not rows:
            # Distinguish unknown tag vs empty group
            from app.repositories import tags as tag_repo

            if not tag_repo.get_tag_by_name(conn, value):
                raise ValueError(
                    f"Unknown tag '{value}'. Call list_context_groups to see tags."
                )
            raise ValueError(
                f"Tag '{value}' has no enabled ready contexts. "
                "Enable/index members in the UI or pick another tag."
            )
        entities = [_validate_ready_entity(dict(r), r["name"]) for r in rows]
        return context, value, entities
    finally:
        if own_conn:
            conn.close()


def _merge_scored_hits(
    groups: list[list[dict]],
    *,
    limit: int,
    offset: int = 0,
    query: str = "",
    entities: list[dict] | None = None,
    tag: str | None = None,
) -> list[dict]:
    merged: list[dict] = []
    for g in groups:
        merged.extend(g)
    merged = _dedupe_hits_by_context_path(merged)
    if ranking_svc.looks_like_report_query(query):
        merged = ranking_svc.ensure_report_roots(merged)
        merged = _dedupe_hits_by_context_path(merged)
    if ranking_svc.looks_like_user_howto_query(query):
        merged = ranking_svc.ensure_help_owners(merged)
        merged = _dedupe_hits_by_context_path(merged)
    by_name = {e["name"]: e for e in (entities or []) if e.get("name")}
    merged = ranking_svc.rerank_hits(merged, query, by_name, tag=bool(tag))
    window = max(int(limit) + int(offset), int(limit))
    merged = ranking_svc.diversify_by_context(
        merged,
        limit=window,
        tag=bool(tag),
        n_contexts=len(entities or []),
    )
    return merged[offset : offset + limit]


def fts_search(
    context: str,
    query: str,
    *,
    kind: str | None = None,
    include_borrowed: bool = False,
    limit: int = 50,
    offset: int = 0,
    path_prefix: str | None = None,
    compact: bool = False,
    literal: bool = False,
) -> dict:
    timer = PhaseTimer()
    ref, tag, entities = resolve_context_group(context)
    timer.lap("resolve")
    prefix = (path_prefix or "").strip() or None
    if literal:
        return _literal_metadata_search(
            ref,
            tag,
            entities,
            query,
            kind=kind,
            include_borrowed=include_borrowed,
            limit=limit,
            offset=offset,
            path_prefix=prefix,
            compact=compact,
            timer=timer,
        )
    # kind=Report: do not filter FTS/SQL by kind (roots often absent); search tree + promote.
    search_kind, prefix, prefer_report_roots = _report_search_scope(kind, prefix)
    # Over-fetch when filtering by path prefix so pagination still works.
    fetch_n = limit + offset
    if prefix:
        fetch_n = max(fetch_n * 8, 80)
    if tag and len(entities) > 1:
        fetch_n = max(fetch_n * 4, 80)
    # Single context without filters: FTS candidates == limit can drop the
    # best metadata root (ranked below noisier hits at top-k), so over-fetch a
    # little. FTS query itself is cheap (~ms); the rerank pool stays tiny.
    fetch_n = max(fetch_n, limit * 4)
    topk = min(max(fetch_n, 1), settings.search_max_limit)
    per_groups: list[list[dict]] = []
    recall_budgets: list[_SqlRecallBudget] = []
    objects_sum = sum(int(e.get("object_count") or 0) for e in entities)
    conn = connect(settings.db_path)
    try:
        for entity in entities:
            with timer.span("open_coll"):
                coll = _open_collection(entity)
            with timer.span("fts"):
                docs = _query_fts(
                    coll,
                    query,
                    topk=topk,
                    filter_expr=_filter_expr(
                        kind=search_kind,
                        include_borrowed=include_borrowed,
                        exclude_methods=not _bsl_enabled(entity),
                    ),
                    output_fields=["path", "kind", "belong", "name", "synonym"],
                )
            hits = _dedupe_hits_by_path(
                [_doc_hit(d, context_name=entity["name"]) for d in docs]
            )
            if prefix:
                hits = [h for h in hits if _path_under(str(h.get("path") or ""), prefix)]
            # Outer SQL_RECALL_SPAN_BUDGET_MS ceiling over class-C
            # STEM_RECALL_BUDGET_MS inside _intent_stem_sql_hits / howto — not
            # a replacement for C; flags stay separate (sql_recall_budget_hit
            # vs stem_recall_budget_hit).
            recall_budget = _SqlRecallBudget()
            with timer.span("sql_recall"):
                if not recall_budget.exhausted():
                    hits = _merge_sql_hits(
                        hits,
                        _sql_token_hits(
                            conn,
                            entity,
                            query,
                            kind=search_kind,
                            include_borrowed=include_borrowed,
                            limit=limit + offset,
                            path_prefix=prefix,
                            fts_hit_count=len(hits),
                            pool_strong=_pool_strong_for_token_sql(query, hits),
                            budget=recall_budget,
                            needs_report_roots=(
                                (
                                    prefer_report_roots
                                    or ranking_svc.looks_like_report_query(query)
                                )
                                and not any(
                                    str(h.get("kind") or "") == "Report"
                                    and ranking_svc.is_root_object(h)
                                    for h in hits
                                )
                                and not _report_recall_blocked(query, hits)
                            ),
                        ),
                    )
                if not recall_budget.exhausted():
                    if prefer_report_roots or ranking_svc.looks_like_report_query(query):
                        hits = ranking_svc.ensure_report_roots(hits)
                    hits = _prefer_report_roots(hits, prefer=prefer_report_roots)
                if not recall_budget.exhausted():
                    hits = _apply_howto_help_recall(
                        conn,
                        entity,
                        query,
                        hits,
                        path_prefix=prefix,
                        limit=limit + offset,
                        stem_recall=False,
                        budget=recall_budget,
                    )
            per_groups.append(hits)
            recall_budgets.append(recall_budget)
    finally:
        conn.close()

    if len(entities) == 1:
        by_name = {entities[0]["name"]: entities[0]}
        with timer.span("rank"):
            flat = ranking_svc.rerank_hits(per_groups[0], query, by_name, tag=False)
            items = flat[offset : offset + limit]
            _enrich_comments(items, entities)
        out = {
            "context": ref,
            "contexts": [entities[0]["name"]],
            "tag": tag,
            "total_returned": len(items),
            "offset": offset,
            "limit": limit,
            "has_more": len(flat) > offset + limit,
            "results": items,
        }
        if prefix:
            out["path_prefix"] = prefix
    else:
        with timer.span("rank"):
            items = _merge_scored_hits(
                per_groups,
                limit=limit,
                offset=offset,
                query=query,
                entities=entities,
                tag=tag,
            )
            _enrich_comments(items, entities)
        has_more = any(len(g) >= topk for g in per_groups) or (
            sum(len(g) for g in per_groups) > offset + limit
        )
        out = {
            "context": ref,
            "contexts": [e["name"] for e in entities],
            "tag": tag,
            "total_returned": len(items),
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "results": items,
        }
        if prefix:
            out["path_prefix"] = prefix

    warn = None if tag else _note_fanout(query, [e["name"] for e in entities])
    if warn:
        out["fanout_warning"] = warn
    _attach_title_candidates(out, query, entities, path_prefix=prefix)
    _attach_skd_and_compare_hints(out, query, items)
    out = _compact_results(out, compact=compact)
    attach_timing(
        out,
        timer,
        scale={
            "objects": objects_sum,
            "contexts": len(entities),
            "limit": limit,
            "hits": len(items),
        },
    )
    for rb in recall_budgets:
        rb.attach_flags(out)
    out["hint"] = bottleneck_hint(str(out.get("bottleneck") or ""))
    if out.get("sql_recall_skipped") and not items:
        out["next_tool"] = out.get("next_tool") or "search_under"
        skip_note = (
            "sql_recall skipped unscoped mid-LIKE; "
            "narrow with path_prefix / search_under / semantic_search"
        )
        base = str(out.get("hint") or "").strip()
        out["hint"] = f"{base}; {skip_note}" if base else skip_note
    return out


def _literal_metadata_search(
    ref: str,
    tag: str | None,
    entities: list[dict],
    query: str,
    *,
    kind: str | None,
    include_borrowed: bool,
    limit: int,
    offset: int,
    path_prefix: str | None,
    compact: bool,
    timer: PhaseTimer,
) -> dict:
    """SQL substring path for exact identifiers / error fragments (no FTS)."""
    q = (query or "").strip()
    objects_sum = sum(int(e.get("object_count") or 0) for e in entities)
    raw_prefix = (path_prefix or "").strip() or None
    if len(q) < 2:
        out = {
            "context": ref,
            "contexts": [e["name"] for e in entities],
            "tag": tag,
            "match_mode": "literal",
            "total_returned": 0,
            "offset": offset,
            "limit": limit,
            "has_more": False,
            "results": [],
            "hint": "literal=true requires query length ≥ 2",
        }
        if raw_prefix:
            out["path_prefix"] = raw_prefix
        attach_timing(
            out,
            timer,
            scale={"objects": objects_sum, "contexts": len(entities), "limit": limit, "hits": 0},
        )
        return out

    # Guard on caller-supplied path_prefix only (before kind=Report injects
    # Отчеты.). Synthetic report default uses R2 stem lane below — not too_broad.
    if _is_collection_root_path(raw_prefix):
        out = {
            "context": ref,
            "contexts": [e["name"] for e in entities],
            "tag": tag,
            "match_mode": "literal",
            "path_prefix": raw_prefix,
            "path_prefix_too_broad": True,
            "total_returned": 0,
            "offset": offset,
            "limit": limit,
            "has_more": False,
            "results": [],
            "hint": (
                f"path_prefix={raw_prefix!r} is a metadata collection root — "
                "literal mid-string would scan the whole tree. "
                "Pass a concrete object path (e.g. Документы.ИмяДокумента), "
                "or search_metadata without literal / semantic_search first."
            ),
            "next_tool": "search_metadata",
        }
        attach_timing(
            out,
            timer,
            scale={"objects": objects_sum, "contexts": len(entities), "limit": limit, "hits": 0},
        )
        return out

    # kind=Report: widen like FTS (roots often lack kind=Report in dump).
    search_kind, prefix, prefer_report_roots = _report_search_scope(kind, path_prefix)
    # R2: synthetic Отчеты. inject → stem/collapse lane, not full mid search_literal.
    report_stem_lane = bool(prefer_report_roots and raw_prefix is None)
    fetch_n = max(limit + offset, limit)
    per_groups: list[list[dict]] = []
    conn = connect(settings.db_path)
    try:
        for entity in entities:
            with timer.span("literal"):
                if report_stem_lane:
                    stem_hits = obj_repo.search_by_stem_roots(
                        conn,
                        int(entity["id"]),
                        q,
                        path_prefix=(prefix or _DEFAULT_REPORT_PATH_PREFIX).rstrip("."),
                        include_borrowed=include_borrowed,
                        limit=fetch_n,
                    )
                    hits = [
                        {
                            "path": h.get("path"),
                            "kind": h.get("kind"),
                            "belong": h.get("belong"),
                            "name": h.get("name"),
                            "synonym": h.get("synonym") or "",
                            "comment": h.get("comment") or "",
                            "score": float(h.get("score") or 1.0),
                        }
                        for h in stem_hits
                    ]
                else:
                    hits = obj_repo.search_literal(
                        conn,
                        int(entity["id"]),
                        q,
                        kind=search_kind,
                        include_borrowed=include_borrowed,
                        path_prefix=prefix,
                        exclude_methods=not _bsl_enabled(entity),
                        limit=fetch_n,
                    )
            for h in hits:
                h["context"] = entity["name"]
            if prefer_report_roots or ranking_svc.looks_like_report_query(q):
                hits = ranking_svc.ensure_report_roots(hits)
                hits = _prefer_report_roots(hits, prefer=prefer_report_roots)
            per_groups.append(hits)
    finally:
        conn.close()

    if len(entities) == 1:
        by_name = {entities[0]["name"]: entities[0]}
        with timer.span("rank"):
            flat = ranking_svc.rerank_hits(per_groups[0], q, by_name, tag=False)
            items = flat[offset : offset + limit]
            _enrich_comments(items, entities)
        out = {
            "context": ref,
            "contexts": [entities[0]["name"]],
            "tag": tag,
            "match_mode": "literal",
            "total_returned": len(items),
            "offset": offset,
            "limit": limit,
            "has_more": len(flat) > offset + limit,
            "results": items,
        }
    else:
        with timer.span("rank"):
            items = _merge_scored_hits(
                per_groups,
                limit=limit,
                offset=offset,
                query=q,
                entities=entities,
                tag=tag,
            )
            _enrich_comments(items, entities)
        has_more = sum(len(g) for g in per_groups) > offset + limit
        out = {
            "context": ref,
            "contexts": [e["name"] for e in entities],
            "tag": tag,
            "match_mode": "literal",
            "total_returned": len(items),
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "results": items,
        }
    if prefix:
        out["path_prefix"] = prefix
    if report_stem_lane:
        out["literal_lane"] = "report_stem"
    warn = None if tag else _note_fanout(q, [e["name"] for e in entities])
    if warn:
        out["fanout_warning"] = warn
    _attach_title_candidates(out, q, entities, path_prefix=prefix)
    _attach_skd_and_compare_hints(out, q, items)
    out = _compact_results(out, compact=compact)
    attach_timing(
        out,
        timer,
        scale={
            "objects": objects_sum,
            "contexts": len(entities),
            "limit": limit,
            "hits": len(items),
        },
    )
    if not prefix:
        out["skipped_unscoped_mid_like"] = True
    if not items:
        if not prefix and any(c.isspace() for c in q):
            out["hint"] = (
                "unscoped literal skipped for a phrase (full-table LIKE). "
                "Use semantic_search, or retry literal=true with path_prefix / search_under"
            )
            out["next_tool"] = "semantic_search"
        elif not prefix:
            out["hint"] = (
                "unscoped literal is indexed name/path equality only. "
                "For a substring, pass path_prefix / search_under; "
                "or search_metadata without literal / semantic_search"
            )
            out["next_tool"] = "semantic_search"
        else:
            out["hint"] = (
                "literal miss; try search_metadata without literal, "
                "semantic_search, or a narrower path_prefix"
            )
            out["next_tool"] = "semantic_search"
    else:
        out["hint"] = bottleneck_hint(str(out.get("bottleneck") or "")) or (
            "literal substring match (name/path/synonym/comment under path_prefix)"
            if prefix
            else "literal indexed name/path equality (unscoped mid-string skipped)"
        )
    return out


def semantic_search(
    context: str,
    query: str,
    *,
    top_n: int = 20,
    path_prefix: str | None = None,
    compact: bool = False,
) -> dict:
    timer = PhaseTimer()
    ref, tag, entities = resolve_context_group(context)
    timer.lap("resolve")
    prefix = (path_prefix or "").strip() or None
    fetch_n = top_n * 8 if (prefix or (tag and len(entities) > 1)) else top_n
    # Single context: hybrid candidates == top_n can miss the best metadata
    # root in the FTS leg; over-fetch a little (vector+RRF cost stays small).
    # Domain-collapse: payroll/lease attribute clones fill a tiny top_n pool —
    # keep at least 80 hybrid candidates so counter-domain roots can win after
    # cluster penalties + leaf diversification.
    fetch_n = max(fetch_n, top_n * 4)
    fetch_n = max(fetch_n, 80)
    fetch_n = min(max(fetch_n, 1), settings.search_max_limit)
    client = EmbeddingClient()
    vec_by_model: dict[str, list[float]] = {}
    per_groups: list[list[dict]] = []
    recall_budgets: list[_SqlRecallBudget] = []
    fts_match = _fts_match_string(query)
    objects_sum = sum(int(e.get("object_count") or 0) for e in entities)
    conn = connect(settings.db_path)
    try:
        for entity in entities:
            model = entity["model"]
            if model not in vec_by_model:
                with timer.span("embed"):
                    vec_by_model[model] = client.embed([query], model, for_query=True)[0]
            vec = vec_by_model[model]
            with timer.span("open_coll"):
                coll = _open_collection(entity)
            vq = zvec.Query(field_name="embedding", vector=vec)
            fq = zvec.Query(field_name="text", fts=zvec.Fts(match_string=fts_match))
            try:
                with timer.span("zvec"):
                    docs = coll.query(
                        queries=[vq, fq],
                        topk=fetch_n,
                        reranker=zvec.RrfReRanker(rank_constant=60),
                        filter=_filter_expr(
                            kind=None,
                            include_borrowed=True,
                            exclude_methods=not _bsl_enabled(entity),
                        ),
                        output_fields=["path", "kind", "belong", "name", "synonym"],
                    )
            except Exception:
                if fts_match != query:
                    fq = zvec.Query(field_name="text", fts=zvec.Fts(match_string=query))
                    with timer.span("zvec"):
                        docs = coll.query(
                            queries=[vq, fq],
                            topk=fetch_n,
                            reranker=zvec.RrfReRanker(rank_constant=60),
                            filter=_filter_expr(
                                kind=None,
                                include_borrowed=True,
                                exclude_methods=not _bsl_enabled(entity),
                            ),
                            output_fields=["path", "kind", "belong", "name", "synonym"],
                        )
                else:
                    raise
            hits = _dedupe_hits_by_path(
                [_doc_hit(d, context_name=entity["name"]) for d in docs]
            )
            if prefix:
                hits = [h for h in hits if _path_under(str(h.get("path") or ""), prefix)]
            # Outer SQL_RECALL_SPAN_BUDGET_MS over class-C STEM_RECALL_BUDGET_MS
            # (nested, not a replacement). Separate budget-hit flags.
            recall_budget = _SqlRecallBudget()
            with timer.span("sql_recall"):
                # U41: skip unscoped token/report SQL when hybrid already has a
                # content-covered root pool (howto/join) — that leg is the tax.
                domains_pre = ranking_svc.query_domains(query)
                skip_report_token = (
                    ranking_svc.looks_like_budget_exec_intent(query)
                    or "goods_return" in domains_pre
                )
                pool_strong = _pool_strong_for_token_sql(query, hits)
                skip_token_sql = (
                    (
                        ranking_svc.looks_like_user_howto_query(query)
                        or ranking_svc.looks_like_query_join_intent(query)
                    )
                    and _root_content_hits(query, hits)
                    >= ranking_svc.MIN_STEM_ROOT_HITS
                ) or pool_strong
                if not skip_token_sql and not recall_budget.exhausted():
                    hits = _merge_sql_hits(
                        hits,
                        _sql_token_hits(
                            conn,
                            entity,
                            query,
                            kind=None,
                            include_borrowed=True,
                            limit=top_n,
                            path_prefix=prefix,
                            fts_hit_count=len(hits),
                            pool_strong=pool_strong,
                            budget=recall_budget,
                            needs_report_roots=(
                                ranking_svc.looks_like_report_query(query)
                                and not skip_report_token
                                and not any(
                                    str(h.get("kind") or "") == "Report"
                                    and ranking_svc.is_root_object(h)
                                    for h in hits
                                )
                                and not _report_recall_blocked(query, hits)
                            ),
                        ),
                    )
                if not recall_budget.exhausted():
                    if ranking_svc.looks_like_report_query(query):
                        hits = ranking_svc.ensure_report_roots(hits)
                    hits = _apply_howto_help_recall(
                        conn,
                        entity,
                        query,
                        hits,
                        path_prefix=prefix,
                        limit=top_n,
                        stem_recall=True,
                        budget=recall_budget,
                    )
            per_groups.append(hits)
            recall_budgets.append(recall_budget)
    finally:
        conn.close()

    by_name = {e["name"]: e for e in entities}
    with timer.span("rank"):
        if len(entities) == 1:
            ranked = ranking_svc.rerank_hits(per_groups[0], query, by_name, tag=False)
            results = ranking_svc.diversify_by_leaf_name(ranked, limit=top_n)
        else:
            results = _merge_scored_hits(
                per_groups,
                limit=top_n,
                offset=0,
                query=query,
                entities=entities,
                tag=tag,
            )
            results = ranking_svc.diversify_by_leaf_name(results, limit=top_n)

        _enrich_comments(results, entities)
    out = {
        "context": ref,
        "contexts": [e["name"] for e in entities],
        "tag": tag,
        "results": results,
    }
    if prefix:
        out["path_prefix"] = prefix
    warn = None if tag else _note_fanout(query, [e["name"] for e in entities])
    if warn:
        out["fanout_warning"] = warn
    _attach_title_candidates(out, query, entities, path_prefix=prefix)
    _attach_skd_and_compare_hints(out, query, results)
    out = _compact_results(out, compact=compact)
    snap = timer.snapshot(
        scale={
            "objects": objects_sum,
            "contexts": len(entities),
            "top_n": top_n,
            "fetch_n": fetch_n,
            "hits": len(results),
        },
    )
    snap["hint"] = bottleneck_hint(str(snap.get("bottleneck") or ""))
    out["timing_ms"] = snap["timing_ms"]
    out["bottleneck"] = snap.get("bottleneck") or ""
    out["scale"] = snap.get("scale") or {}
    out["hint"] = snap.get("hint") or ""
    for rb in recall_budgets:
        rb.attach_flags(out)
    if out.get("sql_recall_skipped") and not results:
        out["next_tool"] = out.get("next_tool") or "search_under"
        skip_note = (
            "sql_recall skipped unscoped mid-LIKE; "
            "narrow with path_prefix / search_under"
        )
        base = str(out.get("hint") or "").strip()
        out["hint"] = f"{base}; {skip_note}" if base else skip_note
    return out


def help_search(
    context: str,
    query: str,
    *,
    limit: int = 8,
    owner: str | None = None,
    compact: bool = False,
) -> dict:
    """Поиск по пользовательской справке конфигурации (kind=Help, U10).

    Гибрид vector+FTS, ограниченный kind=Help. Каждый хит — раздел справки
    (comment = текст раздела, props.owner — объект-владелец). ``owner`` —
    необязательный фильтр по объекту (``Документы.ТранспортнаяНакладная``).
    """
    ref, tag, entities = resolve_context_group(context)
    prefix = None
    if owner:
        prefix = f"Help.{owner.strip().rstrip('.')}"
    fetch_n = max(int(limit) * 8, 80)
    if prefix:
        fetch_n = max(fetch_n * 2, 80)
    fetch_n = min(max(fetch_n, 1), settings.search_max_limit)
    client = EmbeddingClient()
    vec_by_model: dict[str, list[float]] = {}
    per_groups: list[list[dict]] = []
    fts_match = _fts_match_string(query)
    conn = connect(settings.db_path)
    try:
        for entity in entities:
            model = entity["model"]
            if model not in vec_by_model:
                vec_by_model[model] = client.embed([query], model, for_query=True)[0]
            coll = _open_collection(entity)
            vq = zvec.Query(field_name="embedding", vector=vec_by_model[model])
            fq = zvec.Query(field_name="text", fts=zvec.Fts(match_string=fts_match))
            docs = coll.query(
                queries=[vq, fq],
                topk=fetch_n,
                reranker=zvec.RrfReRanker(rank_constant=60),
                filter=_filter_expr(kind=HELP_KIND, include_borrowed=True),
                output_fields=["path", "kind", "belong", "name", "synonym"],
            )
            hits = _dedupe_hits_by_path(
                [_doc_hit(d, context_name=entity["name"]) for d in docs]
            )
            if prefix:
                hits = [h for h in hits if _path_under(str(h.get("path") or ""), prefix)]
            if not hits:
                # Fallback: SQL LIKE по тексту справки (комментарий = текст раздела).
                hits = _merge_sql_hits(
                    hits,
                    _sql_token_hits(
                        conn,
                        entity,
                        query,
                        kind=HELP_KIND,
                        include_borrowed=True,
                        limit=max(int(limit), 20),
                        path_prefix=prefix,
                    ),
                )
            per_groups.append(hits)
    finally:
        conn.close()

    by_name = {e["name"]: e for e in entities}
    if len(entities) == 1:
        results = ranking_svc.rerank_hits(per_groups[0], query, by_name, tag=False)[:limit]
    else:
        results = _merge_scored_hits(
            per_groups,
            limit=limit,
            offset=0,
            query=query,
            entities=entities,
            tag=tag,
        )
    _enrich_comments(results, entities)
    out = {
        "context": ref,
        "contexts": [e["name"] for e in entities],
        "tag": tag,
        "results": results,
    }
    if prefix:
        out["owner"] = owner
    warn = None if tag else _note_fanout(query, [e["name"] for e in entities])
    if warn:
        out["fanout_warning"] = warn
    return _compact_results(out, compact=compact)


def search_under(
    context: str,
    parent_path: str,
    query: str,
    *,
    kind: str | None = None,
    include_borrowed: bool = False,
    limit: int = 50,
    compact: bool = True,
) -> dict:
    """FTS (+ SQL fallback) restricted to objects under parent_path."""
    return fts_search(
        context,
        query,
        kind=kind,
        include_borrowed=include_borrowed,
        limit=limit,
        offset=0,
        path_prefix=parent_path,
        compact=compact,
    )


def _parse_added_names(body: str) -> list[str]:
    return list(dict.fromkeys(_RE_ADD_STRING.findall(body or "")))


def get_required_fields(context: str, path: str) -> dict:
    """Platform FillChecking + code arrays МассивОбязательных* for an object."""
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        paths = refs.resolve_qualified_name(entity, path, conn)
        canonical = paths[0] if paths else path
        obj = obj_repo.get_object(conn, entity["id"], canonical)
        if not obj:
            raise ValueError(f"Object '{path}' not found in context '{context}'.")

        props = obj.get("props") or {}
        if not isinstance(props, dict):
            props = {}

        platform = obj_repo.list_fill_checking_under(
            conn, int(entity["id"]), canonical, limit=300
        )
        self_fc = props.get("FillChecking")
        self_entry = None
        if obj.get("kind") in {
            "Attribute",
            "TabularSectionAttribute",
            "Dimension",
            "Resource",
        }:
            self_entry = {
                "path": canonical,
                "kind": obj.get("kind"),
                "name": obj.get("name"),
                "synonym": obj.get("synonym"),
                "comment": obj.get("comment") or "",
                "fill_checking": self_fc or "DontCheck",
            }

        code_lists: list[dict] = []
        if _bsl_enabled(entity):
            found = method_repo.get_methods_named_under(
                conn,
                int(entity["id"]),
                canonical,
                list(_REQUIRED_METHOD_NAMES),
            )
            by_name: dict[str, dict] = {}
            for m in found:
                # Prefer ManagerModule if duplicates
                name = m.get("name") or ""
                prev = by_name.get(name)
                if prev is None or "ManagerModule" in (m.get("path") or ""):
                    by_name[name] = m
            for mname in _REQUIRED_METHOD_NAMES:
                m = by_name.get(mname)
                if not m:
                    continue
                names = _parse_added_names(m.get("body") or "")
                code_lists.append(
                    {
                        "method": mname,
                        "path": m.get("path"),
                        "fields": names,
                        "conditional": mname.startswith("МассивУсловно"),
                        "tabular": mname.endswith("ТЧ"),
                    }
                )

        unchecked: list[dict] = []
        operation_rules: list[dict] = []
        if _bsl_enabled(entity):
            # Prefilter by needle in props_json — do not load every method body
            # under a large document (ERP Приобретение* was ~55s full scan).
            scan_needles = [
                "МассивНепроверяемыхРеквизитов",
                *_OP_FIELD_NEEDLES,
            ]
            for m in method_repo.iter_methods_under_props_matching(
                conn,
                int(entity["id"]),
                canonical,
                scan_needles,
                limit=80,
            ):
                body = m.get("body") or ""
                if not body:
                    continue
                fields = list(dict.fromkeys(_RE_UNCHECKED_ADD.findall(body)))
                if fields or "МассивНепроверяемыхРеквизитов" in body:
                    unchecked.append(
                        {
                            "method": m.get("name"),
                            "path": m.get("path"),
                            "fields": fields,
                        }
                    )
                for needle in _OP_FIELD_NEEDLES:
                    if needle not in body:
                        continue
                    added = _parse_added_names(body)
                    operation_rules.append(
                        {
                            "method": m.get("name"),
                            "path": m.get("path"),
                            "hook": needle,
                            "fields": added,
                        }
                    )
                    break
            # Prefer unique method paths
            seen_u: set[str] = set()
            uniq_u = []
            for u in unchecked:
                key = str(u.get("path") or "")
                if key in seen_u:
                    continue
                seen_u.add(key)
                uniq_u.append(u)
            unchecked = uniq_u
            seen_o: set[str] = set()
            uniq_o = []
            for u in operation_rules:
                key = str(u.get("path") or "")
                if key in seen_o:
                    continue
                seen_o.add(key)
                uniq_o.append(u)
            operation_rules = uniq_o

        out = {
            "context": entity["name"],
            "path": canonical,
            "kind": obj.get("kind"),
            "name": obj.get("name"),
            "synonym": obj.get("synonym"),
            "comment": obj.get("comment") or "",
            "self_fill_checking": self_fc,
            "self": self_entry,
            "platform_fill_checking": platform,
            "code_required_lists": code_lists,
            "code_unchecked_lists": unchecked,
            "operation_field_rules": operation_rules,
            "hint": (
                "platform_fill_checking = metadata FillChecking; "
                "code_required_lists = МассивОбязательных*; "
                "code_unchecked_lists = МассивНепроверяемыхРеквизитов.Добавить; "
                "operation_field_rules = реквизиты по хозяйственной операции "
                "(обязательность может сниматься в коде)."
            ),
        }
        return out
    finally:
        conn.close()


def _norm_detail_level(detail_level: str | None, *, include_props: bool = False) -> str:
    if include_props:
        return "L0"
    lv = (detail_level or "L2").strip().upper()
    if lv not in {"L0", "L2", "L3"}:
        return "L2"
    return lv


def _want_summary(summary: bool, expand: str | None) -> bool:
    if (expand or "").strip().lower() == "paths":
        return False
    return bool(summary)


_SKD_FIELDS_CAP = 40


def _compact_skd_fields(props: dict | None) -> list[dict]:
    """Keep data_path/title from props.skd.fields; skip empty rows."""
    if not isinstance(props, dict):
        return []
    skd = props.get("skd")
    if not isinstance(skd, dict):
        return []
    raw = skd.get("fields") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        data_path = str(item.get("data_path") or item.get("field") or "").strip()
        title = str(item.get("title") or "").strip()
        if not data_path and not title:
            continue
        rec: dict[str, str] = {}
        if data_path:
            rec["data_path"] = data_path
        if title:
            rec["title"] = title
        out.append(rec)
        if len(out) >= _SKD_FIELDS_CAP:
            break
    return out


def _report_root_card(conn, entity: dict, path: str) -> dict | None:
    """Synthetic Report card when objects has only DCS layouts under Отчеты.X."""
    raw = (path or "").strip()
    if not raw:
        return None
    # RU «Отчеты.X» is already canonical; normalize_path may return None
    # because kinds maps Report → «Отчёты» (ё) and omits the dump form.
    canonical = refs.normalize_path(raw) or raw
    root = ranking_svc.report_root_path(canonical)
    if root is None or root != canonical:
        return None
    layouts = obj_repo.search_under_prefix(
        conn,
        int(entity["id"]),
        f"{root}.Макеты",
        "",
        kind="DataCompositionSchema",
        include_borrowed=True,
        limit=50,
    )
    if not layouts:
        return None
    skd: list[dict] = []
    for row in layouts:
        mpath = str(row.get("path") or "")
        if not mpath:
            continue
        full = obj_repo.get_object(conn, int(entity["id"]), mpath)
        name = str(
            (full or {}).get("name")
            or row.get("name")
            or mpath.rsplit(".", 1)[-1]
        )
        entry: dict = {"path": mpath, "name": name}
        props = (full or {}).get("props") if full else None
        fields = _compact_skd_fields(props if isinstance(props, dict) else None)
        if fields:
            entry["fields"] = fields
        skd.append(entry)
    if not skd:
        return None
    first = skd[0]["path"]
    return {
        "context": entity["name"],
        "path": root,
        "name": root.rsplit(".", 1)[-1],
        "kind": "Report",
        "note": (
            "Синтетическая карточка: узел отчёта в objects отсутствует, "
            "доступны макеты СКД."
        ),
        "skd": skd,
        "hint": f"Для полной СКД вызовите get_skd(context, {first}).",
    }


def get_object(context: str, path: str, detail_level: str = "L2") -> dict:
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        obj = obj_repo.get_object(conn, entity["id"], path)
        if not obj:
            paths = refs.resolve_qualified_name(entity, path, conn)
            if paths:
                obj = obj_repo.get_object(conn, entity["id"], paths[0])
        if not obj:
            card = _report_root_card(conn, entity, path)
            if card is not None:
                card["detail_level"] = _norm_detail_level(detail_level)
                return card
            raise ValueError(f"Object '{path}' not found in context '{context}'.")
        if obj.get("kind") in {"Procedure", "Function"} and not _bsl_enabled(entity):
            raise ValueError(
                f"BSL methods are disabled for context '{context}'. Enable BSL in the UI."
            )
        lv = _norm_detail_level(detail_level)
        if lv == "L0":
            out = dict(obj)
            out["context"] = entity["name"]
            out["detail_level"] = "L0"
            return out
        card = payload_svc.object_card(obj)
        card["context"] = entity["name"]
        card["detail_level"] = lv
        if lv == "L3":
            return card
        children = obj_repo.list_structure_children(conn, int(entity["id"]), obj["path"])
        outgoing = obj_repo.get_links(conn, entity["id"], obj["path"], "out")["outgoing"]
        card["structure"] = payload_svc.compact_structure(obj["path"], children, outgoing)
        # U10: пользовательская справка по объекту (kind=Help, если есть).
        help_rows = obj_repo.get_help_for_owner(conn, int(entity["id"]), obj["path"])
        if help_rows:
            card["help"] = help_rows
        return card
    finally:
        conn.close()


def get_links(context: str, path: str, direction: str = "both") -> dict:
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        paths = refs.resolve_qualified_name(entity, path, conn)
        canonical = paths[0] if paths else path
        outgoing: list = []
        incoming: list = []
        if direction in ("out", "both", "outgoing"):
            outgoing = obj_repo.get_links(conn, entity["id"], canonical, "out")[
                "outgoing"
            ]
        if direction in ("in", "both", "incoming"):
            # Exact to_ref variants only (indexed) — never LIKE '%Name' on ERP.
            refs_set = refs.link_ref_candidates(canonical) | {canonical}
            if paths:
                for p in paths:
                    refs_set |= refs.link_ref_candidates(p) | {p}
            incoming = obj_repo.query_links_by_to_refs(
                conn, entity["id"], refs_set
            )
        return {
            "context": entity["name"],
            "path": canonical,
            "outgoing": outgoing,
            "incoming": incoming,
        }
    finally:
        conn.close()


_LINK_TYPE_ORDER: dict[str, int] = {
    "type": 0,
    "movements": 1,
    "based_on": 2,
    "composition": 3,
    "calls": 4,
}


def _query_usages(
    conn,
    entity: dict,
    canonical: str,
    link_type: str | None = None,
) -> list[dict]:
    """Входящие ссылки на объект по нормализованным to_ref."""
    refs_set = refs.link_ref_candidates(canonical)
    rows = obj_repo.query_links_by_to_refs(conn, entity["id"], refs_set, link_type)
    out = [
        {
            "from_path": r["from_path"],
            "link_type": r["link_type"],
            "depth": 0,
        }
        for r in rows
    ]
    out.sort(
        key=lambda i: (
            _LINK_TYPE_ORDER.get(i["link_type"], 99),
            i["from_path"],
        )
    )
    return out


def find_usages(
    context: str,
    path: str,
    link_type: str | None = None,
    *,
    summary: bool = True,
    expand: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Где объект используется: входящие ссылки (type/movements/based_on)."""
    timer = PhaseTimer()
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        paths = refs.resolve_qualified_name(entity, path, conn)
        timer.lap("resolve")
        if not paths:
            raise ValueError(f"Object '{path}' not found in context '{context}'.")
        canonical = paths[0]
        with timer.span("sql_links"):
            usages = _query_usages(conn, entity, canonical, link_type)
        by_type: dict[str, list[dict]] = {}
        for u in usages:
            by_type.setdefault(u["link_type"], []).append(u)
        want = _want_summary(summary, expand)
        out = {
            "context": entity["name"],
            "path": canonical,
            "link_type": link_type,
            "total": len(usages),
            "by_link_type": {k: len(v) for k, v in by_type.items()},
            "summary": want,
        }
        with timer.span("expand_paths"):
            if want:
                agg = payload_svc.aggregate_paths(
                    usages, path_key="from_path", limit=limit, offset=offset
                )
                out["usages"] = agg["items"]
                out["counts"] = agg["counts"]
                out["truncated"] = agg["truncated"]
                out["offset"] = agg["offset"]
                out["limit"] = agg["limit"]
            else:
                page = payload_svc.paginate(usages, limit=limit, offset=offset)
                out["usages"] = page["items"]
                out["truncated"] = page["truncated"]
                out["offset"] = page["offset"]
                out["limit"] = page["limit"]
                out["expand"] = "paths"
        out = attach_timing(
            out,
            timer,
            scale={
                "objects": int(entity.get("object_count") or 0),
                "links": int(entity.get("link_count") or 0),
                "total_usages": len(usages),
                "limit": limit,
                "expand": "paths" if not want else "summary",
            },
        )
        out["hint"] = bottleneck_hint(
            str(out.get("bottleneck") or ""),
            expand="paths" if not want else "",
        )
        return out
    finally:
        conn.close()


def get_object_dossier(
    context: str,
    path: str,
    include_props: bool = False,
    *,
    detail_level: str = "L2",
    summary: bool = True,
    expand: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Компактный «паспорт» объекта: карточка + attrs/ТЧ/movements + usages."""
    timer = PhaseTimer()
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        paths = refs.resolve_qualified_name(entity, path, conn)
        timer.lap("resolve")
        if not paths:
            card = _report_root_card(conn, entity, path)
            if card is not None:
                return card
            raise ValueError(f"Object '{path}' not found in context '{context}'.")
        canonical = paths[0]
        with timer.span("structure"):
            obj = obj_repo.get_object(conn, entity["id"], canonical)
            if not obj:
                card = _report_root_card(conn, entity, path)
                if card is not None:
                    return card
                raise ValueError(f"Object '{path}' not found in context '{context}'.")
            if obj.get("kind") in {"Procedure", "Function"} and not _bsl_enabled(entity):
                raise ValueError(
                    f"BSL methods are disabled for context '{context}'. Enable BSL in the UI."
                )
            lv = _norm_detail_level(detail_level, include_props=include_props)
            want = _want_summary(summary, expand) and lv != "L0"
            outgoing = obj_repo.get_links(conn, entity["id"], canonical, "out")["outgoing"]
            children = obj_repo.list_structure_children(conn, int(entity["id"]), canonical)
            compact = payload_svc.compact_structure(canonical, children, outgoing)
        with timer.span("sql_links"):
            usages = _query_usages(conn, entity, canonical)
        by_type: dict[str, list[dict]] = {}
        for u in usages:
            by_type.setdefault(u["link_type"], []).append(u)

        def _finish(payload: dict) -> dict:
            attach_timing(
                payload,
                timer,
                scale={
                    "objects": int(entity.get("object_count") or 0),
                    "links": int(entity.get("link_count") or 0),
                    "incoming_links": len(usages),
                    "detail_level": lv,
                    "limit": limit,
                },
            )
            payload["hint"] = bottleneck_hint(
                str(payload.get("bottleneck") or ""),
                expand="paths" if (lv == "L0" or not want) else "",
            )
            return payload

        if lv == "L3":
            return _finish(
                {
                    "context": entity["name"],
                    "path": canonical,
                    "detail_level": "L3",
                    "object": payload_svc.object_card(obj),
                }
            )

        counts = {
            "objects": int(entity.get("object_count") or 0),
            "links": int(entity.get("link_count") or 0),
            "outgoing_links": len(outgoing),
            "incoming_links": len(usages),
        }
        with timer.span("expand_paths"):
            if lv == "L0" or not want:
                obj_out = dict(obj)
                if lv != "L0":
                    obj_out.pop("props", None)
                page = payload_svc.paginate(usages, limit=limit, offset=offset)
                return _finish(
                    {
                        "context": entity["name"],
                        "path": canonical,
                        "detail_level": lv,
                        "summary": False,
                        "expand": "paths",
                        "object": obj_out,
                        "structure": payload_svc.links_by_type(outgoing),
                        "structure_compact": compact,
                        "usages": page["items"],
                        "usage_by_link_type": {k: len(v) for k, v in by_type.items()},
                        "counts": counts,
                        "truncated": page["truncated"],
                        "offset": page["offset"],
                        "limit": page["limit"],
                    }
                )

            agg = payload_svc.aggregate_paths(
                usages, path_key="from_path", limit=limit, offset=offset
            )
            counts.update(agg["counts"])
            return _finish(
                {
                    "context": entity["name"],
                    "path": canonical,
                    "detail_level": "L2",
                    "summary": True,
                    "object": payload_svc.object_card(obj),
                    "structure": compact,
                    "usages": agg["items"],
                    "usage_by_link_type": {k: len(v) for k, v in by_type.items()},
                    "counts": counts,
                    "truncated": agg["truncated"],
                    "offset": agg["offset"],
                    "limit": agg["limit"],
                }
            )
    finally:
        conn.close()


def trace_impact(
    context: str,
    path: str,
    direction: str = "down",
    depth: int = 3,
    limit: int = 100,
    *,
    summary: bool = True,
    expand: str | None = None,
    offset: int = 0,
) -> dict:
    """Рекурсивный анализ влияния по связям (BFS, без циклов).

    direction='down' — кто использует объект (входящие ссылки, to_ref → from_path);
    direction='up' — на что ссылается объект (исходящие ссылки).
    """
    from collections import deque

    timer = PhaseTimer()
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        paths = refs.resolve_qualified_name(entity, path, conn)
        timer.lap("resolve")
        if not paths:
            raise ValueError(f"Object '{path}' not found in context '{context}'.")
        start = paths[0]
        want = _want_summary(summary, expand)
        collect_cap = min(max(int(limit) * 8, int(limit)), 2000) if want else int(limit)

        results: list[dict] = []
        visited: set[str] = {start}
        queue: deque = deque([(start, 0)])
        truncated = False
        # Cap BFS work globally: popular catalogs fan out to hundreds of owners.
        max_queue_visits = min(max(int(limit) * 20, 200), 2000)
        visits = 0
        with timer.span("bfs"):
            while queue:
                cur, d = queue.popleft()
                visits += 1
                if visits > max_queue_visits:
                    truncated = True
                    break
                if d >= depth:
                    continue
                if direction in ("up", "out", "outgoing"):
                    rows = obj_repo.get_links(conn, entity["id"], cur, "out")["outgoing"]
                    for l in rows:
                        results.append(
                            {"path": l["to_ref"], "link_type": l["link_type"], "depth": d + 1}
                        )
                        if len(results) >= collect_cap:
                            truncated = True
                            break
                        for np in refs.resolve_qualified_name(entity, l["to_ref"], conn):
                            if np not in visited:
                                visited.add(np)
                                queue.append((np, d + 1))
                else:
                    rows = _query_usages(conn, entity, cur)
                    for l in rows:
                        from_path = l["from_path"]
                        results.append(
                            {
                                "path": from_path,
                                "link_type": l["link_type"],
                                "depth": d + 1,
                            }
                        )
                        if len(results) >= collect_cap:
                            truncated = True
                            break
                        # Follow owner metadata objects, not every attribute leaf
                        # (avoids N×_query_usages on Реквизиты.* paths → MCP timeout).
                        owner = payload_svc.owner_path(from_path)
                        nxt = owner or from_path
                        if nxt not in visited:
                            visited.add(nxt)
                            queue.append((nxt, d + 1))
                if len(results) >= collect_cap:
                    truncated = True
                    break

        # дедупликация по (path, link_type) с сохранением наименьшей глубины
        dedup: dict[tuple[str, str], dict] = {}
        for r in results:
            key = (r["path"], r["link_type"])
            if key not in dedup or r["depth"] < dedup[key]["depth"]:
                dedup[key] = r
        final = sorted(
            dedup.values(),
            key=lambda r: (r["depth"], r["link_type"], r["path"]),
        )
        if not want:
            final = final[:limit]
        link_types_seen = sorted({r["link_type"] for r in final})
        out = {
            "context": entity["name"],
            "path": start,
            "direction": direction,
            "depth": depth,
            "summary": want,
            "link_types_seen": link_types_seen,
        }
        with timer.span("expand_paths"):
            if want:
                agg = payload_svc.aggregate_paths(
                    final, path_key="path", limit=limit, offset=offset
                )
                out["impact"] = agg["items"]
                out["total"] = agg["total"]
                out["truncated"] = truncated or agg["truncated"]
                out["counts"] = agg["counts"]
                out["offset"] = agg["offset"]
                out["limit"] = agg["limit"]
            else:
                page = payload_svc.paginate(final, limit=limit, offset=offset)
                out["impact"] = page["items"]
                out["total"] = page["total"]
                out["truncated"] = truncated or page["truncated"]
                out["offset"] = page["offset"]
                out["limit"] = page["limit"]
                out["expand"] = "paths"
        attach_timing(
            out,
            timer,
            scale={
                "objects": int(entity.get("object_count") or 0),
                "links": int(entity.get("link_count") or 0),
                "depth": depth,
                "collected": len(final),
                "limit": limit,
            },
        )
        out["hint"] = bottleneck_hint(
            str(out.get("bottleneck") or ""),
            expand="paths" if not want else "",
        )
        return out
    finally:
        conn.close()


def _call_link_neighbors(
    conn, entity_id: int, path: str, *, outgoing: bool
) -> list[str]:
    """Exact ``calls`` edges only (no LIKE noise from get_links incoming)."""
    if outgoing:
        rows = conn.execute(
            """
            SELECT to_ref AS nb FROM links
            WHERE entity_id=? AND from_path=? AND link_type='calls'
            ORDER BY to_ref
            """,
            (entity_id, path),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT from_path AS nb FROM links
            WHERE entity_id=? AND to_ref=? AND link_type='calls'
            ORDER BY from_path
            """,
            (entity_id, path),
        ).fetchall()
    return [str(r["nb"]) for r in rows if r["nb"]]


def _count_call_links(conn, entity_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM links WHERE entity_id=? AND link_type='calls'",
        (entity_id,),
    ).fetchone()
    return int(row["n"] if row else 0)


_HINT_IMPACT = (
    "No BSL call edges for this path in the resolved calls graph. "
    "Do not retry trace_call_chain with another depth. "
    "Use find_usages (who references this object/method) or "
    "trace_impact (type / movements / based_on / composition)."
)
_HINT_SIGNATURES = (
    "No calls links in this context. Re-ingest with BSL mode "
    "«signatures» (with calls) or «full». "
    "Mode signatures_only skips the call graph. "
    "For metadata links use trace_impact / find_usages."
)
_HINT_NO_CALLS_GRAPH = (
    "No calls links resolved in this context (ambiguous/unresolved names "
    "are skipped at ingest). For metadata dependencies use trace_impact / find_usages."
)


def _apply_call_chain_hints(out: dict, *, mode: str, calls_n: int) -> None:
    """Option A: never auto-call impact; steer the agent with hint + next_tool."""
    mode_n = str(mode or "").strip().lower()
    if calls_n == 0 and mode_n in ("signatures_only", "signatures", "code", ""):
        out["hint"] = _HINT_SIGNATURES
        out["next_tool"] = "trace_impact"
        return
    if calls_n == 0:
        out["hint"] = _HINT_NO_CALLS_GRAPH
        out["next_tool"] = "trace_impact"
        return

    direction = out.get("direction")
    empty = False
    if direction == "both":
        empty = not (out.get("callees") or out.get("callers"))
    else:
        empty = not (out.get("chain") or [])

    if empty:
        out["hint"] = _HINT_IMPACT
        out["next_tool"] = "trace_impact"


def trace_call_chain(
    context: str,
    path: str,
    direction: str = "callees",
    depth: int = 3,
    limit: int = 100,
) -> dict:
    """BFS по рёбрам ``calls`` (экспериментально).

    direction:
    - ``callees`` — кого вызывает метод (исходящие calls);
    - ``callers`` — кто вызывает метод (входящие calls);
    - ``both`` — оба направления отдельными списками.
    """
    from collections import deque

    from app.services import runtime_settings

    if not runtime_settings.get_experimental_call_chain():
        return {
            "enabled": False,
            "error": (
                "experimental_call_chain is off. Enable via PATCH /api/settings "
                "{\"experimental_call_chain\": true}."
            ),
        }

    depth = max(1, min(int(depth), 5))
    limit = max(1, min(int(limit), 200))
    direction = (direction or "callees").strip().lower()
    if direction in ("down", "out", "outgoing"):
        direction = "callees"
    elif direction in ("up", "in", "incoming"):
        direction = "callers"
    if direction not in ("callees", "callers", "both"):
        raise ValueError("direction must be callees, callers, or both")

    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity, context)
        paths = refs.resolve_qualified_name(entity, path, conn)
        if not paths:
            raise ValueError(f"Object '{path}' not found in context '{context}'.")
        start = paths[0]
        mode = _effective_load_mode(entity)
        calls_n = _count_call_links(conn, int(entity["id"]))

        def _bfs(outgoing: bool) -> tuple[list[dict], bool]:
            results: list[dict] = []
            visited: set[str] = {start}
            queue: deque = deque([(start, 0)])
            truncated = False
            while queue:
                cur, d = queue.popleft()
                if d >= depth:
                    continue
                for nb in _call_link_neighbors(
                    conn, int(entity["id"]), cur, outgoing=outgoing
                ):
                    if nb in visited:
                        continue
                    visited.add(nb)
                    results.append({"path": nb, "depth": d + 1})
                    if len(results) >= limit:
                        truncated = True
                        return results, truncated
                    queue.append((nb, d + 1))
            return results, truncated

        out: dict = {
            "enabled": True,
            "context": entity["name"],
            "path": start,
            "direction": direction,
            "depth": depth,
            "limit": limit,
            "load_mode": mode,
            "calls_links": calls_n,
            "experimental": True,
        }

        if direction == "both":
            callees, t1 = _bfs(True)
            callers, t2 = _bfs(False)
            out["callees"] = callees
            out["callers"] = callers
            out["total_callees"] = len(callees)
            out["total_callers"] = len(callers)
            out["truncated"] = t1 or t2
        elif direction == "callees":
            chain, trunc = _bfs(True)
            out["chain"] = chain
            out["total"] = len(chain)
            out["truncated"] = trunc
        else:
            chain, trunc = _bfs(False)
            out["chain"] = chain
            out["total"] = len(chain)
            out["truncated"] = trunc
        _apply_call_chain_hints(out, mode=mode, calls_n=calls_n)
        return out
    finally:
        conn.close()


def _effective_load_mode(row: dict) -> str:
    """signatures_only|signatures|full; legacy code→full."""
    from app.schemas.entities import normalize_bsl_load_mode

    mode = str(row.get("bsl_load_mode") or "").strip()
    if mode:
        return normalize_bsl_load_mode(mode)
    if int(row.get("bsl_method_count") or 0) > 0:
        return "signatures"
    return ""


def _require_bsl(entity: dict, context: str) -> None:
    if not _bsl_enabled(entity):
        raise ValueError(
            f"BSL methods are disabled for context '{context}'. Enable BSL in the UI."
        )
    if not _profile_bsl_on(entity):
        raise ValueError(
            f"Context '{context}' was ingested without BSL (lean/metadata-only or report). "
            "Create a new entity with BSL enabled, or use metadata tools "
            "(search_metadata, get_object, find_usages, trace_impact)."
        )


_MAX_METHOD_BODY = 120_000
_DUMP_MISSING_HINT = (
    "Тела методов не в индексе (signatures). Каталог выгрузки недоступен — "
    "чтение .bsl с диска не выполнено. Проверьте dumps_dir/путь или загрузите с "
    "bsl_load_mode=code/full."
)


def _entity_dumps_dir(entity: dict) -> Path | None:
    raw = str(entity.get("dumps_dir") or "").strip()
    if not raw:
        raw = str(entity.get("source_path") or entity.get("file_path") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        return None
    try:
        from app.services.dump_zip import resolve_dump_root

        return resolve_dump_root(p)
    except Exception:
        return p


def _source_file_status(
    conn,
    entity: dict,
    source_rel: str,
) -> dict:
    """Compare dump file on disk to ``dump_file_state`` stamp from last parse.

    Returns keys: ``source_rel``, ``on_disk``, ``index_stale`` (file newer/changed
    vs stamp), ``stamp_mtime_ns``, ``disk_mtime_ns``. Missing stamp → not stale
    (cannot prove drift); missing file → on_disk=false.
    """
    from app.repositories import dump_files as dump_file_repo
    from app.services.bsl_parser import resolve_dump_file
    from app.services.dump_file_index import file_stamp
    from app.services.path_nfc import nfc_rel

    rel = nfc_rel((source_rel or "").strip())
    out: dict = {
        "source_rel": rel,
        "on_disk": False,
        "index_stale": False,
        "stamp_mtime_ns": 0,
        "disk_mtime_ns": 0,
    }
    if not rel:
        return out
    dumps_dir = _entity_dumps_dir(entity)
    if dumps_dir is None:
        return out
    path = resolve_dump_file(dumps_dir, rel)
    if path is None:
        return out
    cur = file_stamp(path)
    if cur is None:
        return out
    out["on_disk"] = True
    out["disk_mtime_ns"] = int(cur[0])
    stamps = dump_file_repo.load_stamps(conn, int(entity["id"]))
    stamp = stamps.get(rel)
    if stamp is None:
        # No parse snapshot for this rel — cannot claim stale.
        return out
    out["stamp_mtime_ns"] = int(stamp[0])
    if cur != stamp:
        out["index_stale"] = True
    return out


def _entity_index_stale_flag(conn, entity: dict) -> bool:
    """Cheap dump-vs-index drift signal via Configuration.xml / ConfigDumpInfo.xml."""
    from app.repositories import dump_files as dump_file_repo
    from app.services.dump_file_index import file_stamp, path_from_rel

    dumps_dir = _entity_dumps_dir(entity)
    if dumps_dir is None:
        return False
    stamps = dump_file_repo.load_stamps(conn, int(entity["id"]))
    if not stamps:
        return False
    for rel in ("Configuration.xml", "ConfigDumpInfo.xml"):
        stamp = stamps.get(rel)
        if stamp is None:
            continue
        cur = file_stamp(path_from_rel(dumps_dir, rel))
        if cur is not None and cur != stamp:
            return True
    return False


def _clip_method_body(body: str) -> tuple[str, bool]:
    if len(body) <= _MAX_METHOD_BODY:
        return body, False
    return body[:_MAX_METHOD_BODY] + "\n// … truncated", True


def _fill_method_body_from_dump(entity: dict, method: dict) -> str:
    dumps_dir = _entity_dumps_dir(entity)
    if dumps_dir is None:
        return ""
    rel = str(method.get("source_rel") or method.get("source_file") or "").strip()
    if not rel:
        return ""
    from app.services.bsl_parser import extract_method_body, read_module_text

    text = read_module_text(dumps_dir, rel)
    if not text:
        return ""
    line = method.get("line")
    try:
        line_n = int(line) if line is not None and str(line).strip() != "" else None
    except (TypeError, ValueError):
        line_n = None
    return extract_method_body(text, name=str(method.get("name") or ""), line=line_n)


def _snippet_around(text: str, idx: int, *, before: int = 40, after: int = 80) -> str:
    start = max(0, idx - before)
    end = min(len(text), idx + after)
    snippet = text[start:end].replace("\n", " ")
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def list_code_modules(context: str, *, kind: str | None = None, q: str | None = None) -> dict:
    """Модули с методами: {path, kind, name, methods_count, module_roles}.

    ``q`` is required (≥2 chars). Unfiltered dump of all modules is not supported.
    """
    timer = PhaseTimer()
    qn = (q or "").strip()
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity, context)
        timer.lap("resolve")
        if len(qn) < 2:
            out = {
                "context": entity["name"],
                "kind": kind,
                "query": q,
                "total": 0,
                "modules": [],
                "hint": (
                    "Pass q=… (at least 2 characters). "
                    "Unfiltered listing of all code modules is not supported. "
                    "If you already have an object/module path, use list_methods "
                    "with parent_path instead."
                ),
                "next_tool": "search_metadata",
            }
            attach_timing(out, timer, scale={"modules": 0})
            return out
        with timer.span("list"):
            items = method_repo.list_code_modules(
                conn, int(entity["id"]), kind=kind, q=qn
            )
        out = {
            "context": entity["name"],
            "kind": kind,
            "query": qn,
            "total": len(items),
            "modules": items,
        }
        attach_timing(
            out,
            timer,
            scale={
                "objects": int(entity.get("object_count") or 0),
                "methods": int(entity.get("bsl_method_count") or 0),
                "modules": len(items),
            },
        )
        return out
    finally:
        conn.close()


def list_methods(
    context: str,
    *,
    parent_path: str | None = None,
    q: str | None = None,
    export_only: bool = False,
    limit: int = 50,
) -> dict:
    """Методы контекста: {path, parent_path, name, kind, export, signature, doc_preview, module_role}."""
    limit = max(1, min(int(limit), 200))
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity, context)
        parent_f = (parent_path or "").strip().rstrip(".") or None
        if parent_f and _body_search_parent_too_broad(parent_f):
            return {
                "context": entity["name"],
                "parent_path": parent_f,
                "query": q,
                "export_only": export_only,
                "parent_too_broad": True,
                "total": 0,
                "methods": [],
                "hint": (
                    f"parent_path={parent_f!r} is a metadata collection root — "
                    "listing methods would scan thousands of modules. "
                    "Use list_code_modules(q=…) or search_metadata, then retry with "
                    "parent_path like ОбщиеМодули.ИмяМодуля."
                ),
                "next_tool": "list_code_modules",
            }
        items = method_repo.list_methods(
            conn,
            int(entity["id"]),
            parent_path=parent_path,
            q=q,
            export_only=export_only,
            limit=limit,
        )
        return {
            "context": entity["name"],
            "parent_path": parent_path,
            "query": q,
            "export_only": export_only,
            "total": len(items),
            "methods": items,
        }
    finally:
        conn.close()


def _fuzzy_methods(
    conn,
    entity: dict,
    query: str,
    *,
    parent_path: str | None,
    export_only: bool,
    limit: int,
    timer: PhaseTimer | None = None,
) -> list[dict]:
    """Семантический поиск методов (zvec + RRF), только Procedure/Function.

    When ``parent_path`` is set: push path prefix into the zvec filter and
    drop off-parent hits *before* any SQLite props load. Loading ``props_json``
    via unscoped ``path IN`` was ~40–90s on ERP (batch 2026-08-17).
    """
    t = timer or PhaseTimer()
    parent = (parent_path or "").strip().rstrip(".") or None
    with t.span("open_coll"):
        coll = _open_collection(entity)
    client = EmbeddingClient()
    with t.span("embed"):
        vec = client.embed([query], entity["model"], for_query=True)[0]
    vq = zvec.Query(field_name="embedding", vector=vec)
    fq = zvec.Query(field_name="text", fts=zvec.Fts(match_string=_fts_match_string(query)))
    # Methods-only filter + smaller topk: avoid scanning whole metadata collection.
    topk = max(limit * 2, 24) if parent else max(limit * 3, 40)
    with t.span("zvec"):
        filt = _filter_expr(
            kind=None,
            include_borrowed=True,
            methods_only=True,
            path_prefix=parent,
        )
        try:
            docs = coll.query(
                queries=[vq, fq],
                topk=topk,
                reranker=zvec.RrfReRanker(rank_constant=60),
                filter=filt,
                output_fields=["path", "kind", "belong", "name", "synonym"],
            )
        except Exception as exc:
            # Do not retry unscoped: parent filter fail + full methods hybrid
            # was ~6.6s on ERP (find_methods 2026-08-18). Unscoped still
            # raises; parent surfaces ZvecFilterFailed so the agent can tell
            # «no methods» from «fuzzy unavailable».
            if parent:
                log.warning(
                    "zvec methods filter failed parent=%s: %s; skip unscoped retry",
                    parent,
                    exc,
                )
                raise ZvecFilterFailed(parent, exc) from exc
            raise
    hits = _dedupe_hits_by_path([_doc_hit(d, context_name=entity["name"]) for d in docs])
    method_hits = [h for h in hits if h.get("kind") in {"Procedure", "Function"}]
    if parent:
        method_hits = [
            h
            for h in method_hits
            if _method_hit_under_parent(str(h.get("path") or ""), parent)
        ]
    if not method_hits:
        return []
    with t.span("rank"):
        # export_only needs props; parent filter is already path-based (no SQL).
        need_sql = export_only
        methods: dict[str, dict] = {}
        if need_sql:
            methods = method_repo.get_methods_by_paths(
                conn,
                int(entity["id"]),
                [str(h["path"]) for h in method_hits if h.get("path")],
            )
        out: list[dict] = []
        for h in method_hits:
            path = str(h.get("path") or "")
            if need_sql:
                m = methods.get(path)
                if not m:
                    continue
                if export_only and not m["export"]:
                    continue
                out.append(_method_to_item(m))
            else:
                name = str(h.get("name") or "")
                hit_parent = _method_parent_from_path(path) if path else ""
                out.append(
                    {
                        "path": path,
                        "parent_path": hit_parent,
                        "name": name,
                        "kind": h.get("kind") or "",
                        "export": False,
                        "signature": str(h.get("synonym") or ""),
                        "doc_preview": "",
                        "module_role": "",
                    }
                )
            if len(out) >= limit:
                break
    return out


def _method_to_item(m: dict) -> dict:
    doc = (m.get("doc") or "").replace("\n", " ").strip()
    if len(doc) > 200:
        doc = doc[:197] + "..."
    return {
        "path": m["path"],
        "parent_path": m["parent_path"],
        "name": m["name"],
        "kind": m["kind"],
        "export": bool(m["export"]),
        "signature": m["signature"],
        "doc_preview": doc,
        "module_role": m["module_role"],
    }


def find_methods(
    context: str,
    query: str,
    *,
    parent_path: str | None = None,
    export_only: bool = False,
    limit: int = 50,
    literal: bool = False,
) -> dict:
    """Методы: exact (SQL по имени/пути/сигнатуре) → fuzzy только при необходимости.

    ``literal=true``: mid-string LIKE on name/path/signature, no fuzzy.
    Fuzzy (zvec) запускается если exact пуст, или exact < 3 и запрос — фраза
    (не одиночный идентификатор). Идентификатор с ≥1 exact hit → без semantic.
    """
    timer = PhaseTimer()
    limit = max(1, min(int(limit), 200))
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity, context)
        timer.lap("resolve")
        parent_f = (parent_path or "").strip().rstrip(".") or None

        if _body_search_parent_too_broad(parent_f):
            out = {
                "context": entity["name"],
                "query": query,
                "parent_path": parent_f,
                "parent_too_broad": True,
                "match_mode": "literal" if literal else "exact",
                "total": 0,
                "methods": [],
                "hint": (
                    f"parent_path={parent_f!r} is a metadata collection root — "
                    "method search would scan thousands of modules. "
                    "Locate a concrete module first (search_metadata / list_code_modules "
                    "with q), then retry with parent_path like ОбщиеМодули.ИмяМодуля."
                ),
                "next_tool": "search_metadata",
            }
            attach_timing(
                out,
                timer,
                scale={
                    "objects": int(entity.get("object_count") or 0),
                    "methods": int(entity.get("bsl_method_count") or 0),
                    "limit": limit,
                    "exact_hits": 0,
                    "match_mode": out["match_mode"],
                },
            )
            return out

        if parent_f and not obj_repo.path_or_children_exist(
            conn, int(entity["id"]), parent_f
        ):
            out = {
                "context": entity["name"],
                "query": query,
                "parent_path": parent_f,
                "parent_not_found": True,
                "match_mode": "literal" if literal else "exact",
                "total": 0,
                "methods": [],
            }
            attach_timing(
                out,
                timer,
                scale={
                    "objects": int(entity.get("object_count") or 0),
                    "methods": int(entity.get("bsl_method_count") or 0),
                    "limit": limit,
                    "exact_hits": 0,
                    "match_mode": out["match_mode"],
                },
            )
            out["hint"] = (
                "parent_path does not match any object in this context "
                "(not an empty method list — do not guess the metadata name)."
            )
            out["next_tool"] = "search_metadata"
            return out

        if literal:
            skipped_unscoped = False
            if not parent_f:
                skipped_unscoped = True
                if _looks_like_method_identifier(query):
                    with timer.span("exact"):
                        rows = method_repo.list_methods(
                            conn,
                            int(entity["id"]),
                            parent_path=parent_path,
                            q=query,
                            export_only=export_only,
                            limit=limit,
                        )
                    match_mode = "exact"
                else:
                    with timer.span("literal"):
                        rows = []
                    match_mode = "literal"
            else:
                with timer.span("literal"):
                    rows = method_repo.list_methods_literal(
                        conn,
                        int(entity["id"]),
                        query,
                        parent_path=parent_path,
                        export_only=export_only,
                        limit=limit,
                    )
                match_mode = "literal"
            out = {
                "context": entity["name"],
                "query": query,
                "match_mode": match_mode,
                "total": len(rows),
                "methods": rows,
            }
            if parent_f:
                out["parent_path"] = parent_f
            if skipped_unscoped:
                out["skipped_unscoped_mid_like"] = True
            attach_timing(
                out,
                timer,
                scale={
                    "objects": int(entity.get("object_count") or 0),
                    "methods": int(entity.get("bsl_method_count") or 0),
                    "limit": limit,
                    "exact_hits": len(rows),
                    "match_mode": match_mode,
                },
            )
            if skipped_unscoped and not rows:
                out["hint"] = (
                    "unscoped literal mid-string skipped on this dump. "
                    "Pass parent_path for substring, omit literal for exact/prefix/fuzzy, "
                    "or find_code_references after locating a module"
                )
                out["next_tool"] = "find_methods"
            elif skipped_unscoped:
                out["hint"] = (
                    "unscoped literal used indexed exact/prefix only; "
                    "pass parent_path for mid-string name/path/signature"
                )
            elif not rows:
                out["hint"] = (
                    "literal miss on name/path/signature; "
                    "try find_methods without literal, or find_code_references "
                    "for body/error text"
                )
                out["next_tool"] = "find_code_references"
            else:
                out["hint"] = bottleneck_hint(str(out.get("bottleneck") or ""))
            return out

        with timer.span("exact"):
            exact = method_repo.list_methods(
                conn,
                int(entity["id"]),
                parent_path=parent_path,
                q=query,
                export_only=export_only,
                limit=limit,
            )
        scale = {
            "objects": int(entity.get("object_count") or 0),
            "methods": int(entity.get("bsl_method_count") or 0),
            "limit": limit,
            "exact_hits": len(exact),
        }

        def _done(payload: dict, *, match_mode: str) -> dict:
            attach_timing(payload, timer, scale={**scale, "match_mode": match_mode})
            hint = bottleneck_hint(
                str(payload.get("bottleneck") or ""),
                match_mode=match_mode,
            )
            err = str(payload.get("fuzzy_error") or "").strip()
            if err:
                hint = f"{hint}; {err}" if hint else err
            payload["hint"] = hint
            return payload

        if not _should_fuzzy_methods(query, len(exact)):
            return _done(
                {
                    "context": entity["name"],
                    "query": query,
                    "match_mode": "exact",
                    "total": len(exact),
                    "methods": exact,
                },
                match_mode="exact",
            )
        try:
            fuzzy = _fuzzy_methods(
                conn,
                entity,
                query,
                parent_path=parent_path,
                export_only=export_only,
                limit=limit,
                timer=timer,
            )
        except ZvecFilterFailed as exc:
            return _done(
                {
                    "context": entity["name"],
                    "query": query,
                    "match_mode": "exact",
                    "total": len(exact),
                    "methods": exact,
                    "fuzzy_error": str(exc),
                },
                match_mode="exact",
            )
        except (FileNotFoundError, OSError) as exc:
            return _done(
                {
                    "context": entity["name"],
                    "query": query,
                    "match_mode": "exact",
                    "total": len(exact),
                    "methods": exact,
                    "fuzzy_error": f"semantic search unavailable: {exc}",
                },
                match_mode="exact",
            )
        except Exception as exc:
            from app.services.embeddings import LmStudioUnavailable

            if not isinstance(exc, LmStudioUnavailable):
                raise
            return _done(
                {
                    "context": entity["name"],
                    "query": query,
                    "match_mode": "exact",
                    "total": len(exact),
                    "methods": exact,
                    "fuzzy_error": f"semantic search unavailable: {exc}",
                },
                match_mode="exact",
            )
        if fuzzy:
            return _done(
                {
                    "context": entity["name"],
                    "query": query,
                    "match_mode": "fuzzy",
                    "total": len(fuzzy),
                    "methods": fuzzy,
                },
                match_mode="fuzzy",
            )
        return _done(
            {
                "context": entity["name"],
                "query": query,
                "match_mode": "exact",
                "total": len(exact),
                "methods": exact,
            },
            match_mode="exact",
        )
    finally:
        conn.close()


def get_method(context: str, path: str) -> dict:
    """Метод с телом; body отдаётся только при load_mode code/full или с диска.

    If the dump file is newer than the last-index stamp, body is always read
    from disk and ``index_stale=true`` is set (search index may still miss edits).
    """
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity, context)
        m = method_repo.get_method(conn, int(entity["id"]), path)
        if not m:
            paths = refs.resolve_qualified_name(entity, path, conn)
            if paths:
                m = method_repo.get_method(conn, int(entity["id"]), paths[0])
        if not m:
            raise ValueError(f"Method '{path}' not found in context '{context}'.")
        body = str(m.get("body") or "")
        from app.schemas.entities import bsl_stores_body

        rel = str(m.get("source_rel") or m.get("source_file") or "").strip()
        freshness = _source_file_status(conn, entity, rel)
        index_stale = bool(freshness.get("index_stale"))
        from_index = bsl_stores_body(m.get("load_mode")) and bool(body) and not index_stale
        truncated = False
        if from_index:
            body, truncated = _clip_method_body(body)
            m["body"] = body
            m["body_available"] = True
            m["body_source"] = "index"
        else:
            filled = _fill_method_body_from_dump(entity, m)
            if filled:
                body, truncated = _clip_method_body(filled)
                m["body"] = body
                m["body_available"] = True
                m["body_source"] = "file"
            else:
                m["body"] = ""
                m["body_available"] = False
                m["body_source"] = ""
                if not bsl_stores_body(m.get("load_mode")):
                    m["body_hint"] = (
                        "Тело не в индексе (signatures*) и файл выгрузки не прочитан "
                        "(нет dumps_dir или source_rel)."
                    )
                elif index_stale:
                    m["body_hint"] = (
                        "index_stale but dump file body could not be read; "
                        "refresh/reindex the entity."
                    )
        if truncated:
            m["body_truncated"] = True
        m["index_stale"] = index_stale
        if freshness.get("on_disk"):
            m["source_on_disk"] = True
        if index_stale:
            m["hint"] = (
                "Dump file newer than last index (index_stale). Body from disk; "
                "search/find_* may miss local edits until refresh."
            )
        m["context"] = entity["name"]
        return m
    finally:
        conn.close()


def get_module_structure(context: str, module_path: str) -> dict:
    """Структура модуля: методы (line/region), регионы, экспорты."""
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity, context)
        resolved = refs.resolve_qualified_name(entity, module_path, conn)
        canonical = resolved[0] if resolved else module_path
        structure = method_repo.get_module_structure(conn, int(entity["id"]), canonical)
        if not structure:
            raise ValueError(
                f"Module '{module_path}' not found in context '{context}' "
                "(no methods for this parent_path)."
            )
        structure["context"] = entity["name"]
        return structure
    finally:
        conn.close()


def find_code_references(
    context: str,
    name: str,
    *,
    limit: int = 100,
    offset: int = 0,
    parent_path: str | None = None,
    literal: bool = False,
) -> dict:
    """Где имя упоминается в телах BSL-методов (индекс code/full или файлы dump).

    ``literal=true``: always prefer dump files when available (freshest source for
    identifiers / error text after local edits). Default keeps index bodies when
    load_mode stores them.

    Unscoped search on a large dump is refused — pass ``parent_path``.
    """
    timer = PhaseTimer()
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    parent_filter = (parent_path or "").strip() or None
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity, context)
        timer.lap("resolve")
        mode = _effective_load_mode(entity)
        needle = (name or "").strip().lower()
        if not needle:
            raise ValueError("name must not be empty")
        from app.schemas.entities import bsl_stores_body

        if _unscoped_body_search_blocked(entity, parent_filter):
            out = {
                "context": entity["name"],
                "name": name,
                "load_mode": mode,
                "source": "",
                "match_mode": "literal" if literal else "index",
                "parent_path": parent_filter,
                "parent_required": True,
                "total": 0,
                "offset": offset,
                "limit": limit,
                "truncated": False,
                "results": [],
                "hint": (
                    "Body search without parent_path scans the whole dump. "
                    "Locate the module/object first (find_methods / search_metadata), "
                    "then retry with parent_path like ОбщиеМодули.ИмяМодуля "
                    "(not the collection root ОбщиеМодули)."
                ),
                "next_tool": "find_methods",
            }
            attach_timing(
                out,
                timer,
                scale={
                    "objects": int(entity.get("object_count") or 0),
                    "methods": int(entity.get("bsl_method_count") or 0),
                    "limit": limit,
                },
            )
            return out

        if _body_search_parent_too_broad(parent_filter):
            out = {
                "context": entity["name"],
                "name": name,
                "load_mode": mode,
                "source": "",
                "match_mode": "literal" if literal else "index",
                "parent_path": parent_filter,
                "parent_too_broad": True,
                "total": 0,
                "offset": offset,
                "limit": limit,
                "truncated": False,
                "results": [],
                "hint": (
                    f"parent_path={parent_filter!r} is a metadata collection root — "
                    "body search would scan thousands of modules. "
                    "Pass a concrete object/module from a hit "
                    "(e.g. ОбщиеМодули.ОбменСГИСЭПД or Документы.ЭлектроннаяТранспортнаяНакладная)."
                ),
                "next_tool": "find_methods",
            }
            attach_timing(
                out,
                timer,
                scale={
                    "objects": int(entity.get("object_count") or 0),
                    "methods": int(entity.get("bsl_method_count") or 0),
                    "limit": limit,
                },
            )
            return out

        dumps_dir = _entity_dumps_dir(entity)
        prefer_disk = bool(literal) and dumps_dir is not None
        if prefer_disk or not bsl_stores_body(mode):
            out = _find_code_references_in_dump(
                conn,
                entity,
                name,
                needle,
                limit,
                offset=offset,
                parent_path=parent_filter,
                timer=timer,
            )
            if literal:
                out["match_mode"] = "literal"
                if dumps_dir is None:
                    out["hint"] = (
                        "literal requested but dumps_dir unavailable; "
                        "falling back to empty/file miss — refresh path source"
                    )
            return out

        with timer.span("sql_catalog"):
            rows = _method_body_catalog_rows(
                conn, entity, parent_filter, include_body=True
            )
        results: list[dict] = []
        with timer.span("scan"):
            for r in rows:
                pp = str(r["parent_path"] or "")
                body = str(r["body"] or "")
                if not body:
                    continue
                idx = body.lower().find(needle)
                if idx < 0:
                    continue
                line = body.count("\n", 0, idx) + 1
                results.append(
                    {
                        "path": r["path"],
                        "parent_path": pp,
                        "name": r["name"] or "",
                        "line": line,
                        "snippet": _snippet_around(body, idx),
                    }
                )
        total = len(results)
        page = results[offset : offset + limit]
        out = {
            "context": entity["name"],
            "name": name,
            "load_mode": mode,
            "source": "index",
            "match_mode": "literal" if literal else "index",
            "parent_path": parent_filter,
            "total": total,
            "offset": offset,
            "limit": limit,
            "truncated": offset + len(page) < total,
            "results": page,
        }
        attach_timing(
            out,
            timer,
            scale={
                "objects": int(entity.get("object_count") or 0),
                "methods": int(entity.get("bsl_method_count") or 0),
                "catalog": len(rows),
                "limit": limit,
            },
        )
        out["hint"] = bottleneck_hint(str(out.get("bottleneck") or ""))
        return out
    finally:
        conn.close()


def _method_body_catalog_rows(
    conn,
    entity: dict,
    parent_path: str | None,
    *,
    include_body: bool,
):
    """Method catalog for body search: path range, json_extract — not full props_json."""
    parent = (parent_path or "").strip().rstrip(".")
    where = ["entity_id=?", "kind IN ('Procedure','Function')"]
    params: list = [int(entity["id"])]
    indexed = ""
    if parent:
        where.append("path BETWEEN ? AND ?")
        params.extend(obj_repo.path_range_params(parent + "."))
        indexed = " INDEXED BY idx_objects_path"
    body_col = ", json_extract(props_json, '$.body') AS body" if include_body else ""
    return list(
        conn.execute(
            f"""
            SELECT path, name, source_rel,
                   json_extract(props_json, '$.parent_path') AS parent_path,
                   json_extract(props_json, '$.source_file') AS source_file,
                   json_extract(props_json, '$.line') AS line
                   {body_col}
            FROM objects{indexed}
            WHERE {" AND ".join(where)}
            """,
            params,
        ).fetchall()
    )


def _find_code_references_in_dump(
    conn,
    entity: dict,
    name: str,
    needle: str,
    limit: int,
    *,
    offset: int = 0,
    parent_path: str | None = None,
    timer: PhaseTimer | None = None,
) -> dict:
    """Поиск подстроки в .bsl выгрузки, маппинг строки → метод из индекса."""
    t = timer or PhaseTimer()
    dumps_dir = _entity_dumps_dir(entity)
    base = {
        "context": entity["name"],
        "name": name,
        "load_mode": "signatures",
        "source": "file",
        "parent_path": parent_path,
        "offset": offset,
        "limit": limit,
    }
    if dumps_dir is None:
        out = {
            **base,
            "load_mode_hint": _DUMP_MISSING_HINT,
            "total": 0,
            "truncated": False,
            "results": [],
        }
        attach_timing(out, t, scale={"methods": int(entity.get("bsl_method_count") or 0)})
        return out

    with t.span("sql_catalog"):
        rows = _method_body_catalog_rows(
            conn, entity, parent_path, include_body=False
        )

    by_file: dict[str, list[tuple[int, str, str, str]]] = {}
    for r in rows:
        pp = str(r["parent_path"] or "")
        rel = str(r["source_rel"] or r["source_file"] or "").strip()
        if not rel:
            continue
        try:
            decl = int(r["line"] or 0)
        except (TypeError, ValueError):
            decl = 0
        by_file.setdefault(rel, []).append(
            (decl, r["path"] or "", r["name"] or "", pp)
        )
    if not by_file:
        out = {
            **base,
            "load_mode_hint": _DUMP_MISSING_HINT,
            "total": 0,
            "truncated": False,
            "results": [],
        }
        attach_timing(
            out,
            t,
            scale={
                "methods": int(entity.get("bsl_method_count") or 0),
                "catalog": len(rows),
            },
        )
        return out

    from app.services.bsl_parser import read_module_text

    results: list[dict] = []
    with t.span("disk"):
        for rel in sorted(by_file):
            entries = sorted(by_file[rel], key=lambda x: x[0])
            decl_lines = {e[0] for e in entries if e[0]}
            text = read_module_text(dumps_dir, rel)
            if not text:
                continue
            lower = text.lower()
            start = 0
            while True:
                idx = lower.find(needle, start)
                if idx < 0:
                    break
                hit_line = lower.count("\n", 0, idx) + 1
                start = idx + max(len(needle), 1)
                if hit_line in decl_lines:
                    continue
                chosen = None
                for decl, path, mname, parent in entries:
                    if decl and decl < hit_line:
                        chosen = (path, mname, parent)
                    elif decl and decl >= hit_line:
                        break
                if not chosen:
                    continue
                path, mname, parent = chosen
                results.append(
                    {
                        "path": path,
                        "parent_path": parent,
                        "name": mname,
                        "line": hit_line,
                        "snippet": _snippet_around(text, idx),
                    }
                )
    total = len(results)
    page = results[offset : offset + limit]
    out = {
        **base,
        "total": total,
        "truncated": offset + len(page) < total,
        "results": page,
    }
    attach_timing(
        out,
        t,
        scale={
            "methods": int(entity.get("bsl_method_count") or 0),
            "catalog": len(rows),
            "files": len(by_file),
            "limit": limit,
        },
    )
    out["hint"] = bottleneck_hint(str(out.get("bottleneck") or ""))
    return out


def find_by_guid(context: str, guid: str, *, limit: int = 20) -> dict:
    """Resolve metadata UUID → path/kind (index column and/or ConfigDumpInfo).

    Dump mode. Index ``guid`` is filled on reparse after 0.2.17; until then
    ConfigDumpInfo.xml on disk is used when dumps_dir is resolvable.
    """
    from app.services.guid_lookup import find_in_config_dump_info, normalize_guid

    limit = max(1, min(int(limit), 50))
    base, full = normalize_guid(guid)
    if not base:
        raise ValueError("guid must be a UUID (optional braces / .N module suffix)")
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        if (entity.get("source_mode") or "") != "dump":
            return {
                "context": entity["name"],
                "guid": full or base,
                "total": 0,
                "results": [],
                "hint": "find_by_guid requires source_mode=dump (Hierarchical XML)",
            }
        indexed = obj_repo.find_by_guid(conn, int(entity["id"]), guid, limit=limit)
        results: list[dict] = list(indexed)
        seen_paths = {str(r.get("path") or "") for r in results if r.get("path")}

        dumps_dir = _entity_dumps_dir(entity)
        dump_hits: list[dict] = []
        if dumps_dir is not None and len(results) < limit:
            for h in find_in_config_dump_info(dumps_dir, guid, limit=limit):
                path = str(h.get("path") or "")
                is_module = bool(h.get("module_role"))
                if path and path in seen_paths and not is_module:
                    for r in results:
                        if r.get("path") == path:
                            r.setdefault("dump_name", h.get("dump_name") or "")
                    continue
                item = {
                    "path": path,
                    "kind": "",
                    "kind_ru": "",
                    "name": path.rsplit(".", 1)[-1] if path else "",
                    "synonym": "",
                    "source_rel": "",
                    "guid": h.get("guid") or full or base,
                    "dump_name": h.get("dump_name") or "",
                    "module_role": h.get("module_role") or "",
                    "source": "ConfigDumpInfo.xml",
                }
                if is_module:
                    item["kind"] = "Module"
                    item["kind_hint"] = h.get("kind_hint") or "Module"
                    item["name"] = str(h.get("module_role") or item["name"])
                elif path:
                    obj = obj_repo.get_object(conn, int(entity["id"]), path)
                    if obj:
                        item["kind"] = obj.get("kind") or ""
                        item["kind_ru"] = obj.get("kind_ru") or ""
                        item["name"] = obj.get("name") or item["name"]
                        item["synonym"] = obj.get("synonym") or ""
                        item["source_rel"] = obj.get("source_rel") or ""
                        seen_paths.add(path)
                dump_hits.append(item)
                if path and not is_module:
                    seen_paths.add(path)
                if len(results) + len(dump_hits) >= limit:
                    break

        results.extend(dump_hits)
        out = {
            "context": entity["name"],
            "guid": full or base,
            "guid_base": base,
            "total": len(results),
            "results": results[:limit],
        }
        if not results:
            out["hint"] = (
                "GUID not in index and not found in ConfigDumpInfo.xml. "
                "Refresh/reindex dump after upgrading to fill objects.guid, "
                "or check the UUID / context."
            )
            if dumps_dir is None:
                out["hint"] = (
                    "GUID not in index; dumps_dir not readable "
                    "(host W:\\ path inside Docker?). "
                    "Use a /mnt/… path entity or refresh after reparse with guid column."
                )
            out["next_tool"] = "search_metadata"
        elif all(r.get("source") == "ConfigDumpInfo.xml" for r in results) and not indexed:
            out["hint"] = (
                "Resolved via ConfigDumpInfo (index guid empty until reparse). "
                "Use path with get_object."
            )
            out["next_tool"] = "get_object"
        return out
    finally:
        conn.close()


def get_indexing_status(context_ref: str | None = None) -> dict:
    """Статусы ВСЕХ сущностей (в отличие от list_contexts — не только ready)."""
    conn = connect(settings.db_path)
    try:
        if context_ref:
            kind, value = parse_context_ref(context_ref)
            if kind == "tag":
                rows = ent_repo.list_entities_with_tag(conn, value)
            else:
                ent = ent_repo.get_entity_by_name(conn, value)
                if not ent:
                    raise ValueError(f"Unknown context '{value}'.")
                rows = [ent]
        else:
            rows = ent_repo.list_entities(conn)

        ids = [int(r["id"]) for r in rows]
        method_counts: dict[int, int] = {}
        for i in range(0, len(ids), 400):
            part = ids[i : i + 400]
            ph = ",".join("?" * len(part))
            for cr in conn.execute(
                f"""
                SELECT entity_id, COUNT(*) AS c FROM objects
                WHERE entity_id IN ({ph}) AND kind IN ('Procedure','Function')
                GROUP BY entity_id
                """,
                part,
            ).fetchall():
                method_counts[int(cr["entity_id"])] = int(cr["c"])

        entities = []
        for r in rows:
            rid = int(r["id"])
            method_count = method_counts.get(rid, int(r.get("bsl_method_count") or 0))
            load_mode = _effective_load_mode({**dict(r), "bsl_method_count": method_count})
            item = {
                "id": rid,
                "name": r.get("name") or "",
                "model": r.get("model") or "",
                "source_mode": r.get("source_mode") or "report",
                "source_location": r.get("source_location") or "upload",
                "entity_type": r.get("entity_type") or "configuration",
                "status": r.get("status") or "",
                "enabled": bool(r.get("enabled")),
                "bsl_enabled": bool(r.get("bsl_enabled")) if r.get("bsl_enabled") is not None else True,
                "bsl_load_mode": load_mode,
                "bsl_method_count": method_count,
                "object_count": int(r.get("object_count") or 0),
                "link_count": int(r.get("link_count") or 0),
                "indexed_count": int(r.get("indexed_count") or 0),
                "index_target": int(r.get("index_target") or 0),
                "index_scope": str(r.get("index_scope") or ""),
                "parse_gen": int(r.get("parse_gen") or 0),
                "parse_added": int(r.get("parse_added") or 0),
                "parse_changed": int(r.get("parse_changed") or 0),
                "parse_deleted": int(r.get("parse_deleted") or 0),
                "parse_unchanged": int(r.get("parse_unchanged") or 0),
                "error_message": r.get("error_message") or "",
                "updated_at": str(r.get("updated_at") or ""),
            }
            if (r.get("source_mode") or "") == "dump":
                item["index_stale"] = _entity_index_stale_flag(conn, dict(r))
            entities.append(item)
        return {
            "context": context_ref or "*",
            "total": len(entities),
            "entities": entities,
        }
    finally:
        conn.close()


def get_skd(context: str, path: str) -> dict:
    """Структурированное извлечение из схемы компоновки данных (СКД)."""
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        obj = obj_repo.get_object(conn, entity["id"], path)
        if not obj:
            paths = refs.resolve_qualified_name(entity, path, conn)
            if paths:
                obj = obj_repo.get_object(conn, entity["id"], paths[0])
        if not obj:
            raise ValueError(f"Object '{path}' not found in context '{context}'.")
        if obj.get("kind") != "DataCompositionSchema":
            raise ValueError(
                f"Object '{obj.get('path')}' is kind '{obj.get('kind')}', "
                "expected DataCompositionSchema (СКД-макет)."
            )
        props = obj.get("props") or {}
        skd = props.get("skd") if isinstance(props.get("skd"), dict) else {}
        if not skd or not skd.get("fields"):
            source_rel = str(props.get("_source_rel") or "").strip()
            dumps_dir = Path(entity.get("dumps_dir") or "")
            if source_rel and dumps_dir.is_dir():
                from app.services.dump_assets_parser import load_skd_from_file

                skd = load_skd_from_file(dumps_dir, source_rel) or skd
        return {
            "context": entity["name"],
            "path": obj.get("path") or path,
            "name": obj.get("name") or "",
            "kind": obj.get("kind") or "DataCompositionSchema",
            "data_sets": skd.get("data_sets") or [],
            "fields": skd.get("fields") or [],
            "parameters": skd.get("parameters") or [],
            "settings_variants": skd.get("settings_variants") or [],
        }
    finally:
        conn.close()


def _compare_side(conn, entity: dict, path: str) -> dict:
    """Light card for compare_objects: identity + attr/TS/movement names + help titles."""
    paths = refs.resolve_qualified_name(entity, path, conn)
    if not paths:
        raise ValueError(f"Object '{path}' not found in context '{entity.get('name')}'.")
    canonical = paths[0]
    obj = obj_repo.get_object(conn, entity["id"], canonical)
    if not obj:
        raise ValueError(f"Object '{path}' not found in context '{entity.get('name')}'.")
    children = obj_repo.list_structure_children(conn, int(entity["id"]), canonical)
    outgoing = obj_repo.get_links(conn, entity["id"], canonical, "out")["outgoing"]
    structure = payload_svc.compact_structure(canonical, children, outgoing)
    help_rows = obj_repo.get_help_for_owner(conn, int(entity["id"]), canonical)
    help_titles = [
        str(r.get("name") or r.get("path") or "")
        for r in (help_rows or [])[:8]
        if str(r.get("name") or r.get("path") or "")
    ]
    return {
        "path": canonical,
        "kind": obj.get("kind") or "",
        "name": obj.get("name") or "",
        "synonym": obj.get("synonym") or "",
        "comment": (obj.get("comment") or "")[:400],
        "attrs": [a.get("name") for a in (structure.get("attrs") or []) if a.get("name")],
        "tabular_sections": [
            t.get("name")
            for t in (structure.get("tabular_sections") or [])
            if t.get("name")
        ],
        "movements": [
            m.get("path") for m in (structure.get("movements") or []) if m.get("path")
        ],
        "help_titles": help_titles,
    }


def _set_diff(a: list[str], b: list[str]) -> dict:
    sa, sb = set(a), set(b)
    return {
        "only_a": sorted(sa - sb),
        "only_b": sorted(sb - sa),
        "both": sorted(sa & sb),
    }


def compare_objects(context: str, path_a: str, path_b: str) -> dict:
    """U32: light side-by-side of two metadata roots (attrs/TS/movements/help)."""
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        a = _compare_side(conn, entity, path_a)
        b = _compare_side(conn, entity, path_b)
        return {
            "context": entity["name"],
            "a": a,
            "b": b,
            "diff": {
                "attrs": _set_diff(a["attrs"], b["attrs"]),
                "tabular_sections": _set_diff(
                    a["tabular_sections"], b["tabular_sections"]
                ),
                "movements": _set_diff(a["movements"], b["movements"]),
            },
            "hint": (
                "For full field types / FillChecking use get_object on each path; "
                "for report layouts use get_skd on …Макеты.*."
            ),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# U20: join-path card for «получить X запросом с fallback A→B».
# Given a source object and one or more targets, show the tabular-section /
# attribute fields that carry a type link between them (the query JOIN keys),
# plus ready-made *ТекстЗапроса* methods that mention both objects.
# ---------------------------------------------------------------------------
_METHOD_NAME_PREFIXES = (
    "ТекстЗапроса",
    "ПолучитьТекстЗапроса",
    "ТекстЗапросаДля",
    "ДанныеПеревозк",
)


def _resolve_one_path(entity: dict, ref: str, conn: sqlite3.Connection) -> str:
    paths = refs.resolve_qualified_name(entity, ref, conn)
    if paths:
        return paths[0]
    # Bare object name (РеализацияТоваровУслуг) → root object with exact name.
    name = refs.nfc(ref.strip().strip('"')).rsplit(".", 1)[-1]
    if name:
        row = conn.execute(
            """
            SELECT path FROM objects
            WHERE entity_id=? AND name=? AND path NOT LIKE '%.%.%'
            ORDER BY length(path), path LIMIT 1
            """,
            (int(entity["id"]), name),
        ).fetchone()
        if row:
            return row["path"]
    raise ValueError(f"Object '{ref}' not found in context '{entity['name']}'.")


def _obj_summary(entity: dict, path: str, conn: sqlite3.Connection) -> dict:
    obj = obj_repo.get_object(conn, int(entity["id"]), path) or {}
    return {
        "path": path,
        "name": obj.get("name") or path.rsplit(".", 1)[-1],
        "kind": obj.get("kind") or "",
        "synonym": obj.get("synonym") or "",
    }


def _rel_field(obj_path: str, from_path: str) -> tuple[str, str]:
    """(relative_field, container). ``Документы.Х.ТабличныеЧасти.Т.Реквизиты.Ф``
    → (``Т.Ф``, ``Т``); plain attribute → (``Ф``, ``""``)."""
    parts = (from_path or "").split(".")
    pref = obj_path.split(".")
    if parts[: len(pref)] != pref:
        rel = parts[len(pref):]
    else:
        rel = parts[len(pref):]
    field = ".".join(rel)
    container = ""
    if len(rel) >= 4 and rel[-4] == "ТабличныеЧасти":
        container = rel[-3]
        field = f"{container}.{rel[-1]}"
    elif len(rel) >= 2 and rel[-2] in ("Реквизиты", "Измерения", "Ресурсы"):
        field = rel[-1]
    return field, container


def _type_links_to(
    conn: sqlite3.Connection,
    entity: dict,
    obj_path: str,
    counterpart: str,
    *,
    limit: int = 30,
) -> list[dict]:
    """Type-link columns under obj_path whose ref matches the counterpart object.

    Uses the (entity_id, from_path) index range so the scan stays cheap on big
    contexts (УправлениеПредприятием ~540k).
    """
    eid = int(entity["id"])
    lo, hi = obj_repo.path_range_params(obj_path)
    rows = conn.execute(
        """
        SELECT from_path, to_ref FROM links
        WHERE entity_id=? AND from_path BETWEEN ? AND ?
          AND link_type='type'
        ORDER BY from_path LIMIT ?
        """,
        (eid, lo, hi, 5000),
    ).fetchall()
    if not rows:
        return []
    cands = refs.link_ref_candidates(counterpart)
    name = counterpart.rsplit(".", 1)[-1]
    grouped: dict[str, list[str]] = {}
    for r in rows:
        tr = r["to_ref"] or ""
        grouped.setdefault(r["from_path"], []).append(tr)
    out: list[dict] = []
    for from_path in sorted(grouped):
        types = sorted({t for t in grouped[from_path] if t})
        matched = [t for t in types if t in cands or t.endswith("." + name)]
        if not matched:
            continue
        rel, container = _rel_field(obj_path, from_path)
        out.append(
            {
                "field": rel,
                "path": from_path,
                "container": container,
                "types": types,
                "matches": sorted({m.rsplit(".", 1)[-1] for m in matched}),
            }
        )
        if len(out) >= limit:
            break
    return out


def _ready_query_methods(
    conn: sqlite3.Connection,
    entity: dict,
    names: set[str],
    *,
    limit: int = 10,
) -> list[dict]:
    """*ТекстЗапроса* / ДанныеПеревозк* methods related to the requested objects.

    A method is "related" when its name/path/comment mentions at least one of the
    object names; results sorted by number of distinct names matched, then by path
    length. Comments rarely name every object, so an exact "both objects" filter
    would miss the useful coalesce helpers.
    """
    eid = int(entity["id"])
    clauses = " OR ".join("name LIKE ?" for _ in _METHOD_NAME_PREFIXES)
    params: list = [eid, *[p + "%" for p in _METHOD_NAME_PREFIXES]]
    rows = conn.execute(
        f"""
        SELECT path, name, synonym, comment FROM objects
        WHERE entity_id=? AND kind IN ('Procedure', 'Function')
          AND ({clauses})
        ORDER BY length(path) ASC
        """,
        params,
    ).fetchall()
    if not rows:
        return []
    cf = sorted({n.casefold() for n in names})
    scored: list[tuple[int, dict]] = []
    for r in rows:
        hay = f"{r['path'] or ''} {r['name'] or ''} {r['comment'] or ''}".casefold()
        cnt = sum(1 for n in cf if n in hay)
        if not cnt:
            continue
        scored.append(
            (
                cnt,
                {
                    "path": r["path"],
                    "name": r["name"],
                    "synonym": r["synonym"] or "",
                    "comment": (r["comment"] or "")[:300],
                },
            )
        )
    scored.sort(key=lambda item: (-item[0], len(item[1]["path"])))
    return [it for _, it in scored[:limit]]


def get_query_path(
    context: str,
    source: str,
    target: str,
    fallbacks: list[str] | None = None,
    *,
    limit: int = 10,
) -> dict:
    """Join-path card: how to get from `source` to `target` (with fallbacks).

    Returns the fields (attributes / tabular-section columns) that carry a type
    link between each pair (the JOIN keys usable in a query), and ready-made
    *ТекстЗапроса* methods mentioning both objects. Distinguishes "no such
    attribute" (no join field) from "no data" (join exists, but the value may
    be empty — handle with ЕСТЬNULL/ВЫБОР).
    """
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        src = _resolve_one_path(entity, source, conn)
        tgt = _resolve_one_path(entity, target, conn)
        names = {src.rsplit(".", 1)[-1], tgt.rsplit(".", 1)[-1]}

        def pair(a: str, b: str) -> dict:
            ba = _type_links_to(conn, entity, b, a)
            ab = _type_links_to(conn, entity, a, b)
            hint = ""
            if not ba and not ab:
                hint = (
                    "Прямых type-связей между объектами нет — связь двухшаговая "
                    "(через промежуточный документ/справочник) или через код. "
                    "См. ready_query_methods, find_usages, get_links."
                )
            return {"from_b_to_a": ba, "from_a_to_b": ab, "hint": hint}

        fb_out: list[dict] = []
        for fb in fallbacks or []:
            fpath = _resolve_one_path(entity, fb, conn)
            names.add(fpath.rsplit(".", 1)[-1])
            fb_out.append(
                {
                    "target": _obj_summary(entity, fpath, conn),
                    **pair(src, fpath),
                }
            )

        join = pair(src, tgt)
        ready = _ready_query_methods(conn, entity, names, limit=limit)
        return {
            "context": entity["name"],
            "source": _obj_summary(entity, src, conn),
            "target": _obj_summary(entity, tgt, conn),
            "join": {
                "target_to_source": join["from_b_to_a"],
                "source_to_target": join["from_a_to_b"],
            },
            "fallbacks": fb_out,
            "ready_query_methods": ready,
        }
    finally:
        conn.close()


def search_forms(
    context: str,
    query: str,
    *,
    path_prefix: str | None = None,
    limit: int = 20,
    compact: bool = True,
) -> dict:
    """Gap 3: search ManagedForm by name/path and form structure in props_json."""
    limit = max(1, min(int(limit), 100))
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        rows = obj_repo.search_managed_forms(
            conn,
            int(entity["id"]),
            query,
            path_prefix=path_prefix,
            limit=limit,
        )
        results = [
            {
                "context": entity["name"],
                "path": r.get("path"),
                "kind": r.get("kind") or "ManagedForm",
                "belong": r.get("belong"),
                "name": r.get("name"),
                "synonym": r.get("synonym"),
                "comment": r.get("comment") or "",
                "score": float(r.get("score") or 1.0),
            }
            for r in rows
        ]
        out = {
            "context": entity["name"],
            "contexts": [entity["name"]],
            "query": query,
            "total_returned": len(results),
            "results": results,
        }
        if path_prefix:
            out["path_prefix"] = path_prefix
        return _compact_results(out, compact=compact)
    finally:
        conn.close()


def queue_managed_form_reembed(context: str) -> dict:
    """Gap 3: mark ManagedForm rows embed_done=0 and resume reindex (embed only)."""
    from app.services import jobs
    from app.services.pipeline import reindex_entity

    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        eid = int(entity["id"])
        n = obj_repo.reset_embed_flags_for_kind(conn, eid, "ManagedForm")
        conn.commit()
        name = entity["name"]
    finally:
        conn.close()
    if n > 0:
        jobs.submit(reindex_entity, eid, resume=True)
    return {
        "context": name,
        "kind": "ManagedForm",
        "reset_count": n,
        "reindex": "submitted" if n > 0 else "skipped",
        "detail": (
            f"Marked {n} ManagedForm rows for re-embed; resume reindex submitted."
            if n > 0
            else "No ManagedForm rows to re-embed."
        ),
    }


def queue_method_reembed(context: str) -> dict:
    """Mark Procedure/Function rows for re-embed after meta_text(signature) change."""
    from app.services import jobs
    from app.services.pipeline import reindex_entity

    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        eid = int(entity["id"])
        n = obj_repo.reset_method_embed_flags(conn, eid)
        conn.commit()
        name = entity["name"]
        methods_total = int(entity.get("bsl_method_count") or 0)
    finally:
        conn.close()
    if n > 0:
        jobs.submit(reindex_entity, eid, resume=True)
    return {
        "context": name,
        "kind": "Procedure,Function",
        "reset_count": n,
        "methods_total": methods_total,
        "reindex": "submitted" if n > 0 else "skipped",
        "detail": (
            f"Marked {n} method rows for re-embed; resume reindex submitted."
            if n > 0
            else "No method rows to re-embed."
        ),
    }