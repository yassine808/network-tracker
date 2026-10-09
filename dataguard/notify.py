"""Desktop notifications (fire-and-forget, one per platform)."""

import base64
import logging
import os
import shutil
import subprocess

from .common import APP, IS_MAC, IS_WIN, NO_WINDOW

PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:DG_TITLE)
$m = [Security.SecurityElement]::Escape($env:DG_MSG)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>")
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def notify(title, msg):
    """Fire-and-forget desktop notification; quietly does nothing if the OS can't show one."""
    try:
        kw = dict(env=dict(os.environ, DG_TITLE=title, DG_MSG=msg), stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        if IS_WIN:
            enc = base64.b64encode(PS_TOAST.encode("utf-16-le")).decode()
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                              "-EncodedCommand", enc], **kw)
        elif IS_MAC:
            subprocess.Popen(["osascript", "-e",
                              'display notification (system attribute "DG_MSG") '
                              'with title (system attribute "DG_TITLE")'], **kw)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", APP, title, msg], **kw)
    except Exception as e:
        # fire-and-forget, but never silent: a toast that didn't show explains missing alerts
        logging.warning("notify: could not show %r: %s", title, e)
