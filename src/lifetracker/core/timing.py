"""Timing of request handling, for the admin footer and ``Server-Timing``.

``RequestTimingMiddleware`` records when Django started handling each request,
for the ``page_generation_time`` template tag (``core/templatetags/
page_timing.py``) to show in the admin's footer, and adds a ``Server-Timing``
header with the total, which browsers' developer tools show. The footer can't
include what happens after it's rendered (the rest of the template and the
outer middleware), so it's slightly less than the header's figure.
"""

import time

REQUEST_START_ATTR = "lifetracker_start_time"


def elapsed_seconds(request):
    """Seconds since the middleware saw ``request``, or ``None`` if it didn't."""
    start = getattr(request, REQUEST_START_ATTR, None)
    if start is None:
        return None
    return time.perf_counter() - start


class RequestTimingMiddleware:
    """Record each request's start time and add a ``Server-Timing`` header.

    List it first in ``MIDDLEWARE``, so the time covers the other middleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        setattr(request, REQUEST_START_ATTR, time.perf_counter())
        response = self.get_response(request)
        duration_ms = elapsed_seconds(request) * 1000
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
        return response
