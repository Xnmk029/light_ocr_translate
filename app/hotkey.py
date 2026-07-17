"""全局快捷键：Win32 RegisterHotKey + Qt 原生事件过滤器，零第三方依赖。

RegisterHotKey(hwnd=NULL) 会把 WM_HOTKEY 投递到当前线程消息队列，
Qt 的 Windows 事件分发器可通过 QAbstractNativeEventFilter 截获。
"""
import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

_user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_NOREPEAT = 0x4000

_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
         "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN}
_VKS = {f"f{i}": 0x6F + i for i in range(1, 13)}
_VKS.update({"space": 0x20, "tab": 0x09, "`": 0xC0})


def _parse(spec: str) -> tuple[int, int]:
    """'ctrl+alt+d' -> (修饰键位掩码, 虚拟键码)"""
    mods, vk = MOD_NOREPEAT, None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _VKS:
            vk = _VKS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"无法识别的按键: {part!r}")
    if vk is None:
        raise ValueError("快捷键缺少主键, 例如 ctrl+alt+d")
    return mods, vk


class GlobalHotkey(QAbstractNativeEventFilter):
    def __init__(self, hotkey_id: int = 1):
        super().__init__()
        self._id = hotkey_id
        self._callback = None
        self._registered = False
        QCoreApplication.instance().installNativeEventFilter(self)

    def register(self, spec: str, callback) -> bool:
        self.unregister()
        mods, vk = _parse(spec)
        if not _user32.RegisterHotKey(None, self._id, mods, vk):
            return False
        self._callback = callback
        self._registered = True
        return True

    def unregister(self) -> None:
        if self._registered:
            _user32.UnregisterHotKey(None, self._id)
            self._registered = False

    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == WM_HOTKEY and msg.wParam == self._id and self._callback:
                self._callback()
                return True, 0
        return False, 0
