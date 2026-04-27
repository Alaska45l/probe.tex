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
import binascii
import dataclasses
import enum
import hashlib
import hmac
import json
import logging
import re
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ed25519 library selection (PF-4): prefer cryptography, fall back to pynacl
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

if not _HAS_CRYPTOGRAPHY:
    try:
        import nacl.exceptions
        from nacl.signing import VerifyKey
        _HAS_PYNACL = True
    except ImportError:
        _HAS_PYNACL = False

_log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
#  BUILD-TIME SECURITY CONSTANTS
#  These must be set correctly before each ISO build.
#  Incorrect values will either reject all valid licenses or weaken security.
# ════════════════════════════════════════════════════════════════════════════

# ISO build timestamp (UTC Unix seconds).
# Patched by forge.sh at build time via the sed command shown in module docstring.
# MUST be > 0 in production. A value of 0 disables the anti-rollback check,
# which should ONLY be used during local development.
_ISO_BUILD_TIMESTAMP: int = 0  # patched by forge.sh

# forge.sh patch command (add to ISO build script, after timestamp patch):
# sed -i "s/^_EMBEDDED_PUBKEY_HEX: str = .*/\
# _EMBEDDED_PUBKEY_HEX: str = \"$(cat /path/to/ed25519_pub.hex)\" /" \
#   /path/to/probe-tex/core/license_verifier.py
_EMBEDDED_PUBKEY_HEX: str = "0" * 64  # patched by forge.sh

# ════════════════════════════════════════════════════════════════════════════
#  PATH CONSTANTS — RD-1 File Manifest
# ════════════════════════════════════════════════════════════════════════════

_MOUNT_POINT: Path = Path("/mnt/invariant_data")
_PROV_STAMP_FILE: Path = _MOUNT_POINT / ".invariant_prov_stamp"
_BOOTSTRAP_FILE: Path = _MOUNT_POINT / "bootstrap.sig"
_LICENSE_FILE: Path = _MOUNT_POINT / "license.sig"
_SECRET_FILE: Path = _MOUNT_POINT / ".invariant_secret"
_HWM_FILE: Path = _MOUNT_POINT / ".invariant_rtc_hwm"
_HWM_NEW_FILE: Path = _MOUNT_POINT / ".invariant_rtc_hwm.new"
_HWM_BAK_FILE: Path = _MOUNT_POINT / ".invariant_rtc_hwm.bak"

# Hard cap on license file size. Prevents unbounded read on malicious FS.
_LICENSE_MAX_BYTES: int = 8_192  # 8 KiB

# Air-Gap Bridge: activation URL base
_ACTIVATION_URL_BASE: str = "https://invariant-api.onrender.com/api/v1/license/activate"

# ════════════════════════════════════════════════════════════════════════════
#  EXCEPTION
# ════════════════════════════════════════════════════════════════════════════

class LicenseErrorCode(str, enum.Enum):
    """Machine-readable failure identifiers."""
    TIME_ROLLBACK_DETECTED = "TIME_ROLLBACK_DETECTED"
    RTC_READ_FAILED = "RTC_READ_FAILED"
    HWM_WRITE_FAILED = "HWM_WRITE_FAILED"
    FINGERPRINT_UNAVAILABLE = "FINGERPRINT_UNAVAILABLE"
    ERR_WITNESS_MISSING = "ERR_WITNESS_MISSING"
    ERR_WITNESS_ALREADY_EXISTS = "ERR_WITNESS_ALREADY_EXISTS"
    ERR_LICENSE_DELETED = "ERR_LICENSE_DELETED"
    LICENSE_FILE_NOT_FOUND = "LICENSE_FILE_NOT_FOUND"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    BOOTSTRAP_EXPIRED = "BOOTSTRAP_EXPIRED"
    LICENSE_EXPIRED = "LICENSE_EXPIRED"
    ACTIVATION_TIMEOUT = "ACTIVATION_TIMEOUT"
    HARDWARE_ID_MISMATCH = "HARDWARE_ID_MISMATCH"
    BOUND_SIGNATURE_INVALID = "BOUND_SIGNATURE_INVALID"
    ERR_PARTITION_CORRUPT = "ERR_PARTITION_CORRUPT"


class LicenseError(Exception):
    """
    Raised on any license validation failure.

    ``code`` is a machine-readable identifier used for telemetry and log
    correlation. It is intentionally opaque in the user-facing lock screen
    to prevent attackers from learning which specific check failed.

    Callers must not catch this selectively — treat it as fatal.
    """
    def __init__(self, code: LicenseErrorCode, detail: str = "") -> None:
        self.code = code.value
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


# ════════════════════════════════════════════════════════════════════════════
#  PAYLOAD
# ════════════════════════════════════════════════════════════════════════════

@dataclasses.dataclass(frozen=True, slots=True)
class LicensePayload:
    """Immutable, verified license payload. Only constructed after sig check."""
    version: int
    subscription_id: str
    hardware_id: str
    plan: str
    issued_at: int   # UTC Unix seconds
    expires_at: int  # UTC Unix seconds
    nonce: str
    activated_at: int
    activation_nonce: str


@dataclasses.dataclass(frozen=True, slots=True)
class BootstrapPayload:
    """Hardware-unbound bootstrap token."""
    version: int
    subscription_id: str
    plan: str
    issued_at: int
    expires_at: int
    nonce: str
    activation_secret: str


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 1 — Monotonic Time Persistence (RD-6)
# ════════════════════════════════════════════════════════════════════════════

def _read_hwm() -> int:
    """
    Return the highest-seen RTC timestamp, or _ISO_BUILD_TIMESTAMP if none valid.
    CRC32 format: "<unix_int>:<crc32_hex>\n"
    Fallback chain: primary → .bak → _ISO_BUILD_TIMESTAMP.
    Never raises.
    """
    for label, path in (("primary", _HWM_FILE), ("backup", _HWM_BAK_FILE)):
        try:
            raw = path.read_text(encoding="ascii").strip()
            if not raw:
                continue
            unix_str, crc_str = raw.split(":")
            unix_val = int(unix_str)
            expected_crc = binascii.crc32(unix_str.encode("ascii")) & 0xFFFFFFFF
            if int(crc_str, 16) == expected_crc:
                sys.stderr.write(f"[HWM] Using {label} timestamp: {unix_val}\n")
                return unix_val
        except Exception:
            continue
    sys.stderr.write(
        f"[HWM] No valid HWM found; falling back to build timestamp: "
        f"{_ISO_BUILD_TIMESTAMP}\n"
    )
    return _ISO_BUILD_TIMESTAMP


def _write_hwm_atomic(rtc_unix: int) -> None:
    """
    Atomically update the monotonic high-water mark. (RD-6)
    Sequence: write .new → fsync → copy current→.bak → fsync → replace .new→primary
    """
    content = (
        f"{rtc_unix}:"
        f"{binascii.crc32(str(rtc_unix).encode('ascii')) & 0xFFFFFFFF:08x}\n"
    ).encode("ascii")
    fd_new = None
    tmp_path = None
    try:
        fd_new, tmp_path = tempfile.mkstemp(
            dir=_HWM_FILE.parent, prefix=".invariant_rtc_hwm_"
        )
        os.write(fd_new, content)
        os.fsync(fd_new)
        os.close(fd_new)
        fd_new = None

        if _HWM_FILE.exists():
            with open(_HWM_BAK_FILE, "wb") as f_bak:
                f_bak.write(_HWM_FILE.read_bytes())
                f_bak.flush()
                os.fsync(f_bak.fileno())

        os.replace(tmp_path, _HWM_FILE)
        tmp_path = None

        with open(_HWM_FILE, "rb") as f_prim:
            os.fsync(f_prim.fileno())
    except OSError as exc:
        if fd_new is not None:
            try:
                os.close(fd_new)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise LicenseError(LicenseErrorCode.HWM_WRITE_FAILED, str(exc))


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 2 — Hardware Fingerprint (USB DONGLE MODE)
# ════════════════════════════════════════════════════════════════════════════

_JUNK_SERIAL = re.compile(r"^0+$|^none$|^null$", re.IGNORECASE)


class HardwareBindingError(Exception):
    """Raised when the INVARIANT USB device cannot be resolved from the running system."""
    pass


def _get_parent_device(partition_path: str) -> str | None:
    """Given a partition path, return its parent block device path (e.g. /dev/sdb)."""
    try:
        result = subprocess.run(
            ["lsblk", "-no", "pkname", partition_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        pkname = result.stdout.strip()
        if pkname:
            return f"/dev/{pkname}"
    except Exception:
        pass
    return None


def _try_resolve_invariant_device() -> str | None:
    """Single attempt at resolving the INVARIANT parent block device."""
    label_link = Path("/dev/disk/by-label/INVARIANT")
    if label_link.exists():
        try:
            partition = str(label_link.resolve(strict=True))
            parent = _get_parent_device(partition)
            if parent:
                return parent
        except Exception:
            pass

    for mount_point in ("/mnt/invariant_data", "/run/media/root/INVARIANT"):
        try:
            result = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE", mount_point],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            source = result.stdout.strip()
            if not source:
                continue

            src_path = Path(source)
            if not src_path.exists() and Path("/dev", source).exists():
                src_path = Path("/dev", source)

            if src_path.exists():
                partition = str(src_path.resolve(strict=True))
                parent = _get_parent_device(partition)
                if parent:
                    return parent
        except Exception:
            pass

    return None


def _resolve_invariant_device() -> str:
    """
    Resolve the parent block device of the INVARIANT USB partition.

    Retry loop guards against udev races on slow xHCI hubs.
    Raises HardwareBindingError if resolution fails after all attempts.
    """
    for attempt in range(1, 4):
        device = _try_resolve_invariant_device()
        if device:
            return device
        if attempt < 3:
            subprocess.run(
                ["udevadm", "settle", "--timeout=3"],
                capture_output=True,
                timeout=5,
            )
    raise HardwareBindingError(
        "INVARIANT device resolution failed: label not found and mount table "
        "contains no INVARIANT partition after 3 attempts"
    )


def _udevadm_properties(device: str) -> dict[str, str]:
    """Query udev properties for a block device. Returns empty dict on failure."""
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={device}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        props: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value
        return props
    except Exception:
        return {}


def _get_device_size_bytes(device: str) -> str:
    """Return device size in bytes via blockdev. Returns 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["blockdev", "--getsize64", device],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        size = result.stdout.strip()
        if size.isdigit():
            return size
    except Exception:
        pass
    return "unknown"


def _resolve_invariant_partition() -> str:
    """Resolve the INVARIANT partition device path."""
    label_link = Path("/dev/disk/by-label/INVARIANT")
    if label_link.exists():
        return str(label_link.resolve(strict=True))
    parent = _resolve_invariant_device()
    for suffix in ("1", "2", "p1", "p2"):
        candidate = Path(f"{parent}{suffix}")
        if candidate.exists():
            return str(candidate)
    raise HardwareBindingError("Could not resolve INVARIANT partition device")


def _extract_usb_serial(block_device: str) -> tuple[str, str]:
    """
    Extract a serial identifier from the USB block device.

    Returns (serial_value, confidence_level) where confidence_level is one of:
        HIGH   -> physical iSerial or ID_SERIAL_SHORT from udev
        MEDIUM -> drive firmware serial (ID_SERIAL full string)
        LOW    -> deterministic fallback of geometry + fs UUID + label
    """
    props = _udevadm_properties(block_device)

    for key in ("ID_USB_SERIAL", "ID_SERIAL_SHORT"):
        val = props.get(key, "").strip()
        if val and not _JUNK_SERIAL.match(val):
            return (val, "HIGH")

    id_serial = props.get("ID_SERIAL", "").strip()
    if id_serial and not _JUNK_SERIAL.match(id_serial):
        return (id_serial, "MEDIUM")

    geometry = _get_device_size_bytes(block_device)
    partition = _resolve_invariant_partition()
    part_props = _udevadm_properties(partition)
    fs_uuid = part_props.get("ID_FS_UUID", "").strip()
    label = part_props.get("ID_FS_LABEL", "INVARIANT").strip()

    fallback_input = f"geom={geometry}\nuuid={fs_uuid}\nlabel={label}"
    fallback_hash = hashlib.sha256(fallback_input.encode("utf-8")).hexdigest()[:32]
    return (fallback_hash, "LOW")


def _compute_hwid() -> str:
    """
    Compute the hardware fingerprint of the INVARIANT USB dongle.

    The HWID is bound to the physical USB device, not the host machine,
    enabling the roaming technician workflow.

    Returns a string of the form '<confidence>:<64-char-hex>' so that
    HIGH- and LOW-confidence HWIDs are structurally distinguishable.
    """
    try:
        parent_device = _resolve_invariant_device()
        serial, confidence = _extract_usb_serial(parent_device)

        partition = _resolve_invariant_partition()
        part_props = _udevadm_properties(partition)
        fs_uuid = part_props.get("ID_FS_UUID", "").strip()

        composite = (
            f"usb_serial={serial}\n"
            f"fs_uuid={fs_uuid}\n"
            f"confidence={confidence}"
        ).encode("utf-8")

        key = bytes.fromhex(_EMBEDDED_PUBKEY_HEX)
        hwid_digest = hmac.new(key, composite, hashlib.sha256).hexdigest()

        return f"{confidence}:{hwid_digest}"
    except HardwareBindingError as exc:
        raise LicenseError(LicenseErrorCode.FINGERPRINT_UNAVAILABLE, str(exc))


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 3 — State Machine Router (RD-5)
# ════════════════════════════════════════════════════════════════════════════

def verify_license() -> LicensePayload:
    """
    Boot-time license gate. Must be called before any diagnostic work begins.

    Returns the validated LicensePayload on success.
    Raises LicenseError with a machine-readable code on ANY failure.
    """
    # 1. Acquire RTC
    rtc_unix = _read_rtc_unix()

    # 2. Build-time anti-rollback
    if _ISO_BUILD_TIMESTAMP > 0 and rtc_unix < _ISO_BUILD_TIMESTAMP:
        raise LicenseError(LicenseErrorCode.TIME_ROLLBACK_DETECTED)

    # 3. Read HWM
    hwm = _read_hwm()

    # 4. HWM anti-rollback
    if rtc_unix < hwm:
        raise LicenseError(LicenseErrorCode.TIME_ROLLBACK_DETECTED)

    # 5. Advance HWM if clock has moved forward
    if rtc_unix > hwm:
        _write_hwm_atomic(rtc_unix)

    # 6. Read indicator files
    license_raw = _read_file_safe(_LICENSE_FILE)
    bootstrap_raw = _read_file_safe(_BOOTSTRAP_FILE)

    # 7. Route per RD-5 priority
    if license_raw is not None:
        return _verify_bound_license_flow(license_raw, rtc_unix)
    if bootstrap_raw is not None:
        return _verify_bootstrap_flow(bootstrap_raw, rtc_unix)

    # Neither license nor bootstrap present
    if _PROV_STAMP_FILE.exists():
        raise LicenseError(LicenseErrorCode.ERR_LICENSE_DELETED)

    raise LicenseError(LicenseErrorCode.LICENSE_FILE_NOT_FOUND)


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 4 — Bound License Verification (RD-2, RD-3, RD-4)
# ════════════════════════════════════════════════════════════════════════════

def _b64url_decode(s: str) -> bytes:
    """Decode base64url with padding restoration."""
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def _verify_bound_license_flow(raw_token: str, rtc_unix: int) -> LicensePayload:
    """Path A: verify an existing hardware-bound license."""
    # 1. Witness check
    if not _PROV_STAMP_FILE.exists():
        raise LicenseError(LicenseErrorCode.ERR_WITNESS_MISSING)

    # 2. Structural split + explicit per-part strip
    parts = [p.strip() for p in raw_token.split(".")]
    if len(parts) != 2:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Malformed token")

    b64_payload, b64_sig = parts[0], parts[1]

    # 3. Decode raw base64url components (never re-serialize)
    try:
        json_bytes = _b64url_decode(b64_payload)
        sig_bytes = _b64url_decode(b64_sig)
    except Exception as exc:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, f"Decode error: {exc}")

    # 4. Secret presence — strict hex validation
    secret_hex = _read_file_safe(_SECRET_FILE)
    if secret_hex is None:
        raise LicenseError(
            LicenseErrorCode.BOUND_SIGNATURE_INVALID, "Missing secret file"
        )
    secret_hex = secret_hex.strip()
    if len(secret_hex) != 64:
        raise LicenseError(
            LicenseErrorCode.BOUND_SIGNATURE_INVALID, f"Secret length {len(secret_hex)} != 64"
        )
    try:
        secret_bytes = bytes.fromhex(secret_hex)
    except ValueError:
        raise LicenseError(
            LicenseErrorCode.BOUND_SIGNATURE_INVALID, "Secret is not valid hex"
        )
    if len(secret_bytes) != 32:
        raise LicenseError(
            LicenseErrorCode.BOUND_SIGNATURE_INVALID, f"Secret decoded to {len(secret_bytes)} bytes"
        )

    # 5. Parse payload
    try:
        payload = json.loads(json_bytes.decode("utf-8"))
    except Exception as exc:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, f"JSON parse error: {exc}")

    if payload.get("v") != 1:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad version")

    hwid = payload.get("hardware_id", "")
    if not isinstance(hwid, str) or len(hwid) < 64:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad hardware_id length")
    # Accept legacy 64-char hex OR prefixed HIGH:/MEDIUM:/LOW: format
    _hwid_digest = hwid.split(":")[-1]
    if len(_hwid_digest) != 64:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad hardware_id digest length")
    try:
        bytes.fromhex(_hwid_digest)
    except ValueError:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Non-hex hardware_id digest")

    if not isinstance(payload.get("subscription_id"), str) or not payload["subscription_id"]:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad subscription_id")
    for int_key in ("issued_at", "expires_at", "activated_at"):
        if not isinstance(payload.get(int_key), int) or payload[int_key] <= 0:
            raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, f"Bad {int_key}")
    if not isinstance(payload.get("activation_nonce"), str) or not payload["activation_nonce"]:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad activation_nonce")
    if not isinstance(payload.get("nonce"), str) or not payload["nonce"]:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad nonce")
    if not isinstance(payload.get("plan"), str) or not payload["plan"]:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad plan")

    # 7. Derive bound key (RD-4)
    bound_key = hmac.new(
        secret_bytes,
        (payload["subscription_id"] + hwid + "|bound_v1").encode(),
        hashlib.sha256,
    ).digest()

    # 8. Verify HMAC over raw JSON bytes (NOT a re-serialized string)
    expected_sig = hmac.new(bound_key, json_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, sig_bytes):
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID)

    # 9. Hardware binding
    local_hwid = _compute_hwid()
    if not hmac.compare_digest(payload["hardware_id"], local_hwid):
        raise LicenseError(LicenseErrorCode.HARDWARE_ID_MISMATCH)

    # 10. Expiry
    if rtc_unix >= payload["expires_at"]:
        raise LicenseError(LicenseErrorCode.LICENSE_EXPIRED)

    # 11. Return
    return LicensePayload(
        version=payload["v"],
        subscription_id=payload["subscription_id"],
        hardware_id=payload["hardware_id"],
        plan=payload["plan"],
        issued_at=payload["issued_at"],
        expires_at=payload["expires_at"],
        nonce=payload["nonce"],
        activated_at=payload["activated_at"],
        activation_nonce=payload["activation_nonce"],
    )


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 5 — Bootstrap Activation Flow
# ════════════════════════════════════════════════════════════════════════════

def _generate_challenge(bootstrap: BootstrapPayload, local_hwid: str, rtc_now: datetime):
    """Generate QR challenge (retained from original verifier)."""
    import secrets as _secrets
    import base64 as _b64

    challenge_nonce = _secrets.token_hex(16)
    boot_id = _secrets.token_hex(8)

    challenge = {
        "v": 1,
        "subscription_id": bootstrap.subscription_id,
        "quimera": local_hwid,
        "nonce": challenge_nonce,
        "ts": int(rtc_now.timestamp()),
        "boot_id": boot_id,
    }

    challenge_json = json.dumps(challenge, separators=(",", ":")).encode("utf-8")
    challenge_b64 = _b64.urlsafe_b64encode(challenge_json).rstrip(b"=").decode("ascii")
    url = f"{_ACTIVATION_URL_BASE}?c={challenge_b64}"
    return challenge, url


def _verify_activation_pin(
    activation_secret: str, challenge_nonce: str, quimera: str, entered_pin: str
) -> bool:
    """Verify 6-digit PIN via HMAC derivation."""
    secret_bytes = bytes.fromhex(activation_secret)
    mac = hmac.new(secret_bytes, (challenge_nonce + quimera).encode(), hashlib.sha256)
    pin_material = mac.digest()
    pin_int = int.from_bytes(pin_material[:4], "big") % 1_000_000
    expected_pin = str(pin_int).zfill(6)
    return hmac.compare_digest(expected_pin, entered_pin)


def _atomic_write_file(path: Path, content: str) -> None:
    """Atomic write via mkstemp + fsync + replace."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        os.write(fd, content.encode("ascii"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_activation(
    bootstrap: BootstrapPayload, hwid: str, challenge_nonce: str, rtc_unix: int
) -> None:
    """Write bound license artifacts atomically. (RD-4, RD-6)"""

    # PATCH-2a: Precondition — prov_stamp must exist or state routing failed
    if not _PROV_STAMP_FILE.exists():
        raise LicenseError(
            LicenseErrorCode.ERR_PARTITION_CORRUPT,
            "Provisioner stamp missing — verifier reached activation on unprovisioned device"
        )

    # PATCH-2b: Double-activation guard — secret already written means already activated
    if _SECRET_FILE.exists():
        raise LicenseError(LicenseErrorCode.ERR_WITNESS_ALREADY_EXISTS)

    bound_payload = {
        "v": 1,
        "hardware_id": hwid,
        "subscription_id": bootstrap.subscription_id,
        "plan": bootstrap.plan,
        "issued_at": bootstrap.issued_at,
        "expires_at": bootstrap.expires_at,
        "nonce": bootstrap.nonce,
        "activated_at": rtc_unix,
        "activation_nonce": challenge_nonce,
    }

    json_bytes = json.dumps(bound_payload, separators=(",", ":")).encode("utf-8")

    base_secret = bytes.fromhex(bootstrap.activation_secret)
    bound_key = hmac.new(
        base_secret,
        (bootstrap.subscription_id + hwid + "|bound_v1").encode(),
        hashlib.sha256,
    ).digest()
    sig = hmac.new(bound_key, json_bytes, hashlib.sha256).digest()

    enc = base64.urlsafe_b64encode
    token = (
        enc(json_bytes).rstrip(b"=").decode("ascii")
        + "."
        + enc(sig).rstrip(b"=").decode("ascii")
    )

    try:
        _atomic_write_file(_SECRET_FILE, bootstrap.activation_secret + "\n")
        _atomic_write_file(_LICENSE_FILE, token + "\n")
        if _BOOTSTRAP_FILE.exists():
            _BOOTSTRAP_FILE.unlink()
    except OSError as exc:
        _log.error("Activation write failed: %s", exc)
        raise LicenseError(LicenseErrorCode.HWM_WRITE_FAILED, f"Activation write failed: {exc}")


def _verify_bootstrap_flow(bootstrap_raw: str, rtc_unix: int) -> LicensePayload:
    """Path B: bootstrap activation flow."""
    # PATCH-1: Sentinel guard — detect unpatched ISO before Ed25519 is invoked
    if _EMBEDDED_PUBKEY_HEX == "0" * 64:
        raise LicenseError(
            LicenseErrorCode.SIGNATURE_INVALID,
            "Ed25519 public key not injected — rebuild ISO with forge.sh"
        )

    # 1. Split + decode
    parts = bootstrap_raw.split(".")
    if len(parts) != 2:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Malformed bootstrap token")

    try:
        json_bytes = _b64url_decode(parts[0])
        sig_bytes = _b64url_decode(parts[1])
    except Exception as exc:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, f"Decode error: {exc}")

    # 2. Ed25519 verify
    try:
        pubkey_bytes = bytes.fromhex(_EMBEDDED_PUBKEY_HEX)
    except ValueError:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Malformed public key")
    if len(pubkey_bytes) != 32:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad public key size")

    if _HAS_CRYPTOGRAPHY:
        try:
            pk = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
            pk.verify(sig_bytes, json_bytes)
        except InvalidSignature:
            raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID)
    elif _HAS_PYNACL:
        try:
            vk = VerifyKey(pubkey_bytes)
            vk.verify(json_bytes, sig_bytes)
        except Exception:
            raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID)
    else:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "No Ed25519 library available")

    # 3. Parse + validate
    try:
        data = json.loads(json_bytes.decode("utf-8"))
    except Exception as exc:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, f"JSON parse error: {exc}")

    if data.get("v") != 1:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad version")
    if not isinstance(data.get("subscription_id"), str) or not data["subscription_id"]:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad subscription_id")
    if not isinstance(data.get("plan"), str) or not data["plan"]:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad plan")
    for int_key in ("issued_at", "expires_at"):
        if not isinstance(data.get(int_key), int) or data[int_key] <= 0:
            raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, f"Bad {int_key}")
    activation_secret = data.get("activation_secret", "")
    if not isinstance(activation_secret, str) or len(activation_secret) != 64:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad activation_secret")

    # PATCH-5: nonce validation (must appear before BootstrapPayload construction)
    if not isinstance(data.get("nonce"), str) or not data["nonce"]:
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "Bad nonce")

    bootstrap = BootstrapPayload(
        version=data["v"],
        subscription_id=data["subscription_id"],
        plan=data["plan"],
        issued_at=data["issued_at"],
        expires_at=data["expires_at"],
        nonce=data["nonce"],
        activation_secret=activation_secret,
    )

    # 4. Expiry
    if rtc_unix >= bootstrap.expires_at:
        raise LicenseError(LicenseErrorCode.BOOTSTRAP_EXPIRED)

    # 5. HWID
    local_hwid = _compute_hwid()

    # 6. QR challenge
    rtc_now = datetime.fromtimestamp(rtc_unix, tz=timezone.utc)
    challenge, challenge_url = _generate_challenge(bootstrap, local_hwid, rtc_now)

    # PATCH-4: Distinguish timeout vs import failure vs internal crash
    try:
        from core.qr_display import display_activation_screen
    except ImportError:
        raise LicenseError(
            LicenseErrorCode.SIGNATURE_INVALID,
            "qr_display module unavailable — ISO build defect"
        )

    try:
        entered_pin = display_activation_screen(
            challenge_url=challenge_url,
            boot_id=challenge["boot_id"],
            timeout_seconds=300,
        )
    except Exception as exc:
        _log.exception("Activation screen crashed unexpectedly")
        raise LicenseError(
            LicenseErrorCode.SIGNATURE_INVALID,
            f"Activation screen error: {exc}"
        )

    if entered_pin is None:
        raise LicenseError(LicenseErrorCode.ACTIVATION_TIMEOUT)

    if not _verify_activation_pin(
        activation_secret=bootstrap.activation_secret,
        challenge_nonce=challenge["nonce"],
        quimera=local_hwid,
        entered_pin=entered_pin,
    ):
        raise LicenseError(LicenseErrorCode.SIGNATURE_INVALID, "PIN verification failed")

    # 7. Atomic write
    _atomic_write_activation(bootstrap, local_hwid, challenge["nonce"], rtc_unix)

    # 8. Return
    return LicensePayload(
        version=1,
        subscription_id=bootstrap.subscription_id,
        hardware_id=local_hwid,
        plan=bootstrap.plan,
        issued_at=bootstrap.issued_at,
        expires_at=bootstrap.expires_at,
        nonce=bootstrap.nonce,
        activated_at=rtc_unix,
        activation_nonce=challenge["nonce"],
    )


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 6 — Time & File Helpers
# ════════════════════════════════════════════════════════════════════════════

def _read_rtc_unix() -> int:
    """Read current UTC time. Raises RTC_READ_FAILED on any problem."""
    try:
        dt = datetime.now(timezone.utc)
        ts = int(dt.timestamp())
    except Exception as exc:
        raise LicenseError(LicenseErrorCode.RTC_READ_FAILED, str(exc))
    if ts < 1_000_000_000:
        raise LicenseError(LicenseErrorCode.RTC_READ_FAILED, f"Implausible timestamp {ts}")
    return ts


def _read_file_safe(path: Path) -> Optional[str]:
    """Safe file read with symlink/size/traversal guards. (RD-5)"""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LicenseError(LicenseErrorCode.ERR_PARTITION_CORRUPT, str(exc))

    if stat.S_ISLNK(st.st_mode):
        raise LicenseError(LicenseErrorCode.ERR_PARTITION_CORRUPT, f"Symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise LicenseError(LicenseErrorCode.ERR_PARTITION_CORRUPT, f"Not regular: {path}")
    # PATCH-3: use the named constant instead of a hardcoded literal
    if st.st_size > _LICENSE_MAX_BYTES:
        raise LicenseError(LicenseErrorCode.ERR_PARTITION_CORRUPT, f"Oversized: {path}")

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(_MOUNT_POINT.resolve())
    except ValueError:
        raise LicenseError(LicenseErrorCode.ERR_PARTITION_CORRUPT, f"Traversal: {path}")
    except OSError as exc:
        raise LicenseError(LicenseErrorCode.ERR_PARTITION_CORRUPT, str(exc))

    try:
        return path.read_text(encoding="ascii").strip()
    except Exception as exc:
        raise LicenseError(LicenseErrorCode.ERR_PARTITION_CORRUPT, str(exc))
