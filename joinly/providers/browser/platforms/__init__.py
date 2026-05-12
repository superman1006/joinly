from .base import BrowserPlatformController
from .feishu import FeishuBrowserPlatformController
from .google_meet import GoogleMeetBrowserPlatformController
from .teams import TeamsBrowserPlatformController
from .zoom import ZoomBrowserPlatformController

__all__ = [
    "BrowserPlatformController",
    "FeishuBrowserPlatformController",
    "GoogleMeetBrowserPlatformController",
    "TeamsBrowserPlatformController",
    "ZoomBrowserPlatformController",
]
