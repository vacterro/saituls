import os
import time
import threading
import collections
from datetime import datetime
import hashlib
import io
import struct
import socket
import re
import urllib.parse
import base64
import sys
import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass

# ==============================================================================
# DEPENDENCY BOUNDARY (T-164 A4)
# ==============================================================================
# Every native dependency is imported through a guard. A resident that cannot
# import pywin32 or Pillow must SAY SO and refuse to start (or report it on
# --check-deps); it must never launch and then die on the first clipboard
# touch, and it must never die during a one-shot command without a reason.
IMPORT_ERRORS = []


def _record_import_error(what, exc):
    IMPORT_ERRORS.append('%s (%s)' % (what, exc))


try:
    import win32clipboard
except Exception as _exc:  # pragma: no cover - only on a Python without pywin32
    win32clipboard = None
    _record_import_error('pywin32 win32clipboard', _exc)


class _Win32ConFallback(object):
    """The handful of win32con constants this module needs, so a missing
    pywin32 degrades into an explicit dependency report instead of a crash."""
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    CF_TEXT = 0x0001
    CF_DIB = 0x0008
    CF_UNICODETEXT = 0x000D
    CF_HDROP = 0x000F


try:
    import win32con
except Exception as _exc:  # pragma: no cover - only on a Python without pywin32
    win32con = _Win32ConFallback()
    _record_import_error('pywin32 win32con', _exc)

try:
    from PIL import ImageGrab, Image
except Exception as _exc:  # pragma: no cover - only on a Python without Pillow
    ImageGrab = None
    Image = None
    _record_import_error('Pillow', _exc)

# Ensure console handles UTF-8 / emojis on Windows without crash
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==============================================================================
# STATE, SETTINGS AND STATUS (T-164 A3 / A7)
# ==============================================================================
# One persistent per-user settings file under the SAITULS per-user state
# directory, written as UTF-16 INI so the SAITULS shell reads the very same
# values with GetPrivateProfileStringW. Nothing here is clipboard content.
STATE_DIR_ENV = 'SAITULS_CLIPBOARD_PLUS_STATE_DIR'
MUTEX_ENV = 'SAITULS_CLIPBOARD_PLUS_MUTEX'
STOP_EVENT_ENV = 'SAITULS_CLIPBOARD_PLUS_STOP_EVENT'
PORT_ENV = 'SAITULS_CLIPBOARD_PLUS_PORT'

DEFAULT_MUTEX_NAME = 'Local\\SaitulsClipboardPlus'
DEFAULT_STOP_EVENT_NAME = 'Local\\SaitulsClipboardPlusStop'

SETTINGS_FILE = 'clipboard-plus.ini'
STATUS_FILE = 'status.ini'
SETTINGS_SECTION = 'clipboard+'
STATUS_SECTION = 'status'

VERSION = '1.0.0'

# Per-user default; existing preferences in clipboard-plus.ini take precedence.
DEFAULT_SAVE_PATH = os.path.join(os.path.expanduser('~'), 'Pictures', 'Clipboard+')
DEFAULT_HOTKEY = 'Ctrl+Alt+Shift+C'
DEFAULT_FALLBACK_PATH = r'%USERPROFILE%\Pictures\Clipboard+'


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def state_dir():
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        base = os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    return os.path.join(base, 'SAITULS', 'clipboard+')


def settings_path():
    return os.path.join(state_dir(), SETTINGS_FILE)


def status_path():
    return os.path.join(state_dir(), STATUS_FILE)


def mutex_name():
    return os.environ.get(MUTEX_ENV) or DEFAULT_MUTEX_NAME


def stop_event_name():
    return os.environ.get(STOP_EVENT_ENV) or DEFAULT_STOP_EVENT_NAME


def control_port():
    """Loopback control channel port. Ownership is the mutex, never the port,
    so a taken port is a reported degradation (commands unavailable) and not a
    reason to kill anything."""
    try:
        return int(os.environ.get(PORT_ENV) or PORT)
    except ValueError:
        return PORT


def expand_path(value):
    """%VAR% and ~ expansion, then absolute. Unicode is preserved as-is."""
    if not value:
        return value
    value = os.path.expandvars(str(value))
    if value.startswith('~'):
        value = os.path.expanduser(value)
    return os.path.abspath(value)


def read_ini(path):
    """Minimal section/key INI reader. Accepts the UTF-16 file this module
    writes, UTF-8 (with or without BOM) and plain ANSI."""
    data = {}
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except Exception:
        return data
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        text = raw.decode('utf-16', 'replace')
    elif raw[:3] == b'\xef\xbb\xbf':
        text = raw.decode('utf-8-sig', 'replace')
    else:
        text = raw.decode('utf-8', 'replace')
    section = ''
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ';#':
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if section and '=' in line:
            key, value = line.split('=', 1)
            data[section][key.strip()] = value.strip()
    return data


def write_ini(path, sections):
    lines = []
    for name, values in sections.items():
        lines.append('[%s]' % name)
        for key, value in values.items():
            lines.append('%s=%s' % (key, str(value).replace('\r', ' ').replace('\n', ' ')))
        lines.append('')
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-16', newline='') as handle:
        handle.write('\r\n'.join(lines))
    os.replace(tmp, path)
    return path


def default_settings():
    return {
        'save_path': DEFAULT_SAVE_PATH,
        'hotkey': DEFAULT_HOTKEY,
        'fallback_enabled': False,
        'fallback_path': expand_path(DEFAULT_FALLBACK_PATH),
    }


def load_settings(path=None):
    path = path or settings_path()
    section = read_ini(path).get(SETTINGS_SECTION, {})
    values = default_settings()
    if section.get('save_path'):
        values['save_path'] = expand_path(section['save_path'])
    if section.get('hotkey'):
        values['hotkey'] = section['hotkey'].strip()
    if section.get('fallback_path'):
        values['fallback_path'] = expand_path(section['fallback_path'])
    values['fallback_enabled'] = section.get('fallback_enabled', '').strip().lower() in ('1', 'true', 'yes', 'on')
    return values


def save_settings(values, path=None):
    path = path or settings_path()
    merged = default_settings()
    merged.update(values or {})
    write_ini(path, {SETTINGS_SECTION: {
        'save_path': merged['save_path'],
        'hotkey': merged['hotkey'],
        'fallback_enabled': '1' if merged['fallback_enabled'] else '0',
        'fallback_path': merged['fallback_path'],
    }})
    return path


def reset_settings(path=None):
    return save_settings(default_settings(), path)


# --- Safe Copy hotkey (A3) ---------------------------------------------------

_HOTKEY_MOD_BITS = {
    'Ctrl': win32con.MOD_CONTROL,
    'Alt': win32con.MOD_ALT,
    'Shift': win32con.MOD_SHIFT,
    'Win': win32con.MOD_WIN,
}
_HOTKEY_MOD_ALIASES = {
    'CTRL': 'Ctrl', 'CONTROL': 'Ctrl', 'ALT': 'Alt', 'SHIFT': 'Shift',
    'WIN': 'Win', 'WINDOWS': 'Win', 'META': 'Win', 'CMD': 'Win',
}


def parse_hotkey(spec):
    """'Ctrl+Alt+Shift+C' -> (modifiers, virtual-key). Raises ValueError with a
    human reason for anything Windows could not register."""
    if not spec or not str(spec).strip():
        raise ValueError('the hotkey is empty')
    parts = [p.strip() for p in re.split(r'[+\-]', str(spec)) if p.strip()]
    if len(parts) < 2:
        raise ValueError('a hotkey needs a modifier and a key, e.g. Ctrl+Alt+Shift+C')
    modifiers = 0
    key = None
    for part in parts:
        alias = _HOTKEY_MOD_ALIASES.get(part.upper())
        if alias:
            modifiers |= _HOTKEY_MOD_BITS[alias]
            continue
        if key is not None:
            raise ValueError('a hotkey may contain only one key')
        upper = part.upper()
        if len(upper) == 1 and (upper.isalpha() or upper.isdigit()):
            key = ord(upper)
        elif re.match(r'^F([1-9]|1[0-9]|2[0-4])$', upper):
            key = 0x70 + int(upper[1:]) - 1
        else:
            raise ValueError('unsupported hotkey key: %s' % part)
    if key is None:
        raise ValueError('a hotkey needs a key, e.g. Ctrl+Alt+Shift+C')
    if modifiers == 0:
        raise ValueError('a hotkey needs at least one modifier')
    return modifiers, key


def format_hotkey(modifiers, vk):
    names = []
    if modifiers & win32con.MOD_CONTROL:
        names.append('Ctrl')
    if modifiers & win32con.MOD_ALT:
        names.append('Alt')
    if modifiers & win32con.MOD_SHIFT:
        names.append('Shift')
    if modifiers & win32con.MOD_WIN:
        names.append('Win')
    if 0x70 <= vk <= 0x87:
        key = 'F%d' % (vk - 0x70 + 1)
    elif 0x30 <= vk <= 0x5A:
        key = chr(vk)
    else:
        key = 'VK%02X' % vk
    return '+'.join(names + [key])


def _ini_value(value):
    if isinstance(value, bool):
        return '1' if value else '0'
    if value is None:
        return ''
    return value


class RuntimeStatus(object):
    """Durable, NON-SECRET runtime state for the SAITULS shell (A7).

    Reports liveness, monitor state, hotkey registration, save-folder
    usability and counters. Never clipboard content, never sanitized secrets.
    """

    FIELDS = ('pid', 'started', 'updated', 'monitor', 'hotkey',
              'hotkey_registered', 'hotkey_error', 'save_path',
              'effective_save_path', 'save_path_ok', 'save_error',
              'fallback_active', 'saved_count', 'unsaved_pending',
              'dedup_entries', 'safe_copy_events', 'restore_available',
              'control_channel', 'control_port', 'control_error',
              'version', 'stopped')

    def __init__(self, path=None, **initial):
        self.path = path or status_path()
        self._lock = threading.Lock()
        self.fields = dict((name, '') for name in self.FIELDS)
        self.fields.update({
            'pid': str(os.getpid()),
            'started': _now(),
            'updated': _now(),
            'monitor': '0',
            'hotkey': DEFAULT_HOTKEY,
            'hotkey_registered': '0',
            'save_path': DEFAULT_SAVE_PATH,
            'save_path_ok': '0',
            'fallback_active': '0',
            'saved_count': '0',
            'unsaved_pending': '0',
            'dedup_entries': '0',
            'safe_copy_events': '0',
            'restore_available': '0',
            'control_channel': 'unknown',
            'control_port': '',
            'control_error': '',
            'version': VERSION,
            'stopped': '0',
        })
        self.update(write=False, **initial)
        self.write()

    def update(self, write=True, **values):
        with self._lock:
            for key, value in values.items():
                if key not in self.fields:
                    self.fields[key] = ''
                self.fields[key] = _ini_value(value)
            self.fields['updated'] = _now()
            if write:
                return self._write_locked()
        return None

    def write(self):
        with self._lock:
            self.fields['updated'] = _now()
            return self._write_locked()

    def _write_locked(self):
        try:
            write_ini(self.path, {STATUS_SECTION: dict(self.fields)})
            return True
        except Exception as exc:
            print('\u26a0\ufe0f Could not write status file %s: %s' % (self.path, exc))
            return False

    def get(self, key, default=''):
        with self._lock:
            return self.fields.get(key, default)

    def as_line(self):
        with self._lock:
            fields = dict(self.fields)
        return ('pid={pid} monitor={monitor} hotkey={hotkey} '
                'hotkey_registered={hotkey_registered} save_path_ok={save_path_ok} '
                'effective_save_path={effective_save_path} saved_count={saved_count} '
                'updated={updated}').format(**fields)


# ==============================================================================
# SAFE SHARE SANITIZER CONFIGURATION
# ==============================================================================

PORT = 49213
HOTKEY_ID = 42
try:
    HOTKEY_MODIFIERS, HOTKEY_VK = parse_hotkey(DEFAULT_HOTKEY)
except ValueError:  # pragma: no cover - the default is parsed and tested
    HOTKEY_MODIFIERS, HOTKEY_VK = (win32con.MOD_CONTROL | win32con.MOD_ALT | win32con.MOD_SHIFT, ord('C'))
HOTKEY_NAME = DEFAULT_HOTKEY

EXACT_CREDENTIAL_KEYS = {
    "token", "access_token", "accesstoken", "refresh_token", "refreshtoken",
    "id_token", "idtoken", "auth", "authorization", "session", "session_id",
    "sessionid", "code", "sig", "signature", "key", "api_key", "apikey",
    "secret", "email", "user_id", "userid", "valid_until", "validuntil",
    "expires", "expiry"
}

TRACKING_PREFIXES = ("utm_", "fb_", "ga_")
EXACT_TRACKING_KEYS = {
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "dclid", "yclid",
    "igshid", "twclid", "ttclid", "sc_src", "sc_lid", "_hsenc", "_hsmi",
    "vero_id", "wickedid", "ref_src", "ref_url", "rb_clickid", "zanpid",
    "ml_subscriber", "ml_subscriber_hash", "trk", "tracking_id"
}

ACTION_KEYWORDS = [
    "unsubscribe", "optout", "opt-out", "opt_out",
    "verify-email", "email-verification", "confirm-email", "email-confirm",
    "verify_email", "confirm_email", "email_verification", "email_confirm",
    "reset-password", "password-reset", "reset_password", "password_reset",
    "magic-login", "magic-link", "magic_login", "magic_link",
    "account-recovery", "recover-account", "account_recovery", "recover_account",
    "activation", "activate",
    "invite", "invitation"
]

TRACKING_HOST_SUBSTRINGS = (
    "click.", "track.", "tracking.", "trk.", "redirect.", "redir.",
    "links.", "s-links.", "link.producthunt.com", "t.co", "bit.ly",
    "tinyurl.com", "ow.ly", "is.gd", "buff.ly"
)

TRACKING_PATH_REGEX = re.compile(
    r'(?i)/(?:click|track|tracking|trk|redirect|redir|s-links)(?:/|$)'
)

PEM_REGEX = re.compile(
    r'-----BEGIN [A-Z0-9_\- ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9_\- ]+PRIVATE KEY-----'
)

BEARER_REGEX = re.compile(
    r'(?i)\bAuthorization:\s*Bearer\s+[A-Za-z0-9\-_.~+/=]+'
)

JWT_REGEX = re.compile(
    r'\bey[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\b'
)

ASSIGNMENT_REGEX = re.compile(
    r'(?i)(?P<prefix>(?:^|[^\w]))(?P<quote1>["\']?)(?P<key>api_key|apikey|access_token|refresh_token|id_token|auth_token|password|secret|client_secret|session_token|session_id|sessionid|token|session)(?P=quote1)\s*(?P<sep>[=:])(?P<space>\s*)(?P<quote2>["\']?)(?P<val>[A-Za-z0-9\-_.~+/=@$!%*#?&]{6,})(?P=quote2)'
)

URL_SCANNER = re.compile(
    r'(?P<md_link>\[(?P<label>[^\]\r\n]*)\]\((?P<md_url>https?://[^\s\)]+)\))|'
    r'(?P<autolink><(?P<auto_url>https?://[^\s>]+)>)|'
    r'(?P<plain_url>https?://[^\s<>\x22\x27{}|\\^`\[\]\)]+)'
)


@dataclass
class SanitizeResult:
    text: str
    urls_changed: int = 0
    urls_removed: int = 0
    secrets_redacted: int = 0
    modified: bool = False

    @property
    def sanitized_text(self) -> str:
        return self.text


def is_credential_param(key: str) -> bool:
    norm = key.lower().replace("-", "_")
    norm_plain = norm.replace("_", "")
    return norm in EXACT_CREDENTIAL_KEYS or norm_plain in EXACT_CREDENTIAL_KEYS


def is_tracking_param(key: str) -> bool:
    k = key.lower()
    if any(k.startswith(p) for p in TRACKING_PREFIXES):
        return True
    return k in EXACT_TRACKING_KEYS or k.replace("-", "_") in EXACT_TRACKING_KEYS


def strip_trailing_punct(url: str) -> tuple[str, str]:
    punct = ""
    while url and url[-1] in ".,;:!?":
        punct = url[-1] + punct
        url = url[:-1]
    while url and url.endswith(")") and url.count("(") < url.count(")"):
        punct = ")" + punct
        url = url[:-1]
    while url and url.endswith("]") and url.count("[") < url.count("]"):
        punct = "]" + punct
        url = url[:-1]
    return url, punct


def try_extract_embedded_url(val: str) -> str | None:
    if not val:
        return None
    val = val.strip()
    candidates = [val]
    try:
        unquoted = urllib.parse.unquote(val)
        if unquoted != val:
            candidates.append(unquoted)
    except Exception:
        pass

    if len(val) >= 8 and not val.startswith("http"):
        try:
            padded = val + '=' * (-len(val) % 4)
            b64_dec = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8', errors='ignore')
            if b64_dec.startswith("http://") or b64_dec.startswith("https://"):
                candidates.append(b64_dec)
        except Exception:
            pass

    for cand in candidates:
        if cand.startswith("http://") or cand.startswith("https://"):
            return cand
        idx = cand.find("https://")
        if idx == -1:
            idx = cand.find("http://")
        if idx != -1:
            sub = cand[idx:]
            sub = re.split(r'[\s"\'&]', sub)[0]
            if sub.startswith("http://") or sub.startswith("https://"):
                return sub
    return None


def sanitize_url(url: str, depth: int = 0) -> tuple[str, str]:
    """Pure URL sanitizer. Offline only.
    Returns (result_url, change_type) where change_type in ('none', 'changed', 'removed')."""
    if depth > 3:
        return "[TRACKING LINK REMOVED]", "removed"

    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url, "none"

    if parts.scheme.lower() not in ("http", "https"):
        return url, "none"

    netloc = parts.netloc.lower()
    path = parts.path
    query = parts.query
    fragment = parts.fragment

    # 1. Action URLs (Rule C)
    path_lower = path.lower()
    query_lower = query.lower()
    if any(kw in path_lower or kw in query_lower for kw in ACTION_KEYWORDS):
        return "[LINK REMOVED]", "removed"

    # 2. Tracking Redirects (Rule D)
    is_tracking_redirect = (
        any(sub in netloc for sub in TRACKING_HOST_SUBSTRINGS) or
        bool(TRACKING_PATH_REGEX.search(path)) or
        path in ("/r", "/l", "/r/", "/l/")
    )

    if is_tracking_redirect:
        embedded_url = None
        # Try extracting from query params
        if query:
            try:
                parsed_qs = urllib.parse.parse_qs(query)
                for k, vals in parsed_qs.items():
                    for v in vals:
                        embedded_url = try_extract_embedded_url(v)
                        if embedded_url:
                            break
                    if embedded_url:
                        break
            except Exception:
                pass

        # Try extracting from path
        if not embedded_url:
            embedded_url = try_extract_embedded_url(path)

        if embedded_url:
            clean_dest, _ = sanitize_url(embedded_url, depth + 1)
            if clean_dest not in ("[LINK REMOVED]", "[TRACKING LINK REMOVED]"):
                return clean_dest, "changed"
            else:
                return "[TRACKING LINK REMOVED]", "removed"
        else:
            return "[TRACKING LINK REMOVED]", "removed"

    # 3. Parameter and Fragment Cleanup (Rules A, B, E)
    query_changed = False
    new_query = query
    if query:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        safe_pairs = []
        for k, v in pairs:
            if is_tracking_param(k) or is_credential_param(k):
                query_changed = True
            else:
                safe_pairs.append((k, v))
        new_query = urllib.parse.urlencode(safe_pairs, doseq=True)
        if new_query != query:
            query_changed = True

    fragment_changed = False
    new_fragment = fragment
    if fragment:
        frag_lower = fragment.lower()
        if JWT_REGEX.search(fragment) or any(k in frag_lower for k in EXACT_CREDENTIAL_KEYS):
            if "=" in fragment:
                try:
                    frag_pairs = urllib.parse.parse_qsl(fragment, keep_blank_values=True)
                    safe_frag_pairs = [
                        (k, v) for k, v in frag_pairs
                        if not is_tracking_param(k) and not is_credential_param(k)
                    ]
                    new_fragment = urllib.parse.urlencode(safe_frag_pairs, doseq=True)
                except Exception:
                    new_fragment = ""
            else:
                new_fragment = ""
            fragment_changed = True

    if query_changed or fragment_changed:
        rebuilt = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, new_fragment))
        return rebuilt, "changed"

    return url, "none"


def sanitize_share_text(text: str) -> SanitizeResult:
    """Pure text sanitizer. Offline only.
    Redacts obvious secrets, strips tracking/credential parameters, neutralizes action URLs."""
    if not text:
        return SanitizeResult(text="", modified=False)

    original_text = text
    secrets_redacted = 0
    urls_changed = 0
    urls_removed = 0

    # 1. Redact PEM private key blocks
    def _redact_pem(m):
        nonlocal secrets_redacted
        secrets_redacted += 1
        return "[PRIVATE KEY REDACTED]"

    text = PEM_REGEX.sub(_redact_pem, text)

    # 2. Redact Authorization: Bearer
    def _redact_bearer(m):
        nonlocal secrets_redacted
        secrets_redacted += 1
        return "Authorization: Bearer [REDACTED]"

    text = BEARER_REGEX.sub(_redact_bearer, text)

    # 3. Sanitize URLs (Markdown links, autolinks, plain URLs)
    def _replace_url_match(m):
        nonlocal urls_changed, urls_removed

        if m.group("md_link"):
            label = m.group("label")
            raw_url = m.group("md_url")
            clean_url, change = sanitize_url(raw_url)
            if change == "removed":
                urls_removed += 1
                return f"[{label}]({clean_url})"
            elif change == "changed":
                urls_changed += 1
                return f"[{label}]({clean_url})"
            else:
                return m.group(0)

        elif m.group("autolink"):
            raw_url = m.group("auto_url")
            clean_url, change = sanitize_url(raw_url)
            if change == "removed":
                urls_removed += 1
                return clean_url
            elif change == "changed":
                urls_changed += 1
                return f"<{clean_url}>"
            else:
                return m.group(0)

        else:
            raw_match = m.group("plain_url")
            url, punct = strip_trailing_punct(raw_match)
            clean_url, change = sanitize_url(url)
            if change == "removed":
                urls_removed += 1
                return f"{clean_url}{punct}"
            elif change == "changed":
                urls_changed += 1
                return f"{clean_url}{punct}"
            else:
                return m.group(0)

    text = URL_SCANNER.sub(_replace_url_match, text)

    # 4. Redact standalone JWT tokens
    def _redact_jwt(m):
        nonlocal secrets_redacted
        secrets_redacted += 1
        return "[JWT REDACTED]"

    text = JWT_REGEX.sub(_redact_jwt, text)

    # 5. Redact obvious credential assignments
    def _redact_assignment(m):
        nonlocal secrets_redacted
        val = m.group("val")
        if val in ("[REDACTED]", "[JWT REDACTED]", "[PRIVATE KEY REDACTED]"):
            return m.group(0)
        secrets_redacted += 1
        prefix = m.group("prefix") or ""
        q1 = m.group("quote1") or ""
        key = m.group("key")
        sep = m.group("sep")
        space = m.group("space") or ""
        q2 = m.group("quote2") or ""
        return f"{prefix}{q1}{key}{q1}{sep}{space}{q2}[REDACTED]{q2}"

    text = ASSIGNMENT_REGEX.sub(_redact_assignment, text)
    modified = (text != original_text)

    return SanitizeResult(
        text=text,
        urls_changed=urls_changed,
        urls_removed=urls_removed,
        secrets_redacted=secrets_redacted,
        modified=modified
    )


# ==============================================================================
# IN-MEMORY SAFE COPY RECOVERY & CLIPBOARD TEXT BOUNDARY
# ==============================================================================

class SafeCopyHistory:
    """Stores one previous clipboard text in process memory only with TTL."""
    def __init__(self, ttl_seconds: float = 300.0):
        self._saved_text: str | None = None
        self._saved_at: float = 0.0
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def store(self, text: str):
        with self._lock:
            self._saved_text = text
            self._saved_at = time.time()

    def retrieve(self) -> str | None:
        with self._lock:
            if self._saved_text is None:
                return None
            if time.time() - self._saved_at > self._ttl:
                self._saved_text = None
                return None
            val = self._saved_text
            self._saved_text = None
            return val

    def clear(self):
        with self._lock:
            self._saved_text = None


GLOBAL_HISTORY = SafeCopyHistory()


def get_clipboard_text() -> str | None:
    """Reads CF_UNICODETEXT from Windows clipboard.
    Returns None if clipboard contains CF_HDROP (Explorer file-copy), no text, or is inaccessible."""
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
            return None
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return None

        opened = False
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                opened = True
                break
            except Exception:
                time.sleep(0.05)

        if not opened:
            return None

        try:
            data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            return data if isinstance(data, str) else None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def set_clipboard_text(text: str) -> bool:
    """Safely writes CF_UNICODETEXT to Windows clipboard."""
    opened = False
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            opened = True
            break
        except Exception:
            time.sleep(0.05)

    if not opened:
        return False

    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
        return True
    except Exception:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return False


def safe_copy_clipboard(monitor=None) -> SanitizeResult | None:
    """One-shot operation against the current clipboard text."""
    text = get_clipboard_text()
    if text is None:
        print("Safe Copy: No text on clipboard or file-copy active (untouched).")
        return None

    result = sanitize_share_text(text)
    if result.modified:
        GLOBAL_HISTORY.store(text)
        if monitor:
            monitor.ignore_next_change = True
        set_clipboard_text(result.text)
        if monitor:
            monitor.last_clipboard_sequence = monitor.get_clipboard_sequence()
        print(f"Safe Copy: {result.urls_changed} URLs sanitized, {result.urls_removed} sensitive links removed, {result.secrets_redacted} credentials redacted.")
    else:
        print("Safe Copy: Text already clean, no changes needed.")

    return result


def restore_last_safe_copy(monitor=None) -> bool:
    """Restores previously saved clipboard text from process RAM."""
    saved = GLOBAL_HISTORY.retrieve()
    if saved is None:
        print("Safe Copy: No previous original text available in memory to restore (or TTL expired).")
        return False

    if monitor:
        monitor.ignore_next_change = True
    success = set_clipboard_text(saved)
    if monitor:
        monitor.last_clipboard_sequence = monitor.get_clipboard_sequence()

    if success:
        print("Safe Copy: Successfully restored previous clipboard text.")
    else:
        print("Safe Copy: Failed to write restored text to clipboard.")
    return success


# ==============================================================================
# NATIVE WINDOWS HOTKEY LISTENER
# ==============================================================================

class HotkeyListener(object):
    """Global Safe Copy hotkey. A registration failure is a REPORTED state
    (hotkey_registered=0 + reason), never a silent loss and never fatal: the
    image monitor keeps running."""

    def __init__(self, callback, modifiers=HOTKEY_MODIFIERS, vk=HOTKEY_VK,
                 name=HOTKEY_NAME, status=None):
        self.callback = callback
        self.modifiers = modifiers
        self.vk = vk
        self.name = name
        self.status = status
        self.running = False
        self.thread = None
        self.thread_id = None
        self.registered = False
        self.error = ''

    def _report(self):
        status = getattr(self, 'status', None)
        if status is None:
            return
        try:
            status.update(hotkey=self.name,
                          hotkey_registered='1' if self.registered else '0',
                          hotkey_error=self.error)
        except Exception as exc:
            print("⚠️ Hotkey status update failed: %s" % exc)

    def _loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        self.registered = False
        self.error = ''

        res = user32.RegisterHotKey(None, HOTKEY_ID, self.modifiers, self.vk)
        if not res:
            err = ctypes.GetLastError()
            self.error = 'RegisterHotKey failed for %s (error %s)' % (self.name, err)
            print("⚠️ Hotkey registration failed for %s (error %s). Safe Copy hotkey disabled, but image monitor continues."
                  % (self.name, err))
            self._report()
            return

        self.registered = True
        print("⌨️ Safe Copy hotkey active: %s" % self.name)
        self._report()

        msg = wintypes.MSG()
        while self.running:
            res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if res == 0 or res == -1:
                break
            if msg.message == win32con.WM_HOTKEY and msg.wParam == HOTKEY_ID:
                try:
                    self.callback()
                except Exception as e:
                    print("⚠️ Error executing hotkey callback: %s" % e)

        if self.registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered = False
            self.error = ''
            self._report()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        if self.running:
            self.running = False
            if self.thread_id:
                ctypes.windll.user32.PostThreadMessageW(self.thread_id, win32con.WM_QUIT, 0, 0)
            thread = getattr(self, 'thread', None)
            if thread is not None:
                thread.join(timeout=2.0)

    def reconfigure(self, modifiers, vk, name):
        """Re-register in place (settings reload) without restarting the
        resident or losing the in-memory Safe Copy history."""
        was_running = self.running
        if was_running:
            self.stop()
        self.modifiers = modifiers
        self.vk = vk
        self.name = name
        if was_running:
            self.start()


# ==============================================================================
# IMAGE CLIPBOARD MONITOR (EXISTING AUTHORITATIVE BEHAVIOR)
# ==============================================================================

MAX_DEDUP_HASHES = 512          # bounded process-lifetime dedup (A6)
PATH_RECHECK_SECONDS = 15.0     # re-probe an unavailable save folder


class BoundedHashSet(object):
    """Insertion-ordered, capacity-bounded set of image hashes.

    The resident runs for days: an unbounded set of every clipboard image hash
    ever seen is a slow leak, and re-scanning a huge folder on every start is a
    slow boot. Oldest entries are evicted first; the newest stay authoritative
    for "already saved" dedup.
    """

    def __init__(self, capacity=MAX_DEDUP_HASHES):
        self.capacity = max(1, int(capacity))
        self._items = collections.OrderedDict()

    def add(self, item):
        if item in self._items:
            del self._items[item]
        self._items[item] = True
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def __contains__(self, item):
        return item in self._items

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


class LightweightClipboardMonitor(object):
    def __init__(self, save_path=DEFAULT_SAVE_PATH, status=None,
                 fallback_enabled=False, fallback_path=None):
        self.save_path = save_path or DEFAULT_SAVE_PATH
        self.fallback_enabled = bool(fallback_enabled)
        self.fallback_path = fallback_path or expand_path(DEFAULT_FALLBACK_PATH)
        self.status = status
        self.running = False
        self.processed_hashes = BoundedHashSet()
        self.last_clipboard_sequence = 0
        self.ignore_next_change = False

        self.effective_save_path = ''
        self.save_path_ok = False
        self.save_path_error = ''
        self.fallback_active = False
        self.saved_count = 0
        self.error_count = 0
        self.unsaved_pending = None    # (bytes, format) awaiting a usable folder
        self._last_path_check = 0.0

        self._prepare_save_path(initial=True)
        self.load_existing_hashes()

        print("🚀 Clipboard+ Monitor Active")
        print("📂 Saving to: %s" % (self.effective_save_path or '(unavailable)'))
        print("🔍 Loaded %d recent hashes" % len(self.processed_hashes))
        if not self.save_path_ok:
            print("⚠️ SAVE PATH UNAVAILABLE: %s (%s)" % (self.save_path, self.save_path_error))
        self._publish_status()

    # ---- save folder usability (A3) -------------------------------------

    @staticmethod
    def _probe_path(path):
        """Usable means: creatable, writable. Not merely present."""
        try:
            if not path:
                return False, 'no folder configured'
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, '.clipboard_plus_write_test')
            with open(probe, 'wb') as handle:
                handle.write(b'ok')
            os.remove(probe)
            return True, ''
        except Exception as exc:
            return False, '%s: %s' % (type(exc).__name__, exc)

    def _prepare_save_path(self, initial=False):
        """Never raises: an unavailable folder is a reported STATE, not a crash.
        A fallback folder is used only when the user enabled one, and the
        effective path is always what gets reported and printed."""
        self._last_path_check = time.time()
        ok, reason = self._probe_path(self.save_path)
        if ok:
            self.effective_save_path = self.save_path
            self.fallback_active = False
            self.save_path_ok = True
            self.save_path_error = ''
            return True

        self.save_path_ok = False
        self.save_path_error = reason
        self.effective_save_path = ''
        if self.fallback_enabled:
            fb_ok, fb_reason = self._probe_path(self.fallback_path)
            if fb_ok:
                self.effective_save_path = self.fallback_path
                self.fallback_active = True
                self.save_path_ok = True
                print("⚠️ Configured folder unavailable (%s) - using the enabled fallback: %s"
                      % (reason, self.fallback_path))
                return True
            self.save_path_error = '%s; fallback unusable: %s' % (reason, fb_reason)
        if not initial:
            print("⚠️ Save folder unavailable: %s (%s)" % (self.save_path, self.save_path_error))
        return False

    def load_existing_hashes(self):
        """Hashes the 500 most recent files so a restart does not re-save them.
        Unreadable files are reported, never silently swallowed (T-172)."""
        if not self.effective_save_path:
            return 0
        try:
            files = [os.path.join(self.effective_save_path, f)
                     for f in os.listdir(self.effective_save_path)]
        except Exception as exc:
            print("⚠️ Cannot list save folder %s: %s" % (self.effective_save_path, exc))
            return 0
        files = [f for f in files if os.path.isfile(f)]
        try:
            files.sort(key=os.path.getmtime, reverse=True)
        except Exception as exc:
            print("⚠️ Cannot order save folder by modified time: %s" % exc)
        skipped = 0
        for filepath in files[:500]:
            try:
                md5 = hashlib.md5()
                with open(filepath, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        md5.update(chunk)
                self.processed_hashes.add(md5.hexdigest())
            except Exception as exc:
                skipped += 1
                print("⚠️ Skipped unreadable file in the save folder: %s (%s)"
                      % (os.path.basename(filepath), exc))
        return skipped

    def get_clipboard_sequence(self):
        try:
            return win32clipboard.GetClipboardSequenceNumber()
        except Exception:
            return 0

    def open_clipboard_with_retry(self, retries=5, delay=0.05):
        """Attempts to safely open the clipboard, retrying if locked by another app."""
        for _ in range(retries):
            try:
                win32clipboard.OpenClipboard()
                return True
            except Exception:
                time.sleep(delay)
        return False

    def get_clipboard_image(self):
        if ImageGrab is None:
            print("⚠️ Pillow is not importable - image capture is disabled.")
            return None, None
        try:
            image = ImageGrab.grabclipboard()

            if image is None:
                return None, None

            # If the clipboard contains files (e.g. copied from Explorer), pillow returns a list of paths.
            # We explicitly ignore these to preserve native file copy behavior.
            if isinstance(image, list):
                return None, None

            is_animated = getattr(image, "is_animated", False)
            format_type = "GIF" if is_animated else "PNG"

            img_bytes = io.BytesIO()
            if is_animated:
                image.save(img_bytes, format="GIF", save_all=True)
            else:
                image.save(img_bytes, format="PNG")

            image.close()
            return img_bytes.getvalue(), format_type

        except Exception as e:
            print(f"⚠️ Failed to grab image from clipboard: {e}")
            return None, None

    def _note_save_failure(self, message):
        self.save_path_ok = False
        self.save_path_error = message
        print("⚠️ Error saving file: %s" % message)

    def save_image_file(self, data, format_type):
        target_dir = getattr(self, 'effective_save_path', '') or getattr(self, 'save_path', '')
        if not target_dir:
            self._note_save_failure('no usable save folder')
            return None, None
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_hash = hashlib.md5(data).hexdigest()
            extension = ".gif" if format_type == "GIF" else ".png"
            filename = f"clipboard_{timestamp}_{file_hash[:8]}{extension}"
            filepath = os.path.join(target_dir, filename)

            with open(filepath, "wb") as f:
                f.write(data)

            self.save_path_ok = True
            self.save_path_error = ''
            return filepath, filename
        except Exception as e:
            self._note_save_failure('%s: %s' % (type(e).__name__, e))
            return None, None

    def set_image_to_clipboard(self, image_data, filepath, format_type):
        """Injects formats back into the clipboard so it pastes flawlessly everywhere."""
        if win32clipboard is None or win32con is None:
            print("❌ pywin32 is not importable - cannot write the clipboard.")
            return False
        if not self.open_clipboard_with_retry():
            print("❌ Failed to open clipboard for writing.")
            return False

        try:
            self.ignore_next_change = True
            win32clipboard.EmptyClipboard()

            # Format 1: CF_DIB (For older editors like Paint)
            try:
                with Image.open(io.BytesIO(image_data)) as image:
                    if image.mode in ('RGBA', 'LA'):
                        background = Image.new('RGB', image.size, (255, 255, 255))
                        background.paste(image, mask=image.split()[-1])
                        image = background
                    elif image.mode != 'RGB':
                        image = image.convert('RGB')

                    output = io.BytesIO()
                    image.save(output, 'BMP')
                    dib_data = output.getvalue()[14:]  # Strip 14-byte BMP header
                    win32clipboard.SetClipboardData(win32con.CF_DIB, dib_data)
            except Exception as e:
                print(f"  ⚠️ CF_DIB Error: {e}")

            # Format 1.5: Modern PNG/GIF format (Preserves transparency/animation)
            try:
                cf_format = win32clipboard.RegisterClipboardFormat(format_type)
                win32clipboard.SetClipboardData(cf_format, image_data)
            except Exception as e:
                print(f"  ⚠️ {format_type} Error: {e}")

            # Format 2: CF_HDROP (For pasting the file directly into Explorer/Total Commander)
            try:
                abs_path = os.path.abspath(filepath)
                file_list_wide = abs_path.encode("utf-16le") + b'\0\0\0\0'
                header = struct.pack("IIIII", 20, 0, 0, 0, 1)
                win32clipboard.SetClipboardData(win32con.CF_HDROP, header + file_list_wide)

                cf_drop_effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
                win32clipboard.SetClipboardData(cf_drop_effect, struct.pack("I", 1)) # DROPEFFECT_COPY
            except Exception as e:
                print(f"  ⚠️ CF_HDROP Error: {e}")

            win32clipboard.CloseClipboard()

            # Reset sequence number AFTER we finish our modifications so we don't trigger ourselves
            self.last_clipboard_sequence = self.get_clipboard_sequence()
            return True

        except Exception as e:
            print(f"  ❌ Clipboard write error: {e}")
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
            return False
        finally:
            def reset_flag():
                time.sleep(0.5)
                self.ignore_next_change = False
            threading.Thread(target=reset_flag, daemon=True).start()

    def _publish_status(self, **values):
        status = getattr(self, 'status', None)
        if status is None:
            return
        try:
            payload = dict(
                monitor='1' if getattr(self, 'running', False) else '0',
                save_path=getattr(self, 'save_path', ''),
                effective_save_path=getattr(self, 'effective_save_path', ''),
                save_path_ok='1' if getattr(self, 'save_path_ok', False) else '0',
                save_error=getattr(self, 'save_path_error', ''),
                fallback_active='1' if getattr(self, 'fallback_active', False) else '0',
                saved_count=getattr(self, 'saved_count', 0),
                unsaved_pending='1' if getattr(self, 'unsaved_pending', None) is not None else '0',
                dedup_entries=len(getattr(self, 'processed_hashes', None) or ()),
            )
            payload.update(values)
            status.update(**payload)
        except Exception as exc:
            print("⚠️ Status update failed: %s" % exc)

    def _retry_pending_save(self):
        """A capture that could not be written is held in RAM (one slot, bounded)
        and retried once the folder is usable again, so an unavailable drive
        never silently discards an image."""
        pending = getattr(self, 'unsaved_pending', None)
        if pending is None:
            return
        if not getattr(self, 'save_path_ok', False):
            if time.time() - getattr(self, '_last_path_check', 0.0) < PATH_RECHECK_SECONDS:
                return
            if not self._prepare_save_path():
                return
        data, format_type = pending
        filepath, filename = self.save_image_file(data, format_type)
        if not filepath:
            return
        self.unsaved_pending = None
        self.processed_hashes.add(hashlib.md5(data).hexdigest())
        self.saved_count = getattr(self, 'saved_count', 0) + 1
        # Deliberately no clipboard rewrite here: the clipboard may hold newer
        # content by now, and replacing it with this deferred image would be a
        # surprising side effect.
        print("\n💾 Saved (deferred): %s" % filename)
        self._publish_status()

    def process_clipboard(self):
        try:
            if self.ignore_next_change:
                return False

            if getattr(self, 'unsaved_pending', None) is not None:
                self._retry_pending_save()

            current_sequence = self.get_clipboard_sequence()
            if current_sequence == self.last_clipboard_sequence:
                return False

            data, format_type = self.get_clipboard_image()
            if data is None:
                self.last_clipboard_sequence = current_sequence
                return False

            data_hash = hashlib.md5(data).hexdigest()
            if data_hash in self.processed_hashes:
                self.last_clipboard_sequence = current_sequence
                return False

            filepath, filename = self.save_image_file(data, format_type)
            if not filepath:
                # Never silently drop the image: keep the newest capture in RAM
                # for a retry and keep reporting the real reason. The clipboard
                # itself still holds the image, so nothing is lost to the user.
                self.last_clipboard_sequence = current_sequence
                self.unsaved_pending = (data, format_type)
                self._publish_status()
                return False

            self.processed_hashes.add(data_hash)
            self.saved_count = getattr(self, 'saved_count', 0) + 1
            print(f"\n💾 Saved: {filename}")

            if self.set_image_to_clipboard(data, filepath, format_type):
                print("  ✅ Ready to paste anywhere!")
            else:
                print("  ⚠️ Saved, but the clipboard could not be rewritten this time.")

            self._publish_status()
            return True

        except Exception as e:
            # One failing image (locked clipboard, corrupt image, dead drive,
            # write error) must never end the monitor.
            print(f"⚠️ Monitor processing error: {e}")
            self.error_count = getattr(self, 'error_count', 0) + 1
            return False

    def monitor_loop(self):
        self.last_clipboard_sequence = self.get_clipboard_sequence()

        while self.running:
            try:
                self.process_clipboard()
                time.sleep(0.2)  # High responsiveness polling
            except Exception as e:
                print(f"⚠️ Loop error: {e}")
                time.sleep(1)
        self._publish_status()

    def apply_settings(self, values):
        """Live settings reload: save folder and fallback only. The hotkey is
        re-registered by the resident, which owns that Win32 registration."""
        values = values or {}
        self.fallback_enabled = bool(values.get('fallback_enabled'))
        if values.get('fallback_path'):
            self.fallback_path = values['fallback_path']
        new_path = values.get('save_path') or DEFAULT_SAVE_PATH
        if new_path != self.save_path:
            self.save_path = new_path
            if self._prepare_save_path():
                print("📂 Save folder now: %s" % self.effective_save_path)
                if getattr(self, 'unsaved_pending', None) is not None:
                    self._retry_pending_save()
            else:
                print("⚠️ New save folder is unavailable: %s (%s)"
                      % (self.save_path, self.save_path_error))
        self._publish_status()

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.monitor_loop, daemon=True)
            self.thread.start()
            self._publish_status()

    def stop(self):
        if self.running:
            self.running = False
            self._publish_status()


# ==============================================================================
# SINGLE-INSTANCE & CONTROL CHANNEL IPC
# ==============================================================================

# --- ownership primitives (T-164 A2) ----------------------------------------
# Ownership is a Windows named mutex, per-user and per-session. The loopback
# IPC stays for commands, but it is NOT the ownership mechanism: no more
# "kill the old one, sleep, hope the port freed up".
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0x00000000


def acquire_instance_mutex():
    """Returns the owned handle, or None when a healthy resident already owns
    the single-instance mutex. Never kills anything."""
    handle = _kernel32.CreateMutexW(None, False, mutex_name())
    if not handle:
        raise OSError('CreateMutexW failed (%s)' % ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


def instance_running():
    """Read-only probe used by --status, --stop and the SAITULS shell."""
    handle = _kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name())
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


def create_stop_event():
    """Manual-reset event a running resident waits on. Its own process owns
    it, so a stop request can never be left pending for the next start."""
    handle = _kernel32.CreateEventW(None, True, False, stop_event_name())
    if not handle:
        raise OSError('CreateEventW failed (%s)' % ctypes.get_last_error())
    return handle


def signal_stop():
    """Ask a running resident to exit cleanly. Returns False when none is up."""
    handle = _kernel32.OpenEventW(EVENT_MODIFY_STATE, False, stop_event_name())
    if not handle:
        return False
    try:
        return bool(_kernel32.SetEvent(handle))
    finally:
        _kernel32.CloseHandle(handle)


# --- control channel ---------------------------------------------------------

def send_ipc_command(cmd: bytes, timeout: float = 1.0) -> tuple[bool, str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(('127.0.0.1', control_port()))
        s.sendall(cmd + b"\n")
        resp = s.recv(1024).decode('utf-8', errors='ignore').strip()
        s.close()
        return True, resp
    except Exception:
        return False, ""


class ResidentControl(object):
    """Everything the control channel may touch, plus the shutdown/reload
    signals and the non-secret status counters."""

    def __init__(self, monitor=None, status=None):
        self.monitor = monitor
        self.status = status
        self.shutdown = threading.Event()
        self.reload_event = threading.Event()

    def _bump(self, key, delta=1):
        if self.status is None:
            return
        try:
            current = int(self.status.get(key, '0') or '0')
        except (TypeError, ValueError):
            current = 0
        try:
            self.status.update(**{key: current + delta})
        except Exception as exc:
            print("⚠️ Status counter update failed: %s" % exc)

    def safe_copy(self):
        result = safe_copy_clipboard(self.monitor)
        self._bump('safe_copy_events')
        if result is not None and result.modified and self.status is not None:
            self.status.update(restore_available='1')
        return result

    def restore(self):
        success = restore_last_safe_copy(self.monitor)
        if success and self.status is not None:
            self.status.update(restore_available='0')
        return success

    def describe(self):
        if self.status is None:
            return 'Clipboard+ resident running'
        return self.status.as_line()


def start_control_listener(control):
    port = control_port()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('127.0.0.1', port))
        s.listen(5)
    except Exception as e:
        print("⚠️ Could not bind control socket to 127.0.0.1:%s: %s" % (port, e))
        print("⚠️ Clipboard+ keeps running; only the loopback control commands are unavailable.")
        if control.status is not None:
            control.status.update(control_channel='unavailable', control_error=str(e))
        return None
    if control.status is not None:
        control.status.update(control_channel='ok', control_port=port, control_error='')

    def listener():
        while True:
            try:
                conn, _ = s.accept()
                conn.settimeout(2.0)
                data = conn.recv(1024).strip()
                if not data:
                    conn.close()
                    continue

                if data in (b"STOP", b"EXIT"):
                    # EXIT is kept as a STOP alias for old callers; it no longer
                    # means "die so a replacement can take over".
                    conn.sendall(b"OK: stopping\n")
                    conn.close()
                    print("\n🛑 Stop requested. Shutting down cleanly...")
                    control.shutdown.set()
                elif data == b"SAFE_COPY":
                    res = control.safe_copy()
                    if res is None:
                        conn.sendall(b"NO_OP: No text on clipboard or file-copy active\n")
                    elif res.modified:
                        msg = f"OK: {res.urls_changed} URLs sanitized, {res.urls_removed} sensitive links removed, {res.secrets_redacted} credentials redacted\n"
                        conn.sendall(msg.encode("utf-8"))
                    else:
                        conn.sendall(b"OK: Text already clean, no changes needed\n")
                    conn.close()
                elif data == b"RESTORE":
                    if control.restore():
                        conn.sendall(b"OK: Restored\n")
                    else:
                        conn.sendall(b"ERR: No backup in memory (or TTL expired)\n")
                    conn.close()
                elif data == b"STATUS":
                    conn.sendall(("OK: %s\n" % control.describe()).encode("utf-8"))
                    conn.close()
                elif data == b"RELOAD":
                    control.reload_event.set()
                    conn.sendall(b"OK: reloading settings\n")
                    conn.close()
                else:
                    conn.sendall(b"ERR: Unknown command\n")
                    conn.close()
            except Exception as exc:
                # A broken/abandoned connection must not end the control
                # channel, but it is reported rather than swallowed.
                print("⚠️ Control channel error: %s" % exc)
                time.sleep(0.2)

    t = threading.Thread(target=listener, daemon=True)
    t.start()
    return s# ==============================================================================
# COMMANDS (status / stop / dependency readiness / one-shot Safe Copy)
# ==============================================================================

def dependency_report():
    """Real readiness, not "python.exe exists": every dependency is imported
    AND exercised with one real call (A4)."""
    problems = list(IMPORT_ERRORS)
    if win32clipboard is not None:
        try:
            win32clipboard.GetClipboardSequenceNumber()
        except Exception as exc:
            problems.append('pywin32 clipboard API unusable (%s)' % exc)
    if Image is not None:
        try:
            buffer = io.BytesIO()
            Image.new('RGB', (1, 1), (255, 0, 0)).save(buffer, format='PNG')
            if not buffer.getvalue():
                problems.append('Pillow cannot encode PNG')
        except Exception as exc:
            problems.append('Pillow unusable (%s)' % exc)
    unique = []
    for problem in problems:
        if problem not in unique:
            unique.append(problem)
    return unique


def _print_dependency_report(problems):
    if problems:
        print('Clipboard+: DEPENDENCY MISSING - %s' % '; '.join(problems))
        print('DEPENDENCY_STATUS=MISSING')
        print('MISSING=%s' % ','.join(problems))
    else:
        print('Clipboard+: dependencies OK (Python, pywin32, Pillow)')
        print('DEPENDENCY_STATUS=READY')


def cmd_check_deps():
    problems = dependency_report()
    _print_dependency_report(problems)
    return 4 if problems else 0


def cmd_status():
    running = instance_running()
    stored = read_ini(status_path()).get(STATUS_SECTION, {})
    print('Clipboard+: %s' % ('RUNNING' if running else 'STOPPED'))
    if stored:
        print('monitor=%s hotkey=%s hotkey_registered=%s save_path_ok=%s effective_save_path=%s saved_count=%s updated=%s'
              % (stored.get('monitor', '?'), stored.get('hotkey', '?'),
                 stored.get('hotkey_registered', '?'), stored.get('save_path_ok', '?'),
                 stored.get('effective_save_path', '?'), stored.get('saved_count', '?'),
                 stored.get('updated', '?')))
    return 0 if running else 3


def cmd_stop():
    if not instance_running():
        print('Clipboard+: STOPPED (nothing was running)')
        return 3
    if not signal_stop():
        print('Clipboard+: ERROR - a resident is running but its stop channel could not be signalled')
        return 5
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not instance_running():
            print('Clipboard+: STOPPED')
            return 0
        time.sleep(0.2)
    print('Clipboard+: ERROR - the resident did not stop within 10s')
    return 5


def cmd_safe_copy_once():
    problems = dependency_report()
    if problems:
        _print_dependency_report(problems)
        return 4
    alive, response = send_ipc_command(b"SAFE_COPY")
    if alive:
        print(response)
        return 0
    result = safe_copy_clipboard()
    if result is None:
        print('Safe Copy: nothing to sanitize (no text clipboard / file copy untouched).')
    return 0


def cmd_restore_once():
    problems = dependency_report()
    if problems:
        _print_dependency_report(problems)
        return 4
    alive, response = send_ipc_command(b"RESTORE")
    if alive:
        print(response)
        return 0
    print('Restore failed: the Clipboard+ resident is not running (the original text is kept in process RAM only and is never persisted to disk).')
    return 3


# ==============================================================================
# SETTINGS GUI (T-164 A3) -- owned by the subsystem, launched by SAITULS
# ==============================================================================

def run_settings_gui(settings_file=None):
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception as exc:
        print('Clipboard+ settings needs tkinter: %s' % exc)
        return 4

    path = settings_file or settings_path()
    values = load_settings(path)

    root = tk.Tk()
    root.title('Clipboard+ settings')
    root.configure(bg='#232018')
    root.resizable(False, False)

    label_opts = dict(bg='#232018', fg='#D4C89A', anchor='w')
    entry_opts = dict(bg='#14120C', fg='#D4C89A', insertbackground='#F0D060',
                      relief='flat', highlightthickness=1, highlightbackground='#75663D')
    button_opts = dict(bg='#332E22', fg='#D4C89A', activebackground='#3D372A',
                       activeforeground='#F0D060', relief='flat', highlightthickness=0)

    tk.Label(root, text='Clipboard+ settings', bg='#1A1810', fg='#F0D060',
             anchor='w', font=('Segoe UI', 10, 'bold')).grid(
        row=0, column=0, columnspan=3, sticky='we', padx=8, pady=(8, 6))

    tk.Label(root, text='Image save folder', **label_opts).grid(
        row=1, column=0, columnspan=3, sticky='w', padx=8, pady=(4, 2))
    folder_var = tk.StringVar(value=values['save_path'])
    tk.Entry(root, textvariable=folder_var, width=56, **entry_opts).grid(
        row=2, column=0, columnspan=2, sticky='we', padx=8)

    def browse():
        start = folder_var.get() if os.path.isdir(folder_var.get()) else state_dir()
        chosen = filedialog.askdirectory(title='Pick the Clipboard+ image folder', initialdir=start)
        if chosen:
            folder_var.set(os.path.normpath(chosen))

    tk.Button(root, text='Browse', command=browse, **button_opts).grid(row=2, column=2, padx=(4, 8))

    tk.Label(root, text='Safe Copy hotkey (e.g. Ctrl+Alt+Shift+C)', **label_opts).grid(
        row=3, column=0, columnspan=3, sticky='w', padx=8, pady=(10, 2))
    hotkey_var = tk.StringVar(value=values['hotkey'])
    tk.Entry(root, textvariable=hotkey_var, width=24, **entry_opts).grid(
        row=4, column=0, sticky='w', padx=8)

    fallback_var = tk.BooleanVar(value=bool(values['fallback_enabled']))
    tk.Checkbutton(root, text='Use a fallback folder when the save folder is unavailable (the effective folder is always shown)',
                   variable=fallback_var, bg='#232018', fg='#D4C89A', selectcolor='#14120C',
                   activebackground='#232018', activeforeground='#F0D060', anchor='w').grid(
        row=5, column=0, columnspan=3, sticky='w', padx=8, pady=(10, 2))
    fallback_path_var = tk.StringVar(value=values['fallback_path'])
    tk.Entry(root, textvariable=fallback_path_var, width=56, **entry_opts).grid(
        row=6, column=0, columnspan=2, sticky='we', padx=8)

    def browse_fallback():
        start = fallback_path_var.get() if os.path.isdir(fallback_path_var.get()) else state_dir()
        chosen = filedialog.askdirectory(title='Pick the fallback image folder', initialdir=start)
        if chosen:
            fallback_path_var.set(os.path.normpath(chosen))

    tk.Button(root, text='Browse', command=browse_fallback, **button_opts).grid(row=6, column=2, padx=(4, 8))

    status_var = tk.StringVar(value='Clipboard+: %s' % ('RUNNING' if instance_running() else 'STOPPED'))
    tk.Label(root, textvariable=status_var, bg='#232018', fg='#9C9371', anchor='w').grid(
        row=7, column=0, columnspan=3, sticky='w', padx=8, pady=(10, 2))

    def reload_into_widgets(new_values):
        folder_var.set(new_values['save_path'])
        hotkey_var.set(new_values['hotkey'])
        fallback_var.set(bool(new_values['fallback_enabled']))
        fallback_path_var.set(new_values['fallback_path'])

    def do_save():
        hotkey_value = hotkey_var.get().strip()
        try:
            parse_hotkey(hotkey_value)
        except ValueError as exc:
            messagebox.showerror('Clipboard+ settings', 'Safe Copy hotkey is not usable:\n%s' % exc)
            return
        folder = expand_path(folder_var.get().strip())
        usable, reason = LightweightClipboardMonitor._probe_path(folder)
        if not usable and not messagebox.askyesno(
                'Clipboard+ settings',
                'That save folder is not usable right now:\n%s\n\n%s\n\nSave it anyway? Clipboard+ will show SAVE PATH UNAVAILABLE until it is usable.' % (folder, reason)):
            return
        fallback = expand_path(fallback_path_var.get().strip())
        if fallback_var.get():
            fb_usable, fb_reason = LightweightClipboardMonitor._probe_path(fallback)
            if not fb_usable and not messagebox.askyesno(
                    'Clipboard+ settings',
                    'The fallback folder is not usable right now:\n%s\n\n%s\n\nSave it anyway?' % (fallback, fb_reason)):
                return
        save_settings({'save_path': folder, 'hotkey': hotkey_value,
                       'fallback_enabled': bool(fallback_var.get()),
                       'fallback_path': fallback}, path)
        alive, _ = send_ipc_command(b'RELOAD')
        messagebox.showinfo('Clipboard+ settings',
                            'Saved.\n\nImage save folder: %s\nSafe Copy hotkey: %s\n\n%s'
                            % (folder, hotkey_value,
                               'A running Clipboard+ picked the change up.' if alive
                               else 'Start Clipboard+ to use it.'))

    def do_reset():
        reset_settings(path)
        reload_into_widgets(load_settings(path))
        alive, _ = send_ipc_command(b'RELOAD')
        messagebox.showinfo('Clipboard+ settings', 'Reset to defaults (%s).%s'
                            % (DEFAULT_SAVE_PATH,
                               ' A running Clipboard+ picked it up.' if alive else ''))

    buttons = tk.Frame(root, bg='#232018')
    buttons.grid(row=8, column=0, columnspan=3, sticky='we', padx=8, pady=12)
    tk.Button(buttons, text='Save', width=12, command=do_save, **button_opts).pack(side='left')
    tk.Button(buttons, text='Reset', width=12, command=do_reset, **button_opts).pack(side='left', padx=8)
    tk.Button(buttons, text='Close', width=12, command=root.destroy, **button_opts).pack(side='right')
    tk.Label(root, text='Settings file: %s' % path, bg='#232018', fg='#6E674E', anchor='w').grid(
        row=9, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 8))

    root.mainloop()
    return 0


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================

def _settings_stamp():
    try:
        return os.path.getmtime(settings_path())
    except Exception:
        return 0.0


def reload_settings(hotkey, monitor, status):
    values = load_settings()
    monitor.apply_settings(values)
    try:
        modifiers, vk = parse_hotkey(values['hotkey'])
        if (modifiers, vk) != (hotkey.modifiers, hotkey.vk):
            hotkey.reconfigure(modifiers, vk, values['hotkey'])
            print('⌨️ Safe Copy hotkey now: %s' % values['hotkey'])
    except ValueError as exc:
        print('⚠️ Hotkey in settings is invalid: %s (keeping %s)' % (exc, hotkey.name))
    if status is not None:
        status.update(save_path=values['save_path'], hotkey=values['hotkey'])


def _heartbeat_loop(status, control, interval=10.0):
    """Keeps status fresh so the shell can tell a live resident from a stale
    status file even when nothing else changed."""
    while not control.shutdown.is_set():
        time.sleep(interval)
        if control.shutdown.is_set():
            break
        try:
            status.write()
        except Exception as exc:
            print('⚠️ Heartbeat status write failed: %s' % exc)


def run_resident(args):
    problems = dependency_report()
    if problems:
        _print_dependency_report(problems)
        print('Clipboard+ will not start a resident that cannot import its dependencies.')
        return 4

    try:
        handle = acquire_instance_mutex()
    except OSError as exc:
        print('Clipboard+ ERROR: %s' % exc)
        return 6
    if handle is None:
        print('ALREADY_RUNNING: Clipboard+ is already running; the existing process and its RAM-only Safe Copy history were left untouched.')
        return 0

    stop_handle = None
    status = None
    hotkey = None
    monitor = None
    try:
        stop_handle = create_stop_event()
        values = load_settings()
        if args.save_path:
            values['save_path'] = expand_path(args.save_path)
        try:
            modifiers, vk = parse_hotkey(values['hotkey'])
        except ValueError as exc:
            print('⚠️ Configured hotkey is invalid (%s). Falling back to %s.' % (exc, DEFAULT_HOTKEY))
            values['hotkey'] = DEFAULT_HOTKEY
            modifiers, vk = parse_hotkey(DEFAULT_HOTKEY)

        status = RuntimeStatus(save_path=values['save_path'], hotkey=values['hotkey'])
        monitor = LightweightClipboardMonitor(save_path=values['save_path'], status=status,
                                              fallback_enabled=values['fallback_enabled'],
                                              fallback_path=values['fallback_path'])
        monitor.start()

        hotkey = HotkeyListener(callback=lambda: safe_copy_clipboard(monitor),
                                modifiers=modifiers, vk=vk, name=values['hotkey'],
                                status=status)
        if args.no_hotkey:
            status.update(hotkey_registered='0', hotkey=values['hotkey'],
                          hotkey_error='not registered (--no-hotkey)')
        else:
            hotkey.start()

        control = ResidentControl(monitor=monitor, status=status)
        start_control_listener(control)
        threading.Thread(target=_heartbeat_loop, args=(status, control), daemon=True).start()

        print('Clipboard+ resident started (pid %d). Safe Copy: %s'
              % (os.getpid(), values['hotkey']))

        stamp = _settings_stamp()
        while True:
            if _kernel32.WaitForSingleObject(stop_handle, 500) == WAIT_OBJECT_0:
                print('🛑 Stop signal received. Exiting cleanly.')
                break
            if control.reload_event.is_set():
                control.reload_event.clear()
                reload_settings(hotkey, monitor, status)
            new_stamp = _settings_stamp()
            if new_stamp != stamp:
                stamp = new_stamp
                reload_settings(hotkey, monitor, status)
        return 0
    except KeyboardInterrupt:
        print('\n🛑 Interrupted. Exiting cleanly.')
        return 0
    finally:
        if hotkey is not None:
            try:
                hotkey.stop()
            except Exception as exc:
                print('⚠️ Hotkey shutdown error: %s' % exc)
        if monitor is not None:
            try:
                monitor.stop()
            except Exception as exc:
                print('⚠️ Monitor shutdown error: %s' % exc)
        if status is not None:
            try:
                status.update(monitor='0', hotkey_registered='0', stopped='1')
            except Exception:
                pass
        if stop_handle:
            _kernel32.CloseHandle(stop_handle)
        if handle:
            _kernel32.CloseHandle(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clipboard+ with Safe Share Sanitizer")
    parser.add_argument("--sanitize-text-once", action="store_true", help="One-shot sanitize text currently in clipboard")
    parser.add_argument("--restore-last-safe-copy", action="store_true", help="Restore clipboard text prior to last Safe Copy")
    parser.add_argument("--save-path", default=None, help="Folder to save captured clipboard images (overrides the saved setting for this run)")
    parser.add_argument("--start", action="store_true", help="Start the resident (idempotent: an existing resident is left alone)")
    parser.add_argument("--stop", action="store_true", help="Ask the resident to exit cleanly")
    parser.add_argument("--status", action="store_true", help="Print the resident state")
    parser.add_argument("--check-deps", action="store_true", help="Report dependency readiness and exit")
    parser.add_argument("--settings", action="store_true", help="Open the Clipboard+ settings dialog")
    parser.add_argument("--state-dir", default=None, help="Override the per-user state directory (tests)")
    parser.add_argument("--no-hotkey", action="store_true", help="Do not register the global Safe Copy hotkey (tests/CI)")
    args = parser.parse_args(argv)

    if args.state_dir:
        os.environ[STATE_DIR_ENV] = args.state_dir

    if args.check_deps:
        return cmd_check_deps()
    if args.status:
        return cmd_status()
    if args.stop:
        return cmd_stop()
    if args.settings:
        return run_settings_gui()
    if args.sanitize_text_once:
        return cmd_safe_copy_once()
    if args.restore_last_safe_copy:
        return cmd_restore_once()
    return run_resident(args)


if __name__ == "__main__":
    sys.exit(main())
