"""yt-dlp PO token provider backed by the live patchright browser.

Importing this module registers the provider with yt-dlp. It outranks the
bgutil providers, which stay installed as a fallback if the browser can't mint.
"""
from __future__ import annotations

from yt_dlp.extractor.youtube.pot.provider import (
    PoTokenContext,
    PoTokenProvider,
    PoTokenProviderError,
    PoTokenProviderRejectedRequest,
    PoTokenRequest,
    PoTokenResponse,
    register_preference,
    register_provider,
)
from yt_dlp.extractor.youtube.pot.utils import WEBPO_CLIENTS, get_webpo_content_binding

from .browser import BrowserSession

_session: BrowserSession | None = None


def set_session(session: BrowserSession | None):
    global _session
    _session = session


@register_provider
class YtdlBrowserPTP(PoTokenProvider):
    PROVIDER_VERSION = '0.1.0'
    BUG_REPORT_LOCATION = 'ytdl (local project)'

    _SUPPORTED_CLIENTS = WEBPO_CLIENTS
    _SUPPORTED_CONTEXTS = (PoTokenContext.GVS, PoTokenContext.PLAYER, PoTokenContext.SUBS)
    # Tokens are minted by the browser, which has no per-request proxy support.
    _SUPPORTED_EXTERNAL_REQUEST_FEATURES = ()

    def is_available(self) -> bool:
        return _session is not None and _session.context is not None

    def _real_request_pot(self, request: PoTokenRequest) -> PoTokenResponse:
        binding, _ = get_webpo_content_binding(request)
        if not binding:
            raise PoTokenProviderRejectedRequest('No content binding available for this request')
        self.logger.debug(f'Minting {request.context.value} PO token in browser')
        try:
            token, expires_at = _session.mint_po_token_sync(binding)
        except Exception as e:
            raise PoTokenProviderError(f'Browser failed to mint PO token: {e}') from e
        return PoTokenResponse(po_token=token, expires_at=expires_at)


@register_preference(YtdlBrowserPTP)
def ytdl_browser_preference(provider, request):
    return 1000
