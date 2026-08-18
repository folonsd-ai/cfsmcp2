from __future__ import annotations

import time
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from app.core.config import settings
from app.core.database import connect
from app.repositories import entities as ent_repo
from app.repositories import tags as tag_repo
from app.services import search as search_svc
from app.services.usage_stats import usage_stats

_CTX_DESC = (
    "Exact entity name (configuration/extension) as in the user's request or list_contexts, "
    "OR tag:TagName from list_context_groups (only when the user asked to search a tag group). "
    "If the user named a context — pass that name only; do NOT loop the same query over other contexts. "
    "Example: РасширениеКонтурЛогистика or tag:КА2"
)
_CTX_SINGLE_DESC = (
    "Exact single entity name (from the user, list_contexts, or a hit's 'context' field). "
    "Never pass tag:… here. Never substitute a different context than the one in the question."
)

mcp = FastMCP(
    name="cfsmcp2",
    instructions=(
        "Search 1C metadata/BSL in MCP contexts (configuration or extension names).\n"
        "\n"
        "CONTEXT SCOPING (mandatory):\n"
        "- If the user names a context (e.g. «в РасширениеКонтурЛогистика…») — use ONLY that "
        "exact name in every search_metadata / semantic_search / find_* / get_* call.\n"
        "- Do NOT call list_contexts first when the context is already named.\n"
        "- Do NOT fan-out: never repeat the same query across all contexts from list_contexts.\n"
        "- Search other contexts only if the user explicitly asks (all configs, another name, "
        "or a tag group).\n"
        "- Multi-context search only via context=tag:TagName when the user asked for that tag.\n"
        "- Partial context names may resolve uniquely; if several candidates are returned, pick one.\n"
        "\n"
        "Workflow when context is UNKNOWN: list_contexts or list_context_groups → pick one name → "
        "search_metadata | semantic_search → get_object / get_links / find_usages / "
        "get_object_dossier / trace_impact with the hit's context (never tag: on get_*).\n"
        "Workflow when context is KNOWN: go straight to search/get in that context.\n"
        "\n"
        "Search tips:\n"
        "- Exact names / short identifiers → search_metadata (FTS) first; semantic_search only "
        "if FTS is empty or weak.\n"
        "- After finding a document/catalog root, use search_under(parent_path=…) or "
        "path_prefix=… instead of global search.\n"
        "- «Обязательно ли поле?» → get_required_fields(context, path) (FillChecking + "
        "МассивОбязательных* + МассивНепроверяемыхРеквизитов + реквизиты по хоз.операции). "
        "Prefer compact=true on search tools; get_object/get_object_dossier default detail_level=L2 "
        "(attrs+ТЧ+movements); L0 only when full props needed. find_usages/trace_impact default "
        "summary=true (owners+counts); expand=paths for full paths.\n"
        "- Result 'comment' often has business rules — read it before more tool calls.\n"
        "- If search returns title_candidates (ТитулN documents) — pick one Document path "
        "and use search_under / get_required_fields; ignore attribute hits named ТитулN.\n"
        "- «Как / можно ли / где / как пользоваться» → сначала helpsearch(context, query), "
        "затем semantic_search; хит справки: comment = текст, props.owner = объект-владелец. "
        "Открой карточку через get_object(context, owner). На how-to semantic уже подмешивает "
        "Help и поднимает owner-объект — всё равно смотри helpsearch при слабом топе.\n"
        "\n"
        "BSL (dump): list_code_modules / list_methods / find_methods / get_method / "
        "get_module_structure / find_code_references. get_method и find_code_references "
        "читают .bsl с диска, если в индексе только сигнатуры (dumps_dir / path). "
        "search_metadata/semantic_search can also surface Procedure/Function hits "
        "(same index when BSL is enabled) but without find_methods' exact/prefix-first "
        "and signature-aware ranking — prefer find_methods for a known/likely procedure "
        "name or intent; fall back to semantic_search only if find_methods misses.\n"
        "Lean / metadata-only ingest (UI preset «Только метаданные», or report): "
        "list_contexts.profile.lean=true / profile.bsl=false — нет методов и call graph; "
        "используй search_metadata, get_object, find_usages, trace_impact. "
        "Движения регистра: find_usages(path=Регистры…, link_type=movements) или "
        "movements в get_object L2 / dossier.\n"
        "BSL call graph (default on; disable via PATCH /api/settings experimental_call_chain=false): "
        "trace_call_chain — BFS only over resolved BSL calls (built at ingest when "
        "bsl_load_mode is signatures or full; not signatures_only). "
        "Do NOT use it for type/movements impact — that is trace_impact / find_usages. "
        "If chain is empty, follow hint/next_tool (usually trace_impact); never assume "
        "call-chain covers metadata links.\n"
        "СКД (dump, profile dcs): get_skd. Forms/XML templates: get_object on "
        "….Формы.<Name> / ….Макеты.<Name>. Если search вернул skd_hint / "
        "skd_candidates — вызови get_skd по кандидату.\n"
        "«В чём разница / чем отличаются» два объекта → compare_objects(context, a, b).\n"
        "Sources: configuration report (report) or Hierarchical dump. Metadata: objects, "
        "attributes, types, hierarchy, links (get_links), full props (get_object; dump richer)."
    ),
)


def _track(name: str, fn, *, context: str = "", **tool_args):
    t0 = time.perf_counter()
    ok = True
    detail = ""
    result = None
    try:
        result = fn()
        if isinstance(result, dict) and result.get("error"):
            ok = False
            detail = str(result.get("error"))[:800]
        return result
    except Exception as exc:
        ok = False
        detail = str(exc)[:800]
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000
        # Persist handled below with replayable args (not the short usage line).
        usage_stats.record(
            kind="mcp",
            name=name,
            ok=ok,
            duration_ms=ms,
            context=context,
            detail=detail[:200],
            tier="usage",
            persist=False,
        )
        try:
            from app.services import app_log as app_log_svc

            replay = app_log_svc.format_mcp_replay_detail(
                tool=name,
                context=context,
                duration_ms=ms,
                args=tool_args,
                result=result,
                error=detail,
                ok=ok,
            )
            not_found = "not found in context" in detail
            if not ok:
                if not not_found:
                    app_log_svc.append_error(
                        kind="mcp",
                        name=name,
                        detail=replay,
                        context=context,
                        duration_ms=ms,
                    )
            else:
                app_log_svc.maybe_slow(
                    kind="mcp",
                    name=name,
                    duration_ms=ms,
                    context=context,
                    detail=replay,
                    ok=True,
                )
        except Exception:
            pass
        if not ok and detail and "not found in context" not in detail:
            # In-memory errors-level line; SQLite already has full replay above.
            usage_stats.record_error_detail(
                kind="mcp",
                name=name,
                context=context,
                detail=detail,
                duration_ms=ms,
                persist=False,
            )
        usage_stats.record_mcp_verbose(
            name=name,
            ok=ok,
            duration_ms=ms,
            context=context,
            args=tool_args,
            result=result,
            error=detail,
        )


@mcp.tool(
    name="list_contexts",
    annotations={
        "title": "List metadata contexts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_contexts() -> list[dict]:
    """List enabled ready contexts (name, tags, counts, profile flags).

    Use ONLY when the user did not name a context yet.
    If they already named one (e.g. РасширениеКонтурЛогистика) — skip this tool
    and search that name directly; do not iterate all returned contexts.

    Each item includes ``profile``: {bsl, help, lean, …}. When lean=true or
    bsl=false — metadata-only (no find_methods / trace_call_chain).
    """

    def _run():
        conn = connect(settings.db_path)
        try:
            return ent_repo.list_ready_contexts(conn)
        finally:
            conn.close()

    return _track("list_contexts", _run)


@mcp.tool(
    name="list_context_groups",
    annotations={
        "title": "List context groups by tag",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_context_groups() -> list[dict]:
    """List tags that group ready contexts. Use only if the user asked for a tag group
    or the context name is unknown. Then search with context=tag:Name — not a manual loop.
    """

    def _run():
        conn = connect(settings.db_path)
        try:
            return tag_repo.list_context_groups(conn)
        finally:
            conn.close()

    return _track("list_context_groups", _run)


@mcp.tool(
    name="search_metadata",
    annotations={
        "title": "FTS search metadata",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def search_metadata(
    context: Annotated[str, Field(description=_CTX_DESC)],
    query: Annotated[str, Field(description="Full-text search query")],
    kind: Annotated[str | None, Field(description="Optional kind filter, e.g. Catalog, Document")] = None,
    include_borrowed: Annotated[bool, Field(description="Include borrowed objects")] = False,
    limit: Annotated[int, Field(description="Page size", ge=1, le=200)] = 50,
    offset: Annotated[int, Field(description="Offset for pagination", ge=0)] = 0,
    path_prefix: Annotated[
        str | None,
        Field(description="Only paths under this prefix, e.g. Документы.КонтурЛогистика_ДФ_Титул1"),
    ] = None,
    compact: Annotated[
        bool,
        Field(description="Return compact hits (path/kind/name/synonym/comment/score)"),
    ] = True,
) -> dict:
    """Full-text search in ONE context (or one tag: group).

    Pass the user-named context exactly. Do not call this repeatedly for every
    context from list_contexts with the same query. tag:… only when searching a tag group.
    Prefer path_prefix / search_under after you know the parent object.
    """

    def _run():
        try:
            return search_svc.fts_search(
                context,
                query,
                kind=kind,
                include_borrowed=include_borrowed,
                limit=limit,
                offset=offset,
                path_prefix=path_prefix,
                compact=compact,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "search_metadata",
        _run,
        context=context,
        query=query,
        kind=kind,
        include_borrowed=include_borrowed,
        limit=limit,
        offset=offset,
        path_prefix=path_prefix,
        compact=compact,
    )


@mcp.tool(
    name="search_under",
    annotations={
        "title": "Search under parent path",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def search_under(
    context: Annotated[str, Field(description=_CTX_DESC)],
    parent_path: Annotated[
        str,
        Field(description="Parent object path, e.g. Документы.КонтурЛогистика_ДФ_Титул1"),
    ],
    query: Annotated[str, Field(description="Search query within the parent subtree")],
    kind: Annotated[str | None, Field(description="Optional kind filter")] = None,
    include_borrowed: Annotated[bool, Field(description="Include borrowed objects")] = False,
    limit: Annotated[int, Field(description="Page size", ge=1, le=200)] = 50,
    compact: Annotated[bool, Field(description="Compact hits")] = True,
) -> dict:
    """FTS (+ SQL) search restricted to children of parent_path. Prefer over global search."""

    def _run():
        try:
            return search_svc.search_under(
                context,
                parent_path,
                query,
                kind=kind,
                include_borrowed=include_borrowed,
                limit=limit,
                compact=compact,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "search_under",
        _run,
        context=context,
        parent_path=parent_path,
        query=query,
        kind=kind,
        limit=limit,
    )


@mcp.tool(
    name="semantic_search",
    annotations={
        "title": "Semantic/hybrid metadata search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def semantic_search(
    context: Annotated[str, Field(description=_CTX_DESC)],
    query: Annotated[str, Field(description="Natural language query")],
    top_n: Annotated[int, Field(description="Number of results", ge=1, le=100)] = 20,
    path_prefix: Annotated[
        str | None,
        Field(description="Only paths under this prefix"),
    ] = None,
    compact: Annotated[bool, Field(description="Compact hits")] = True,
) -> dict:
    """Hybrid vector + FTS search in ONE context (or tag: group).

    Same scoping rules as search_metadata: user-named context only; no fan-out.
    Use after FTS when the question is conceptual, not an exact identifier.
    """

    def _run():
        try:
            return search_svc.semantic_search(
                context,
                query,
                top_n=top_n,
                path_prefix=path_prefix,
                compact=compact,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "semantic_search",
        _run,
        context=context,
        query=query,
        top_n=top_n,
        path_prefix=path_prefix,
        compact=compact,
    )


@mcp.tool(
    name="helpsearch",
    annotations={
        "title": "Search configuration help files",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def helpsearch(
    context: Annotated[str, Field(description=_CTX_DESC)],
    query: Annotated[str, Field(description="Natural language question about how to use the configuration")],
    limit: Annotated[int, Field(description="Number of results", ge=1, le=50)] = 8,
    owner: Annotated[
        str | None,
        Field(
            description="Optional owner object path to scope help to, e.g. Документы.ТранспортнаяНакладная"
        ),
    ] = None,
    compact: Annotated[bool, Field(description="Compact hits")] = True,
) -> dict:
    """Search user help files (Ext/Help/*.html) of the configuration.

    Each hit is a help section: comment holds the section text, props.owner the
    owning object. Use when the question is «как пользоваться/как заполнять»;
    pair with get_object(owner) to see the full object card.
    """

    def _run():
        try:
            return search_svc.help_search(
                context,
                query,
                limit=limit,
                owner=owner,
                compact=compact,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "helpsearch",
        _run,
        context=context,
        query=query,
        limit=limit,
        owner=owner,
        compact=compact,
    )


@mcp.tool(
    name="get_required_fields",
    annotations={
        "title": "Required / FillChecking fields",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_required_fields(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[
        str,
        Field(description="Document/catalog/attribute path, e.g. Документы.…_Титул1"),
    ],
) -> dict:
    """Answer «обязательно ли?» via FillChecking, МассивОбязательных*, and
    code that clears checks (МассивНепроверяемыхРеквизитов / хоз.операция)."""

    def _run():
        try:
            return search_svc.get_required_fields(context, path)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_required_fields", _run, context=context, path=path)


@mcp.tool(
    name="get_object",
    annotations={
        "title": "Get metadata object",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_object(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[str, Field(description="Full object path, e.g. Catalogs.X.Attributes.Y or Russian path")],
    detail_level: Annotated[
        Literal["L3", "L2", "L0"],
        Field(
            description="L3=path/kind/name/synonym; L2=attrs+ТЧ+movements (default); L0=full props"
        ),
    ] = "L2",
) -> dict:
    """Return object card. Default L2 is enough for «какие поля заполнять».

    Requires a single entity name (not tag:). For usage counts / top owners
    («кто ссылается», «сколько документов используют») use get_object_dossier instead.
    """

    def _run():
        try:
            return search_svc.get_object(context, path, detail_level=detail_level)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_object", _run, context=context, path=path)


@mcp.tool(
    name="get_links",
    annotations={
        "title": "Get metadata links",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_links(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    object: Annotated[str, Field(description="Object path")],
    direction: Annotated[
        Literal["both", "in", "out", "incoming", "outgoing"],
        Field(description="Link direction"),
    ] = "both",
) -> dict:
    """Return incoming/outgoing links for an object. Requires a single entity name (not tag:)."""

    def _run():
        try:
            return search_svc.get_links(context, object, direction)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_links", _run, context=context, object=object, direction=direction)


@mcp.tool(
    name="get_query_path",
    annotations={
        "title": "Join-path card source→target (fallback A→B)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_query_path(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    source: Annotated[str, Field(description="Source object path we have (e.g. РеализацияТоваровУслуг)")],
    target: Annotated[str, Field(description="Target object path to get data from (e.g. ТранспортнаяНакладная)")],
    fallbacks: Annotated[
        list[str] | None,
        Field(description="Fallback targets when data is absent in `target` (e.g. [\"фмРейс\"])"),
    ] = None,
    limit: Annotated[int, Field(description="Max ready query methods", ge=1, le=50)] = 10,
) -> dict:
    """Build a JOIN-path card: how to get fields of `target` for a given `source`.

    Shows the attributes / tabular-section columns carrying a type link between
    source↔target (the fields usable in a query to fetch related data), plus
    ready-made *ТекстЗапроса* methods that mention both objects. Use with
    fallbacks for «получить X из A, иначе из B»: the card covers every pair and
    distinguishes "no such attribute" (empty join list) from "no data" (join
    field exists, value may be empty — handle with ЕСТЬNULL/ВЫБОР).
    """

    def _run():
        try:
            return search_svc.get_query_path(
                context,
                source,
                target,
                fallbacks,
                limit=limit,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "get_query_path",
        _run,
        context=context,
        source=source,
        target=target,
        fallbacks=fallbacks,
        limit=limit,
    )


@mcp.tool(
    name="find_usages",
    annotations={
        "title": "Find where an object is used",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def find_usages(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[str, Field(description="Object path (RU or EN form)")],
    link_type: Annotated[
        Literal["type", "movements", "based_on", "composition", "calls"] | None,
        Field(description="Filter by link type; None = all. movements = docs writing to a register"),
    ] = None,
    summary: Annotated[
        bool,
        Field(description="Aggregate to owner objects + counts (default). False or expand=paths for full paths."),
    ] = True,
    expand: Annotated[
        Literal["paths"] | None,
        Field(description="expand=paths returns full from_path list (paginated)"),
    ] = None,
    limit: Annotated[int, Field(description="Page size", ge=1, le=200)] = 50,
    offset: Annotated[int, Field(description="Offset for pagination", ge=0)] = 0,
) -> dict:
    """Find where the object is used: incoming links by normalized refs.

    Default summary groups to owner (Документы.X) + count; DefinedType excluded.
    Covers type references, movements, based_on, composition, and BSL calls.
    Register movements: pass the register path with link_type=movements
    (documents that write to the register).
    """

    def _run():
        try:
            return search_svc.find_usages(
                context,
                path,
                link_type,
                summary=summary,
                expand=expand,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "find_usages",
        _run,
        context=context,
        path=path,
        link_type=link_type,
        summary=summary,
        expand=expand,
        limit=limit,
        offset=offset,
    )


@mcp.tool(
    name="get_object_dossier",
    annotations={
        "title": "Object dossier",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_object_dossier(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[str, Field(description="Object path (RU or EN form)")],
    include_props: Annotated[
        bool, Field(description="Force L0 full properties (default False)")
    ] = False,
    detail_level: Annotated[
        Literal["L3", "L2", "L0"],
        Field(description="L3 card; L2 summary attrs+ТЧ+movements+usage counts (default); L0 full"),
    ] = "L2",
    summary: Annotated[
        bool, Field(description="Aggregate usages to owners (default True)")
    ] = True,
    expand: Annotated[
        Literal["paths"] | None,
        Field(description="expand=paths keeps full usage paths"),
    ] = None,
    limit: Annotated[int, Field(description="Usage page size", ge=1, le=200)] = 50,
    offset: Annotated[int, Field(description="Usage offset", ge=0)] = 0,
) -> dict:
    """Passport of an object. Default L2: attrs (name+type), tabular sections,
    movements, usage counts + top owners — without XML props or path dumps."""

    def _run():
        try:
            return search_svc.get_object_dossier(
                context,
                path,
                include_props,
                detail_level=detail_level,
                summary=summary,
                expand=expand,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "get_object_dossier",
        _run,
        context=context,
        path=path,
        include_props=include_props,
        detail_level=detail_level,
        summary=summary,
        expand=expand,
        limit=limit,
        offset=offset,
    )


@mcp.tool(
    name="trace_impact",
    annotations={
        "title": "Trace impact through links",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def trace_impact(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[str, Field(description="Object path (RU or EN form)")],
    direction: Annotated[
        Literal["down", "up"],
        Field(
            description="down = who uses the object (incoming); up = what the object references (outgoing)"
        ),
    ] = "down",
    depth: Annotated[int, Field(description="Max depth (1..5)", ge=1, le=5)] = 3,
    limit: Annotated[int, Field(description="Max result rows / owners", ge=1, le=200)] = 100,
    summary: Annotated[
        bool, Field(description="Aggregate to owner objects + counts (default True)")
    ] = True,
    expand: Annotated[
        Literal["paths"] | None,
        Field(description="expand=paths returns full paths"),
    ] = None,
    offset: Annotated[int, Field(description="Offset for pagination", ge=0)] = 0,
) -> dict:
    """Recursive impact analysis over links (BFS with cycle guard).

    Default summary: owner path + count. Each full-mode row: {path, link_type, depth}.
    """

    def _run():
        try:
            return search_svc.trace_impact(
                context,
                path,
                direction,
                depth,
                limit,
                summary=summary,
                expand=expand,
                offset=offset,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "trace_impact",
        _run,
        context=context,
        path=path,
        direction=direction,
        depth=depth,
        limit=limit,
        summary=summary,
        expand=expand,
        offset=offset,
    )


@mcp.tool(
    name="trace_call_chain",
    annotations={
        "title": "Trace BSL call chain",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def trace_call_chain(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[
        str,
        Field(description="Method path, e.g. ОбщиеМодули.X.Методы.Module.Foo"),
    ],
    direction: Annotated[
        Literal["callees", "callers", "both"],
        Field(
            description="callees = who this method calls; callers = who calls it; both"
        ),
    ] = "callees",
    depth: Annotated[int, Field(description="Max depth (1..5)", ge=1, le=5)] = 3,
    limit: Annotated[int, Field(description="Max nodes per direction", ge=1, le=200)] = 100,
) -> dict:
    """BSL call-chain BFS over resolved ``calls`` links only.

    Not for «who writes to a register», types, or movements — use find_usages
    or trace_impact. Empty chain: follow hint/next_tool; do not retry this tool
    with another depth.
    """

    def _run():
        try:
            return search_svc.trace_call_chain(
                context, path, direction=direction, depth=depth, limit=limit
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "trace_call_chain",
        _run,
        context=context,
        path=path,
        direction=direction,
        depth=depth,
        limit=limit,
    )


@mcp.tool(
    name="list_code_modules",
    annotations={
        "title": "List modules with BSL methods",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_code_modules(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    kind: Annotated[
        str | None,
        Field(description="Optional module kind filter, e.g. CommonModule, Catalog, Document"),
    ] = None,
    q: Annotated[
        str | None,
        Field(
            description=(
                "Required name filter (≥2 chars). Prefix always; mid-string only "
                "when kind is set. If the object/module path is already known, "
                "call list_methods with parent_path instead."
            )
        ),
    ] = None,
) -> dict:
    """List code modules matching ``q`` (parents of BSL methods) with method counts.

    Always pass ``q``. Prefer list_methods(parent_path=…) when the module or
    metadata object path is already known from a previous hit.
    """

    def _run():
        try:
            return search_svc.list_code_modules(context, kind=kind, q=q)
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "list_code_modules", _run, context=context, kind=kind, q=q
    )


@mcp.tool(
    name="list_methods",
    annotations={
        "title": "List BSL methods",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_methods(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    parent_path: Annotated[
        str | None,
        Field(
            description=(
                "Optional module/object path from a previous hit "
                "(e.g. ОбщиеМодули.ИмяМодуля). Do not invent a path."
            )
        ),
    ] = None,
    q: Annotated[str | None, Field(description="Substring filter on name/signature/doc/path")] = None,
    export_only: Annotated[bool, Field(description="Only exported methods")] = False,
    limit: Annotated[int, Field(description="Max rows (<=200)", ge=1, le=200)] = 50,
) -> dict:
    """List BSL methods of the context (optionally scoped to a module).

    q is substring-only — no fuzzy/NL fallback; for a topic/intent query
    (not an exact name fragment) use find_methods instead.
    """

    def _run():
        try:
            return search_svc.list_methods(
                context,
                parent_path=parent_path,
                q=q,
                export_only=export_only,
                limit=limit,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "list_methods",
        _run,
        context=context,
        parent_path=parent_path,
        q=q,
        export_only=export_only,
        limit=limit,
    )


@mcp.tool(
    name="find_methods",
    annotations={
        "title": "Find BSL methods (exact then fuzzy)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def find_methods(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    query: Annotated[str, Field(description="Method name / signature / intent")],
    parent_path: Annotated[
        str | None,
        Field(
            description=(
                "Optional module/object path already seen in a hit. "
                "Do not invent a path. If the response has parent_not_found, "
                "call search_metadata — do not retry find_methods with another guess."
            )
        ),
    ] = None,
    export_only: Annotated[bool, Field(description="Only exported methods")] = False,
    limit: Annotated[int, Field(description="Max rows (<=200)", ge=1, le=200)] = 50,
) -> dict:
    """Find methods: SQL exact/prefix first; fuzzy (zvec) when empty or weak phrase hits.

    Use fuzzy for stem/NL recall. parent_path only if taken from a previous hit.
    Terms matching the method NAME rank above terms found only in parameters/
    signature — if the target likely has a generic/verb-like name, narrow with
    parent_path instead of expecting a bare NL query to surface it.
    """

    def _run():
        try:
            return search_svc.find_methods(
                context,
                query,
                parent_path=parent_path,
                export_only=export_only,
                limit=limit,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "find_methods",
        _run,
        context=context,
        query=query,
        parent_path=parent_path,
        export_only=export_only,
        limit=limit,
    )


@mcp.tool(
    name="get_method",
    annotations={
        "title": "Get BSL method details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_method(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[
        str,
        Field(description="Method path, e.g. ОбщиеМодули.Х.Методы.Module.ИмяМетода"),
    ],
) -> dict:
    """Return a BSL method with signature/doc; body from index (code/full) or dump file."""

    def _run():
        try:
            return search_svc.get_method(context, path)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_method", _run, context=context, path=path)


@mcp.tool(
    name="get_module_structure",
    annotations={
        "title": "Get BSL module structure",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_module_structure(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    module_path: Annotated[
        str,
        Field(description="Module path, e.g. ОбщиеМодули.ИмяМодуля"),
    ],
) -> dict:
    """Structure of a code module: methods (line/region/signature), regions, exports."""

    def _run():
        try:
            return search_svc.get_module_structure(context, module_path)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_module_structure", _run, context=context, module_path=module_path)


@mcp.tool(
    name="find_code_references",
    annotations={
        "title": "Find name references in method bodies",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def find_code_references(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    name: Annotated[str, Field(description="Identifier to find in BSL method bodies")],
    parent_path: Annotated[
        str | None,
        Field(description="Optional filter by module path prefix, e.g. ОбщиеМодули.X"),
    ] = None,
    limit: Annotated[int, Field(description="Max hits", ge=1, le=100)] = 100,
    offset: Annotated[int, Field(description="Offset for pagination", ge=0)] = 0,
) -> dict:
    """Where a name/identifier appears in method bodies (index code/full, or dump files)."""

    def _run():
        try:
            return search_svc.find_code_references(
                context,
                name,
                limit=limit,
                offset=offset,
                parent_path=parent_path,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "find_code_references",
        _run,
        context=context,
        name=name,
        parent_path=parent_path,
        limit=limit,
        offset=offset,
    )


@mcp.tool(
    name="get_skd",
    annotations={
        "title": "Get data composition schema structure",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_skd(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[
        str,
        Field(description="Path to DCS template object, e.g. …Макеты.ИмяСКД"),
    ],
) -> dict:
    """Structured DCS extraction: data sets, fields, parameters, settings variants."""

    def _run():
        try:
            return search_svc.get_skd(context, path)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_skd", _run, context=context, path=path)


@mcp.tool(
    name="compare_objects",
    annotations={
        "title": "Compare two metadata objects",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def compare_objects(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path_a: Annotated[str, Field(description="First object path, e.g. Документы.ЗаказНаПроизводство")],
    path_b: Annotated[
        str,
        Field(description="Second object path, e.g. Документы.ЭтапПроизводства2_2"),
    ],
) -> dict:
    """Light side-by-side of two roots: attrs / tabular sections / movements / help titles.

    Use for «в чём разница между A и B». Diff is name-level only; open get_object
    for types/FillChecking or get_skd for report layouts.
    """

    def _run():
        try:
            return search_svc.compare_objects(context, path_a, path_b)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("compare_objects", _run, context=context, path_a=path_a, path_b=path_b)


@mcp.tool(
    name="search_forms",
    annotations={
        "title": "Search managed forms (structure in props)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def search_forms(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    query: Annotated[
        str,
        Field(description="Form name or UI element/command/attribute text"),
    ],
    path_prefix: Annotated[
        str | None,
        Field(description="Optional owner prefix, e.g. Документы.ЗаказКлиента"),
    ] = None,
    limit: Annotated[int, Field(description="Max rows", ge=1, le=100)] = 20,
    compact: Annotated[bool, Field(description="Compact hits")] = True,
) -> dict:
    """Search ManagedForm objects by name/path and form structure (props_json).

    Structure comes from Form.xml summary at ingest. For FTS/semantic on the same
    text after a code upgrade, call queue_managed_form_reembed then wait for index.
    """

    def _run():
        try:
            return search_svc.search_forms(
                context,
                query,
                path_prefix=path_prefix,
                limit=limit,
                compact=compact,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track(
        "search_forms",
        _run,
        context=context,
        query=query,
        path_prefix=path_prefix,
        limit=limit,
    )


@mcp.tool(
    name="queue_managed_form_reembed",
    annotations={
        "title": "Re-embed ManagedForm texts only",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def queue_managed_form_reembed(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
) -> dict:
    """Mark ManagedForm rows for re-embed and resume indexing (Gap 3).

    Use after upgrading form_structure_text / meta_text so zvec FTS sees UI
    element names. Does not re-parse Form.xml or touch other kinds.
    """

    def _run():
        try:
            return search_svc.queue_managed_form_reembed(context)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("queue_managed_form_reembed", _run, context=context)


@mcp.tool(
    name="queue_method_reembed",
    annotations={
        "title": "Re-embed Procedure/Function texts only",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def queue_method_reembed(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
) -> dict:
    """Mark Procedure/Function rows for re-embed and resume indexing.

    Use after upgrading meta_text to include props.signature so find_methods
    FTS/vectors see parameter names. Does not re-parse BSL or touch other kinds.
    """

    def _run():
        try:
            return search_svc.queue_method_reembed(context)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("queue_method_reembed", _run, context=context)


@mcp.tool(
    name="get_indexing_status",
    annotations={
        "title": "Indexing status of all entities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_indexing_status(
    context_ref: Annotated[
        str | None,
        Field(description="Optional entity name or tag:Name to filter by"),
    ] = None,
) -> dict:
    """Status of ALL entities (not just ready/enabled, unlike list_contexts)."""

    def _run():
        try:
            return search_svc.get_indexing_status(context_ref)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_indexing_status", _run, context=context_ref or "")
