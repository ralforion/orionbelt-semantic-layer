"""Which data source a model's queries run on.

A model names its source in ``settings.defaultDataSource``; the server's
environment (``OBSL_DATABASES``, see ``ob_flight.datasources``) says where that
source is. A model that names none runs on the sole source of its dialect.
Every surface that executes a model's query asks :func:`resolve_datasource`,
so REST, Flight SQL, pgwire and the CLI send the same model to the same
database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orionbelt.service.db_executor import ExecutionUnavailableError

if TYPE_CHECKING:
    from ob_flight.datasources import DataSource

    from orionbelt.models.semantic import SemanticModel


def model_datasource_name(model: SemanticModel | None) -> str | None:
    """The data source a model names, if any."""
    settings = model.settings if model is not None else None
    return settings.default_data_source if settings is not None else None


def named_source_dialect(model: SemanticModel | None) -> str | None:
    """The dialect of the configured source the model names, if it names one.

    ``None`` when it names none, or one that is not configured: what to say
    about that is :func:`resolve_datasource`'s to decide.
    """
    name = model_datasource_name(model)
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
        raise ExecutionUnavailableError(str(exc)) from None


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
    """
    name = model_datasource_name(model)
    label = _model_label(model)
    if name is None:
        named = [s for s in configured_datasource_list() if s.named and s.dialect == dialect]
        if len(named) > 1:
            listed = ", ".join(source.name for source in named)
            raise ExecutionUnavailableError(
                f"{label} runs on {dialect}, and {len(named)} {dialect} data sources are "
                f"configured ({listed}). Set settings.defaultDataSource, or load the "
                "model with a datasource."
            )
        return named[0] if named else None

    try:
        from ob_flight.datasources import DataSourceNotFoundError, find_datasource
    except ImportError:
        raise ExecutionUnavailableError(
            f"{label} runs on data source '{name}', but the installed ob-flight-extension "
            "has no named data sources; upgrade it to this OBSL version."
        ) from None
    try:
        source = find_datasource(name, configured_datasource_list())
    except DataSourceNotFoundError as exc:
        raise ExecutionUnavailableError(f"{label}: {exc}") from None
    if source.dialect != dialect:
        raise ExecutionUnavailableError(
            f"{label} runs on data source '{source.name}', which is {source.dialect}, but "
            f"the query was compiled for {dialect}"
        )
    return source


def _model_label(model: Any) -> str:
    name = getattr(model, "name", None)
    return f"Model '{name}'" if name else "The model"
