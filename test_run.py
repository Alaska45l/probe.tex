import logging
from core.license_verifier import verify_license, LicenseError

# Activar logs en DEBUG para ver las entrañas del proceso
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

try:
    print("[*] Iniciando secuencia de booteo y validación DRM...")
    licencia = verify_license()
    
    print("\n[+] ÉXITO: Sistema Desbloqueado - Entropía fluyendo.")
    print(f"    Hardware ID: {licencia.hardware_id[:8]}...{licencia.hardware_id[-8:]}")
    print(f"    Plan Activo: {licencia.plan.upper()}")
except LicenseError as e:
    print(f"\n[-] LOCKDOWN: Pantalla Roja de la Muerte")
    print(f"    Código de Error: {e.code}")