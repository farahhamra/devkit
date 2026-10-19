"""Optional ERP browser addon. Playwright is loaded only inside a browser worker."""

from .client import browser
from .storage import BrowserContext

__all__ = ["BrowserContext", "browser"]
