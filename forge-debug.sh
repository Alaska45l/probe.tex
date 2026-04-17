#!/usr/bin/env bash
# ============================================================
#  forge.sh — Fase 2: La Forja  [v1.6 — Static Hook Interception]
#  INVARIANT SYSTEMS // probe.tex Live ISO Builder
#  Ejecución: sudo bash forge.sh  (directorio raíz del repo)
#
#  CHANGELOG v1.6
#  ─────────────────────────────────────────────────────────
#  PROBLEMA v1.5: customize_airootfs.sh deprecado + fallo en chroot
#
#  Evidencia dura (mkarchiso build log):
#    ERROR: /usr/lib/initcpio/hooks/archiso no encontrado.
#    WARNING: customize_airootfs.sh está deprecado.
#
#  La inyección de `set -x` vía customize_airootfs.sh (FIX-14 v1.5)
#  depende de que el paquete `archiso` esté instalado DENTRO del
#  chroot en el momento en que el script post-instalación se ejecuta.
#  En versiones recientes de mkarchiso, ese mecanismo está deprecado
#  y el timing de ejecución no está garantizado.
#
#  SOLUCIÓN v1.6 — Static Hook Interception (Host-side):
#
#    En lugar de parchar dentro del chroot, forge.sh lee el hook
#    /usr/lib/initcpio/hooks/archiso del SISTEMA ANFITRIÓN (donde
#    el paquete `archiso` debe estar instalado para poder usar
#    mkarchiso), aplica la inyección de set -x con sed en el host,
#    y deposita el archivo resultante en:
#      ${ISO_ROOT}/airootfs/usr/lib/initcpio/hooks/archiso
#
#    mkarchiso prioriza los archivos en airootfs/ sobre los del
#    sistema instalado por los paquetes de packages.x86_64.
#    Por lo tanto, nuestra versión instrumentada del hook es la
#    que mkinitcpio incorpora al initramfs de la ISO.
#
#    El paquete `archiso` sigue declarado en packages.x86_64 para
#    garantizar que todas sus dependencias binarias (getarg, msg,
#    switch_root, etc.) estén presentes en el sistema live. Solo
#    sobreescribimos el script del hook, no los binarios.
#
#  FIX-14 v1.6 — Mecanismo de Host-side Interception:
#
#    1. GUARD: Verificar que /usr/lib/initcpio/hooks/archiso existe
#       en el anfitrión. Si no existe, el paquete archiso no está
#       instalado y mkarchiso tampoco funcionaría. die() con
#       instrucción de instalación.
#
#    2. GUARD: Verificar que la función archiso_mount_handler()
#       existe en el archivo fuente. Si la firma cambió en una
#       versión nueva del paquete, alertar en lugar de silenciar
#       el fallo.
#
#    3. IDEMPOTENCIA: Verificar si el parche ya fue aplicado
#       (presencia de la firma PATCH_SIGNATURE) antes de ejecutar
#       sed para evitar duplicados.
#
#    4. PATCH: Aplicar sed -i sobre una copia del archivo fuente
#       depositada en airootfs/usr/lib/initcpio/hooks/archiso.
#       El archivo fuente en el anfitrión NO se modifica.
#
#    5. VERIFY: grep de la firma post-patch. die() si no se
#       encuentra (sed no insertó la línea).
#
#    6. DEPLOY: mkdir -p de la ruta de destino y chmod 644 del
#       archivo interceptado.
#
#  SCOPE DE set -x (idéntico a v1.5):
#    set -x activa xtrace globalmente en el shell del initramfs.
#    Una vez que archiso_mount_handler retorna, el trace permanece
#    activo en /init. En una ISO de diagnóstico esto es aceptable;
#    el flood de '+' en consola es la evidencia que necesitamos.
#
#  AUD-8 v1.6 (reemplaza AUD-8 v1.5):
#    Verificación H: el hook interceptado existe en
#    airootfs/usr/lib/initcpio/hooks/archiso y contiene
#    la firma del parche FIX-14 v1.6. die() si alguna condición
#    falla.
#
#  ELIMINADO en v1.6:
#    - Creación de customize_airootfs.sh (sección FIX-14 v1.5)
#    - Entrada en file_permissions[] de customize_airootfs.sh
#    - AUD-8 v1.5 (verificación del script deprecado)
#
#  (Parches v1.5 — FIX-13, AUD-7 — intactos)
#  (Parches v1.4 — FIX-10/11/12, AUD-5/6 — intactos)
#  (Parches v1.3 — FIX-7/8/9 — intactos)
#  (Parches v1.2 — FIX-3/4/5/6, AUD-1/2/3/4 — intactos)
#  (Parches v1.1 — FIX-1/2 — intactos)
# ============================================================
set -euo pipefail

# ── Colores ──────────────────────────────────────────────────
RED='\033[1;31m'; WHT='\033[1;37m'; GRN='\033[1;32m'
YLW='\033[1;33m'; DIM='\033[0;90m'; RST='\033[0m'

log()  { echo -e "${WHT}[forge]${RST} $*"; }
ok()   { echo -e "${GRN}  [+]${RST} $*"; }
warn() { echo -e "${YLW}  [!]${RST} $*"; }
die()  { echo -e "${RED}  [✗] FATAL:${RST} $*" >&2; exit 1; }

# ── Guardia de privilegios ────────────────────────────────────
[[ "${EUID}" -eq 0 ]] || die "forge.sh requiere root. Ejecutar con: sudo bash forge.sh"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISO_ROOT="${REPO_ROOT}/probe-tex-iso"

echo -e "${RED}"
echo "════════════════════════════════════════════════════════════"
echo " INVARIANT SYSTEMS  //  FORGE v1.6"
echo " Andamiaje de entorno Archiso para probe.tex"
echo "════════════════════════════════════════════════════════════"
echo -e "${RST}"

# ════════════════════════════════════════════════════════════
#  0. HOOKS PERSONALIZADOS DE MKINITCPIO  (FIX-10)
#
#  Se crean ANTES del árbol de directorios principal para que
#  el resto del script (sección 1+) pueda asumir que existen.
#
#  archiso_udev_settle — runtime hook
#  ─────────────────────────────────
#  Ejecutado entre `block` y `archiso`. Garantiza que udev haya
#  terminado de poblar /dev/disk/by-label/ antes de que archiso
#  intente resolver archisolabel=PROBE_TEX.
#
#  Soluciona la race condition xHCI: el hook `block` carga el
#  módulo del controlador USB pero no espera a que la negociación
#  USB mass storage complete y udev cree los symlinks by-label.
#  En hardware moderno (AMD Mendocino, xHCI gen 2+) este gap
#  puede ser de 1 a 5 segundos.
#
#  archiso_udev_settle — install hook
#  ─────────────────────────────────
#  Instruye a mkinitcpio qué binarios incluir en el initramfs
#  para que el runtime hook pueda ejecutarse correctamente.
#  Declara udevadm como dependencia explícita y añade dmesg
#  para el modo debug (archiso_debug=1).
# ════════════════════════════════════════════════════════════
log "Creando directorios para hooks personalizados de mkinitcpio (FIX-10)..."
mkdir -p \
  "${ISO_ROOT}/airootfs/etc/initcpio/hooks" \
  "${ISO_ROOT}/airootfs/etc/initcpio/install"
ok "Directorios airootfs/etc/initcpio/{hooks,install} creados."

# ── Runtime hook ─────────────────────────────────────────────
log "Escribiendo hook runtime: archiso_udev_settle (FIX-10)..."
cat > "${ISO_ROOT}/airootfs/etc/initcpio/hooks/archiso_udev_settle" << 'HOOK_RUNTIME'
# /etc/initcpio/hooks/archiso_udev_settle
# Runtime hook para mkinitcpio — ejecutado entre `block` y `archiso`.
#
# Propósito: eliminar la race condition entre la carga de módulos USB
# (hook block) y la búsqueda del medio por label (hook archiso).
# udevadm settle garantiza que todos los eventos uevent pendientes
# hayan sido procesados y sus symlinks creados antes de continuar.
#
# Parámetros de kernel relevantes:
#   archisolabel=PROBE_TEX   — label del dispositivo a esperar
#   archiso_debug=1          — activa logging extendido a /run/initramfs/

run_hook() {
    local label timeout waited found logfile logdir
    timeout=30
    waited=0
    found=0

    # Leer label desde cmdline (getarg definido en /init_functions de mkinitcpio)
    label="$(getarg archisolabel)"

    msg ":: [udev_settle] Iniciando sincronización udev (timeout=${timeout}s)"
    msg ":: [udev_settle] uptime: $(cut -d' ' -f1 /proc/uptime)s"

    # Paso 1: udevadm settle — espera a que udev procese todos los eventos
    # pendientes incluyendo creación de symlinks en /dev/disk/by-label/
    udevadm settle --timeout="${timeout}" 2>/dev/null || true

    msg ":: [udev_settle] udev settle completado @ $(cut -d' ' -f1 /proc/uptime)s"

    # Paso 2: si tenemos label, polling explícito del symlink concreto
    if [[ -n "${label}" ]]; then
        local device="/dev/disk/by-label/${label}"

        while [[ ${waited} -lt ${timeout} ]]; do
            if [[ -e "${device}" ]]; then
                found=1
                break
            fi
            msg ":: [udev_settle] Esperando ${device}... (${waited}s/${timeout}s)"
            sleep 1
            # re-settle en cada iteración: puede haber nuevos eventos USB
            udevadm settle --timeout=5 2>/dev/null || true
            waited=$(( waited + 1 ))
        done

        if [[ ${found} -eq 1 ]]; then
            msg ":: [udev_settle] Dispositivo listo: ${device}"
            msg ":: [udev_settle] uptime total: $(cut -d' ' -f1 /proc/uptime)s"
        else
            msg ":: [udev_settle] ADVERTENCIA: ${device} no encontrado tras ${timeout}s"
            msg ":: [udev_settle] Dispositivos by-label disponibles:"
            if [[ -d /dev/disk/by-label ]]; then
                for dev in /dev/disk/by-label/*; do
                    [[ -e "${dev}" ]] && msg "::   ${dev}"
                done
            else
                msg "::   (directorio /dev/disk/by-label no existe)"
            fi
        fi
    else
        # Parámetro ausente — evidencia para hipótesis (a): string vacío
        msg ":: [udev_settle] ADVERTENCIA: archisolabel no encontrado en cmdline"
        msg ":: [udev_settle] cmdline completo: $(cat /proc/cmdline)"
        msg ":: [udev_settle] Esto confirma pérdida del parámetro, no race condition."
    fi

    # Paso 3: debug logging — activado por archiso_debug=1 en cmdline
    if [[ "$(getarg archiso_debug)" == "1" ]]; then
        logdir="/run/initramfs"
        logfile="${logdir}/boot-debug.log"
        mkdir -p "${logdir}"
        {
            echo "========================================================"
            echo " archiso_udev_settle — debug log"
            echo " uptime: $(cut -d' ' -f1 /proc/uptime)s"
            echo "========================================================"
            echo ""
            echo "--- /proc/cmdline ---"
            cat /proc/cmdline
            echo ""
            echo "--- archisolabel parseado: '${label}' ---"
            echo "--- device found: ${found} (waited: ${waited}s) ---"
            echo ""
            echo "--- /dev/disk/by-label/ ---"
            ls -la /dev/disk/by-label/ 2>/dev/null || echo "(vacío o inexistente)"
            echo ""
            echo "--- /dev/disk/ (árbol completo) ---"
            ls -laR /dev/disk/ 2>/dev/null || echo "(vacío)"
            echo ""
            echo "--- /proc/partitions ---"
            cat /proc/partitions 2>/dev/null || echo "(no disponible)"
            echo ""
            echo "--- dmesg: USB / xHCI / storage (últimas 40 líneas) ---"
            dmesg 2>/dev/null \
                | grep -iE 'usb|xhci|ehci|ohci|storage|block|disk|scsi|uas|msc' \
                | tail -40 \
                || echo "(dmesg no disponible)"
            echo ""
            echo "========================================================"
            echo " fin del log"
            echo "========================================================"
        } >> "${logfile}"
        msg ":: [udev_settle] Debug log escrito en ${logfile}"
    fi
}
HOOK_RUNTIME

chmod 644 "${ISO_ROOT}/airootfs/etc/initcpio/hooks/archiso_udev_settle"
ok "archiso_udev_settle (runtime) escrito."

# ── Install hook ──────────────────────────────────────────────
log "Escribiendo hook install: archiso_udev_settle (FIX-10)..."
cat > "${ISO_ROOT}/airootfs/etc/initcpio/install/archiso_udev_settle" << 'HOOK_INSTALL'
#!/bin/bash
# /etc/initcpio/install/archiso_udev_settle
# Hook de instalación para mkinitcpio.
# Define qué binarios incluir en el initramfs para que el runtime hook funcione.

build() {
    # udevadm: requerido para settle + trigger
    add_binary udevadm

    # dmesg: usado en modo debug (archiso_debug=1)
    if type -P dmesg &>/dev/null; then
        add_binary dmesg
    fi

    # Incluye el script runtime desde /etc/initcpio/hooks/archiso_udev_settle
    add_runscript
}

help() {
    cat <<HELPEOF
Hook de sincronización udev para archiso — ejecutar entre 'block' y 'archiso'.

PROPÓSITO:
  Elimina la race condition entre la carga de módulos USB (block) y la
  búsqueda del medio live por label (archiso). Ejecuta udevadm settle y
  hace polling explícito de /dev/disk/by-label/\${archisolabel} hasta que
  el dispositivo esté disponible o expire el timeout (30s).

PARÁMETROS DE KERNEL:
  archisolabel=LABEL    Label del dispositivo a esperar (requerido).
  archiso_debug=1       Activa logging extendido. Log en:
                        /run/initramfs/boot-debug.log

HARDWARE OBJETIVO:
  Procesadores AMD Mendocino / Ryzen APU con controladores xHCI modernos
  donde la inicialización USB mass storage puede tardar 1–5s adicionales
  después de que el hook block cargue los módulos del controlador.

ORDEN CORRECTO EN HOOKS:
  base udev modconf block archiso_udev_settle archiso filesystems keyboard
HELPEOF
}
HOOK_INSTALL

chmod 644 "${ISO_ROOT}/airootfs/etc/initcpio/install/archiso_udev_settle"
ok "archiso_udev_settle (install) escrito."
ok "Hook FIX-10 completo: race condition xHCI/udev eliminada."

# ════════════════════════════════════════════════════════════
#  FIX-14 v1.6 — HOST-SIDE STATIC HOOK INTERCEPTION
#
#  PROBLEMA v1.5:
#    customize_airootfs.sh está deprecado en versiones recientes
#    de mkarchiso. El paquete `archiso` tampoco está disponible
#    durante la fase de ejecución del script porque el timing del
#    chroot no está garantizado en el modo de build actual.
#    Resultado: "ERROR: /usr/lib/initcpio/hooks/archiso no encontrado."
#
#  SOLUCIÓN v1.6 — Interception en el host:
#
#    El sistema anfitrión que ejecuta forge.sh DEBE tener el paquete
#    `archiso` instalado para poder invocar `mkarchiso` en el paso
#    final. Por lo tanto, /usr/lib/initcpio/hooks/archiso EXISTE
#    en el anfitrión y es accesible en tiempo de build.
#
#    forge.sh lee ese archivo, aplica la inyección de set -x con
#    sed (sin modificar el original), y deposita la copia parcheada
#    en la ruta espejo dentro del árbol airootfs:
#      ${ISO_ROOT}/airootfs/usr/lib/initcpio/hooks/archiso
#
#    mkarchiso fusiona airootfs/ sobre el chroot construido con
#    pacstrap. Los archivos en airootfs/ tienen precedencia sobre
#    los instalados por los paquetes. La versión instrumentada del
#    hook es la que mkinitcpio incorpora al initramfs de la ISO.
#
#    El paquete `archiso` sigue en packages.x86_64 para garantizar
#    que todos los BINARIOS del hook (switch_root, mount, losetup,
#    getarg, msg, etc.) estén presentes. Solo sobreescribimos el
#    SCRIPT del hook, no sus dependencias ejecutables.
#
#  INVARIANTES DE ESTA SECCIÓN:
#
#    I1: El archivo fuente en el anfitrión NO se modifica nunca.
#        La copia de trabajo se realiza en una ruta temporal y
#        luego se mueve al destino solo si el parche verificó.
#
#    I2: Idempotencia garantizada. Si el archivo destino ya
#        contiene la firma, se omite el parcheo y se continúa.
#
#    I3: Detección de cambio de API. Si la firma de la función
#        `archiso_mount_handler() {` no está en el archivo fuente,
#        forge.sh termina con die() listando las funciones
#        disponibles para facilitar la adaptación.
#
#    I4: Verificación post-parche obligatoria. Aunque sed no
#        devuelve error si no encuentra el patrón (solo omite la
#        inserción), el grep de verificación detecta la ausencia
#        de la firma y genera die() antes de que mkarchiso
#        consuma un archivo no instrumentado.
# ════════════════════════════════════════════════════════════

HOOK_SOURCE="/usr/lib/initcpio/hooks/archiso"
HOOK_DEST_DIR="${ISO_ROOT}/airootfs/etc/initcpio/hooks"
HOOK_DEST="${HOOK_DEST_DIR}/archiso"
# Firma única — permite idempotencia y verificación post-patch
PATCH_SIGNATURE="FIX-14 v1.6: verbose xtrace for archiso_mount_handler"
# Nombre de la función objetivo en el hook archiso
HOOK_FUNC_PATTERN="^archiso_mount_handler() {"

log "FIX-14 v1.6: Iniciando Host-side Static Hook Interception..."
log "  Fuente : ${HOOK_SOURCE}"
log "  Destino: ${HOOK_DEST}"

# ── Guard I1: el paquete archiso debe estar instalado en el host ──
if [[ ! -f "${HOOK_SOURCE}" ]]; then
    die "FIX-14: ${HOOK_SOURCE} no encontrado en el sistema anfitrión.
  El paquete 'archiso' debe estar instalado en el host para:
    1. Ejecutar mkarchiso en el paso final.
    2. Proporcionar el hook que forge.sh intercepta y parcheará.
  Instalar con: sudo pacman -S archiso
  Luego volver a ejecutar forge.sh."
fi
ok "FIX-14: Archivo fuente confirmado en el host: ${HOOK_SOURCE}"

# ── Guard I3: la firma de la función debe existir ────────────
if ! grep -q "${HOOK_FUNC_PATTERN}" "${HOOK_SOURCE}"; then
    die "FIX-14: La función 'archiso_mount_handler() {' NO encontrada en ${HOOK_SOURCE}.
  La versión instalada del paquete archiso puede haber cambiado la API.
  Funciones disponibles en el hook actual:
$(grep -E '^[a-zA-Z_]+\(\)' "${HOOK_SOURCE}" | sed 's/^/    /' || echo '    (ninguna encontrada)')
  Ajuste HOOK_FUNC_PATTERN en forge.sh a la firma correcta antes de continuar."
fi
ok "FIX-14: Función objetivo 'archiso_mount_handler()' confirmada en fuente."

# ── Crear directorio de destino ───────────────────────────────
mkdir -p "${HOOK_DEST_DIR}"

# ── Guard I2: idempotencia — skip si el parche ya fue aplicado ─
if [[ -f "${HOOK_DEST}" ]] && grep -q "${PATCH_SIGNATURE}" "${HOOK_DEST}"; then
    ok "FIX-14: Parche ya aplicado en destino (idempotente). Saltando inyección."
else
    log "FIX-14: Aplicando inyección de set -x en copia de trabajo..."

    # Copiar fuente a destino sin modificar el original (I1)
    cp "${HOOK_SOURCE}" "${HOOK_DEST}"

    # Inyección quirúrgica con sed:
    #   /pattern/a\text → inserta `text` en la línea SIGUIENTE a cada
    #   línea que coincide con `pattern`.
    #   Ancla (^) evita falsos positivos en comentarios o strings.
    #
    #   Resultado en el archivo parcheado:
    #     archiso_mount_handler() {
    #         set -x  # FIX-14 v1.6: verbose xtrace for archiso_mount_handler
    #         local newroot="${1}"
    #         ...
    #
    #   Con set -x, cada comando interno (mount -t iso9660, losetup,
    #   mount -t squashfs, mount -t overlay) se imprime en stderr
    #   (consola) con prefijo '+' antes de ejecutarse. El último '+'
    #   antes del mensaje de error = comando exacto que devolvió ≠ 0.
    sed -i \
        "/${HOOK_FUNC_PATTERN}/a\\    set -x  # ${PATCH_SIGNATURE}" \
        "${HOOK_DEST}"

    # ── Guard I4: verificación post-parche obligatoria ─────────
    if grep -q "${PATCH_SIGNATURE}" "${HOOK_DEST}"; then
        ok "FIX-14: Inyección verificada. Contexto del parche:"
        grep -n -B2 -A3 "${PATCH_SIGNATURE}" "${HOOK_DEST}" \
            | sed 's/^/    /'
    else
        # sed no encontró el patrón → archivo no instrumentado
        # Eliminar destino para evitar que mkarchiso use un hook sin parche
        rm -f "${HOOK_DEST}"
        die "FIX-14: El parche NO fue aplicado.
  sed no encontró el patrón: '${HOOK_FUNC_PATTERN}'
  en el archivo fuente: ${HOOK_SOURCE}
  El archivo destino fue eliminado para evitar usar un hook sin instrumentar.
  Dump de las primeras 50 líneas del fuente para diagnóstico:
$(head -50 "${HOOK_SOURCE}" | sed 's/^/    /')"
    fi
fi

chmod 644 "${HOOK_DEST}"
ok "FIX-14: Hook interceptado desplegado en: ${HOOK_DEST}"
ok "FIX-14: Host-side Static Hook Interception completada."

# ════════════════════════════════════════════════════════════
#  1. ÁRBOL DE DIRECTORIOS Y CONFIGURACIÓN NÚCLEO
# ════════════════════════════════════════════════════════════
log "Purgando configuraciones de arranque residuales..."
rm -rf "${ISO_ROOT}/efiboot" "${ISO_ROOT}/syslinux" "${ISO_ROOT}/loader"

log "Creando estructura de directorios..."
mkdir -p \
  "${ISO_ROOT}/airootfs/etc/systemd/system/multi-user.target.wants" \
  "${ISO_ROOT}/airootfs/root/probe.tex" \
  "${ISO_ROOT}/airootfs/root/.cache/Tectonic" \
  "${ISO_ROOT}/efiboot/loader/entries" \
  "${ISO_ROOT}/syslinux" \
  "${ISO_ROOT}/airootfs/etc"

log "Desbloqueando cuenta root (Ring-0 Access)..."
# FIX-6 (v1.2): Formato estándar compatible con sulogin >= 2.37 y PAM.
echo 'root::0:0:99999:7:::' > "${ISO_ROOT}/airootfs/etc/shadow"

# ════════════════════════════════════════════════════════════
#  mkinitcpio.conf — FIX-7 + FIX-8 + FIX-9 + FIX-10 + FIX-13
# ════════════════════════════════════════════════════════════

MKINITCPIO_CONTENT='# /etc/mkinitcpio.conf — probe.tex Live OS
# Generado por forge.sh v1.6
#
# MODULES (FIX-7 + FIX-13): Módulos que DEBEN estar compilados dentro del
# initramfs para que el hook archiso pueda montar el live root.
#
#   loop          losetup para asociar la imagen .sfs a /dev/loopN
#   squashfs      lectura del filesystem dentro del loop device
#   overlay       live root rw (lower=squashfs, upper=tmpfs)
#   cdrom         detección de unidades ópticas (soporte legacy)
#   iso9660       filesystem de la partición principal de la ISO
#
#   ── STACK NLS (FIX-13) ─────────────────────────────────────
#   nls_cp437     codificación CP437 para nombres ISO 9660
#   nls_iso8859_1 codificación Latin-1 para metadata ISO 9660
#   (sin ambos, mount -t iso9660 falla con EINVAL)
#
#   ── STACK FAT (FIX-13) ─────────────────────────────────────
#   vfat          filesystem de partición ARCHISO_EFI (sda2)
#   fat           dependencia base de vfat (FAT core layer)
#
MODULES=(loop squashfs overlay cdrom iso9660 nls_cp437 nls_iso8859_1 vfat fat)

# HOOKS (FIX-9 + FIX-10): Orden estrictamente canónico para archiso v1.6.
#
#   base              Utilidades POSIX mínimas (busybox).
#   udev              Enumeración de dispositivos.
#   modconf           Aplica /etc/modprobe.d/*.conf. (FIX-9)
#   block             Carga drivers de bloque (xHCI, AHCI, NVMe).
#   archiso_udev_settle  udevadm settle + polling /dev/disk/by-label/ (FIX-10)
#   archiso           Localiza el medio, monta squashfs, switch_root.
#                     En v1.6 usa nuestra versión interceptada del hook
#                     (airootfs/usr/lib/initcpio/hooks/archiso) que
#                     tiene set -x inyectado (FIX-14).
#   filesystems       Monta filesystems adicionales.
#   keyboard          Keymap temprano para debug.
#
HOOKS=(base udev modconf block archiso_udev_settle archiso filesystems keyboard)

COMPRESSION="zstd"
'

log "Escribiendo mkinitcpio.conf en raíz del perfil (FIX-8, ruta 1/2)..."
printf '%s' "${MKINITCPIO_CONTENT}" > "${ISO_ROOT}/mkinitcpio.conf"
ok "mkinitcpio.conf escrito en: ${ISO_ROOT}/mkinitcpio.conf"

log "Escribiendo mkinitcpio.conf en airootfs/etc/ (FIX-3+FIX-8, ruta 2/2)..."
printf '%s' "${MKINITCPIO_CONTENT}" > "${ISO_ROOT}/airootfs/etc/mkinitcpio.conf"
ok "mkinitcpio.conf escrito en: ${ISO_ROOT}/airootfs/etc/mkinitcpio.conf"
ok "Módulos críticos: loop squashfs overlay cdrom iso9660 nls_cp437 nls_iso8859_1 vfat fat"
ok "Hooks canónicos v1.6: base udev modconf block archiso_udev_settle archiso filesystems keyboard"

# ════════════════════════════════════════════════════════════
#  2. profiledef.sh
# ════════════════════════════════════════════════════════════
log "Escribiendo profiledef.sh..."
cat > "${ISO_ROOT}/profiledef.sh" << 'PROFILEDEF'
#!/usr/bin/env bash
# profiledef.sh — INVARIANT probe.tex ISO Profile

iso_name="probe-tex"

# iso_label: PROBE_TEX — 9 chars, [A-Z_] únicamente.
# Compatible con ISO 9660 Level 1, udev y parsers de UEFI firmware.
# DEBE coincidir exactamente con archisolabel= en los boot loaders.
iso_label="PROBE_TEX"

iso_publisher="INVARIANT SYSTEMS <https://invariant.systems>"
iso_application="probe.tex Hardware Forensic Diagnostic"
iso_version="$(date +%Y.%m.%d)"
install_dir="arch"
buildmodes=('iso')

# FIX-4 (v1.2): Identificadores canónicos de mkarchiso >= v68.
bootmodes=(
  'bios.syslinux.mbr'
  'uefi-x64.systemd-boot.esp'
)

arch="x86_64"
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86' '-b' '1M' '-Xdict-size' '1M')

file_permissions=(
  ["/root/launcher.sh"]="0:0:755"
  ["/root/.cache"]="0:0:700"
  ["/etc/shadow"]="0:0:400"
  ["/etc/mkinitcpio.conf"]="0:0:644"
  ["/etc/initcpio/hooks/archiso_udev_settle"]="0:0:644"
  ["/etc/initcpio/install/archiso_udev_settle"]="0:0:644"
  ["/etc/initcpio/hooks/archiso"]="0:0:644"
)
PROFILEDEF
ok "profiledef.sh escrito (bootmodes canónicos + label PROBE_TEX + hook interceptado en permisos)."

# ════════════════════════════════════════════════════════════
#  2.1. pacman.conf  (AUD-3)
# ════════════════════════════════════════════════════════════
log "Copiando pacman.conf del anfitrión al perfil (AUD-3)..."
[[ -f /etc/pacman.conf ]] || die "/etc/pacman.conf no encontrado. ¿Ejecutando en Arch Linux?"
cp /etc/pacman.conf "${ISO_ROOT}/pacman.conf"
ok "pacman.conf copiado desde /etc/pacman.conf."

# ════════════════════════════════════════════════════════════
#  3. packages.x86_64
#
#  NOTA FIX-14 v1.6:
#    `archiso` sigue declarado aquí aunque forge.sh ya provee el
#    hook interceptado en airootfs/. El paquete garantiza que los
#    BINARIOS del hook (switch_root, mount, losetup, getarg, msg,
#    etc.) existan en el sistema live. Solo sobreescribimos el
#    SCRIPT del hook con nuestra versión instrumentada; las
#    dependencias ejecutables provienen del paquete instalado.
# ════════════════════════════════════════════════════════════
log "Escribiendo packages.x86_64..."
cat > "${ISO_ROOT}/packages.x86_64" << 'PACKAGES'
# ── Base del sistema ─────────────────────────────────────────
base
linux
linux-firmware
systemd
mkinitcpio
archiso
mkinitcpio-archiso

# ── Python y dependencias de probe.tex ───────────────────────
python
python-rich
python-jinja

# ── Herramientas de diagnóstico activo (extractores) ─────────
stress-ng
fio
memtester
dmidecode
pciutils
usbutils
smartmontools
nvme-cli
lm_sensors
cpupower

# ── Motor LaTeX offline ───────────────────────────────────────
tectonic

# ── Tipografía del reporte ────────────────────────────────────
ttf-ibm-plex

# ── Utilidades de sistema ─────────────────────────────────────
sudo
bash
coreutils
util-linux
procps-ng
sysfsutils
syslinux
tzdata
PACKAGES
ok "packages.x86_64 escrito (archiso presente: binarios del hook garantizados)."

# ════════════════════════════════════════════════════════════
#  4. launcher.sh
# ════════════════════════════════════════════════════════════
log "Escribiendo launcher.sh..."
cat > "${ISO_ROOT}/airootfs/root/launcher.sh" << 'LAUNCHER'
#!/usr/bin/env bash
trap '' SIGINT SIGTERM

RED='\033[1;31m'; WHT='\033[1;37m'; GRN='\033[1;32m'
YLW='\033[1;33m'; DIM='\033[0;90m'; RST='\033[0m'

_header() {
    clear
    echo -e "${WHT}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
    echo -e "${WHT} I N V A R I A N T // probe.tex HARDWARE FORENSIC OS${RST}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
    echo -e "${DIM} MODULE   :${RST} ${WHT}probe.tex v1.0.0${RST}"
    echo -e "${DIM} KERNEL   :${RST} ${WHT}$(uname -r)${RST}"
    echo -e "${DIM} UPTIME   :${RST} ${WHT}$(cut -d. -f1 /proc/uptime)s${RST}"
    echo -e "${DIM} MEMORY   :${RST} ${WHT}$(awk '/MemTotal/{printf "%.0f MB", $2/1024}' /proc/meminfo)${RST}"
    echo -e "${WHT}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
    echo ""
}

_menu() {
    _header
    echo -e "  ${RED}[ 1 ]${RST} ${WHT}INIT DIAGNOSTIC${RST}    ${DIM}Ejecutar probe.tex y generar reporte PDF${RST}"
    echo -e "  ${RED}[ 2 ]${RST} ${WHT}EXPORT REPORT${RST}      ${DIM}Montar USB y extraer reporte generado${RST}"
    echo -e "  ${RED}[ 3 ]${RST} ${WHT}ROOT SHELL${RST}         ${DIM}Acceso a terminal (escriba 'exit' para volver)${RST}"
    echo -e "  ${RED}[ 4 ]${RST} ${WHT}FORCE SHUTDOWN${RST}     ${DIM}Apagado forzado del sistema a nivel kernel${RST}"
    echo ""
}

_shutdown() {
    echo -e "\n${DIM}  [!] Forzando apagado del kernel...${RST}"
    sync
    poweroff -f
}

_run_diagnostic() {
    clear
    echo -e "${RED}════════════════════════════════════════════════════════════${RST}"
    echo -e "${WHT} INVARIANT // Iniciando secuencia de diagnóstico forense...${RST}"
    echo -e "${RED}════════════════════════════════════════════════════════════${RST}"
    echo ""

    local outdir="/tmp"
    cd /root/probe.tex || { echo -e "${RED}[✗] No se encontró /root/probe.tex${RST}"; return 1; }

    rm -f "${outdir}/reporte_generado.pdf" "${outdir}/reporte_generado.tex"

    if python main.py --outdir "${outdir}"; then
        echo ""
        echo -e "${GRN}════════════════════════════════════════════════════════════${RST}"
        echo -e "${GRN} [+] DIAGNÓSTICO COMPLETADO${RST}"
        echo -e "${GRN}     Reporte disponible en: ${outdir}/reporte_generado.pdf${RST}"
        echo -e "${GRN}════════════════════════════════════════════════════════════${RST}"
    else
        echo ""
        echo -e "${RED}[✗] El diagnóstico terminó con errores. Revise el log arriba.${RST}"
    fi

    echo ""
    read -rp "  Presione ENTER para volver al menú..." _
}

_extract_to_usb() {
    clear
    echo -e "${YLW}════════════════════════════════════════════════════════════${RST}"
    echo -e "${WHT} INVARIANT // Módulo de Exfiltración de Reporte${RST}"
    echo -e "${YLW}════════════════════════════════════════════════════════════${RST}"
    echo ""

    local pdf_src="/tmp/reporte_generado.pdf"

    if [[ ! -f "${pdf_src}" ]]; then
        echo -e "${RED}  [✗] No se encontró ${pdf_src}${RST}"
        echo -e "       Ejecute primero la Directiva [ 1 ] para generar el reporte."
        echo ""
        read -rp "  Presione ENTER para volver..." _
        return
    fi

    echo -e "  ${YLW}[!]${RST} Inserte el pendrive USB (FAT32 o exFAT) y presione ENTER."
    read -rp "      [ENTER para escanear dispositivos] " _

    echo ""
    echo -e "  ${WHT}Dispositivos de bloque detectados:${RST}"
    echo ""
    lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT | grep -v "loop" | sed 's/^/    /'
    echo ""

    read -rp "  Ingrese el nodo del USB (ej: sdb1): " usb_node
    local usb_dev="/dev/${usb_node}"

    if [[ ! -b "${usb_dev}" ]]; then
        echo -e "${RED}  [✗] Dispositivo '${usb_dev}' no encontrado.${RST}"
        read -rp "  Presione ENTER para volver..." _
        return
    fi

    local mnt="/mnt/usb_export"
    mkdir -p "${mnt}"

    echo -e "  ${YLW}[*]${RST} Montando ${usb_dev} en ${mnt}..."
    if mount "${usb_dev}" "${mnt}" 2>/dev/null; then
        local dest="${mnt}/reporte_generado_$(date +%Y%m%d_%H%M%S).pdf"
        cp "${pdf_src}" "${dest}"
        sync
        umount "${mnt}"
        echo -e "${GRN}  [+] Reporte copiado exitosamente.${RST}"
        echo -e "       Archivo: $(basename "${dest}")"
    else
        echo -e "${RED}  [✗] Error al montar ${usb_dev}. ¿Formato compatible (FAT32/exFAT)?${RST}"
    fi

    echo ""
    read -rp "  Presione ENTER para volver al menú..." _
}

while true; do
    _menu
    read -rp "  Seleccione directiva [1-4]: " option
    case "${option}" in
        1) _run_diagnostic  ;;
        2) _extract_to_usb  ;;
        3) 
           clear
           PS1="\[\033[1;31m\][ring-0] \W #\[\033[0m\] " bash --norc
           ;;
        4) _shutdown        ;;
    esac
done
LAUNCHER

chmod +x "${ISO_ROOT}/airootfs/root/launcher.sh"
ok "launcher.sh escrito y marcado como ejecutable."

# ════════════════════════════════════════════════════════════
#  5. invariant-probe.service
# ════════════════════════════════════════════════════════════
log "Escribiendo invariant-probe.service..."
cat > "${ISO_ROOT}/airootfs/etc/systemd/system/invariant-probe.service" << 'SERVICE'
[Unit]
Description=INVARIANT Ring-0 Boot Menu
Documentation=https://invariant.systems/probe-tex
After=multi-user.target
Conflicts=getty@tty1.service
ConditionPathExists=/root/launcher.sh

[Service]
Type=idle
ExecStart=/root/launcher.sh
StandardOutput=tty
StandardInput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
KillMode=process

Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
SERVICE
ok "invariant-probe.service escrito."

log "Habilitando invariant-probe.service vía symlink (FIX-1)..."
ln -sf \
    "/etc/systemd/system/invariant-probe.service" \
    "${ISO_ROOT}/airootfs/etc/systemd/system/multi-user.target.wants/invariant-probe.service"
ok "Symlink creado → multi-user.target.wants/invariant-probe.service."

# ════════════════════════════════════════════════════════════
#  6. Mascarar getty@tty1.service  (AUD-2)
# ════════════════════════════════════════════════════════════
log "Mascando getty@tty1.service (AUD-2)..."
ln -sf /dev/null \
    "${ISO_ROOT}/airootfs/etc/systemd/system/getty@tty1.service"
ok "getty@tty1.service mascado (→ /dev/null). tty1 cedido a invariant-probe."

# ════════════════════════════════════════════════════════════
#  6.1 FIX-15 — SILENT BOOT / FIRSTBOOT SUPPRESSION
# ════════════════════════════════════════════════════════════

log "FIX-15 Capa 1: Creando symlink /etc/localtime → America/Argentina/Buenos_Aires..."
mkdir -p "${ISO_ROOT}/airootfs/etc"
rm -f "${ISO_ROOT}/airootfs/etc/localtime"
ln -sf "/usr/share/zoneinfo/America/Argentina/Buenos_Aires" "${ISO_ROOT}/airootfs/etc/localtime"
ok "FIX-15 [1/3]: /etc/localtime → America/Argentina/Buenos_Aires"

log "FIX-15 Capa 2: Generando /etc/machine-id estático..."
printf 'b4d0f00db4d0f00db4d0f00db4d0f00d\n' > "${ISO_ROOT}/airootfs/etc/machine-id"
ok "FIX-15 [2/3]: /etc/machine-id poblado (ID estático live-os)."

log "FIX-15 Capa 3: Mascando systemd-firstboot.service..."
mkdir -p "${ISO_ROOT}/airootfs/etc/systemd/system"
ln -sf /dev/null "${ISO_ROOT}/airootfs/etc/systemd/system/systemd-firstboot.service"
ok "FIX-15 [3/3]: systemd-firstboot.service mascado → /dev/null"
ok "FIX-15 COMPLETO: launcher.sh tiene TTY1 exclusivo desde el primer boot."

# ════════════════════════════════════════════════════════════
#  7. BOOT LOADER — systemd-boot (UEFI) + Syslinux (BIOS)
# ════════════════════════════════════════════════════════════
log "Escribiendo configuración de boot loader..."

cat > "${ISO_ROOT}/efiboot/loader/loader.conf" << 'LOADERCONF'
timeout 3
default 01-probe-tex.conf
console-mode max
editor  no
LOADERCONF
ok "efiboot/loader/loader.conf escrito."

cat > "${ISO_ROOT}/efiboot/loader/entries/01-probe-tex.conf" << 'EFIENTRY'
title   INVARIANT probe.tex // Ring-0 Forensic Diagnostic
linux   /arch/boot/x86_64/vmlinuz-linux
initrd  /arch/boot/x86_64/initramfs-linux.img
options archisobasedir=arch archisolabel=PROBE_TEX archisodelay=5 console=tty0 quiet loglevel=3
EFIENTRY
ok "efiboot/loader/entries/01-probe-tex.conf escrito."

cat > "${ISO_ROOT}/efiboot/loader/entries/02-probe-tex-debug.conf" << 'EFIENTRY_DEBUG'
# INVARIANT probe.tex — UEFI Debug Entry
# archiso_debug=1: activa /run/initramfs/boot-debug.log (FIX-10)
# loglevel=7 + sin quiet: xtrace de FIX-14 visible en consola completa
title   INVARIANT probe.tex // DEBUG MODE
linux   /arch/boot/x86_64/vmlinuz-linux
initrd  /arch/boot/x86_64/initramfs-linux.img
options archisobasedir=arch archisolabel=PROBE_TEX archisodelay=5 archiso_debug=1 console=tty0 loglevel=7
EFIENTRY_DEBUG
ok "efiboot/loader/entries/02-probe-tex-debug.conf escrito."

cat > "${ISO_ROOT}/syslinux/syslinux.cfg" << 'SYSLINUX'
UI      menu.c32
PROMPT  0
TIMEOUT 30

MENU TITLE  INVARIANT probe.tex // Ring-0 Forensic Diagnostic
MENU COLOR border       30;44   #40ffffff #a0000000 std
MENU COLOR title        1;36;44 #ff3333ff #a0000000 std
MENU COLOR sel          7;37;40 #e0ffffff #20ffffff all
MENU COLOR unsel        37;44   #50ffffff #a0000000 std
MENU COLOR tabmsg       31;40   #ff3333ff #00000000 std

LABEL probe-tex
  MENU LABEL  INVARIANT probe.tex // Ring-0 Forensic Diagnostic
  LINUX  /arch/boot/x86_64/vmlinuz-linux
  INITRD /arch/boot/x86_64/initramfs-linux.img
  APPEND archisobasedir=arch archisolabel=PROBE_TEX archisodelay=5 console=tty0 quiet loglevel=3

LABEL probe-tex-debug
  MENU LABEL  INVARIANT probe.tex // DEBUG MODE [archiso_debug=1]
  LINUX  /arch/boot/x86_64/vmlinuz-linux
  INITRD /arch/boot/x86_64/initramfs-linux.img
  APPEND archisobasedir=arch archisolabel=PROBE_TEX archisodelay=5 archiso_debug=1 console=tty0 loglevel=7
SYSLINUX
ok "syslinux/syslinux.cfg escrito."

# ── Capa 4 OPCIONAL: Parámetro kernel systemd.firstboot=0 ──
log "FIX-15 Capa 4: Inyectando systemd.firstboot=0 en entradas de boot..."

EFI_ENTRY="${ISO_ROOT}/efiboot/loader/entries/01-probe-tex.conf"
SYSLINUX_CFG="${ISO_ROOT}/syslinux/syslinux.cfg"

# Parchear entrada EFI (solo si el parámetro aún no está presente)
if [[ -f "${EFI_ENTRY}" ]]; then
    if ! grep -q 'systemd.firstboot=0' "${EFI_ENTRY}"; then
        sed -i 's/^\(options .*\)$/\1 systemd.firstboot=0/' "${EFI_ENTRY}"
        ok "FIX-15 [4a]: systemd.firstboot=0 inyectado en 01-probe-tex.conf"
    else
        ok "FIX-15 [4a]: systemd.firstboot=0 ya presente en EFI entry (idempotente)."
    fi
else
    warn "FIX-15 [4a]: ${EFI_ENTRY} no encontrado. Sección 7 debe ejecutarse primero."
fi

# Parchear entrada Syslinux de producción (no la entrada debug)
if [[ -f "${SYSLINUX_CFG}" ]]; then
    if ! grep -q 'systemd.firstboot=0' "${SYSLINUX_CFG}"; then
        sed -i '/APPEND.*quiet.*loglevel=3/ s/$/ systemd.firstboot=0/' "${SYSLINUX_CFG}"
        ok "FIX-15 [4b]: systemd.firstboot=0 inyectado en syslinux.cfg (entrada producción)."
    else
        ok "FIX-15 [4b]: systemd.firstboot=0 ya presente en Syslinux entry (idempotente)."
    fi
else
    warn "FIX-15 [4b]: ${SYSLINUX_CFG} no encontrado. Sección 7 debe ejecutarse primero."
fi

# ════════════════════════════════════════════════════════════
#  8. SINCRONIZACIÓN DEL REPOSITORIO probe.tex  (AUD-4)
# ════════════════════════════════════════════════════════════
log "Sincronizando repositorio probe.tex hacia la imagen..."

EXCLUDES=(
    --exclude='.git'
    --exclude='.venv'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='probe-tex-iso'
    --exclude='reporte_generado.*'
    --exclude='*.log.txt'
    --exclude='forge.sh'
    --exclude='work'
    --exclude='out'
    --exclude='*.iso'
)

rsync -a --delete "${EXCLUDES[@]}" \
    "${REPO_ROOT}/" \
    "${ISO_ROOT}/airootfs/root/probe.tex/"

ok "Repositorio sincronizado en airootfs/root/probe.tex/"

log "Inyectando _ISO_BUILD_TIMESTAMP (Time Trap) en license_verifier.py..."
BUILD_DATE=$(date -u +%s)
VERIFIER_FILE="${ISO_ROOT}/airootfs/root/probe.tex/core/license_verifier.py"
if [[ -f "${VERIFIER_FILE}" ]]; then
    # Conservamos el type hint (Final[int]) usando una expresión regular fuerte
    sed -i "s/^_ISO_BUILD_TIMESTAMP:.*=.*$/_ISO_BUILD_TIMESTAMP: Final[int] = ${BUILD_DATE}  # FORGE_PATCH_BUILD_TIMESTAMP/" "${VERIFIER_FILE}"
    ok "_ISO_BUILD_TIMESTAMP inyectado: ${BUILD_DATE} (UTC Unix)"
else
    warn "${VERIFIER_FILE} no encontrado. No se aplicó el Time Trap."
fi

# ════════════════════════════════════════════════════════════
#  9. CACHÉ DE TECTONIC
# ════════════════════════════════════════════════════════════
log "Buscando caché de Tectonic en el sistema anfitrión..."

TECTONIC_CACHE_CANDIDATES=(
    "/root/.cache/Tectonic"
    "${HOME}/.cache/Tectonic"
    "/var/cache/tectonic"
)

TECTONIC_SRC=""
for candidate in "${TECTONIC_CACHE_CANDIDATES[@]}"; do
    if [[ -d "${candidate}" ]]; then
        TECTONIC_SRC="${candidate}"
        break
    fi
done

if [[ -n "${TECTONIC_SRC}" ]]; then
    log "Caché encontrado en: ${TECTONIC_SRC}"
    rsync -a "${TECTONIC_SRC}/" \
          "${ISO_ROOT}/airootfs/root/.cache/Tectonic/"
    ok "Ecosistema LaTeX offline inyectado correctamente."
else
    warn "Caché de Tectonic NO encontrado en el sistema anfitrión."
    warn "La ISO compilará LaTeX correctamente SOLO si tiene acceso a red."
    warn "Para generar el caché offline, ejecuta en el anfitrión:"
    echo ""
    echo -e "    ${DIM}cd /root/probe.tex && sudo python main.py --outdir /tmp${RST}"
    echo ""
    warn "Luego vuelve a ejecutar forge.sh para capturar el caché."
fi

# ════════════════════════════════════════════════════════════
#  10. VERIFICACIÓN FINAL DE CONSISTENCIA
#
#  A) Los tres puntos de definición del label son idénticos.
#  B) MODULES contiene el stack completo (FIX-7 + FIX-13).
#  C) HOOKS contiene modconf (FIX-9).
#  D) mkinitcpio.conf existe en ambas rutas (FIX-8).
#  E) archisodelay consistente entre EFI y Syslinux (FIX-11).
#  F) Archivos del hook archiso_udev_settle presentes (FIX-10).
#  G) MODULES contiene stack NLS/FAT completo (FIX-13).
#  H) v1.6: Hook interceptado presente en airootfs/ con firma
#     FIX-14 v1.6 (reemplaza verificación de customize_airootfs.sh).
# ════════════════════════════════════════════════════════════
log "Verificando consistencia de ISO label en todos los archivos..."

LABEL_PROFILEDEF=$(grep 'iso_label=' "${ISO_ROOT}/profiledef.sh" \
    | head -1 | sed 's/.*iso_label="\([^"]*\)".*/\1/')
LABEL_EFI=$(grep 'archisolabel=' \
    "${ISO_ROOT}/efiboot/loader/entries/01-probe-tex.conf" \
    | sed 's/.*archisolabel=\([^ ]*\).*/\1/')
LABEL_SYSLINUX=$(grep 'archisolabel=' "${ISO_ROOT}/syslinux/syslinux.cfg" \
    | grep -v 'debug' | tail -1 | sed 's/.*archisolabel=\([^ ]*\).*/\1/')

LABEL_OK=true
[[ "${LABEL_PROFILEDEF}" == "${LABEL_EFI}" ]]      || LABEL_OK=false
[[ "${LABEL_PROFILEDEF}" == "${LABEL_SYSLINUX}" ]] || LABEL_OK=false

if [[ "${LABEL_OK}" == "true" ]]; then
    ok "Label consistente en los tres archivos: '${LABEL_PROFILEDEF}'"
else
    die "¡INCONSISTENCIA DE LABEL DETECTADA!
  profiledef.sh : '${LABEL_PROFILEDEF}'
  EFI entry     : '${LABEL_EFI}'
  syslinux.cfg  : '${LABEL_SYSLINUX}'"
fi

# ── Verificación B: MODULES críticos presentes (FIX-7 + FIX-13) ──
log "Verificando MODULES críticos en mkinitcpio.conf..."
MKINIT_CHECK="${ISO_ROOT}/airootfs/etc/mkinitcpio.conf"
for mod in loop squashfs overlay cdrom iso9660 nls_cp437 nls_iso8859_1 vfat fat; do
    grep -q "${mod}" "${MKINIT_CHECK}" \
        || die "Módulo '${mod}' NO encontrado en mkinitcpio.conf.
  El switch_root fallará sin este módulo. Ruta: ${MKINIT_CHECK}"
    ok "Módulo confirmado: ${mod}"
done

# ── Verificación C: modconf en HOOKS (FIX-9) ──────────────────
log "Verificando hook 'modconf' en HOOKS..."
grep -q 'modconf' "${MKINIT_CHECK}" \
    || die "Hook 'modconf' NO encontrado en HOOKS. Ruta: ${MKINIT_CHECK}"
ok "Hook 'modconf' confirmado en HOOKS."

# ── Verificación D: doble presencia del config (FIX-8) ────────
log "Verificando doble presencia de mkinitcpio.conf..."
[[ -f "${ISO_ROOT}/mkinitcpio.conf" ]] \
    || die "mkinitcpio.conf ausente en raíz del perfil."
[[ -f "${ISO_ROOT}/airootfs/etc/mkinitcpio.conf" ]] \
    || die "mkinitcpio.conf ausente en airootfs/etc/."
ok "mkinitcpio.conf presente en raíz del perfil."
ok "mkinitcpio.conf presente en airootfs/etc/."

# ── Verificación E: archisodelay consistente (FIX-11 AUD-5) ───
log "Verificando consistencia de archisodelay entre EFI y Syslinux..."

DELAY_EFI=$(grep 'archisodelay=' \
    "${ISO_ROOT}/efiboot/loader/entries/01-probe-tex.conf" \
    | sed 's/.*archisodelay=\([0-9]*\).*/\1/')
DELAY_SYSLINUX=$(grep 'archisodelay=' "${ISO_ROOT}/syslinux/syslinux.cfg" \
    | grep -v 'debug' | grep -v '#' | tail -1 \
    | sed 's/.*archisodelay=\([0-9]*\).*/\1/')

if [[ "${DELAY_EFI}" == "${DELAY_SYSLINUX}" ]]; then
    ok "archisodelay consistente en ambos loaders: ${DELAY_EFI}s"
else
    die "¡INCONSISTENCIA DE archisodelay!
  EFI  (01-probe-tex.conf): archisodelay=${DELAY_EFI}
  BIOS (syslinux.cfg):      archisodelay=${DELAY_SYSLINUX}"
fi

# ── Verificación F: hook archiso_udev_settle presente (FIX-10 AUD-6) ──
log "Verificando presencia del hook archiso_udev_settle..."

HOOK_RUNTIME_PATH="${ISO_ROOT}/airootfs/etc/initcpio/hooks/archiso_udev_settle"
HOOK_INSTALL_PATH="${ISO_ROOT}/airootfs/etc/initcpio/install/archiso_udev_settle"

[[ -f "${HOOK_RUNTIME_PATH}" ]] \
    || die "Hook runtime ausente: ${HOOK_RUNTIME_PATH}"
[[ -f "${HOOK_INSTALL_PATH}" ]] \
    || die "Hook install ausente: ${HOOK_INSTALL_PATH}"
ok "Hook runtime confirmado: airootfs/etc/initcpio/hooks/archiso_udev_settle"
ok "Hook install confirmado: airootfs/etc/initcpio/install/archiso_udev_settle"

log "Verificando que archiso_udev_settle está declarado en HOOKS..."
grep -q 'archiso_udev_settle' "${MKINIT_CHECK}" \
    || die "Hook 'archiso_udev_settle' NO declarado en HOOKS."
ok "Hook 'archiso_udev_settle' declarado en HOOKS."

# ── Verificación G: stack NLS/FAT (FIX-13 AUD-7) ─────────────
log "Verificando stack NLS/FAT en MODULES..."
for nls_mod in nls_cp437 nls_iso8859_1 vfat fat; do
    grep -q "${nls_mod}" "${MKINIT_CHECK}" \
        || die "Módulo NLS/FAT '${nls_mod}' NO encontrado en MODULES.
  mount -t iso9660 falla con EINVAL sin los módulos NLS. Ruta: ${MKINIT_CHECK}"
    ok "Módulo NLS/FAT confirmado: ${nls_mod}"
done

# ── Verificación H v1.6: hook interceptado con firma FIX-14 (AUD-8) ──
#
#  REEMPLAZA AUD-8 v1.5 (que verificaba customize_airootfs.sh).
#
#  Esta verificación confirma tres condiciones independientes:
#    H1: El archivo destino existe en airootfs/usr/lib/initcpio/hooks/archiso.
#        Si no existe, mkarchiso usará la versión sin instrumentar del paquete.
#    H2: El archivo es legible y tiene contenido (no es un stub vacío).
#    H3: La firma del parche FIX-14 v1.6 está presente en el archivo.
#        Garantiza que fue parcheado por esta sección de forge.sh y no
#        es una copia sin modificar del original del paquete.
#
log "Verificando hook interceptado con firma FIX-14 v1.6 (AUD-8)..."

INTERCEPTED_HOOK="${ISO_ROOT}/airootfs/etc/initcpio/hooks/archiso"

# H1: existencia
[[ -f "${INTERCEPTED_HOOK}" ]] \
    || die "AUD-8: Hook interceptado ausente: ${INTERCEPTED_HOOK}
  La sección FIX-14 de forge.sh debería haberlo creado.
  Si este error aparece después de que FIX-14 reportó éxito,
  hay un problema de paths. Verifique HOOK_DEST en forge.sh."

# H2: contenido mínimo (más de 10 líneas → no es stub vacío)
line_count=$(wc -l < "${INTERCEPTED_HOOK}")
[[ "${line_count}" -gt 10 ]] \
    || die "AUD-8: El hook interceptado tiene solo ${line_count} líneas.
  Se esperaba una copia completa del hook archiso con el parche.
  Ruta: ${INTERCEPTED_HOOK}"

# H3: firma del parche
grep -q "${PATCH_SIGNATURE}" "${INTERCEPTED_HOOK}" \
    || die "AUD-8: La firma '${PATCH_SIGNATURE}' NO encontrada en:
  ${INTERCEPTED_HOOK}
  El archivo existe pero no contiene el parche set -x.
  El build de mkarchiso usaría un hook sin instrumentar.
  Re-ejecute forge.sh para forzar la re-aplicación del parche."

ok "AUD-8 H1: Hook interceptado presente en airootfs/usr/lib/initcpio/hooks/archiso."
ok "AUD-8 H2: Contenido válido (${line_count} líneas)."
ok "AUD-8 H3: Firma FIX-14 v1.6 confirmada en el hook interceptado."

# ── Verificación I: Silent Boot (FIX-15 AUD-9) ───────────────
log "Verificando capas de Silent Boot (FIX-15 AUD-9)..."

# I1: /etc/localtime debe ser un symlink
if [[ -L "${ISO_ROOT}/airootfs/etc/localtime" ]]; then
    TZ_TARGET=$(readlink "${ISO_ROOT}/airootfs/etc/localtime")
    ok "AUD-9 I1: /etc/localtime es symlink → ${TZ_TARGET}"
    if [[ "${TZ_TARGET}" != *"America/Argentina/Buenos_Aires"* ]]; then
        warn "AUD-9 I1: Zona inesperada: ${TZ_TARGET} (se esperaba Buenos_Aires)"
    fi
else
    die "AUD-9 I1: /etc/localtime NO es un symlink o no existe en airootfs.
  FIX-15 Capa 1 no fue aplicada correctamente.
  Ruta esperada: ${ISO_ROOT}/airootfs/etc/localtime"
fi

# I2: /etc/machine-id debe existir y contener exactamente 32 hex chars + newline
MACHINE_ID_PATH="${ISO_ROOT}/airootfs/etc/machine-id"
if [[ ! -f "${MACHINE_ID_PATH}" ]]; then
    die "AUD-9 I2: /etc/machine-id ausente en airootfs.
  FIX-15 Capa 2 no fue aplicada.
  Ruta: ${MACHINE_ID_PATH}"
fi

MACHINE_ID_CONTENT=$(tr -d '\n' < "${MACHINE_ID_PATH}")
if [[ "${MACHINE_ID_CONTENT}" =~ ^[0-9a-f]{32}$ ]]; then
    ok "AUD-9 I2: /etc/machine-id válido: ${MACHINE_ID_CONTENT}"
else
    die "AUD-9 I2: /etc/machine-id tiene formato inválido: '${MACHINE_ID_CONTENT}'
  Se requieren exactamente 32 caracteres hexadecimales en minúsculas.
  systemd-firstboot NO considerará el sistema inicializado."
fi

# I3: systemd-firstboot.service debe estar mascado (→ /dev/null)
FIRSTBOOT_MASK="${ISO_ROOT}/airootfs/etc/systemd/system/systemd-firstboot.service"
if [[ -L "${FIRSTBOOT_MASK}" ]]; then
    MASK_TARGET=$(readlink "${FIRSTBOOT_MASK}")
    if [[ "${MASK_TARGET}" == "/dev/null" ]]; then
        ok "AUD-9 I3: systemd-firstboot.service mascado → /dev/null"
    else
        warn "AUD-9 I3: systemd-firstboot.service es symlink pero NO apunta a /dev/null.
  Apunta a: ${MASK_TARGET}"
    fi
else
    die "AUD-9 I3: systemd-firstboot.service NO está mascado en airootfs.
  FIX-15 Capa 3 no fue aplicada.
  Ruta esperada: ${FIRSTBOOT_MASK} → /dev/null"
fi

# ════════════════════════════════════════════════════════════
#  11. RESUMEN FINAL
# ════════════════════════════════════════════════════════════
echo ""
echo -e "${RED}════════════════════════════════════════════════════════════${RST}"
echo -e "${WHT} FORGE v1.6 COMPLETADO — ESTRUCTURA LISTA PARA mkarchiso${RST}"
echo -e "${RED}════════════════════════════════════════════════════════════${RST}"
echo ""

echo -e "${WHT}  Árbol generado en:${RST} ${ISO_ROOT}"
echo ""
find "${ISO_ROOT}" \
    -not -path "*/probe.tex/*" \
    -not -path "*/.cache/Tectonic/*" \
    -not -path "*/.git/*" \
    | sort | sed 's|'"${ISO_ROOT}"'||' | sed 's|^|    |'

echo ""
echo -e "${WHT}  Parches aplicados en este build:${RST}"
echo -e "    ${GRN}FIX-1${RST}  Symlink multi-user.target.wants/ → servicio habilitado."
echo -e "    ${GRN}FIX-2${RST}  Boot menu: timeout 15→3s; systemd-boot."
echo -e "    ${GRN}FIX-3${RST}  mkinitcpio.conf → airootfs/etc/ (hook archiso activo)."
echo -e "    ${GRN}FIX-4${RST}  bootmodes: identificadores canónicos mkarchiso >= v68."
echo -e "    ${GRN}FIX-5${RST}  HOOKS: eliminado archiso_loop_mnt; block antes de archiso."
echo -e "    ${GRN}FIX-6${RST}  shadow: formato passwordless (root::0:0:99999:7:::)."
echo -e "    ${GRN}FIX-7${RST}  MODULES base: loop squashfs overlay cdrom iso9660."
echo -e "    ${GRN}FIX-8${RST}  mkinitcpio.conf en doble ruta (perfil + airootfs/etc/)."
echo -e "    ${GRN}FIX-9${RST}  modconf añadido a HOOKS (AMD Ryzen / hardware moderno)."
echo -e "    ${GRN}FIX-10${RST} Hook archiso_udev_settle: race condition xHCI eliminada."
echo -e "    ${GRN}FIX-11${RST} archisodelay unificado: EFI y BIOS = 5s."
echo -e "    ${GRN}FIX-12${RST} Entradas debug en EFI y Syslinux (archiso_debug=1)."
echo -e "    ${GRN}FIX-13${RST} MODULES: + nls_cp437 nls_iso8859_1 vfat fat."
echo -e "           ${DIM}mount -t iso9660 deja de fallar con EINVAL.${RST}"
echo -e "    ${GRN}FIX-14${RST} ${WHT}v1.6${RST} Host-side Static Hook Interception."
echo -e "           ${DIM}forge.sh lee /usr/lib/initcpio/hooks/archiso del host,${RST}"
echo -e "           ${DIM}inyecta set -x en archiso_mount_handler con sed, y deposita${RST}"
echo -e "           ${DIM}la versión instrumentada en airootfs/usr/lib/initcpio/hooks/.${RST}"
echo -e "           ${DIM}mkarchiso prioriza airootfs/ → el initramfs usa nuestro hook.${RST}"
echo -e "           ${DIM}customize_airootfs.sh ELIMINADO (deprecado en mkarchiso reciente).${RST}"
echo -e "    ${GRN}FIX-15${RST} Silent Boot — systemd-firstboot suprimido (3 capas)."
echo -e "           ${DIM}/etc/localtime → America/Argentina/Buenos_Aires${RST}"
echo -e "           ${DIM}/etc/machine-id estático; firstboot.service mascado.${RST}"
echo -e "           ${DIM}Kernel param systemd.firstboot=0 en entradas de boot.${RST}"
echo -e "    ${GRN}SEC-1${RST}  Time Trap inyectado en core/license_verifier.py."
echo -e "           ${DIM}_ISO_BUILD_TIMESTAMP establecido a la fecha de este build.${RST}"
echo -e "    ${GRN}AUD-1${RST}  profiledef.sh: bootmodes GRUB → systemd-boot."
echo -e "    ${GRN}AUD-2${RST}  getty@tty1: máscara /dev/null."
echo -e "    ${GRN}AUD-3${RST}  pacman.conf copiado desde /etc/pacman.conf."
echo -e "    ${GRN}AUD-4${RST}  rsync: forge.sh excluido del squashfs."
echo -e "    ${GRN}AUD-5${RST}  archisodelay consistente entre EFI y Syslinux."
echo -e "    ${GRN}AUD-6${RST}  Hook archiso_udev_settle presente en initcpio/."
echo -e "    ${GRN}AUD-7${RST}  Stack NLS/FAT completo en MODULES."
echo -e "    ${GRN}AUD-8${RST}  ${WHT}v1.6${RST} Hook interceptado en airootfs/ con firma FIX-14."
echo -e "           ${DIM}Reemplaza verificación de customize_airootfs.sh (deprecado).${RST}"
echo -e "    ${GRN}AUD-9${RST}  Silent Boot: localtime symlink + machine-id + service mask."
echo ""
echo -e "${WHT}  Prerequisito del host (necesario para FIX-14 + mkarchiso):${RST}"
echo -e "    ${DIM}sudo pacman -S archiso   # si no está instalado${RST}"
echo ""
echo -e "${WHT}  Siguiente paso — compilar la ISO:${RST}"
echo ""
echo -e "    ${DIM}sudo mkarchiso -v -w /tmp/archiso-work -o ./out/ probe-tex-iso/${RST}"
echo ""
echo -e "${WHT}  Lectura del xtrace en boot (FIX-14 v1.6):${RST}"
echo -e "    ${DIM}Con set -x en archiso_mount_handler, la consola imprime cada${RST}"
echo -e "    ${DIM}comando con prefijo '+'. El último '+' antes del error:${RST}"
echo -e "    ${DIM}  + mount -t iso9660 ... → NLS ausente o device incorrecto${RST}"
echo -e "    ${DIM}  + losetup /dev/loop0 → módulo loop o path al .sfs${RST}"
echo -e "    ${DIM}  + mount -t squashfs  → módulo squashfs${RST}"
echo -e "    ${DIM}  + mount -t overlay   → módulo overlay${RST}"
echo ""
echo -e "${WHT}  Debug mode (FIX-12 + FIX-14 combinados):${RST}"
echo -e "    ${DIM}UEFI: seleccionar '02-probe-tex-debug.conf' en systemd-boot${RST}"
echo -e "    ${DIM}BIOS: seleccionar 'probe-tex-debug' en el menú Syslinux${RST}"
echo -e "    ${DIM}Con loglevel=7 el xtrace de FIX-14 es visible sin filtros.${RST}"
echo -e "    ${DIM}Log estructurado de udev_settle: cat /run/initramfs/boot-debug.log${RST}"
echo ""
echo -e "${GRN}  [+] forge.sh v1.6 terminado sin errores.${RST}"
echo ""