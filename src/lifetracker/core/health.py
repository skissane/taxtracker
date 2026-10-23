"""The unauthenticated health check, ``/healthz/``.

CI's deploy job polls it after ``flyctl deploy`` until it reports the commit it
deployed (``scripts/check_deploy.py``). A deploy leaves a stopped Machine
stopped, so without that nothing would boot the new image until someone next
visited, and a failed migration or startup would only show up then.

``docker-entrypoint.sh`` runs ``migrate`` before it starts gunicorn, so any
answer at all means that ran; the view still checks the database answers and
that no migration is unapplied, in case the app is started some other way.

It's on the auth whitelist, so it says no more than the logged-out admin header
already does (the commit), and reports a failure only as a short fixed reason;
the details go to the log. Don't point Fly's ``[[http_service.checks]]`` at it:
the idle watchdog counts every request as activity, so regular checks would
keep the Machine from ever stopping.
"""

import logging

from django.conf import settings
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

logger = logging.getLogger(__name__)


def health_problem():
    """A short reason the app isn't healthy, or ``None`` if it is."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        executor = MigrationExecutor(connection)
        unapplied = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except DatabaseError:
        logger.exception("Health check: database error")
        return "database error"
    except Exception:
        # E.g. the migration loader's InconsistentMigrationHistory: still a
        # fixed JSON answer rather than a 500 page.
        logger.exception("Health check failed")
        return "unhealthy"
    if unapplied:
        logger.error(
            "Health check: unapplied migrations: %s",
            ", ".join(f"{m.app_label}.{m.name}" for m, _ in unapplied),
        )
        return "unapplied migrations"
    return None


@never_cache
@require_safe
def healthz(request):
    """``{"status": "ok", "commit": ...}``, or a 503 with ``"error"`` and why."""
    problem = health_problem()
    body = {"status": "error" if problem else "ok"}
    if problem:
        body["problem"] = problem
    body["commit"] = settings.LIFETRACKER_COMMIT_ID
    return JsonResponse(body, status=503 if problem else 200)
