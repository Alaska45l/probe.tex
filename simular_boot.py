import sys
from pathlib import Path
from datetime import datetime, timezone

# Agregar prob-tex al path para poder importar
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from core import license_verifier

# 1. Mock de la lectura de hardware (para no necesitar root/TPM en la prueba local)
def mock_hardware_fingerprint():
    return "deadbeefcafebabe1234567890abcdef1234567890abcdef1234567890abcdef"

license_verifier.compute_hardware_fingerprint = mock_hardware_fingerprint

# 2. Mock del RTC de hardware para usar el reloj normal del sistema operativo
license_verifier._read_hardware_rtc = lambda: datetime.now(timezone.utc)

# 3. Forzar la URL local para la prueba del QR en lugar de invariant.systems
# Reemplaza 'localhost:8080' por la IP local de tu PC (ej: 192.168.1.10:8080)
# si quieres escanear el QR con tu celular en la misma red WiFi.
license_verifier._ACTIVATION_URL_BASE = "http://localhost:8080/api/v1/license/activate"

print("Iniciando simulación del entorno Ring-0 (probe.tex)...")

try:
    # Esto lanzará el flujo de _verify_bootstrap_activation_flow
    # Leerá el /mnt/invariant_data/bootstrap.sig que generaste con curl
    payload = license_verifier.verify_license()
    
    print("\n[ÉXITO] El flujo retornó el payload validado:")
    print(f" - Hardware ID: {payload.hardware_id}")
    print(f" - Plan:        {payload.plan}")
    
    # Comprobar que se consumió el bootstrap y se creó el bound license
    if not Path("/mnt/invariant_data/bootstrap.sig").exists():
        print(" - bootstrap.sig eliminado correctamente.")
    if Path("/mnt/invariant_data/license.sig").exists():
        print(" - license.sig (vinculado al hardware) creado correctamente.")
        
except license_verifier.LicenseError as e:
    print(f"\n[FALLO FATAL] Licencia rechazada: {e.code}")
