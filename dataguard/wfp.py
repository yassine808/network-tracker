"""Windows Filtering Platform: per-app block filters added straight to the platform.

netsh rules are stored by the Windows Firewall service, but on machines where that
service never programs the filter engine (seen with third-party firewalls in control)
the rules exist and never take effect. Filters added with FwpmFilterAdd0 are enforced by
the platform itself. Adding goes through firewall._apply (one UAC yes when not admin);
the read-only check below never prompts."""

import ctypes
import logging
import sys
import uuid

# Our sublayer: weight 0xFFFF, so it is evaluated before every third-party sublayer
# (Avast's sit at 8-14, the built-ins at most 49152). Filters carry FWPM_FILTER_FLAG_
# PERSISTENT so they survive a reboot without re-applying.
SUBLAYER_KEY = uuid.UUID("af17aa61-e40a-5006-8b73-f10f178a57e7")
_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_FWPM_E_FILTER_NOT_FOUND = 0x80320003
_ERROR_ACCESS_DENIED = 0x5
_RPC_C_AUTHN_DEFAULT = 0xFFFFFFFF

CS = r'''
using System;
using System.Runtime.InteropServices;

public static class WfpB
{
    [StructLayout(LayoutKind.Explicit, Size = 16)]
    public struct FWP_VALUE0
    {
        [FieldOffset(0)] public uint type;
        [FieldOffset(8)] public IntPtr ptr;
        [FieldOffset(8)] public ushort u16;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct FWP_BYTE_BLOB { public uint size; public IntPtr data; }

    [StructLayout(LayoutKind.Sequential)]
    public struct FWPM_DISPLAY_DATA0 { public IntPtr name; public IntPtr description; }

    [StructLayout(LayoutKind.Explicit, Size = 40)]
    public struct FWPM_FILTER_CONDITION0
    {
        [FieldOffset(0)] public Guid fieldKey;
        [FieldOffset(16)] public uint matchType;
        [FieldOffset(24)] public FWP_VALUE0 conditionValue;
    }

    [StructLayout(LayoutKind.Explicit, Size = 20)]
    public struct FWPM_ACTION0
    {
        [FieldOffset(0)] public uint type;
        [FieldOffset(4)] public Guid guid;
    }

    [StructLayout(LayoutKind.Explicit, Size = 16)]
    public struct FWPM_CTX_UNION
    {
        [FieldOffset(0)] public ulong rawContext;
        [FieldOffset(0)] public Guid providerContextKey;
    }

    [StructLayout(LayoutKind.Explicit, Size = 200)]
    public struct FWPM_FILTER0
    {
        [FieldOffset(0)] public Guid filterKey;
        [FieldOffset(16)] public FWPM_DISPLAY_DATA0 displayData;
        [FieldOffset(32)] public uint flags;
        [FieldOffset(40)] public IntPtr providerKey;
        [FieldOffset(48)] public FWP_BYTE_BLOB providerData;
        [FieldOffset(64)] public Guid layerKey;
        [FieldOffset(80)] public Guid subLayerKey;
        [FieldOffset(96)] public FWP_VALUE0 weight;
        [FieldOffset(112)] public uint numFilterConditions;
        [FieldOffset(120)] public IntPtr filterCondition;
        [FieldOffset(128)] public FWPM_ACTION0 action;
        [FieldOffset(152)] public FWPM_CTX_UNION ctx;
        [FieldOffset(168)] public IntPtr reserved;
        [FieldOffset(176)] public ulong filterId;
        [FieldOffset(184)] public FWP_VALUE0 effectiveWeight;
    }

    [StructLayout(LayoutKind.Explicit, Size = 72)]
    public struct FWPM_SUBLAYER0
    {
        [FieldOffset(0)] public Guid subLayerKey;
        [FieldOffset(16)] public FWPM_DISPLAY_DATA0 displayData;
        [FieldOffset(32)] public uint flags;
        [FieldOffset(40)] public IntPtr providerKey;
        [FieldOffset(48)] public FWP_BYTE_BLOB providerData;
        [FieldOffset(64)] public ushort weight;
    }

    public static readonly Guid APP_ID = new Guid("d78e1e87-8644-4ea5-9437-d809ecefc971");
    public static readonly Guid LAYER_V4 = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82");
    public static readonly Guid LAYER_V6 = new Guid("4a72393b-319f-44bc-84c3-ba54dcb3b6b4");
    const int ALREADY_EXISTS = unchecked((int)0x80320009);
    const int NOT_FOUND = unchecked((int)0x80320003);

    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    static extern int FwpmEngineOpen0(string serverName, uint authnService, IntPtr authIdentity, IntPtr session, out IntPtr engineHandle);

    [DllImport("fwpuclnt.dll")]
    static extern int FwpmEngineClose0(IntPtr engineHandle);

    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    static extern int FwpmGetAppIdFromFileName0(string fileName, out IntPtr appId);

    [DllImport("fwpuclnt.dll")]
    static extern int FwpmSubLayerAdd0(IntPtr engineHandle, ref FWPM_SUBLAYER0 subLayer, IntPtr sd);

    [DllImport("fwpuclnt.dll")]
    static extern int FwpmFilterAdd0(IntPtr engineHandle, ref FWPM_FILTER0 filter, IntPtr sd, out ulong id);

    [DllImport("fwpuclnt.dll")]
    static extern int FwpmFilterDeleteByKey0(IntPtr engineHandle, ref Guid filterKey);

    [DllImport("fwpuclnt.dll")]
    static extern int FwpmFilterGetByKey0(IntPtr engineHandle, ref Guid filterKey, out IntPtr filter);

    [DllImport("fwpuclnt.dll")]
    static extern void FwpmFreeMemory0(ref IntPtr memory);

    static byte[] AppId(string path)
    {
        IntPtr blob;
        int e = FwpmGetAppIdFromFileName0(path, out blob);
        if (e != 0 || blob == IntPtr.Zero)
            throw new Exception("path not readable (0x" + e.ToString("X8") + ")");
        int size = Marshal.ReadInt32(blob, 0);
        IntPtr data = Marshal.ReadIntPtr(blob, 8);
        byte[] b = new byte[size];
        Marshal.Copy(data, b, 0, size);
        FwpmFreeMemory0(ref blob);
        return b;
    }

    static int SubLayerEnsure(IntPtr h, Guid key)
    {
        var sl = new FWPM_SUBLAYER0();
        sl.subLayerKey = key;
        sl.displayData.name = Marshal.StringToHGlobalUni("DataGuard block filters");
        sl.displayData.description = Marshal.StringToHGlobalUni("DataGuard block filters");
        sl.flags = 0x1;          // FWPM_SUBLAYER_FLAG_PERSISTENT
        sl.weight = 0xFFFF;      // before every third-party sublayer
        try
        {
            int e = FwpmSubLayerAdd0(h, ref sl, IntPtr.Zero);
            return (e == 0 || e == ALREADY_EXISTS) ? 0 : e;
        }
        finally
        {
            Marshal.FreeHGlobal(sl.displayData.name);
            Marshal.FreeHGlobal(sl.displayData.description);
        }
    }

    static int FilterDelete(IntPtr h, Guid key)
    {
        int e = FwpmFilterDeleteByKey0(h, ref key);
        return (e == 0 || e == NOT_FOUND) ? 0 : e;
    }

    static bool FilterPresent(IntPtr h, Guid key)
    {
        IntPtr p;
        int e = FwpmFilterGetByKey0(h, ref key, out p);
        if (e == 0) { FwpmFreeMemory0(ref p); return true; }
        return false;
    }

    static int FilterAdd(IntPtr h, Guid layer, Guid sublayer, Guid filterKey, byte[] appId, string name)
    {
        IntPtr blobMem = Marshal.AllocHGlobal(16);
        IntPtr dataMem = Marshal.AllocHGlobal(appId.Length);
        IntPtr condMem = Marshal.AllocHGlobal(40);
        IntPtr nameMem = Marshal.StringToHGlobalUni(name);
        try
        {
            Marshal.Copy(appId, 0, dataMem, appId.Length);
            Marshal.WriteInt32(blobMem, 0, appId.Length);
            Marshal.WriteIntPtr(blobMem, 8, dataMem);

            var cond = new FWPM_FILTER_CONDITION0();
            cond.fieldKey = APP_ID;
            cond.matchType = 0;          // FWP_MATCH_EQUAL
            cond.conditionValue.type = 12; // FWP_BYTE_BLOB_TYPE
            cond.conditionValue.ptr = blobMem;
            Marshal.StructureToPtr(cond, condMem, false);

            var f = new FWPM_FILTER0();
            f.filterKey = filterKey;
            f.displayData.name = nameMem;
            f.displayData.description = nameMem;
            f.flags = 0x1;               // FWPM_FILTER_FLAG_PERSISTENT
            f.layerKey = layer;
            f.subLayerKey = sublayer;
            f.weight.type = 0;           // FWP_EMPTY: explicit weights must be FWP_UINT64
            f.numFilterConditions = 1;
            f.filterCondition = condMem;
            f.action.type = 0x1001;      // FWP_ACTION_BLOCK
            ulong filterId;              // declared up here: PS 5.1's Add-Type only knows C# 5
            return FwpmFilterAdd0(h, ref f, IntPtr.Zero, out filterId);
        }
        finally
        {
            Marshal.FreeHGlobal(condMem);
            Marshal.FreeHGlobal(blobMem);
            Marshal.FreeHGlobal(dataMem);
            Marshal.FreeHGlobal(nameMem);
        }
    }

    public static int Block(string exe, string sl, string k4, string k6, out string msg)
    {
        msg = "";
        IntPtr h = IntPtr.Zero;
        try
        {
            int e = FwpmEngineOpen0(null, 0xFFFFFFFF, IntPtr.Zero, IntPtr.Zero, out h);
            if (e != 0) { msg = "the Windows filter platform could not be opened (0x" + e.ToString("X8") + ")"; return 1; }
            byte[] app;
            try { app = AppId(exe); }
            catch (Exception ex) { msg = "the program file could not be resolved (" + ex.Message + ")"; return 1; }
            Guid sub = Guid.Parse(sl);
            e = SubLayerEnsure(h, sub);
            if (e != 0) { msg = "Windows refused the DataGuard filter section (0x" + e.ToString("X8") + ")"; return 1; }
            Guid[] layers = { LAYER_V4, LAYER_V6 };
            Guid[] keys = { Guid.Parse(k4), Guid.Parse(k6) };
            for (int i = 0; i < 2; i++)
            {
                e = FilterDelete(h, keys[i]);
                if (e != 0) { msg = "the old block filter could not be removed (0x" + e.ToString("X8") + ")"; return 1; }
                e = FilterAdd(h, layers[i], sub, keys[i], app, "DataGuard block " + System.IO.Path.GetFileName(exe));
                if (e != 0) { msg = "Windows refused the block filter (0x" + e.ToString("X8") + ")"; return 1; }
                if (!FilterPresent(h, keys[i])) { msg = "Windows did not keep the block filter"; return 1; }
            }
            return 0;
        }
        catch (Exception ex) { msg = ex.Message; return 1; }
        finally { if (h != IntPtr.Zero) FwpmEngineClose0(h); }
    }

    public static int Unblock(string k4, string k6, out string msg)
    {
        msg = "";
        IntPtr h = IntPtr.Zero;
        try
        {
            int e = FwpmEngineOpen0(null, 0xFFFFFFFF, IntPtr.Zero, IntPtr.Zero, out h);
            if (e != 0) { msg = "the Windows filter platform could not be opened (0x" + e.ToString("X8") + ")"; return 1; }
            foreach (Guid key in new[] { Guid.Parse(k4), Guid.Parse(k6) })
            {
                e = FilterDelete(h, key);
                if (e != 0) { msg = "removing the block filter failed (0x" + e.ToString("X8") + ")"; return 1; }
                if (FilterPresent(h, key)) { msg = "Windows kept the block filter"; return 1; }
            }
            return 0;
        }
        catch (Exception ex) { msg = ex.Message; return 1; }
        finally { if (h != IntPtr.Zero) FwpmEngineClose0(h); }
    }
}
'''


def _keys(rule):
    """Stable filter keys for a block rule: the same on every call, so add, remove and
    verify all talk about the same two filters (one per IP family)."""
    return (uuid.uuid5(_NS, "dataguard/wfp-block/" + rule + "/v4"),
            uuid.uuid5(_NS, "dataguard/wfp-block/" + rule + "/v6"))


def _q(s):
    return s.replace("'", "''")


def _script(body):
    return "Add-Type -TypeDefinition @'\n" + CS + "'@\n" + body


def add_script(exe, rule):
    """PowerShell source for firewall._apply: install both persistent block filters."""
    k4, k6 = _keys(rule)
    return _script(
        "$msg = ''\n"
        "$e = [WfpB]::Block('%s', '%s', '%s', '%s', [ref]$msg)\n"
        "if ($e -ne 0) { Write-Output $msg; exit 7 }\n"
        % (_q(exe), _q(str(SUBLAYER_KEY)), _q(str(k4)), _q(str(k6))))


def remove_script(rule):
    """PowerShell source for firewall._apply: drop both block filters (gone = fine)."""
    k4, k6 = _keys(rule)
    return _script(
        "$msg = ''\n"
        "$e = [WfpB]::Unblock('%s', '%s', [ref]$msg)\n"
        "if ($e -ne 0) { Write-Output $msg; exit 7 }\n"
        % (_q(str(k4)), _q(str(k6))))


def _guid(u):
    from ctypes import wintypes  # Windows-only; imported late so other platforms can import this module

    class G(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    g = G()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    g.Data4[:] = u.bytes[8:]
    return g


def present(rule):
    """True = the block filter was found; False = it is definitively absent; None = it
    cannot be told (a non-admin may read absent keys but gets access-denied on filters an
    elevated writer created, so "denied" means "it is there"). Read-only; never prompts."""
    if not sys.platform.startswith("win"):
        return False
    try:
        fw = ctypes.WinDLL("fwpuclnt")
        h = ctypes.c_void_p()
        r = fw.FwpmEngineOpen0(None, _RPC_C_AUTHN_DEFAULT, None, None, ctypes.byref(h))
        if r:
            logging.warning("wfp: engine open failed 0x%08X", r & 0xFFFFFFFF)
            return None
        try:
            key = _guid(_keys(rule)[0])
            ptr = ctypes.c_void_p()
            r = fw.FwpmFilterGetByKey0(h, ctypes.byref(key), ctypes.byref(ptr))
            if r == 0:
                fw.FwpmFreeMemory0(ctypes.byref(ptr))
                return True
            if (r & 0xFFFFFFFF) == _FWPM_E_FILTER_NOT_FOUND:
                return False
            if (r & 0xFFFFFFFF) == _ERROR_ACCESS_DENIED:
                return None
            logging.warning("wfp: filter lookup failed 0x%08X", r & 0xFFFFFFFF)
            return None
        finally:
            fw.FwpmEngineClose0(h)
    except OSError as e:
        logging.warning("wfp: cannot read filter state: %s", e)
        return None
