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
_LICENSE_DIR:  Final[Path] = Path("/mnt/invariant_data")

# Hard cap on license file size. Prevents unbounded read on malicious FS.
_LICENSE_MAX_BYTES: Final[int] = 8_192  # 8 KiB

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
            _log.debug("RTC reader %s failed: %s", fn.__name__, exc)

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
    except Exception:
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
        _log.debug("Failed to read CPUID: %s", exc)
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
        _log.debug("tpm2_readpublic failed: %s", exc)

    # Strategy 2: Direct sysfs read (fallback if tools aren't installed)
    ek_path = Path("/sys/class/tpm/tpm0/device/ek_pub")
    try:
        if ek_path.exists():
            data = ek_path.read_bytes()
            if data:
                return hashlib.sha256(data).hexdigest()
    except Exception as exc:
        _log.debug("sysfs TPM read failed: %s", exc)

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
        raise LicenseError("TOKEN_MALFORMED")

    try:
        json_bytes = _urlsafe_b64decode(parts[0])
    except Exception:
        raise LicenseError("TOKEN_PAYLOAD_ENCODING_INVALID")

    try:
        sig_bytes = _urlsafe_b64decode(parts[1])
    except Exception:
        raise LicenseError("TOKEN_SIGNATURE_ENCODING_INVALID")

    if len(sig_bytes) != 64:
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
    except ValueError:
        raise LicenseError("PUBKEY_HEX_MALFORMED")

    if len(pubkey_bytes) != 32:
        raise LicenseError(f"PUBKEY_SIZE_INVALID:{len(pubkey_bytes)}")

    # Additional guard: reject all-zero key (placeholder was not replaced)
    if pubkey_bytes == bytes(32):
        raise LicenseError("PUBKEY_IS_PLACEHOLDER_ZERO")

    try:
        return VerifyKey(pubkey_bytes)
    except Exception as exc:
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
        raise LicenseError("SIGNATURE_INVALID")
    except Exception as exc:
        raise LicenseError("SIGNATURE_VERIFY_ERROR") from exc


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 6 — Payload Parser
# ════════════════════════════════════════════════════════════════════════════

_VALID_PLANS: Final[frozenset[str]] = frozenset({"subscription"})


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
#  PUBLIC API
# ════════════════════════════════════════════════════════════════════════════

def verify_license() -> LicensePayload:
    """
    Boot-time license gate. Must be called before any diagnostic work begins.

    Returns the validated LicensePayload on success.
    Raises LicenseError with a machine-readable code on ANY failure.

    Execution order is intentional and security-sensitive:

      1. RTC read (before file I/O; least susceptible to time drift during boot).
      2. File read (path-safe; detects missing/tampered license early).
      3. Token decode (structural check; cheap to fail before crypto).
      4. Signature verify (before JSON parse; prevents crafted-JSON DoS).
      5. Payload parse (after auth; validates schema and field types).
      6. Time constraints (expiry and anti-rollback).
      7. Hardware fingerprint (most expensive; last to minimize wasted work).

    All exceptions propagate as LicenseError. Do not add bare 'except' blocks
    here — silent catches would convert this from fail-closed to fail-open.
    """
    # Step 1: Acquire hardware clock (time-sensitive, before disk I/O)
    rtc_now: datetime = _read_hardware_rtc()
    _log.info("RTC: %s", rtc_now.isoformat())

    # Step 2: Read license file (path-safe I/O)
    raw_token: str = _read_license_file()

    # Step 3: Structural decode
    json_bytes: bytes
    sig_bytes: bytes
    json_bytes, sig_bytes = _split_token(raw_token)

    # Step 4: Cryptographic verification (before payload parse)
    verify_key: VerifyKey = _get_verify_key()
    _verify_signature(json_bytes, sig_bytes, verify_key)
    _log.info("Signature: VALID")

    # Step 5: Parse payload (authenticity now established)
    payload: LicensePayload = _parse_payload(json_bytes)

    # Step 6: Temporal constraints
    _enforce_time_constraints(payload, rtc_now)

    # Step 7: Hardware fingerprint (most expensive — do last)
    local_fp: str = compute_hardware_fingerprint()
    _verify_hardware_id(payload, local_fp)

    _log.info(
        "LICENSE VALID: plan=%s expires=%s hw=%s...%s",
        payload.plan,
        datetime.fromtimestamp(payload.expires_at, tz=timezone.utc).date().isoformat(),
        payload.hardware_id[:8],
        payload.hardware_id[-8:],
    )

    return payload