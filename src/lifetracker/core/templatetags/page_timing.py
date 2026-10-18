from django import template

from lifetracker.core.timing import elapsed_seconds

register = template.Library()


@register.simple_tag(takes_context=True)
def page_generation_time(context):
    """Seconds spent on this request so far, as text, e.g. ``0.042``.

    Empty if the context has no request, or ``RequestTimingMiddleware`` didn't
    see it.
    """
    request = context.get("request")
    seconds = None if request is None else elapsed_seconds(request)
    return "" if seconds is None else f"{seconds:.3f}"
