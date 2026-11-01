"""Per-process network bytes from ETW's Microsoft-Windows-Kernel-Network provider.

GetProcessIoCounters no longer reports socket traffic: since Windows 8 the process I/O
counters carry file/device bytes only, so a download moves a few hundred KB of "other"
I/O for tens of MB received. The Kernel-Network provider does log every send/receive
with the owning PID; a real-time session needs administrator rights, so `available` can
be False and the caller must fall back.

The same session also listens to Microsoft-Windows-Kernel-Process: its ProcessStart event
carries the image name, so a downloader that lives 300 ms still gets its own row instead
of "pid 1234" (the network events arrive a second or two after the process is gone).

Payload layout: u32 pid, u32 size, then saddr/daddr (4+4 bytes IPv4, 16+16 IPv6) and
sport/dport (network order). Only the first 8 bytes matter for accounting; the addresses
are used to drop loopback, which never reaches the NIC.

Event IDs (opcodes): TCP v4 send/recv 10/11, TCP v6 26/27, UDP v4 42/43, UDP v6 58/59.
18/34 ("Protocol copied data") duplicate the receive event for loopback and are skipped.
"""

import atexit
import ctypes
import logging
import re
import threading
from ctypes import wintypes

from .common import IS_WIN

log = logging.getLogger(__name__)

SESSION = "DataGuardNet"
GUID_KERNEL_NETWORK = (0x7DD42A49, 0x5329, 0x4832,
                       (0x8D, 0xFD, 0x43, 0xD9, 0x79, 0x15, 0x3A, 0x88))
GUID_KERNEL_PROCESS = (0x22FB2CD6, 0x0E7B, 0x422B,
                       (0xA0, 0xC7, 0x2F, 0xAD, 0x1F, 0xD0, 0xE7, 0x16))
PROCESS_KEYWORD = 0x10  # WINEVENT_KEYWORD_PROCESS: ProcessStart/ProcessStop only

DATA_EVENTS = frozenset((10, 11, 26, 27, 42, 43, 58, 59))
IPV6_EVENTS = frozenset((26, 27, 58, 59))

EVENT_TRACE_REAL_TIME_MODE = 0x00000100
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
EVENT_TRACE_CONTROL_STOP = 1
INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", wintypes.ULONG), ("ProviderId", wintypes.ULONG),
                ("HistoricalContext", ctypes.c_ulonglong), ("TimeStamp", ctypes.c_longlong),
                ("Guid", GUID), ("ClientContext", wintypes.ULONG), ("Flags", wintypes.ULONG)]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", WNODE_HEADER), ("BufferSize", wintypes.ULONG),
                ("MinimumBuffers", wintypes.ULONG), ("MaximumBuffers", wintypes.ULONG),
                ("MaximumFileSize", wintypes.ULONG), ("LogFileMode", wintypes.ULONG),
                ("FlushTimer", wintypes.ULONG), ("EnableFlags", wintypes.ULONG),
                ("AgeLimit", wintypes.LONG), ("NumberOfBuffers", wintypes.ULONG),
                ("FreeBuffers", wintypes.ULONG), ("EventsLost", wintypes.ULONG),
                ("BuffersWritten", wintypes.ULONG), ("LogBuffersLost", wintypes.ULONG),
                ("RealTimeBuffersLost", wintypes.ULONG), ("LoggerThreadId", wintypes.HANDLE),
                ("LogFileNameOffset", wintypes.ULONG), ("LoggerNameOffset", wintypes.ULONG)]


class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Id", wintypes.USHORT), ("Version", ctypes.c_ubyte), ("Channel", ctypes.c_ubyte),
                ("Level", ctypes.c_ubyte), ("Opcode", ctypes.c_ubyte), ("Task", wintypes.USHORT),
                ("Keyword", ctypes.c_ulonglong)]


class EVENT_HEADER(ctypes.Structure):
    # 80 bytes: the ProcessorTime union sits between the descriptor and the activity id.
    _fields_ = [("Size", wintypes.USHORT), ("HeaderType", wintypes.USHORT), ("Flags", wintypes.USHORT),
                ("EventProperty", wintypes.USHORT), ("ThreadId", wintypes.ULONG),
                ("ProcessId", wintypes.ULONG), ("TimeStamp", ctypes.c_longlong),
                ("ProviderId", GUID), ("EventDescriptor", EVENT_DESCRIPTOR),
                ("ProcessorTime", ctypes.c_ulonglong), ("ActivityId", GUID)]


class ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [("ProcessorIndex", wintypes.USHORT), ("LoggerId", wintypes.USHORT)]


class EVENT_RECORD(ctypes.Structure):
    _fields_ = [("EventHeader", EVENT_HEADER), ("BufferContext", ETW_BUFFER_CONTEXT),
                ("ExtendedDataCount", wintypes.USHORT), ("UserDataLength", wintypes.USHORT),
                ("ExtendedData", ctypes.c_void_p), ("UserData", ctypes.c_void_p),
                ("UserContext", ctypes.c_void_p)]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [("wYear", wintypes.WORD), ("wMonth", wintypes.WORD), ("wDayOfWeek", wintypes.WORD),
                ("wDay", wintypes.WORD), ("wHour", wintypes.WORD), ("wMinute", wintypes.WORD),
                ("wSecond", wintypes.WORD), ("wMilliseconds", wintypes.WORD)]


class TIME_ZONE_INFORMATION(ctypes.Structure):
    _fields_ = [("Bias", wintypes.LONG), ("StandardName", wintypes.WCHAR * 32),
                ("StandardDate", SYSTEMTIME), ("StandardBias", wintypes.LONG),
                ("DaylightName", wintypes.WCHAR * 32), ("DaylightDate", SYSTEMTIME),
                ("DaylightBias", wintypes.LONG)]


class TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", wintypes.ULONG), ("Version", wintypes.ULONG),
                ("ProviderVersion", wintypes.ULONG), ("NumberOfProcessors", wintypes.ULONG),
                ("EndTime", ctypes.c_longlong), ("TimerResolution", wintypes.ULONG),
                ("MaximumFileSize", wintypes.ULONG), ("LogFileMode", wintypes.ULONG),
                ("BuffersWritten", wintypes.ULONG), ("LogInstanceGuid", GUID),
                ("LoggerName", wintypes.LPWSTR), ("LogFileName", wintypes.LPWSTR),
                ("TimeZone", TIME_ZONE_INFORMATION), ("BootTime", ctypes.c_longlong),
                ("PerfFreq", ctypes.c_longlong), ("StartTime", ctypes.c_longlong),
                ("ReservedFlags", wintypes.ULONG), ("BuffersLost", wintypes.ULONG)]


class EVENT_TRACE_LOGFILE(ctypes.Structure):
    # The embedded CurrentEvent is 88 bytes on this SDK: OpenTraceW writes the ETL header
    # at offset 120 (verified against a captured .etl), which puts EventCallback at 424.
    _fields_ = [("LogFileName", wintypes.LPWSTR), ("LoggerName", wintypes.LPWSTR),
                ("CurrentTime", ctypes.c_longlong), ("BuffersRead", wintypes.ULONG),
                ("LogFileMode", wintypes.ULONG), ("CurrentEvent", ctypes.c_byte * 88),
                ("LogfileHeader", TRACE_LOGFILE_HEADER), ("BufferCallback", ctypes.c_void_p),
                ("BufferSize", wintypes.ULONG), ("Filled", wintypes.ULONG),
                ("EventsLost", wintypes.ULONG), ("EventCallback", ctypes.c_void_p),
                ("IsKernelTrace", wintypes.ULONG), ("Context", ctypes.c_void_p)]


def _local_ipv4(data, n, local):
    """True when the event never crossed a wire: loopback, or both ends on this machine.
    A proxy listening on a virtual adapter (e.g. 10.8.0.1:18118) talks to itself locally,
    and the NIC never counts those bytes - counting them too would bill every app twice."""
    if n < 16:
        return True
    s, d = data[8:12], data[12:16]
    if s[0] == 127 or d[0] == 127:
        return True
    return bool(local) and s in local and d in local


def _local_ipv6(data, n, local):
    if n < 40:
        return True
    s, d = data[8:24], data[24:40]
    for addr in (s, d):
        if addr == b"\x00" * 15 + b"\x01":          # ::1
            return True
        if addr[:10] == b"\x00" * 10 + b"\xff\xff" and addr[12] == 127:  # ::ffff:127.x
            return True
    return bool(local) and s in local and d in local


def _proc_start(data, n):
    """(pid, image path) from a Kernel-Process ProcessStart payload, or None.

    The template grows with every Windows build (this machine sends version 4), so nothing
    past the pid is assumed: the image path is the one UTF-16 string in the payload that ends
    in .exe. Two fields are fixed across versions - the pid leads the payload, and the image
    name is the field the message calls its name - so this reads both without pinning the
    rest of a layout that has already changed three times."""
    if n < 12:
        return None
    pid = int.from_bytes(data[0:4], "little")
    if not pid:
        return None
    best = ""
    for m in re.finditer(rb"\.\x00[eE]\x00[xX]\x00[eE]\x00", data):
        start = m.start()
        while start - 2 >= 4 and data[start - 2:start] != b"\x00\x00":
            start -= 2
        end = m.end()
        while end + 1 < n and 32 <= data[end] < 127 and data[end + 1] == 0:
            end += 2
        text = "".join(ch for ch in data[start:end].decode("utf-16-le", "replace")
                       if ch.isprintable()).strip()
        if text.lower().endswith(".exe") and len(text) > len(best):
            best = text
    return (pid, best) if best else None


if IS_WIN:
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.StartTraceW.argtypes = (ctypes.POINTER(wintypes.HANDLE), wintypes.LPCWSTR,
                                    ctypes.c_void_p)
    _advapi.StartTraceW.restype = wintypes.ULONG
    _advapi.EnableTraceEx2.argtypes = (wintypes.HANDLE, ctypes.POINTER(GUID), wintypes.ULONG,
                                       ctypes.c_ubyte, ctypes.c_ulonglong, ctypes.c_ulonglong,
                                       wintypes.ULONG, ctypes.c_void_p)
    _advapi.EnableTraceEx2.restype = wintypes.ULONG
    _advapi.OpenTraceW.argtypes = (ctypes.c_void_p,)
    _advapi.OpenTraceW.restype = ctypes.c_ulonglong
    _advapi.ProcessTrace.argtypes = (ctypes.POINTER(ctypes.c_ulonglong), wintypes.ULONG,
                                     ctypes.c_void_p, ctypes.c_void_p)
    _advapi.ProcessTrace.restype = wintypes.ULONG
    _advapi.ControlTraceW.argtypes = (ctypes.c_ulonglong, wintypes.LPCWSTR, ctypes.c_void_p,
                                      wintypes.ULONG)
    _advapi.ControlTraceW.restype = wintypes.ULONG
    _advapi.CloseTrace.argtypes = (ctypes.c_ulonglong,)
    _advapi.CloseTrace.restype = wintypes.ULONG
else:  # pragma: no cover - the app is Windows-first, but the module must import everywhere
    _advapi = None


def _props_buf():
    size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + (len(SESSION) + 1) * 2
    buf = ctypes.create_string_buffer(size)
    props = ctypes.cast(buf, ctypes.POINTER(EVENT_TRACE_PROPERTIES))
    props.contents.Wnode.BufferSize = size
    props.contents.Wnode.Flags = 0x00020000  # WNODE_FLAG_TRACED_GUID
    props.contents.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
    return buf, props


def _stop_session():
    buf, props = _props_buf()
    return _advapi.ControlTraceW(0, SESSION, props, EVENT_TRACE_CONTROL_STOP)


class NetWatch:
    """A live ETW session that sums Kernel-Network bytes per PID.

    start() returns True when the session is running; take() swaps out the per-PID
    counters collected since the previous call. Thread-safe: the ETW callback runs on
    the ProcessTrace thread.
    """

    def __init__(self):
        self.available = False
        self.error = ""
        self._counters = {}
        self._lock = threading.Lock()
        self._handle = None
        self._props = None
        self._trace = None
        self._thread = None
        self._callback = None
        self._logfile = None
        self._harr = None
        self.resolve = None   # optional callable(pid) -> name; called the first time a pid is seen
        self.names = {}       # pid -> whatever resolve returned (DataGuard: name + create time),
                              # captured at its first event and kept past the process's exit
        self.procs = {}       # pid -> (image name, creation time) from Kernel-Process events:
                              # the only way to name a program that exited before its bytes landed
        self.starts = 0       # ProcessStart events seen (diagnostics)
        self.bad_start = 0    # ProcessStart payloads that did not parse
        self.seen = {}        # provider -> events received (diagnostics)
        self.local = set()    # raw bytes of every address this machine owns; see _local_ipv4
        self.skipped_local = 0  # events dropped because both ends are this machine

    def start(self):
        if not IS_WIN or self.available:
            return self.available
        try:
            _stop_session()  # a crashed earlier run can leave the session behind
            buf, props = _props_buf()
            props.contents.BufferSize = 64
            props.contents.MinimumBuffers = 8
            props.contents.MaximumBuffers = 32
            props.contents.LogFileMode = EVENT_TRACE_REAL_TIME_MODE
            props.contents.FlushTimer = 1
            handle = wintypes.HANDLE()
            rc = _advapi.StartTraceW(ctypes.byref(handle), SESSION, props)
            if rc:
                self.error = "StartTrace rc=%d (needs administrator)" % rc
                return False
            self._handle, self._props = handle, buf
            if not self._enable():
                self._cleanup()
                return False
            if not self._open():
                self._cleanup()
                return False
            self.available = True
            atexit.register(self.stop)
            log.info("netwatch: ETW session %s running (per-PID bytes are exact)", SESSION)
            return True
        except Exception as e:  # a broken ETW must never take the tracker down
            self.error = "%s: %s" % (type(e).__name__, e)
            self._cleanup()
            log.warning("netwatch: could not start the ETW session: %s", self.error)
            return False

    def _enable(self):
        if not self._enable_provider(GUID_KERNEL_NETWORK, 0xFFFFFFFFFFFFFFFF):
            return False  # without the network events there is nothing to count
        if not self._enable_provider(GUID_KERNEL_PROCESS, PROCESS_KEYWORD):
            # names are an improvement, not the measurement: keep counting without them
            log.warning("netwatch: Kernel-Process names unavailable (%s); short-lived "
                        "programs will be filed as 'pid N'", self.error)
            self.error = ""
        return True

    def _enable_provider(self, spec, match):
        class ENABLE_TRACE_PARAMETERS(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("EnableProperty", wintypes.ULONG),
                        ("ControlFlags", wintypes.ULONG), ("SourceId", GUID),
                        ("EnableFilterDesc", ctypes.c_void_p), ("FilterDescCount", wintypes.ULONG)]

        params = ENABLE_TRACE_PARAMETERS()
        params.Version = 1
        guid = GUID(*spec[:3], (ctypes.c_ubyte * 8)(*spec[3]))
        rc = _advapi.EnableTraceEx2(self._handle, ctypes.byref(guid),
                                    EVENT_CONTROL_CODE_ENABLE_PROVIDER, 5,
                                    ctypes.c_ulonglong(match),
                                    ctypes.c_ulonglong(0), 0, ctypes.byref(params))
        if rc:
            self.error = "EnableTraceEx2 rc=%d" % rc
            return False
        return True

    def _open(self):
        logfile = EVENT_TRACE_LOGFILE()
        logfile.LoggerName = SESSION
        logfile.LogFileMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
        self._callback = ctypes.WINFUNCTYPE(None, ctypes.POINTER(EVENT_RECORD))(self._on_event)
        logfile.EventCallback = ctypes.cast(self._callback, ctypes.c_void_p).value
        self._logfile = logfile  # OpenTraceW keeps pointers into it
        trace = _advapi.OpenTraceW(ctypes.byref(logfile))
        if trace == INVALID_HANDLE:
            self.error = "OpenTrace failed (err=%d)" % ctypes.get_last_error()
            return False
        self._trace = trace
        self._harr = (ctypes.c_ulonglong * 1)(trace)
        self._thread = threading.Thread(target=_advapi.ProcessTrace,
                                        args=(self._harr, 1, None, None),
                                        name="dataguard-etw", daemon=True)
        self._thread.start()
        return True

    def _on_event(self, rec):
        e = rec.contents
        if e.EventHeader.ProviderId.Data1 == GUID_KERNEL_PROCESS[0]:
            self.seen["proc"] = self.seen.get("proc", 0) + 1
            if e.EventHeader.EventDescriptor.Id == 1:  # ProcessStart
                self._process_start(e, rec)
            return
        self.seen["net"] = self.seen.get("net", 0) + 1
        evid = e.EventHeader.EventDescriptor.Id
        if evid not in DATA_EVENTS:
            return
        n = e.UserDataLength
        if n < 16 or not e.UserData:
            return
        data = ctypes.string_at(e.UserData, n)
        if (_local_ipv6(data, n, self.local) if evid in IPV6_EVENTS
                else _local_ipv4(data, n, self.local)):
            self.skipped_local += 1
            return
        pid = int.from_bytes(data[0:4], "little")
        size = int.from_bytes(data[4:8], "little")
        if not size:
            return
        if pid not in self.names:
            # A short-lived downloader is gone by the next tick, so its name is taken now.
            if len(self.names) > 4096:
                self.names.clear()
            try:
                self.names[pid] = self.resolve(pid) if self.resolve else None
            except Exception:
                self.names[pid] = None
        with self._lock:
            self._counters[pid] = self._counters.get(pid, 0) + size

    def _process_start(self, e, _rec=None):
        """Remember a process's image name the moment it starts, so its bytes are billed to a
        real app even if it is gone before the network events for them arrive."""
        n = e.UserDataLength
        if n < 12 or not e.UserData:
            return
        got = _proc_start(ctypes.string_at(e.UserData, n), n)
        if got is None:
            self.bad_start += 1
            if self.bad_start <= 3:
                log.warning("netwatch: ProcessStart payload not understood (%d bytes)", n)
            return
        pid, path = got
        name = path.rsplit("\\", 1)[-1] or path
        # the event's own timestamp orders this name against psutil's creation time
        when = e.EventHeader.TimeStamp / 10_000_000.0 - 11644473600.0
        if len(self.procs) > 4096:  # forget long-gone processes rather than grow forever
            self.procs.clear()
        self.procs[pid] = (name, when)
        self.starts += 1

    def take(self):
        """{pid: bytes} collected since the previous call, and reset."""
        with self._lock:
            out = self._counters
            self._counters = {}
        return out

    def _cleanup(self):
        if self._handle is not None:
            try:
                _stop_session()
            except Exception:
                pass
            self._handle = None
        if self._trace is not None:
            try:
                _advapi.CloseTrace(self._trace)
            except Exception:
                pass
            self._trace = None
        self.available = False

    def stop(self):
        if not self.available and self._handle is None:
            return
        self._cleanup()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._counters = {}
        log.info("netwatch: ETW session stopped")
