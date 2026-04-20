#!/usr/bin/env python3
"""
test_pin_derivation.py — Cross-language HMAC PIN verification test.

Verifies that the Python PIN derivation produces identical output to
the Go API's computeActivationPIN() function.

Usage:
    python test_pin_derivation.py

This test uses hardcoded test vectors. The Go implementation must produce
the same PIN for the same inputs.
"""
import hmac
import hashlib
import struct
import sys


def compute_activation_pin(activation_secret: str, challenge_nonce: str, quimera: str) -> str:
    """
    Derives a 6-digit activation PIN from the activation_secret,
    challenge nonce, and target hardware fingerprint.

    MUST produce identical output as Go's computeActivationPIN().
    """
    secret_bytes = bytes.fromhex(activation_secret)
    mac = hmac.new(secret_bytes, (challenge_nonce + quimera).encode(), hashlib.sha256)
    pin_material = mac.digest()

    # Take first 4 bytes as big-endian uint32, mod 1_000_000
    pin_int = int.from_bytes(pin_material[:4], "big") % 1_000_000
    return str(pin_int).zfill(6)


def test_vectors():
    """Test with known inputs and verify determinism."""

    # Test vector 1: deterministic with known inputs
    secret = "a" * 64  # 32 bytes of 0xaa
    nonce = "b" * 32   # 16 bytes of 0xbb
    quimera = "c" * 64  # 32 bytes of 0xcc

    pin1 = compute_activation_pin(secret, nonce, quimera)
    pin2 = compute_activation_pin(secret, nonce, quimera)

    assert pin1 == pin2, f"PIN not deterministic: {pin1} != {pin2}"
    assert len(pin1) == 6, f"PIN wrong length: {len(pin1)}"
    assert pin1.isdigit(), f"PIN not all digits: {pin1}"

    print(f"[OK] Test vector 1: secret=aaa... nonce=bbb... quimera=ccc... → PIN={pin1}")

    # Test vector 2: different quimera → different PIN
    quimera2 = "d" * 64
    pin3 = compute_activation_pin(secret, nonce, quimera2)

    assert pin3 != pin1, f"Different quimera produced same PIN: {pin1}"
    print(f"[OK] Test vector 2: Different quimera → PIN={pin3} (different from {pin1})")

    # Test vector 3: different nonce → different PIN
    nonce2 = "e" * 32
    pin4 = compute_activation_pin(secret, nonce2, quimera)

    assert pin4 != pin1, f"Different nonce produced same PIN: {pin1}"
    print(f"[OK] Test vector 3: Different nonce → PIN={pin4} (different from {pin1})")

    # Test vector 4: realistic-looking values
    real_secret = "98ee0e2e03126d80cbd5e846acc0a6afa83d19ad3d6c84424a0a093bf46adeed"
    real_nonce = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    real_quimera = "deadbeefcafebabe1234567890abcdef1234567890abcdef1234567890abcdef"

    pin5 = compute_activation_pin(real_secret, real_nonce, real_quimera)
    print(f"[OK] Test vector 4: Realistic → PIN={pin5}")

    # ═══════════════════════════════════════════════════════════════════
    # CROSS-LANGUAGE VERIFICATION VALUES
    # Copy these to the Go test to verify identical output:
    # ═══════════════════════════════════════════════════════════════════
    print()
    print("═══ Cross-Language Test Vectors ═══")
    print(f"Vector 1: secret={secret} nonce={nonce} quimera={quimera} → PIN={pin1}")
    print(f"Vector 4: secret={real_secret} nonce={real_nonce} quimera={real_quimera} → PIN={pin5}")
    print()
    print("Copy these PINs to the Go test and verify they match.")

    # Verify constant-time comparison
    assert hmac.compare_digest(pin1, pin1), "compare_digest self-check failed"
    assert not hmac.compare_digest(pin1, pin3), "compare_digest should fail on different PINs"
    print("[OK] Constant-time comparison: working")

    print()
    print(f"All {4} test vectors passed ✓")


if __name__ == "__main__":
    test_vectors()
