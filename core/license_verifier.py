"""
core/license_verifier.py
========================
Boot-time cryptographic license verifier for probe.tex.

SECURITY MODEL
--------------
  Ed25519 private key lives exclusively on invariant.api (Go backend).
  The public key is embedded here at build time (32 bytes, hex constant).
  An attacker with the public key can verify tokens but cannot forge them.

TOKEN FORMAT (wire format of /mnt/invariant_data/license.sig)
-------------------------------------------------------------
  base64url_raw(utf8_json) + "." + base64url_raw(ed25519_sig_64B)
  Signature covers the exact UTF-8 bytes of the JSON payload.
  JSON field order is fixed by Go's struct declaration order:
    v, hardware_id, plan, issued_at, expires_at, nonce

FAIL-CLOSED CONTRACT
--------------------
  verify_license() → LicensePayload   (success)
  verify_license() → raises LicenseError  (ANY failure)

  No partial success. No fallthrough. No catch-all except in the
  outermost caller (main.py) which translates LicenseError into a
  full-screen lock with the error code logged locally.

INTEGRATION (main.py, top of render_pdf()):
  from core.license_verifier import LicenseError, verify_license
  try:
      _license = verify_license()
      print(f"[LICENSE] plan={_license.plan}")
  except LicenseError as _e:
      _trigger_lockscreen(_e.code)  # implement in tui.py
      return

FORGE.SH PATCH (runs during ISO build):
  sed -i "s/^_ISO_BUILD_TIMESTAMP:.*=.*$/\
_ISO_BUILD_TIMESTAMP: Final[int] = $(date -u +%s)/" \
    /path/to/probe.tex/core/license_verifier.py

DEPENDENCY:
  pip install pynacl==1.5.0
  (PyNaCl wraps libsodium; Ed25519 verification is internally constant-time.)
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json as _json
import logging
import os
import secrets as _secrets
import sys
import stat as _stat
import struct
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from pathlib import Path
from typing import Final

import nacl.exceptions
from nacl.signing import VerifyKey

# ════════════════════════════════════════════════════════════════════════════
#  BUILD-TIME SECURITY CONSTANTS
#  These must be set correctly before each ISO build.
#  Incorrect values will either reject all valid licenses or weaken security.
# ════════════════════════════════════════════════════════════════════════════

# Ed25519 public key — 32 bytes, 64 lowercase hex chars.
# Obtain from: GET /api/v1/admin/license/pubkey (after running keygen).
# This constant MUST match the private key used by invariant.api.
# Mismatching this value causes every valid license to be rejected.
_EMBEDDED_PUBKEY_HEX: Final[str] = (
    "98ee0e2e03126d80cbd5e846acc0a6afa83d19ad3d6c84424a0a093bf46adeed"
    # ^^^ REPLACE WITH ACTUAL 64-HEX-CHAR ED25519 PUBLIC KEY BEFORE EACH BUILD ^^^
)

# ISO build timestamp (UTC Unix seconds).
# Patched by forge.sh at build time via the sed command shown in module docstring.
# MUST be > 0 in production. A value of 0 disables the anti-rollback check,
# which should ONLY be used during local development.
_ISO_BUILD_TIMESTAMP: Final[int] = 0  # FORGE_PATCH_BUILD_TIMESTAMP

# ════════════════════════════════════════════════════════════════════════════
#  OPERATIONAL CONSTANTS — do not change without testing
# ════════════════════════════════════════════════════════════════════════════

# Absolute path to the license file on the USB data partition.
# The verifier refuses to read from any other path.
_LICENSE_FILE: Final[Path] = Path("/mnt/invariant_data/license.sig")
_BOOTSTRAP_FILE: Final[Path] = Path("/mnt/invariant_data/bootstrap.sig")
_LICENSE_DIR:  Final[Path] = Path("/mnt/invariant_data")

# Hard cap on license file size. Prevents unbounded read on malicious FS.
_LICENSE_MAX_BYTES: Final[int] = 8_192  # 8 KiB

# Air-Gap Bridge: activation URL base
_ACTIVATION_URL_BASE: Final[str] = "https://invariant-api.onrender.com/api/v1/license/activate"

# RTC ioctl — RTC_RD_TIME = _IOR('p', 0x09, struct rtc_time)
# x86_64: sizeof(struct rtc_time) = 9 × sizeof(int) = 36 bytes.
# Verified against linux/rtc.h for kernel ≥ 4.0.
_RTC_RD_TIME: Final[int]      = 0x80247009
_RTC_STRUCT:  Final[struct.Struct] = struct.Struct("9i")  # 9 signed ints, native endian

# DMI sources for hardware fingerprinting, in priority order.
_DMI_FIELDS: Final[tuple[str, ...]] = (
    "system-uuid",            # globally unique per board (RFC 4122)
    "baseboard-serial-number",# PCB serial number
    "system-serial-number",   # chassis serial number (OEM)
)

# Placeholder values that manufacturers write into DMI — treated as absent.
_DMI_INVALID_VALUES: Final[frozenset[str]] = frozenset({
    "", "not specified", "to be filled by o.e.m.", "to be filled by oem",
    "none", "n/a", "default string", "unknown", "not present",
    "chassis manufacture", "system manufacturer", "system product name",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",  # Samsung/Gigabyte placeholder UUID
})

_log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
#  EXCEPTION
# ════════════════════════════════════════════════════════════════════════════

class LicenseError(Exception):
    """
    Raised on any license validation failure.

    ``code`` is a machine-readable identifier used for telemetry and log
    correlation. It is intentionally opaque in the user-facing lock screen
    to prevent attackers from learning which specific check failed.

    Callers must not catch this selectively — treat it as fatal.
    """
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# ════════════════════════════════════════════════════════════════════════════
#  PAYLOAD
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class LicensePayload:
    """Immutable, verified license payload. Only constructed after sig check."""
    version:     int
    hardware_id: str
    plan:        str
    issued_at:   int   # UTC Unix seconds
    expires_at:  int   # UTC Unix seconds
    nonce:       str


@dataclass(frozen=True, slots=True)
class BootstrapPayload:
    """Hardware-unbound bootstrap token. Contains activation_secret for PIN verification."""
    version:           int
    subscription_id:   str
    plan:              str
    issued_at:         int   # UTC Unix seconds
    expires_at:        int   # UTC Unix seconds
    nonce:             str
    activation_secret: str   # 64-char hex HMAC key

# ════════════════════════════════════════════════════════════════════════════
#  LAYER 1 — Hardware RTC Reader
#  Goal: obtain a timestamp that the OS cannot forge via adjtimex/settimeofday.
# ════════════════════════════════════════════════════════════════════════════

def _rtc_via_ioctl() -> datetime:
    """
    Reads the hardware CMOS/RTC via ioctl(RTC_RD_TIME) on /dev/rtc0.

    This reads the RTC chip register file directly through the kernel rtc
    driver, bypassing the kernel's software timekeeping layer.
    The kernel timekeeping layer (CLOCK_REALTIME) can be set by root via
    clock_settime(2)/adjtimex(2). The RTC driver does not expose a
    corresponding write path through this ioctl; writing to the RTC requires
    a separate RTC_SET_TIME ioctl, which also requires CAP_SYS_TIME.
    Since we are the root process on our own Live OS, we trust ourselves
    but distrust any pre-boot BIOS clock manipulation.

    Result is always UTC (Linux convention: RTC stores UTC).
    """
    buf = bytearray(_RTC_STRUCT.size)
    with open("/dev/rtc0", "rb") as rtc_fd:
        # ioctl with mutate=True: kernel writes result into `buf` in place.
        fcntl.ioctl(rtc_fd.fileno(), _RTC_RD_TIME, buf, True)

    (tm_sec, tm_min, tm_hour,
     tm_mday, tm_mon, tm_year,
     _wday, _yday, _isdst) = _RTC_STRUCT.unpack_from(buf)

    # Sanity-check raw fields before constructing datetime.
    # A firmware bug or hardware glitch could produce values that would
    # otherwise cause an unguided ValueError deep in datetime().
    if not (0 <= tm_sec <= 60):  # 60 allowed for leap seconds
        raise ValueError(f"RTC: implausible seconds={tm_sec}")
    if not (0 <= tm_min <= 59 and 0 <= tm_hour <= 23):
        raise ValueError(f"RTC: implausible time {tm_hour}:{tm_min}:{tm_sec}")
    if not (1 <= tm_mday <= 31 and 0 <= tm_mon <= 11):
        raise ValueError(f"RTC: implausible date mday={tm_mday} mon={tm_mon}")

    year = tm_year + 1900
    if not (2024 <= year <= 2099):
        raise ValueError(f"RTC: year {year} outside plausible range")

    return datetime(
        year=year,
        month=tm_mon + 1,  # struct rtc_time: January = 0
        day=tm_mday,
        hour=tm_hour,
        minute=tm_min,
        second=min(tm_sec, 59),  # clamp leap second for datetime compat
        tzinfo=timezone.utc,
    )


def _rtc_via_sysfs() -> datetime:
    """
    Fallback: reads /sys/class/rtc/rtc0/{time,date}.

    The rtc driver populates these files from the same register read as
    the ioctl path. Less direct, but still outside the SW timekeeping layer.
    Always UTC per Linux rtc driver convention.
    """
    time_str = Path("/sys/class/rtc/rtc0/time").read_text(encoding="ascii").strip()
    date_str = Path("/sys/class/rtc/rtc0/date").read_text(encoding="ascii").strip()
    # Formats: "HH:MM:SS" and "YYYY-MM-DD"
    dt = datetime.strptime(f"{date_str}T{time_str}", "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def _read_hardware_rtc() -> datetime:
    """
    Acquires RTC time via ioctl (primary) then sysfs (fallback).
    Raises LicenseError if all methods fail.
    """
    for fn in (_rtc_via_ioctl, _rtc_via_sysfs):
        try:
            return fn()
        except Exception as exc:
            _log.error("RTC reader %s failed: %s", fn.__name__, exc, exc_info=True)
            sys.stderr.write(
                f"[LICENSE DEBUG] RTC reader {fn.__name__} failed: "
                f"{type(exc).__name__}: {exc}\n"
            )

    sys.stderr.write("[LICENSE DEBUG] All RTC readers failed. Cannot determine hardware time.\n")
    raise LicenseError("RTC_READ_FAILURE")


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 2 — Hardware Fingerprint
# ════════════════════════════════════════════════════════════════════════════

def _read_dmi(field: str) -> str | None:
    """
    Reads a DMI field via dmidecode. Returns None if the field is missing,
    unreadable, or contains a known-invalid placeholder value.
    """
    try:
        result = subprocess.run(
            ["dmidecode", "-s", field],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        val = result.stdout.strip()
        return val if val.lower() not in _DMI_INVALID_VALUES else None
    except Exception as exc:
        _log.error("DMI read failed for field=%s: %s", field, exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] DMI read failed for field={field}: "
            f"{type(exc).__name__}: {exc}\n"
        )
        return None

def _read_cpuid() -> str | None:
    """
    Extracts the CPUID signature from the SMBIOS Processor table.
    Bypasses OS-level spoofing by reading the firmware table directly.
    """
    try:
        result = subprocess.run(
            ["dmidecode", "-t", "processor"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        # Search for lines like: ID: C3 06 09 00 FF FB EB BF
        match = re.search(r"^\s*ID:\s*(.+)$", result.stdout, re.MULTILINE)
        if match:
            # Strip whitespace and normalize to uppercase hex
            return match.group(1).strip().replace(" ", "").upper()
    except Exception as exc:
        _log.error("Failed to read CPUID: %s", exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] CPUID read failed: {type(exc).__name__}: {exc}\n"
        )
    return None

def _read_tpm_hash() -> str | None:
    """
    Attempts to extract a unique TPM identifier (Endorsement Key).
    Since EK is burned in at manufacturing, it is a robust physical anchor.
    """
    # Strategy 1: Use tpm2-tools to read the EK public key
    try:
        result = subprocess.run(
            ["tpm2_readpublic", "-c", "ek"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            # Hash the stdout to normalize
            return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    except Exception as exc:
        _log.error("tpm2_readpublic failed: %s", exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] tpm2_readpublic failed: {type(exc).__name__}: {exc}\n"
        )

    # Strategy 2: Direct sysfs read (fallback if tools aren't installed)
    ek_path = Path("/sys/class/tpm/tpm0/device/ek_pub")
    try:
        if ek_path.exists():
            data = ek_path.read_bytes()
            if data:
                return hashlib.sha256(data).hexdigest()
    except Exception as exc:
        _log.error("sysfs TPM read failed: %s", exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] sysfs TPM read failed: {type(exc).__name__}: {exc}\n"
        )

    return None


def compute_hardware_fingerprint() -> str:
    """
    Produces a deterministic SHA-256 fingerprint of this machine's physical hardware.
    
    Combines:
    1. Motherboard Serial / UUID (DMI)
    2. Processor ID (CPUID)
    3. TPM Endorsement Key Hash (If present)

    Components are formatted as "prefix:value" strings, sorted
    lexicographically, and joined with NUL bytes before hashing. Sorting
    ensures the fingerprint is identical regardless of which subset of fields
    is readable (order-independent accumulation).

    Raises LicenseError("FINGERPRINT_UNAVAILABLE") if NO valid anchor is
    found — this indicates an anomalous or heavily spoofed environment.

    Returns
    -------
    str
        64-char lowercase hex string (SHA-256 of canonical component string).
    """
    components: list[str] = []
    
    # 1. Motherboard / System DMI
    for field in _DMI_FIELDS:
        val = _read_dmi(field)
        if val is not None:
            components.append(f"dmi:{field}:{val}")
            _log.debug("Fingerprint component: dmi:%s=<redacted>", field)
            
    # 2. Processor ID
    cpuid = _read_cpuid()
    if cpuid is not None:
        components.append(f"cpu:id:{cpuid}")
        _log.debug("Fingerprint component: cpu:id=<redacted>")
        
    # 3. TPM Endorsement Key
    tpm_hash = _read_tpm_hash()
    if tpm_hash is not None:
        components.append(f"tpm:ek:{tpm_hash}")
        _log.debug("Fingerprint component: tpm:ek=<redacted>")

    if not components:
        _log.error(
            "FINGERPRINT_UNAVAILABLE: no hardware anchors found. "
            "DMI fields attempted: %s", _DMI_FIELDS
        )
        sys.stderr.write(
            "[LICENSE DEBUG] FINGERPRINT_UNAVAILABLE: no hardware anchors found. "
            f"DMI fields attempted: {_DMI_FIELDS}\n"
        )
        raise LicenseError("FINGERPRINT_UNAVAILABLE")

    # Sorted + NUL-joined: deterministic regardless of collection order.
    canonical = "\x00".join(sorted(components))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 3 — License File Reader (Path-Safe)
# ════════════════════════════════════════════════════════════════════════════

def _read_license_file() -> str:
    """
    Reads the raw token string from the license file with strict path validation.

    Security properties enforced:
    ─ lstat() check: file must not be a symlink (symlink substitution attack).
    ─ resolve() + relative_to(): real path must be inside _LICENSE_DIR
      (path traversal via directory junction or bind mount).
    ─ Size cap: prevents DoS via arbitrarily large file pre-allocation.
    ─ Single read: no re-open between stat and read (TOCTOU window is minimal).

    Note on TOCTOU: there is an inherent race between lstat() and open() on
    any POSIX filesystem. On our read-only squashfs Live OS root, the only
    writable mount is the USB data partition at /mnt/invariant_data.
    An attacker with write access to that partition can already modify
    license.sig itself, so the attack surface is not expanded by this window.
    """
    target = _LICENSE_FILE

    # --- Existence check ---
    try:
        lstat_result = target.lstat()
    except FileNotFoundError:
        raise LicenseError("LICENSE_FILE_NOT_FOUND")
    except OSError as exc:
        raise LicenseError("LICENSE_FILE_STAT_ERROR") from exc

    # --- Symlink rejection ---
    if _stat.S_ISLNK(lstat_result.st_mode):
        _log.warning("License file %s is a symlink — rejecting", target)
        raise LicenseError("LICENSE_FILE_IS_SYMLINK")

    # --- Regular file check ---
    if not _stat.S_ISREG(lstat_result.st_mode):
        raise LicenseError("LICENSE_FILE_NOT_REGULAR")

    # --- Size guards ---
    size = lstat_result.st_size
    if size == 0:
        raise LicenseError("LICENSE_FILE_EMPTY")
    if size > _LICENSE_MAX_BYTES:
        raise LicenseError("LICENSE_FILE_TOO_LARGE")

    # --- Path confinement: resolve symlinks and check parent ---
    try:
        real_path = target.resolve(strict=True)
        real_dir  = _LICENSE_DIR.resolve()
        real_path.relative_to(real_dir)  # raises ValueError if outside
    except ValueError:
        _log.error("Path traversal: %s resolved outside %s", target, _LICENSE_DIR)
        raise LicenseError("LICENSE_FILE_PATH_TRAVERSAL")
    except OSError as exc:
        raise LicenseError("LICENSE_FILE_RESOLVE_ERROR") from exc

    # --- Single read ---
    try:
        raw = target.read_text(encoding="ascii").strip()
    except Exception as exc:
        raise LicenseError("LICENSE_FILE_READ_FAILED") from exc

    if not raw:
        raise LicenseError("LICENSE_FILE_EMPTY_CONTENT")

    return raw


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 4 — Token Decoder
# ════════════════════════════════════════════════════════════════════════════

def _urlsafe_b64decode(s: str) -> bytes:
    """
    Decodes a base64url string with no padding (RawURLEncoding from Go).
    Adds the correct number of '=' pad chars: (-len(s)) % 4.
    """
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def _split_token(raw: str) -> tuple[bytes, bytes]:
    """
    Splits and decodes the two base64url components of a license token.

    Returns (json_bytes, sig_bytes).
    Raises LicenseError on any structural or encoding failure.
    """
    parts = raw.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        _log.error("TOKEN_MALFORMED: raw[:50]=%r", raw[:50])
        sys.stderr.write(
            f"[LICENSE DEBUG] TOKEN_MALFORMED: raw[:50]={raw[:50]!r}\n"
        )
        raise LicenseError("TOKEN_MALFORMED")

    try:
        json_bytes = _urlsafe_b64decode(parts[0])
    except Exception as exc:
        _log.error(
            "TOKEN_PAYLOAD_ENCODING_INVALID: %s — raw[:50]=%r",
            exc, raw[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] TOKEN_PAYLOAD_ENCODING_INVALID: {type(exc).__name__}: {exc} "
            f"— raw[:50]={raw[:50]!r}\n"
        )
        raise LicenseError("TOKEN_PAYLOAD_ENCODING_INVALID")

    try:
        sig_bytes = _urlsafe_b64decode(parts[1])
    except Exception as exc:
        _log.error(
            "TOKEN_SIGNATURE_ENCODING_INVALID: %s — raw[:50]=%r",
            exc, raw[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] TOKEN_SIGNATURE_ENCODING_INVALID: {type(exc).__name__}: {exc} "
            f"— raw[:50]={raw[:50]!r}\n"
        )
        raise LicenseError("TOKEN_SIGNATURE_ENCODING_INVALID")

    if len(sig_bytes) != 64:
        _log.error(
            "TOKEN_SIGNATURE_SIZE_INVALID: expected=64 got=%d — raw[:50]=%r",
            len(sig_bytes), raw[:50],
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] TOKEN_SIGNATURE_SIZE_INVALID: expected=64 got={len(sig_bytes)} "
            f"— raw[:50]={raw[:50]!r}\n"
        )
        raise LicenseError(f"TOKEN_SIGNATURE_SIZE_INVALID:{len(sig_bytes)}")

    return json_bytes, sig_bytes


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 5 — Signature Verification
# ════════════════════════════════════════════════════════════════════════════

def _get_verify_key() -> VerifyKey:
    """
    Constructs the VerifyKey from the embedded constant.
    This will fail at startup if the constant was not set correctly,
    which is the desired behavior (find config errors early).
    """
    try:
        pubkey_bytes = bytes.fromhex(_EMBEDDED_PUBKEY_HEX)
    except ValueError as exc:
        _log.error(
            "PUBKEY_HEX_MALFORMED: _EMBEDDED_PUBKEY_HEX=%r...",
            _EMBEDDED_PUBKEY_HEX[:20], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] PUBKEY_HEX_MALFORMED: {type(exc).__name__}: {exc}\n"
        )
        raise LicenseError("PUBKEY_HEX_MALFORMED")

    if len(pubkey_bytes) != 32:
        _log.error("PUBKEY_SIZE_INVALID: expected=32 got=%d", len(pubkey_bytes))
        sys.stderr.write(
            f"[LICENSE DEBUG] PUBKEY_SIZE_INVALID: expected=32 got={len(pubkey_bytes)}\n"
        )
        raise LicenseError(f"PUBKEY_SIZE_INVALID:{len(pubkey_bytes)}")

    # Additional guard: reject all-zero key (placeholder was not replaced)
    if pubkey_bytes == bytes(32):
        _log.error("PUBKEY_IS_PLACEHOLDER_ZERO")
        sys.stderr.write("[LICENSE DEBUG] PUBKEY_IS_PLACEHOLDER_ZERO\n")
        raise LicenseError("PUBKEY_IS_PLACEHOLDER_ZERO")

    try:
        return VerifyKey(pubkey_bytes)
    except Exception as exc:
        _log.error("PUBKEY_CONSTRUCTION_FAILED: %s", exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] PUBKEY_CONSTRUCTION_FAILED: {type(exc).__name__}: {exc}\n"
        )
        raise LicenseError("PUBKEY_CONSTRUCTION_FAILED") from exc


def _verify_signature(
    json_bytes: bytes,
    sig_bytes: bytes,
    verify_key: VerifyKey,
) -> None:
    """
    Verifies Ed25519 signature over json_bytes using PyNaCl.

    PyNaCl's VerifyKey.verify() is a thin wrapper over libsodium's
    crypto_sign_ed25519_verify_detached(), which is internally constant-time.

    Signature verification happens BEFORE JSON parsing. This prevents a
    class of attacks where a malformed-but-large JSON payload causes
    excessive CPU/memory usage in the json.loads() call.
    """
    try:
        verify_key.verify(json_bytes, sig_bytes)
    except nacl.exceptions.BadSignatureError:
        _log.error(
            "SIGNATURE_INVALID: json_bytes[:50]=%r sig_bytes[:50]=%r",
            json_bytes[:50], sig_bytes[:50],
        )
        sys.stderr.write(
            "[LICENSE DEBUG] SIGNATURE_INVALID: Ed25519 signature does not match payload.\n"
        )
        raise LicenseError("SIGNATURE_INVALID")
    except Exception as exc:
        _log.error(
            "SIGNATURE_VERIFY_ERROR: %s — json_bytes[:50]=%r sig_bytes[:50]=%r",
            exc, json_bytes[:50], sig_bytes[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] SIGNATURE_VERIFY_ERROR: {type(exc).__name__}: {exc}\n"
        )
        raise LicenseError("SIGNATURE_VERIFY_ERROR") from exc


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 6 — Payload Parser
# ════════════════════════════════════════════════════════════════════════════

_VALID_PLANS: Final[frozenset[str]] = frozenset({
    "subscription",
    "trial",
    "trial_pending",
    "admin_override",
})


def _parse_payload(json_bytes: bytes) -> LicensePayload:
    """
    Parses and structurally validates the signed JSON payload.

    Called ONLY after signature verification succeeds, so the json_bytes
    are known to be authentic. Validation here guards against issuer bugs
    or future schema drift.
    """
    try:
        data: object = _json.loads(json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        _log.error(
            "PAYLOAD_DECODE_FAILED: %s — json_bytes[:50]=%r",
            exc, json_bytes[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] PAYLOAD_DECODE_FAILED: {type(exc).__name__}: {exc} "
            f"— json_bytes[:50]={json_bytes[:50]!r}\n"
        )
        raise LicenseError("PAYLOAD_DECODE_FAILED") from exc

    if not isinstance(data, dict):
        raise LicenseError("PAYLOAD_NOT_OBJECT")

    version = data.get("v")
    if version != 1:
        raise LicenseError(f"PAYLOAD_VERSION_UNSUPPORTED:{version!r}")

    hardware_id = data.get("hardware_id")
    plan        = data.get("plan")
    issued_at   = data.get("issued_at")
    expires_at  = data.get("expires_at")
    nonce       = data.get("nonce")

    if not (isinstance(hardware_id, str) and len(hardware_id) == 64
            and hardware_id.isascii() and hardware_id == hardware_id.lower()):
        raise LicenseError("PAYLOAD_HARDWARE_ID_INVALID")

    if not (isinstance(plan, str) and plan in _VALID_PLANS):
        raise LicenseError(f"PAYLOAD_PLAN_INVALID:{plan!r}")

    if not (isinstance(issued_at, int) and issued_at > 0):
        raise LicenseError("PAYLOAD_ISSUED_AT_INVALID")

    if not (isinstance(expires_at, int) and expires_at > issued_at):
        raise LicenseError("PAYLOAD_EXPIRES_AT_INVALID")

    if not (isinstance(nonce, str) and len(nonce) == 32 and nonce.isascii()):
        raise LicenseError("PAYLOAD_NONCE_INVALID")

    return LicensePayload(
        version=version,
        hardware_id=hardware_id,
        plan=plan,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 7 — Time Trap (Anti-Rollback + Expiry)
# ════════════════════════════════════════════════════════════════════════════

def _enforce_time_constraints(payload: LicensePayload, rtc_now: datetime) -> None:
    """
    Enforces two temporal conditions using the hardware RTC clock.

    Condition 1 — Anti-Rollback Guard:
        rtc_unix < _ISO_BUILD_TIMESTAMP  →  BLOCK
        The hardware clock is before this ISO was built. This is physically
        impossible without deliberately rolling back the BIOS clock.
        Attackers roll back the clock to bring an expired license back into
        its valid window. The ISO build timestamp is the oldest possible
        valid time on a machine running this specific ISO.
        Skipped if _ISO_BUILD_TIMESTAMP == 0 (development/test builds only).

    Condition 2 — Expiry Check:
        rtc_unix >= payload.expires_at  →  BLOCK
        The license has expired.

    Note: the comparison uses '>=' for expiry (not '>'), meaning that at
    the exact second of expiry the license is already invalid. This is
    intentional (fail-closed on the boundary).

    No fuzzing, grace periods, or clock skew tolerance is applied. This is
    a physical device with a hardware clock, not a distributed system.
    """
    rtc_unix: int = int(rtc_now.timestamp())

    # --- Condition 1: Anti-rollback ---
    if _ISO_BUILD_TIMESTAMP > 0:
        if rtc_unix < _ISO_BUILD_TIMESTAMP:
            delta = _ISO_BUILD_TIMESTAMP - rtc_unix
            _log.error(
                "TIME_TRAP TRIGGERED: rtc=%d build_ts=%d delta=-%ds. "
                "BIOS clock rollback detected.",
                rtc_unix, _ISO_BUILD_TIMESTAMP, delta,
            )
            raise LicenseError("TIME_ROLLBACK_DETECTED")

    # --- Condition 2: Expiry ---
    if rtc_unix >= payload.expires_at:
        overdraft = rtc_unix - payload.expires_at
        _log.warning(
            "LICENSE_EXPIRED: rtc=%d expires_at=%d overdraft=%ds",
            rtc_unix, payload.expires_at, overdraft,
        )
        raise LicenseError("LICENSE_EXPIRED")

    # --- Sanity (non-blocking): warn if rtc < issued_at ---
    # Could indicate clock manipulation, but has benign explanations
    # (e.g., license issued with a future-dated timestamp for testing).
    if rtc_unix < payload.issued_at:
        _log.warning(
            "TIME_ANOMALY (non-blocking): rtc=%d < issued_at=%d (delta=%ds)",
            rtc_unix, payload.issued_at, payload.issued_at - rtc_unix,
        )

    _log.debug(
        "Time OK: rtc=%s expires_in=%ds",
        rtc_now.isoformat(), payload.expires_at - rtc_unix,
    )


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 8 — Hardware ID Verification (Constant-Time)
# ════════════════════════════════════════════════════════════════════════════

def _verify_hardware_id(payload: LicensePayload, local_fp: str) -> None:
    """
    Compares the license's hardware_id against this machine's fingerprint.

    hmac.compare_digest() performs a constant-time string comparison,
    preventing timing side-channels that could leak how many characters of
    the expected fingerprint match the local fingerprint. While this is
    less critical here than in a network context (the attacker can't
    measure our response time remotely), it is correct practice.

    Raises LicenseError("HARDWARE_ID_MISMATCH") on failure.
    """
    # Both values are lowercase hex; normalize to be safe.
    expected = payload.hardware_id.lower()
    actual   = local_fp.lower()

    if not hmac.compare_digest(expected, actual):
        # Log only the first/last 8 chars for correlation without exposing full IDs.
        _log.error(
            "HARDWARE_MISMATCH: license_hw=%s...%s local=%s...%s",
            expected[:8], expected[-8:],
            actual[:8],   actual[-8:],
        )
        raise LicenseError("HARDWARE_ID_MISMATCH")

    _log.debug("Hardware ID verified: %s...%s", actual[:8], actual[-8:])


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 9 — Bootstrap Token Parser
# ════════════════════════════════════════════════════════════════════════════

def _parse_bootstrap_payload(json_bytes: bytes) -> BootstrapPayload:
    """
    Parses and validates a bootstrap token payload (hardware-unbound).
    Called ONLY after Ed25519 signature verification.
    """
    try:
        data: object = _json.loads(json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        _log.error(
            "BOOTSTRAP_DECODE_FAILED: %s — json_bytes[:50]=%r",
            exc, json_bytes[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] BOOTSTRAP_DECODE_FAILED: {type(exc).__name__}: {exc} "
            f"— json_bytes[:50]={json_bytes[:50]!r}\n"
        )
        raise LicenseError("BOOTSTRAP_DECODE_FAILED") from exc

    if not isinstance(data, dict):
        raise LicenseError("BOOTSTRAP_NOT_OBJECT")

    version = data.get("v")
    if version != 1:
        raise LicenseError(f"BOOTSTRAP_VERSION_UNSUPPORTED:{version!r}")

    subscription_id = data.get("subscription_id")
    plan            = data.get("plan")
    issued_at       = data.get("issued_at")
    expires_at      = data.get("expires_at")
    nonce           = data.get("nonce")
    activation_secret = data.get("activation_secret")

    if not (isinstance(subscription_id, str) and len(subscription_id) > 0):
        raise LicenseError("BOOTSTRAP_SUBSCRIPTION_ID_INVALID")

    if not (isinstance(plan, str) and plan in {"subscription", "trial", "trial_pending", "admin_override"}):
        raise LicenseError(f"BOOTSTRAP_PLAN_INVALID:{plan!r}")

    if not (isinstance(issued_at, int) and issued_at > 0):
        raise LicenseError("BOOTSTRAP_ISSUED_AT_INVALID")

    if not (isinstance(expires_at, int) and expires_at > issued_at):
        raise LicenseError("BOOTSTRAP_EXPIRES_AT_INVALID")

    if not (isinstance(nonce, str) and len(nonce) == 32 and nonce.isascii()):
        raise LicenseError("BOOTSTRAP_NONCE_INVALID")

    if not (isinstance(activation_secret, str) and len(activation_secret) == 64):
        raise LicenseError("BOOTSTRAP_SECRET_INVALID")

    return BootstrapPayload(
        version=version,
        subscription_id=subscription_id,
        plan=plan,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        activation_secret=activation_secret,
    )


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 10 — Air-Gap Bridge: Challenge-Response Activation
# ════════════════════════════════════════════════════════════════════════════

def _generate_challenge(
    bootstrap: BootstrapPayload,
    local_fp: str,
    rtc_now: datetime,
) -> tuple[dict, str]:
    """
    Generates a QR challenge dict and the activation URL.

    Returns (challenge_dict, full_url).
    """
    import base64 as _b64

    challenge_nonce = _secrets.token_hex(16)  # 32-char hex
    boot_id = _secrets.token_hex(8)           # 16-char hex

    challenge = {
        "v": 1,
        "subscription_id": bootstrap.subscription_id,
        "quimera": local_fp,
        "nonce": challenge_nonce,
        "ts": int(rtc_now.timestamp()),
        "boot_id": boot_id,
    }

    challenge_json = _json.dumps(challenge, separators=(",", ":")).encode("utf-8")
    challenge_b64 = _b64.urlsafe_b64encode(challenge_json).rstrip(b"=").decode("ascii")

    url = f"{_ACTIVATION_URL_BASE}?c={challenge_b64}"

    return challenge, url


def _verify_activation_pin(
    activation_secret: str,
    challenge_nonce: str,
    quimera: str,
    entered_pin: str,
) -> bool:
    """
    Verifies a 6-digit activation PIN against the expected HMAC derivation.

    MUST produce identical results as the Go API's computeActivationPIN().
    Uses constant-time comparison to prevent timing side-channels.
    """
    secret_bytes = bytes.fromhex(activation_secret)
    mac = hmac.new(secret_bytes, (challenge_nonce + quimera).encode(), hashlib.sha256)
    pin_material = mac.digest()

    # Take first 4 bytes as big-endian uint32, mod 1_000_000
    pin_int = int.from_bytes(pin_material[:4], "big") % 1_000_000
    expected_pin = str(pin_int).zfill(6)

    return hmac.compare_digest(expected_pin, entered_pin)


def _write_bound_license(
    bootstrap: BootstrapPayload,
    local_fp: str,
    challenge_nonce: str,
    rtc_now: datetime,
) -> None:
    """
    Writes a hardware-bound license to the INVARIANT partition.

    The bound license is signed with HMAC-SHA256 using the activation_secret,
    not Ed25519 (the ISO doesn't have the private key). On subsequent boots,
    the verifier checks this HMAC signature.

    Also deletes bootstrap.sig to prevent re-use.
    """
    bound_payload = {
        "v": 1,
        "hardware_id": local_fp,
        "subscription_id": bootstrap.subscription_id,
        "plan": bootstrap.plan,
        "issued_at": bootstrap.issued_at,
        "expires_at": bootstrap.expires_at,
        "nonce": bootstrap.nonce,
        "activated_at": int(rtc_now.timestamp()),
        "activation_nonce": challenge_nonce,
        "activation_secret": bootstrap.activation_secret,
    }

    payload_json = _json.dumps(bound_payload, separators=(",", ":")).encode("utf-8")

    # HMAC-SHA256 signature using activation_secret
    secret_bytes = bytes.fromhex(bootstrap.activation_secret)
    sig = hmac.new(secret_bytes, payload_json, hashlib.sha256).digest()

    import base64 as _b64
    enc = _b64.urlsafe_b64encode
    token = (
        enc(payload_json).rstrip(b"=").decode("ascii")
        + "."
        + enc(sig).rstrip(b"=").decode("ascii")
    )

    # Mount INVARIANT partition read-write.
    # CRITICAL: We do NOT remount to RO after writing. The exFAT driver
    # sets the volume dirty bit on any mount transition, and a subsequent
    # remount,rw on a dirty exFAT volume is silently ignored by the kernel.
    # Keeping the filesystem RW for the entire session avoids this trap.
    _mount_invariant_rw()

    try:
        _LICENSE_FILE.write_text(token, encoding="ascii")
        _log.info("Bound license written to %s", _LICENSE_FILE)

        # Delete consumed bootstrap token
        if _BOOTSTRAP_FILE.exists():
            _BOOTSTRAP_FILE.unlink()
            _log.info("Bootstrap token consumed (deleted)")
    except OSError as exc:
        _log.error("LICENSE_WRITE_FAILED: %s", exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] LICENSE_WRITE_FAILED: {type(exc).__name__}: {exc}\n"
        )
        raise LicenseError("LICENSE_WRITE_FAILED") from exc


def _mount_invariant_rw() -> None:
    """Remounts the INVARIANT partition as read-write."""
    try:
        subprocess.run(
            ["mount", "-o", "remount,rw", str(_LICENSE_DIR)],
            capture_output=True, timeout=5, check=False,
        )
    except Exception as exc:
        _log.warning("Failed to remount INVARIANT rw: %s", exc)


def _remount_invariant_ro() -> None:
    """DEPRECATED — No-op.

    Previously remounted the INVARIANT partition to read-only after writing
    the bound license. This has been disabled because the exFAT driver sets
    the volume dirty bit on mount-state transitions, and a subsequent
    remount,rw on a dirty exFAT volume is silently ignored by the kernel,
    breaking the auto-save persistence flow.
    """
    # Intentionally no-op. The filesystem stays RW for the session.
    pass


def _read_file_safe(target: Path) -> str | None:
    """
    Reads a file with the same security checks as _read_license_file,
    but returns None instead of raising on missing file.
    """
    try:
        lstat_result = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.error("File safe-read stat failed: %s — %s", target, exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] File safe-read stat failed: {target} — "
            f"{type(exc).__name__}: {exc}\n"
        )
        return None

    if _stat.S_ISLNK(lstat_result.st_mode):
        _log.warning("File safe-read rejected symlink: %s", target)
        return None
    if not _stat.S_ISREG(lstat_result.st_mode):
        _log.warning("File safe-read rejected non-regular: %s", target)
        return None
    if lstat_result.st_size == 0 or lstat_result.st_size > _LICENSE_MAX_BYTES:
        _log.warning(
            "File safe-read rejected size=%d for %s", lstat_result.st_size, target
        )
        return None

    try:
        real_path = target.resolve(strict=True)
        real_dir = _LICENSE_DIR.resolve()
        real_path.relative_to(real_dir)
    except (ValueError, OSError) as exc:
        _log.error("File safe-read path traversal: %s — %s", target, exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] File safe-read path traversal: {target} — "
            f"{type(exc).__name__}: {exc}\n"
        )
        return None

    try:
        raw = target.read_text(encoding="ascii").strip()
        _log.info("File safe-read OK: %s (len=%d)", target, len(raw))
        return raw
    except Exception as exc:
        _log.error("File safe-read failed: %s — %s", target, exc, exc_info=True)
        sys.stderr.write(
            f"[LICENSE DEBUG] File safe-read failed: {target} — "
            f"{type(exc).__name__}: {exc}\n"
        )
        return None


def _verify_bound_license(raw_token: str, rtc_now: datetime) -> LicensePayload:
    """
    Verifies a locally-signed bound license (HMAC-SHA256, post-activation).

    The bound license was written by _write_bound_license() after successful
    QR activation. It contains activation_secret for HMAC verification and
    hardware_id for machine binding.
    """
    parts = raw_token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        _log.error("BOUND_TOKEN_MALFORMED: raw[:50]=%r", raw_token[:50])
        sys.stderr.write(
            f"[LICENSE DEBUG] BOUND_TOKEN_MALFORMED: raw[:50]={raw_token[:50]!r}\n"
        )
        raise LicenseError("BOUND_TOKEN_MALFORMED")

    try:
        pad = (-len(parts[0])) % 4
        json_bytes = base64.urlsafe_b64decode(parts[0] + "=" * pad)
    except Exception as exc:
        _log.error(
            "BOUND_PAYLOAD_ENCODING_INVALID: %s — raw[:50]=%r",
            exc, raw_token[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] BOUND_PAYLOAD_ENCODING_INVALID: {type(exc).__name__}: {exc} "
            f"— raw[:50]={raw_token[:50]!r}\n"
        )
        raise LicenseError("BOUND_PAYLOAD_ENCODING_INVALID")

    try:
        pad = (-len(parts[1])) % 4
        sig_bytes = base64.urlsafe_b64decode(parts[1] + "=" * pad)
    except Exception as exc:
        _log.error(
            "BOUND_SIGNATURE_ENCODING_INVALID: %s — raw[:50]=%r",
            exc, raw_token[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] BOUND_SIGNATURE_ENCODING_INVALID: {type(exc).__name__}: {exc} "
            f"— raw[:50]={raw_token[:50]!r}\n"
        )
        raise LicenseError("BOUND_SIGNATURE_ENCODING_INVALID")

    # Parse payload first to extract activation_secret for HMAC verification
    try:
        data = _json.loads(json_bytes.decode("utf-8"))
    except Exception as exc:
        _log.error(
            "BOUND_PAYLOAD_DECODE_FAILED: %s — json_bytes[:50]=%r",
            exc, json_bytes[:50], exc_info=True,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] BOUND_PAYLOAD_DECODE_FAILED: {type(exc).__name__}: {exc} "
            f"— json_bytes[:50]={json_bytes[:50]!r}\n"
        )
        raise LicenseError("BOUND_PAYLOAD_DECODE_FAILED")

    if not isinstance(data, dict):
        raise LicenseError("BOUND_PAYLOAD_NOT_OBJECT")

    activation_secret = data.get("activation_secret")
    if not (isinstance(activation_secret, str) and len(activation_secret) == 64):
        raise LicenseError("BOUND_SECRET_INVALID")

    # Verify HMAC-SHA256 signature
    secret_bytes = bytes.fromhex(activation_secret)
    expected_sig = hmac.new(secret_bytes, json_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, sig_bytes):
        raise LicenseError("BOUND_SIGNATURE_INVALID")

    _log.info("Bound license HMAC: VALID")

    # Extract fields
    hardware_id = data.get("hardware_id")
    plan = data.get("plan")
    issued_at = data.get("issued_at")
    expires_at = data.get("expires_at")
    nonce = data.get("nonce")

    if not (isinstance(hardware_id, str) and len(hardware_id) == 64):
        raise LicenseError("BOUND_HARDWARE_ID_INVALID")

    return LicensePayload(
        version=data.get("v", 1),
        hardware_id=hardware_id,
        plan=plan if isinstance(plan, str) else "subscription",
        issued_at=issued_at if isinstance(issued_at, int) else 0,
        expires_at=expires_at if isinstance(expires_at, int) else 0,
        nonce=nonce if isinstance(nonce, str) else "",
    )


# ════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ════════════════════════════════════════════════════════════════════════════

def verify_license() -> LicensePayload:
    """
    Boot-time license gate. Must be called before any diagnostic work begins.

    Returns the validated LicensePayload on success.
    Raises LicenseError with a machine-readable code on ANY failure.

    Flow branching (Air-Gap Bridge):

      A. license.sig exists → BOUND LICENSE PATH
         1. Try Ed25519 verification (original server-signed license)
         2. If Ed25519 fails, try HMAC verification (locally-bound license)
         3. Verify hardware_id match
         4. Verify time constraints

      B. bootstrap.sig exists → ACTIVATION PATH
         1. Verify Ed25519 signature on bootstrap token
         2. Check expiry
         3. Compute Quimera Hash
         4. Display QR code with challenge
         5. Wait for PIN input from technician
         6. Verify PIN via HMAC derivation
         7. Write hardware-bound license to USB
         8. Return validated payload

      C. Neither file exists → LICENSE_FILE_NOT_FOUND

    All exceptions propagate as LicenseError. Do not add bare 'except' blocks
    here — silent catches would convert this from fail-closed to fail-open.
    """
    # Step 1: Acquire hardware clock (time-sensitive, before disk I/O)
    rtc_now: datetime = _read_hardware_rtc()
    _log.info("RTC: %s", rtc_now.isoformat())

    # Step 2: Determine which flow to execute
    license_raw = _read_file_safe(_LICENSE_FILE)
    bootstrap_raw = _read_file_safe(_BOOTSTRAP_FILE)

    if license_raw is not None:
        # ═══ PATH A: Bound license exists ═══
        return _verify_bound_license_flow(license_raw, rtc_now)

    if bootstrap_raw is not None:
        # ═══ PATH B: Bootstrap activation flow ═══
        return _verify_bootstrap_activation_flow(bootstrap_raw, rtc_now)

    # ═══ PATH C: No license at all ═══
    raise LicenseError("LICENSE_FILE_NOT_FOUND")


def _verify_bound_license_flow(raw_token: str, rtc_now: datetime) -> LicensePayload:
    """
    Path A: Verifies an existing license (Ed25519 or HMAC-signed).
    This is the original flow for server-signed licenses + the new path
    for locally-bound licenses written after QR activation.
    """
    _log.info(
        "BOUND_LICENSE_FLOW: raw[:50]=%r rtc_now=%s",
        raw_token[:50], rtc_now.isoformat(),
    )
    sys.stderr.write(
        f"[LICENSE DEBUG] BOUND_LICENSE_FLOW: raw[:50]={raw_token[:50]!r} "
        f"rtc_now={rtc_now.isoformat()}\n"
    )
    json_bytes, sig_bytes = _split_token(raw_token)
    verify_key = _get_verify_key()

    # Try Ed25519 first (server-signed license from handleForgeLicense)
    try:
        _verify_signature(json_bytes, sig_bytes, verify_key)
        _log.info("Signature: VALID (Ed25519)")
        payload = _parse_payload(json_bytes)
    except LicenseError as exc:
        # Fallback: try HMAC verification (locally-bound license from activation)
        _log.error(
            "Ed25519 verification failed (code=%s), trying HMAC bound license fallback",
            exc.code,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] Ed25519 path failed: {exc.code}, "
            f"attempting HMAC bound license fallback\n"
        )
        payload = _verify_bound_license(raw_token, rtc_now)

    # Time constraints
    _enforce_time_constraints(payload, rtc_now)

    # Hardware fingerprint
    local_fp = compute_hardware_fingerprint()
    _verify_hardware_id(payload, local_fp)

    _log.info(
        "LICENSE VALID: plan=%s expires=%s hw=%s...%s",
        payload.plan,
        datetime.fromtimestamp(payload.expires_at, tz=timezone.utc).date().isoformat(),
        payload.hardware_id[:8],
        payload.hardware_id[-8:],
    )

    return payload


def _verify_bootstrap_activation_flow(
    raw_token: str,
    rtc_now: datetime,
) -> LicensePayload:
    """
    Path B: QR Challenge-Response activation flow.

    1. Verify bootstrap token (Ed25519-signed by API)
    2. Check expiry
    3. Compute hardware fingerprint (Quimera Hash)
    4. Generate challenge + display QR code
    5. Wait for PIN from technician
    6. Verify PIN
    7. Write hardware-bound license
    8. Return payload
    """
    _log.info(
        "BOOTSTRAP_FLOW: raw[:50]=%r rtc_now=%s",
        raw_token[:50], rtc_now.isoformat(),
    )
    sys.stderr.write(
        f"[LICENSE DEBUG] BOOTSTRAP_FLOW: raw[:50]={raw_token[:50]!r} "
        f"rtc_now={rtc_now.isoformat()}\n"
    )

    from core.qr_display import (
        display_activation_screen,
        display_activation_success,
        display_activation_failure,
    )

    # Step 1: Decode + verify bootstrap token signature
    json_bytes, sig_bytes = _split_token(raw_token)
    verify_key = _get_verify_key()
    _verify_signature(json_bytes, sig_bytes, verify_key)
    _log.info("Bootstrap signature: VALID")

    # Step 2: Parse bootstrap payload
    bootstrap = _parse_bootstrap_payload(json_bytes)

    # Step 3: Check expiry
    rtc_unix = int(rtc_now.timestamp())
    if rtc_unix >= bootstrap.expires_at:
        _log.error(
            "BOOTSTRAP_EXPIRED: rtc_unix=%d expires_at=%d",
            rtc_unix, bootstrap.expires_at,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] BOOTSTRAP_EXPIRED: rtc_unix={rtc_unix} "
            f"expires_at={bootstrap.expires_at}\n"
        )
        raise LicenseError("BOOTSTRAP_EXPIRED")

    # Step 4: Anti-rollback
    if _ISO_BUILD_TIMESTAMP > 0 and rtc_unix < _ISO_BUILD_TIMESTAMP:
        _log.error(
            "TIME_ROLLBACK_DETECTED: rtc_unix=%d build_ts=%d",
            rtc_unix, _ISO_BUILD_TIMESTAMP,
        )
        sys.stderr.write(
            f"[LICENSE DEBUG] TIME_ROLLBACK_DETECTED: rtc_unix={rtc_unix} "
            f"build_ts={_ISO_BUILD_TIMESTAMP}\n"
        )
        raise LicenseError("TIME_ROLLBACK_DETECTED")

    # Step 5: Compute hardware fingerprint (Quimera Hash)
    _log.info("Computing hardware fingerprint for activation...")
    local_fp = compute_hardware_fingerprint()
    _log.info("Quimera Hash: %s...%s", local_fp[:8], local_fp[-8:])

    # Step 6: Generate challenge and display QR
    challenge, challenge_url = _generate_challenge(bootstrap, local_fp, rtc_now)
    _log.info("Challenge URL generated for activation")

    entered_pin = display_activation_screen(
        challenge_url=challenge_url,
        boot_id=challenge["boot_id"],
        timeout_seconds=300,
    )

    if entered_pin is None:
        display_activation_failure("Tiempo agotado o entrada cancelada.")
        raise LicenseError("ACTIVATION_TIMEOUT")

    # Step 7: Verify PIN
    if not _verify_activation_pin(
        activation_secret=bootstrap.activation_secret,
        challenge_nonce=challenge["nonce"],
        quimera=local_fp,
        entered_pin=entered_pin,
    ):
        display_activation_failure("PIN incorrecto. Regenere el código QR reiniciando.")
        raise LicenseError("ACTIVATION_PIN_INVALID")

    # Step 8: Write hardware-bound license
    _write_bound_license(bootstrap, local_fp, challenge["nonce"], rtc_now)
    display_activation_success()

    _log.info(
        "ACTIVATION COMPLETE: sub=%s plan=%s hw=%s...%s",
        bootstrap.subscription_id,
        bootstrap.plan,
        local_fp[:8], local_fp[-8:],
    )

    # Return a LicensePayload for compatibility with the rest of the system
    return LicensePayload(
        version=1,
        hardware_id=local_fp,
        plan=bootstrap.plan,
        issued_at=bootstrap.issued_at,
        expires_at=bootstrap.expires_at,
        nonce=bootstrap.nonce,
    )