"""Which data source a model's queries run on.

A model names its source in ``settings.defaultDataSource``, and a data object
may name its own in ``dataSource``; the server's environment
(``OBSL_DATABASES``, see ``ob_flight.datasources``) says where each source is.
A model that names none runs on the sole source of its dialect. A model whose
data objects are read from several sources runs each query on the source it
reads, or federated across them (``service/federated_executor.py``); only the
REST API does that yet.
Every surface that executes a model's query asks :func:`resolve_datasource`,
so REST, Flight SQL, pgwire and the CLI send the same model to the same
database.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from orionbelt.models.errors import SemanticError
from orionbelt.models.suggestions import suggest_names
from orionbelt.service.db_executor import ExecutionUnavailableError

if TYPE_CHECKING:
    from ob_flight.datasources import DataSource

    from orionbelt.models.semantic import SemanticModel


class FederationNotEnabledError(ExecutionUnavailableError):
    """A model's data objects are read from more than one data source."""


class DataSourceUnavailableError(ExecutionUnavailableError):
    """A data source a model names cannot serve its queries, and what would repair it.

    ``message`` is the finding alone; ``str()`` adds the hint and suggestions
    for the surfaces that show only text. ``context["reason"]`` tells the
    cases apart: ``unknownSource``, ``dialectMismatch``, ``ambiguousSource``,
    ``invalidConfiguration`` or ``extensionOutdated``.
    """

    code = "DATA_SOURCE_UNAVAILABLE"

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any],
        hint: str | None = None,
        suggestions: Sequence[str] = (),
    ) -> None:
        self.message = message
        self.hint = hint
        self.suggestions = list(suggestions)
        self.context = context
        super().__init__(self.as_error().as_text().removeprefix(f"[{self.code}] "))

    def as_error(self, path: str | None = None) -> SemanticError:
        """The finding as a :class:`SemanticError` at ``path``."""
        return SemanticError(
            code=self.code,
            message=self.message,
            path=path,
            hint=self.hint,
            suggestions=self.suggestions,
            context=self.context,
        )


def source_key(name: str) -> str:
    """A data source name as the registry compares it.

    Case, spaces, dashes and underscores do not matter: ``Shop EU`` and
    ``shop-eu`` are one source. The rule is ``ob_flight.datasources``'
    ``_normalized``, kept here so it holds with a driver older than it; a
    test checks the two agree.
    """
    return re.sub(r"[\s_\-]+", "", name.casefold())


def model_datasource_name(model: SemanticModel | None) -> str | None:
    """The data source a model's data objects are read from, if they name one.

    Each object's ``dataSource``, else ``settings.defaultDataSource``. An
    object read through ``nestedIn`` comes from its parent's table and has no
    source of its own.

    Raises:
        FederationNotEnabledError: The objects are read from more than one
            source; an object naming none counts as the dialect's own.
    """
    if model is None:
        return None
    default = model.settings.default_data_source if model.settings else None
    # Grouped as the registry compares names, shown as first written.
    readers: dict[str | None, tuple[str | None, list[str]]] = {}
    for name, obj in model.data_objects.items():
        if obj.nested_in is None:
            source = obj.data_source or default
            key = source_key(source) if source is not None else None
            readers.setdefault(key, (source, []))[1].append(name)
    if len(readers) > 1:
        listed = "; ".join(
            f"{source or 'no data source'}: {', '.join(objects)}"
            for source, objects in readers.values()
        )
        raise FederationNotEnabledError(
            f"[FEDERATION_NOT_ENABLED] {_model_label(model)} reads its data objects from "
            f"more than one data source ({listed}), and this runs on one. Query "
            "execution runs queries on such a model (query/execute, the one-shot batch, "
            "OBSQL, Flight SQL, pgwire, obsl execute); here, give every data object the "
            "same data source."
        )
    return next(iter(readers.values()), (default, []))[0]


def datasource_errors(model: SemanticModel, execution_dialect: str) -> list[SemanticError]:
    """Where queries execute, the data sources a model names that cannot serve it.

    One error per offending path: a name that is not configured, or, for a model
    read from one source, a source of another dialect than the model's
    (``defaultDialect``, else ``execution_dialect``). A model read from
    several sources loads; each of its queries runs on the sources it reads.
    """
    named = {}
    if model.settings and model.settings.default_data_source:
        named["settings.defaultDataSource"] = model.settings.default_data_source
    for name, obj in model.data_objects.items():
        if obj.data_source:
            named[f"dataObjects.{name}.dataSource"] = obj.data_source
    if not named:
        return []
    try:
        single = model_datasource_name(model)
    except FederationNotEnabledError:
        single = None
    if single is not None:
        dialect = (model.settings.default_dialect if model.settings else None) or execution_dialect
        try:
            resolve_datasource(model, dialect)
        except DataSourceUnavailableError as exc:
            path = next(p for p, n in named.items() if source_key(n) == source_key(single))
            return [exc.as_error(path)]
        return []
    try:
        from ob_flight.datasources import DataSourceNotFoundError, find_datasource
    except ImportError:
        return []
    try:
        sources = configured_datasource_list()
    except DataSourceUnavailableError as exc:
        return [exc.as_error(next(iter(named)))]
    errors = []
    for path, name in named.items():
        try:
            find_datasource(name, sources)
        except DataSourceNotFoundError as exc:
            errors.append(_unknown_source(model, name, sources, exc).as_error(path))
    return errors


def named_source_dialect(model: SemanticModel | None) -> str | None:
    """The dialect of the configured source the model names, if it names one.

    ``None`` when it names none, or one that is not configured: what to say
    about that is :func:`resolve_datasource`'s to decide.
    """
    try:
        return source_dialect(model_datasource_name(model))
    except FederationNotEnabledError:
        return None


def source_dialect(name: str | None) -> str | None:
    """The dialect of the configured data source *name*; ``None`` if unknown here."""
    if name is None:
        return None
    try:
        from ob_flight.datasources import DataSourceNotFoundError, find_datasource
    except ImportError:
        return None
    try:
        return find_datasource(name, configured_datasource_list()).dialect
    except (DataSourceNotFoundError, ExecutionUnavailableError):
        return None


def configured_datasource_list() -> list[DataSource]:
    """Every configured data source; none when the driver extension predates them.

    Raises:
        ExecutionUnavailableError: The data sources in the environment are not
            usable as written.
    """
    try:
        from ob_flight.datasources import DataSourceConfigError, configured_datasources
    except ImportError:
        return []
    try:
        return configured_datasources()
    except DataSourceConfigError as exc:
        raise DataSourceUnavailableError(
            str(exc), context={"reason": "invalidConfiguration"}
        ) from None


def describe_datasources() -> list[dict[str, Any]]:
    """The configured data sources, as much of each as may be shown.

    Name, dialect, description and ``target``: the database, catalog, schema
    or project it reads. Never a host, user, password or token.

    Raises:
        ExecutionUnavailableError: The data sources in the environment are not
            usable as written.
    """
    described = []
    for source in configured_datasource_list():
        shown = source.describe()
        described.append(
            {
                "name": shown.pop("name"),
                "dialect": shown.pop("dialect"),
                "description": shown.pop("description", None),
                "target": shown,
            }
        )
    return described


def resolve_datasource(model: SemanticModel | None, dialect: str) -> DataSource | None:
    """The data source a model's query compiled for ``dialect`` runs on.

    The source the model names, else the sole named source of ``dialect``.
    ``None`` when the dialect has no named source: the query then runs on the
    dialect's own variables, as every query did before named sources, and
    needs nothing of the data-source API that an older published
    ``ob-flight-extension`` lacks.

    Raises:
        ExecutionUnavailableError: The named source is not configured or is of
            another dialect, or several sources of ``dialect`` are configured
            and the model names none of them.
        FederationNotEnabledError: The model's data objects are read from more
            than one source.
    """
    return resolve_named_source(model_datasource_name(model), dialect, model)


def resolve_query_source(model: SemanticModel | None, result: Any) -> DataSource | None:
    """The data source a compiled single-source query runs on.

    A model read from several data sources runs each query on the source it
    reads (``result.data_source``); any other model on its own source, as
    :func:`resolve_datasource` finds it.

    Raises:
        ExecutionUnavailableError: As :func:`resolve_datasource`.
    """
    if reads_several_sources(model):
        return resolve_named_source(result.data_source, result.dialect, model)
    return resolve_datasource(model, result.dialect)


def reads_several_sources(model: SemanticModel | None) -> bool:
    """Whether a model's data objects are read from more than one data source."""
    try:
        model_datasource_name(model)
    except FederationNotEnabledError:
        return True
    return False


def resolve_named_source(
    name: str | None, dialect: str, model: SemanticModel | None = None
) -> DataSource | None:
    """The configured data source *name*, for a query compiled for ``dialect``.

    *name* as a model writes it; ``None`` for the sole named source of
    ``dialect``, else none (the dialect's own variables). *model* only names
    the model in errors.

    Raises:
        ExecutionUnavailableError: As :func:`resolve_datasource`.
    """
    label = _model_label(model)
    if name is None:
        named = [s for s in configured_datasource_list() if s.named and s.dialect == dialect]
        if len(named) > 1:
            names = [source.name for source in named]
            raise DataSourceUnavailableError(
                f"{label} runs on {dialect}, and {len(named)} {dialect} data sources are "
                f"configured ({', '.join(names)}). Set settings.defaultDataSource, or load "
                "the model with a datasource.",
                context={"reason": "ambiguousSource", "dialect": dialect, "dataSources": names},
            )
        return named[0] if named else None

    try:
        from ob_flight.datasources import DataSourceNotFoundError, find_datasource
    except ImportError:
        raise DataSourceUnavailableError(
            f"{label} runs on data source '{name}', but the installed ob-flight-extension "
            "has no named data sources; upgrade it to this OBSL version.",
            context={"reason": "extensionOutdated", "dataSource": name},
        ) from None
    sources = configured_datasource_list()
    try:
        source = find_datasource(name, sources)
    except DataSourceNotFoundError as exc:
        raise _unknown_source(model, name, sources, exc) from None
    if source.dialect != dialect:
        raise DataSourceUnavailableError(
            f"{label} runs on data source '{source.name}', which is {source.dialect}, but "
            f"the query was compiled for {dialect}",
            hint=(
                f"Compile for {source.dialect} (settings.defaultDialect), or name a "
                f"{dialect} data source."
            ),
            context={
                "reason": "dialectMismatch",
                "dataSource": source.name,
                "sourceDialect": source.dialect,
                "dialect": dialect,
            },
        )
    return source


def _unknown_source(
    model: Any, name: str, sources: Sequence[DataSource], exc: LookupError
) -> DataSourceUnavailableError:
    """The error for a data source name no configured source has."""
    configured = [source.name for source in sources]
    return DataSourceUnavailableError(
        f"{_model_label(model)}: {exc}",
        hint="Configure the source in OBSL_DATABASES, or name one that is configured.",
        suggestions=suggest_names(name, dict.fromkeys(configured, ())),
        context={"reason": "unknownSource", "dataSource": name, "dataSources": configured},
    )


def _model_label(model: Any) -> str:
    name = getattr(model, "name", None)
    return f"Model '{name}'" if name else "The model"
