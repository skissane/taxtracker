from django import template

from lifetracker.core.app_icon import Icon, current_icon

register = template.Library()


@register.simple_tag
def app_icon() -> Icon:
    """The current ``app_icon.Icon``, the default or an uploaded one."""
    return current_icon()
