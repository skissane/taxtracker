from django import template

from lifetracker.core.app_icon import current_icon

register = template.Library()


@register.simple_tag
def app_icon():
    """The current ``app_icon.Icon``, the default or an uploaded one."""
    return current_icon()
