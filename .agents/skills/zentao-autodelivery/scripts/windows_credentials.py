"""Read a Windows generic credential for the backend version probe."""

import ctypes
from ctypes import wintypes


def windows_credential(reference, expected_username):
    prefix = 'windows-dpapi:'
    if not reference.startswith(prefix) or not reference[len(prefix):]:
        raise ValueError('Unsupported Windows credential reference')

    class Credential(ctypes.Structure):
        _fields_ = [
            ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
            ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME),
            ('CredentialBlobSize', wintypes.DWORD), ('CredentialBlob', ctypes.c_void_p),
            ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
            ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    pointer = ctypes.POINTER(Credential)()
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(type(pointer))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    if not advapi.CredReadW(reference[len(prefix):], 1, 0, ctypes.byref(pointer)):
        return None
    try:
        item = pointer.contents
        if item.UserName != expected_username or not item.CredentialBlobSize:
            return None
        password = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode('utf-16-le').rstrip('\0')
        return {'username': item.UserName, 'password': password} if password else None
    finally:
        advapi.CredFree(pointer)
