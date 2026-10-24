"""The app icon: the home screen icon, the web app manifest's icon and favicon.

The default is ``core/static/core/app_icon.png`` (512x512, rendered from
``app_icon.svg`` beside it, which is the default favicon). A superuser can
upload a replacement PNG on the App Icon admin page (``core.admin.app_icon_view``);
it's stored in the main database, as the single ``AppIcon`` row, so backups
include it, and resetting to the default deletes that row.

The icon is public, as browsers fetch it without cookies: ``/app-icon.png``
(``app_icon``, on the auth whitelist) serves the uploaded icon, or redirects to
the default if there's none. Links to it carry ``?v=`` and the start of the
image's SHA-256, so a new icon gets a new URL, and a response for the current
version can be cached for a year. iOS copies the icon when the app is added to
the home screen, so it only picks up a change if the app is added again.

PNGs are checked with the standard library rather than Pillow: the chunk
structure, and the IHDR chunk's size and colour type. Nothing decodes the image
data, so a PNG with a valid header but corrupt pixels gets through, which is
harmless: the browser shows it broken, and the page can reset it.
"""

import dataclasses
import datetime
import hashlib

from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.templatetags.static import static
from django.urls import reverse
from django.utils.cache import add_never_cache_headers, patch_cache_control
from django.views.decorators.http import require_safe

from lifetracker.core.models import APP_ICON_PK, AppIcon

DEFAULT_ICON = "core/app_icon.png"
DEFAULT_ICON_SIZE = 512
DEFAULT_FAVICON = "core/app_icon.svg"

# What an uploaded icon must be: square, at least as big as Android's largest
# manifest icon (iOS's is 180x180), and no more than this many bytes.
MIN_SIZE = 512
MAX_BYTES = 1024 * 1024

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# IHDR colour types with an alpha channel: greyscale + alpha, RGB + alpha.
_ALPHA_COLOR_TYPES = {4, 6}
_YEAR_SECONDS = 365 * 24 * 60 * 60


class InvalidIcon(ValueError):
    """An upload that can't be the icon; the message says why, for the page."""


@dataclasses.dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    # An alpha channel or a tRNS chunk, which iOS fills in with black.
    has_transparency: bool


def png_info(data):
    """``data``'s ``PngInfo``, or ``InvalidIcon`` if it isn't a well-formed PNG."""
    if not data.startswith(_PNG_SIGNATURE):
        raise InvalidIcon("The file isn't a PNG image.")
    offset = len(_PNG_SIGNATURE)
    header = None
    has_trns = False
    while True:
        if offset + 8 > len(data):
            raise InvalidIcon("The PNG image is cut off.")
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        # The chunk's data, then a 4-byte CRC.
        if offset + 12 + length > len(data):
            raise InvalidIcon("The PNG image is cut off.")
        if header is None:
            if chunk_type != b"IHDR" or length != 13:
                raise InvalidIcon("The PNG image has no valid header.")
            header = body
        elif chunk_type == b"tRNS":
            has_trns = True
        elif chunk_type == b"IEND":
            break
        offset += 12 + length
    return PngInfo(
        width=int.from_bytes(header[0:4], "big"),
        height=int.from_bytes(header[4:8], "big"),
        has_transparency=has_trns or header[9] in _ALPHA_COLOR_TYPES,
    )


def check_icon(data):
    """``data``'s ``PngInfo`` if it can be the icon, else ``InvalidIcon``."""
    if len(data) > MAX_BYTES:
        raise InvalidIcon(f"The file is bigger than {MAX_BYTES // 1024} KiB.")
    info = png_info(data)
    if info.width != info.height:
        raise InvalidIcon(f"The image is {info.width}x{info.height}, not square.")
    if info.width < MIN_SIZE:
        raise InvalidIcon(
            f"The image is {info.width}x{info.height}, smaller than "
            f"{MIN_SIZE}x{MIN_SIZE}."
        )
    return info


def save_icon(data):
    """Make the PNG ``data`` the icon, replacing any uploaded one.

    Returns its ``PngInfo``; raises ``InvalidIcon`` (changing nothing) if it
    can't be the icon.
    """
    info = check_icon(data)
    with transaction.atomic():
        reset_icon()
        icon = AppIcon(
            pk=APP_ICON_PK,
            width=info.width,
            height=info.height,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        icon.image.save("app_icon.png", ContentFile(data), save=False)
        icon.save()
    return info


def reset_icon():
    """Go back to the default icon, deleting any uploaded one."""
    # A queryset delete still sends post_delete, which deletes the file.
    AppIcon.objects.filter(pk=APP_ICON_PK).delete()


@dataclasses.dataclass(frozen=True)
class Icon:
    """The current icon, for the page head, the manifest and the admin page."""

    url: str
    width: int
    height: int
    favicon_url: str
    favicon_type: str
    # When it was uploaded, or None for the default.
    uploaded_at: datetime.datetime | None = None


def current_icon():
    """The current ``Icon``: the uploaded one, if any, else the default."""
    uploaded = AppIcon.objects.filter(pk=APP_ICON_PK).first()
    if uploaded is None:
        return Icon(
            url=static(DEFAULT_ICON),
            width=DEFAULT_ICON_SIZE,
            height=DEFAULT_ICON_SIZE,
            favicon_url=static(DEFAULT_FAVICON),
            favicon_type="image/svg+xml",
        )
    url = f"{reverse('app_icon')}?v={uploaded.version}"
    return Icon(
        url=url,
        width=uploaded.width,
        height=uploaded.height,
        favicon_url=url,
        favicon_type="image/png",
        uploaded_at=uploaded.uploaded_at,
    )


@require_safe
def app_icon(request):
    """The uploaded icon, or a redirect to the default if there's none."""
    icon = AppIcon.objects.filter(pk=APP_ICON_PK).first()
    if icon is None:
        response = redirect(static(DEFAULT_ICON))
        # An icon may be uploaded later, at this same URL.
        add_never_cache_headers(response)
        return response
    with icon.image.open("rb") as f:
        response = HttpResponse(f.read(), content_type="image/png")
    if request.GET.get("v") == icon.version:
        patch_cache_control(
            response, public=True, max_age=_YEAR_SECONDS, immutable=True
        )
    else:
        add_never_cache_headers(response)
    return response
