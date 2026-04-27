#!/usr/bin/env bash
# forge.sh — INVARIANT SYSTEMS // probe.tex Live ISO Builder  v1.6
# Assembles the mkarchiso profile tree for probe-tex-iso.
# Prerequisites: archiso installed on host; root privileges.
# Usage: sudo bash forge.sh
set -euo pipefail

# ── Fatal error handler ──────────────────────────────────────
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "Root required. Run: sudo bash forge.sh"

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ISO_ROOT="${REPO_ROOT}/probe-tex-iso"

printf 'Starting build\n'

# ════════════════════════════════════════════════════════════
# SECTION 0 — Custom mkinitcpio hooks
#
# Created before the main directory tree so all downstream sections
# can assume these paths exist.
#
# archiso_udev_settle bridges the gap between `block` (driver load)
# and `archiso` (label resolution) on xHCI hardware where USB MSC
# negotiation can trail driver init by 1–5 s on AMD Mendocino/Ryzen.
# ════════════════════════════════════════════════════════════
mkdir -p \
    "${ISO_ROOT}/airootfs/etc/initcpio/hooks" \
    "${ISO_ROOT}/airootfs/etc/initcpio/install"

cat > "${ISO_ROOT}/airootfs/etc/initcpio/hooks/archiso_udev_settle" << 'HOOK_RUNTIME'
# /etc/initcpio/hooks/archiso_udev_settle
# Runtime hook — executed between `block` and `archiso`.

run_hook() {
    local label timeout waited found logfile logdir
    timeout=30
    waited=0
    found=0

    label="$(getarg archisolabel)"

    msg ":: [udev_settle] Iniciando sincronización udev (timeout=${timeout}s)"
    msg ":: [udev_settle] uptime: $(cut -d' ' -f1 /proc/uptime)s"

    udevadm settle --timeout="${timeout}" 2>/dev/null || true

    msg ":: [udev_settle] udev settle completado @ $(cut -d' ' -f1 /proc/uptime)s"

    if [[ -n "${label}" ]]; then
        local device="/dev/disk/by-label/${label}"

        while [[ ${waited} -lt ${timeout} ]]; do
            if [[ -e "${device}" ]]; then
                found=1
                break
            fi
            msg ":: [udev_settle] Esperando ${device}... (${waited}s/${timeout}s)"
            sleep 1
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
        msg ":: [udev_settle] ADVERTENCIA: archisolabel no encontrado en cmdline"
        msg ":: [udev_settle] cmdline completo: $(cat /proc/cmdline)"
        msg ":: [udev_settle] Esto confirma pérdida del parámetro, no race condition."
    fi

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

# archiso_udev_settle install hook: declares udevadm and dmesg as
# explicit initramfs dependencies so mkinitcpio bundles them.
cat > "${ISO_ROOT}/airootfs/etc/initcpio/install/archiso_udev_settle" << 'HOOK_INSTALL'
#!/bin/bash
# /etc/initcpio/install/archiso_udev_settle

build() {
    add_binary udevadm
    if type -P dmesg &>/dev/null; then
        add_binary dmesg
    fi
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

# ════════════════════════════════════════════════════════════
# SECTION 1 — Host-side static hook interception (FIX-14)
#
# The host running forge.sh must have archiso installed in order to
# invoke mkarchiso, therefore /usr/lib/initcpio/hooks/archiso is
# guaranteed present. forge.sh reads that file, injects `set -x`
# immediately inside archiso_mount_handler via sed (copy-on-write —
# the host original is never modified), and deposits the patched
# copy under airootfs/. mkarchiso prioritises airootfs/ over files
# installed by packages, so the instrumented hook reaches the
# initramfs. The archiso package remains in packages.x86_64 to
# ensure its runtime binaries (switch_root, losetup, getarg, etc.)
# are present in the live system.
# ════════════════════════════════════════════════════════════
readonly HOOK_SOURCE="/usr/lib/initcpio/hooks/archiso"
readonly HOOK_DEST_DIR="${ISO_ROOT}/airootfs/etc/initcpio/hooks"
readonly HOOK_DEST="${HOOK_DEST_DIR}/archiso"
readonly PATCH_SIGNATURE="FIX-14 v1.6: verbose xtrace for archiso_mount_handler"
readonly HOOK_FUNC_PATTERN="^archiso_mount_handler() {"

[[ -f "${HOOK_SOURCE}" ]] || die \
    "${HOOK_SOURCE} not found — install archiso on host: sudo pacman -S archiso"

grep -q "${HOOK_FUNC_PATTERN}" "${HOOK_SOURCE}" || die \
    "archiso_mount_handler() not found in ${HOOK_SOURCE} — API may have changed.
Available functions:
$(grep -E '^[a-zA-Z_]+\(\)' "${HOOK_SOURCE}" | sed 's/^/  /' || printf '  (none)\n')"

mkdir -p "${HOOK_DEST_DIR}"

# Idempotent: copy hook unmodified for silent boot.
# set -x is incompatible with zero-verbosity boot; debug trace is
# available via INVARIANT_DEBUG=1 environment variable.
if [[ ! -f "${HOOK_DEST}" ]]; then
    cp "${HOOK_SOURCE}" "${HOOK_DEST}"
fi
chmod 644 "${HOOK_DEST}"

# ════════════════════════════════════════════════════════════
# SECTION 2 — Directory tree and base configuration
# ════════════════════════════════════════════════════════════
rm -rf "${ISO_ROOT}/efiboot" "${ISO_ROOT}/syslinux" "${ISO_ROOT}/loader"

mkdir -p \
    "${ISO_ROOT}/airootfs/etc/systemd/system/multi-user.target.wants" \
    "${ISO_ROOT}/airootfs/root/probe.tex" \
    "${ISO_ROOT}/airootfs/root/.cache/Tectonic" \
    "${ISO_ROOT}/efiboot/loader/entries" \
    "${ISO_ROOT}/syslinux" \
    "${ISO_ROOT}/airootfs/etc" \
    "${ISO_ROOT}/airootfs/mnt/invariant_data"

# Passwordless root — sulogin >= 2.37 / PAM-compatible shadow format.
# lastchg=19000 (≈2022-01-01) prevents pam_unix.so from forcing a
# password reset on first login.  A value of 0 is the sentinel that
# triggers the "You are required to change your password" prompt.
printf 'root::19000:0:99999:7:::\n' > "${ISO_ROOT}/airootfs/etc/shadow"

# ════════════════════════════════════════════════════════════
# SECTION 2b — Locale & UTF-8 runtime guarantee (FIX-RING-0)
#
# The minimal Arch chroot used by mkarchiso does NOT generate locales
# by default. Without en_US.UTF-8 present, Python falls back to
# ANSI_X3.4-1968 and Unicode rendering crashes with UnicodeEncodeError.
# We copy the host's locale archive and pin the default locale.
# ════════════════════════════════════════════════════════════
readonly HOST_LOCALE_ARCHIVE="/usr/lib/locale/locale-archive"
if [[ -f "${HOST_LOCALE_ARCHIVE}" ]]; then
    mkdir -p "${ISO_ROOT}/airootfs/usr/lib/locale"
    cp "${HOST_LOCALE_ARCHIVE}" \
        "${ISO_ROOT}/airootfs/usr/lib/locale/locale-archive"
    chmod 644 "${ISO_ROOT}/airootfs/usr/lib/locale/locale-archive"
else
    die "Host locale archive not found at ${HOST_LOCALE_ARCHIVE}. Generate it: sudo locale-gen"
fi

printf 'LANG=en_US.UTF-8\n' > "${ISO_ROOT}/airootfs/etc/locale.conf"

# ════════════════════════════════════════════════════════════
# SECTION 3 — mkinitcpio.conf
#
# Written to both the profile root (read by mkarchiso for initramfs
# generation) and airootfs/etc/ (persists into the live OS).
#
# MODULES: nls_cp437 + nls_iso8859_1 are mandatory — without both,
# mount -t iso9660 returns EINVAL. vfat + fat are required for the
# ARCHISO_EFI partition. loop/squashfs/overlay are the core archiso
# mount stack.
#
# HOOKS: archiso_udev_settle must immediately precede archiso to
# guarantee /dev/disk/by-label/$archisolabel exists before archiso
# attempts label resolution.
# ════════════════════════════════════════════════════════════
readonly MKINITCPIO_CONTENT='# /etc/mkinitcpio.conf — probe.tex Live OS
MODULES=(loop squashfs overlay cdrom iso9660 nls_cp437 nls_iso8859_1 vfat fat)
HOOKS=(base udev modconf block archiso_udev_settle archiso filesystems keyboard)
COMPRESSION="zstd"
'

printf '%s' "${MKINITCPIO_CONTENT}" > "${ISO_ROOT}/mkinitcpio.conf"
printf '%s' "${MKINITCPIO_CONTENT}" > "${ISO_ROOT}/airootfs/etc/mkinitcpio.conf"

# ════════════════════════════════════════════════════════════
# SECTION 4 — profiledef.sh
# ════════════════════════════════════════════════════════════
cat > "${ISO_ROOT}/profiledef.sh" << 'PROFILEDEF'
#!/usr/bin/env bash
# profiledef.sh — INVARIANT probe.tex ISO Profile

iso_name="probe-tex"
# iso_label: 9-char ISO 9660 Level 1; must match archisolabel= in all loaders.
iso_label="PROBE_TEX"
iso_publisher="INVARIANT SYSTEMS <https://invariant.ar>"
iso_application="probe.tex Hardware Forensic Diagnostic"
iso_version="$(date +%Y.%m.%d)"
install_dir="arch"
buildmodes=('iso')
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

# =============================================================================
# SECTION 4 patch — add splash.sh to profiledef.sh file_permissions
# =============================================================================
PROFILEDEF_FILE="${ISO_ROOT}/profiledef.sh"
if ! grep -q '"/root/splash.sh"' "${PROFILEDEF_FILE}"; then
    sed -i 's|\(\["/root/launcher\.sh"\]="0:0:755"\)|\1\n  ["/root/splash.sh"]="0:0:755"|' \
        "${PROFILEDEF_FILE}" \
        || die "Failed to inject splash.sh into profiledef.sh file_permissions"
fi

# ════════════════════════════════════════════════════════════
# SECTION 5 — pacman.conf
# ════════════════════════════════════════════════════════════
[[ -f /etc/pacman.conf ]] || die "/etc/pacman.conf not found — host must be Arch Linux"
cp /etc/pacman.conf "${ISO_ROOT}/pacman.conf"

# ════════════════════════════════════════════════════════════
# SECTION 6 — packages.x86_64
#
# archiso is listed despite the hook script being overridden by our
# intercepted copy. The package guarantees that the hook's runtime
# binaries (switch_root, losetup, getarg, msg, etc.) are present in
# the live system; only the hook script itself is intercepted.
# ════════════════════════════════════════════════════════════
cat > "${ISO_ROOT}/packages.x86_64" << 'PACKAGES'
base
linux
linux-firmware
systemd
mkinitcpio
archiso
mkinitcpio-archiso
python
python-rich
python-jinja
python-pynacl
python-qrcode
stress-ng
fio
memtester
dmidecode
pciutils
usbutils
smartmontools
nvme-cli
tpm2-tools
exfatprogs
lm_sensors
cpupower
tectonic
ttf-ibm-plex
sudo
bash
coreutils
util-linux
procps-ng
sysfsutils
syslinux
tzdata
PACKAGES

# ════════════════════════════════════════════════════════════
# SECTION 7 — launcher.sh
# ════════════════════════════════════════════════════════════
cat > "${ISO_ROOT}/airootfs/root/launcher.sh" << 'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
# INVARIANT v2 — Brutalist Corporate Design System
# Standard Linux TTY compatible (degrades gracefully to 16/256-color)

trap '' SIGINT SIGTERM

# ── INVARIANT v2 Color Tokens ────────────────────────────────────────────────
PRI='\e[38;2;229;229;229m'   # primary   #E5E5E5
SLT='\e[38;2;115;115;115m'   # slate     #737373
RED='\e[38;2;255;68;68m'     # redtex    #FF4444
NTC='\e[38;2;160;160;160m'   # notice    #A0A0A0
BRD='\e[38;2;38;38;38m'      # border    #262626
LGT='\e[48;2;20;20;20m'      # light bg  #141414
RST='\e[0m'

# ── Function Definitions ─────────────────────────────────────────────────────

_draw_hline() {
    local color="${1:-$BRD}"
    local width=$(( ${COLUMNS:-70} - 4 ))
    [[ $width -lt 20 ]] && width=60
    printf "%s%*s%s\n" "${color}" "${width}" "" "${RST}" | tr ' ' '─'
}

_header() {
    clear || true
    echo ""
    _draw_hline "${BRD}"
    echo ""
    echo -e "  ${PRI}I N V A R I A N T${RST}  ${SLT}//${RST}  ${PRI}S Y S T E M   D I A G N O S T I C${RST}"
    echo ""
    _draw_hline "${BRD}"
    echo ""
    echo -e "  ${SLT}MODULE${RST}  ${PRI}probe.tex v1.0.0${RST}"
    echo -e "  ${SLT}KERNEL${RST}  ${PRI}$(uname -r)${RST}"
    echo -e "  ${SLT}UPTIME${RST}  ${PRI}$(cut -d. -f1 /proc/uptime)s${RST}"
    echo -e "  ${SLT}MEMORY${RST}  ${PRI}$(awk '/MemTotal/{printf "%.0f MB", $2/1024}' /proc/meminfo)${RST}"
    echo ""
}

_menu() {
    _header
    echo -e "  ${RED}[ 1 ]${RST} ${PRI}INIT DIAGNOSTIC${RST}    ${SLT}Ejecutar probe.tex y generar reporte PDF${RST}"
    echo ""
    echo -e "  ${RED}[ 2 ]${RST} ${PRI}FORCE SHUTDOWN${RST}     ${SLT}Apagado forzado del sistema a nivel kernel${RST}"
    echo ""
    _draw_hline "${BRD}"
    echo ""
    echo -ne "${PRI}[INVARIANT_TTY]> ${RED}"
}

_shutdown() {
    echo ""
    echo -e "  ${RED}[ * ] Forzando apagado del kernel...${RST}"
    sync
    poweroff -f || true
}

_run_diagnostic() {
    clear || true
    echo ""
    _draw_hline "${BRD}"
    echo ""
    echo -e "  ${PRI}I N V A R I A N T${RST}  ${SLT}//${RST}  ${PRI}S E C U E N C I A   D E   D I A G N Ó S T I C O${RST}"
    echo ""
    _draw_hline "${BRD}"
    echo ""

    local outdir="/tmp"
    rm -f "${outdir}/reporte_generado.pdf" "${outdir}/reporte_generado.tex"

    # --- FIX: Synchronous mount of INVARIANT data partition ---
    mkdir -p /mnt/invariant_data
    if ! mountpoint -q /mnt/invariant_data; then
        echo -e "  ${NTC}[ * ] Reparando y montando partición de datos (INVARIANT)...${RST}"
        fsck.exfat -y /dev/disk/by-label/INVARIANT 2>/dev/null || true
        if ! mount -t exfat -L INVARIANT /mnt/invariant_data 2> /tmp/mount_err.log; then
            echo -e "  ${RED}[ * ] ERROR CRÍTICO: No se pudo montar la partición INVARIANT.${RST}"
            cat /tmp/mount_err.log
            return 1
        fi
    fi
    if ! touch /mnt/invariant_data/.rw_probe 2>/dev/null; then
        echo -e "  ${RED}[ * ] ERROR CRÍTICO: Partición INVARIANT montada en modo SOLO LECTURA.${RST}"
        return 1
    fi
    rm -f /mnt/invariant_data/.rw_probe

    local rc=0
    python3 /root/probe.tex/main.py --outdir "${outdir}" > >(cat) 2> >(tee -a /tmp/invariant_error.log >&2) || rc=$?
    wait

    if [[ ${rc} -eq 0 ]]; then
        local inv_dev inv_mnt="/mnt/invariant_data"
        inv_dev="$(blkid -L INVARIANT 2>/dev/null || true)"
        if [[ -n "${inv_dev}" && -b "${inv_dev}" ]]; then
            mkdir -p "${inv_mnt}"

            local _actually_rw=0
            if mountpoint -q "${inv_mnt}"; then
                if touch "${inv_mnt}/.rw_probe" 2>/dev/null; then
                    rm -f "${inv_mnt}/.rw_probe"
                    _actually_rw=1
                else
                    umount "${inv_mnt}" 2>/dev/null || true
                    fsck.exfat -y "${inv_dev}" 2>/dev/null || true
                    if mount -t exfat "${inv_dev}" "${inv_mnt}" 2>/dev/null; then
                        touch "${inv_mnt}/.rw_probe" 2>/dev/null && rm -f "${inv_mnt}/.rw_probe" && _actually_rw=1
                    fi
                fi
            else
                if mount -t exfat "${inv_dev}" "${inv_mnt}" 2>/dev/null; then
                    touch "${inv_mnt}/.rw_probe" 2>/dev/null && rm -f "${inv_mnt}/.rw_probe" && _actually_rw=1
                fi
            fi

            if [[ ${_actually_rw} -eq 1 ]]; then
                local ts
                ts="$(date +%Y%m%d_%H%M%S)"
                if cp "${outdir}/reporte_generado.pdf" \
                      "${inv_mnt}/reporte_${ts}.pdf" 2>/dev/null; then
                    sync
                    echo -e "  ${PRI}[ * ] Auto-saved to INVARIANT partition${RST}"
                else
                    echo -e "  ${NTC}[ * ] Auto-save copy failed (filesystem full?)${RST}"
                fi
            else
                echo -e "  ${RED}[ * ] INVARIANT partition is read-only (dirty bit).${RST}"
                echo -e "  ${NTC}      PDF remains available at: ${outdir}/reporte_generado.pdf${RST}"
            fi

            sync
            umount "${inv_mnt}" 2>/dev/null || true
        fi
        echo ""
        _draw_hline "${BRD}"
        echo ""
        echo -e "  ${PRI}[ * ]  D I A G N Ó S T I C O   C O M P L E T A D O${RST}"
        echo -e "  ${NTC}Reporte disponible en: ${PRI}${outdir}/reporte_generado.pdf${RST}"
        echo ""
        _draw_hline "${BRD}"
    else
        echo ""
        echo -e "  ${RED}[ * ] El diagnóstico terminó con errores.${RST}"
        if [[ -s /tmp/invariant_error.log ]]; then
            echo -e "  ${RED}─── Últimas líneas del log de error ───${RST}"
            tail -n 20 /tmp/invariant_error.log
            echo -e "  ${RED}────────────────────────────────────────${RST}"
        fi
    fi

    echo ""
    echo -ne "${PRI}[INVARIANT_TTY]> ${RED}"
    read -rp "Presione ENTER para volver al menú..." _ || true
    echo -e "${RST}"

    return "${rc}"
}

_extract_to_usb() {
    clear || true
    echo ""
    _draw_hline "${BRD}"
    echo ""
    echo -e "  ${PRI}I N V A R I A N T${RST}  ${SLT}//${RST}  ${PRI}M Ó D U L O   D E   E X F I L T R A C I Ó N${RST}"
    echo ""
    _draw_hline "${BRD}"
    echo ""

    local pdf_src="/tmp/reporte_generado.pdf"

    if [[ ! -f "${pdf_src}" ]]; then
        echo -e "  ${RED}[ * ] No se encontró ${pdf_src}${RST}"
        echo -e "       ${NTC}Ejecute primero la Directiva [ * ] para generar el reporte.${RST}"
        echo ""
        echo -ne "${PRI}[INVARIANT_TTY]> ${RED}"
        read -rp "Presione ENTER para volver..." _ || true
        echo -e "${RST}"
        return
    fi

    echo -e "  ${NTC}[ * ] Inserte el pendrive USB (FAT32 o exFAT) y presione ENTER.${RST}"
    echo -ne "${PRI}[INVARIANT_TTY]> ${RED}"
    read -rp "[ENTER para escanear dispositivos] " _ || true
    echo -e "${RST}"

    echo ""
    echo -e "  ${PRI}Dispositivos de bloque detectados:${RST}"
    echo ""
    lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT | grep -v "loop" | sed 's/^/    /'
    echo ""

    echo -ne "${PRI}[INVARIANT_TTY]> ${RED}"
    read -rp "Ingrese el nodo del USB (ej: sdb1): " usb_node || true
    echo -e "${RST}"
    local usb_dev="/dev/${usb_node}"

    if [[ ! -b "${usb_dev}" ]]; then
        echo -e "  ${RED}[ * ] Dispositivo '${usb_dev}' no encontrado.${RST}"
        echo -ne "${PRI}[INVARIANT_TTY]> ${RED}"
        read -rp "Presione ENTER para volver..." _ || true
        echo -e "${RST}"
        return
    fi

    local mnt="/mnt/usb_export"
    mkdir -p "${mnt}"

    echo -e "  ${NTC}[ * ] Montando ${usb_dev} en ${mnt}...${RST}"
    if mount "${usb_dev}" "${mnt}" 2>/dev/null; then
        local dest="${mnt}/reporte_generado_$(date +%Y%m%d_%H%M%S).pdf"
        if cp "${pdf_src}" "${dest}" 2>/dev/null; then
            sync
            umount "${mnt}" 2>/dev/null || true
            echo -e "  ${PRI}[ * ] Reporte copiado exitosamente.${RST}"
            echo -e "       ${NTC}Archivo: $(basename "${dest}")${RST}"
        else
            echo -e "  ${RED}[ * ] Error al copiar el reporte.${RST}"
            umount "${mnt}" 2>/dev/null || true
        fi
    else
        echo -e "  ${RED}[ * ] Error al montar ${usb_dev}. ¿Formato compatible (FAT32/exFAT)?${RST}"
    fi

    echo ""
    echo -ne "${PRI}[INVARIANT_TTY]> ${RED}"
    read -rp "Presione ENTER para volver al menú..." _ || true
    echo -e "${RST}"
}

# ── Entry Point ──────────────────────────────────────────────────────────────

main() {
    if [[ "${1:-}" == "--run-diagnostic" ]]; then
        _run_diagnostic
        exit $?
    fi

    while true; do
        _menu
        local option
        read -r option || break
        echo -e "${RST}"
        case "${option}" in
            1) _run_diagnostic  ;;
            2) _extract_to_usb  ;;
            3)
               clear || true
               echo ""
               _draw_hline "${RED}"
               echo ""
               echo -e "  ${RED}W A R N I N G${RST}  ${SLT}//${RST}  ${NTC}Root shell access is logged and recorded.${RST}"
               echo ""
               _draw_hline "${RED}"
               echo ""
               PS1="\[\e[38;2;255;68;68m\][ring-0] \W #\[\e[0m\] " \
                   script -q /tmp/shell_session_$(date +%s).log -c bash || true
               ;;
            4) _shutdown        ;;
        esac
    done
}

main "$@"
LAUNCHER
chmod +x "${ISO_ROOT}/airootfs/root/launcher.sh"

# =============================================================================
# SECTION 7.5 — splash.sh creation
# =============================================================================
cat > "${ISO_ROOT}/airootfs/root/splash.sh" << 'SPLASH'
#!/bin/bash
# set -euo pipefail intentionally omitted: early-boot TTY writes can fail
# transiently; -e would exit without restoring the cursor.

echo -en "\e[?25l"
trap 'echo -en "\e[?25h\e[0m"; exit 0' TERM INT

echo -en "\e[48;2;20;20;20m\e[2J\e[H"

echo -en "\e[38;2;229;229;229m"
echo "                                                  "
echo "                                                  "
echo "                 I N V A R I A N T                "
echo "                                                  "
echo "            FORENSIC DIAGNOSTIC PLATFORM          "
echo "                                                  "
echo "                                                  "
echo ""
echo -en "\e[38;2;115;115;115m"
echo "                  Initializing environment..."
echo ""

frames=$'-\\|/'
i=0
count=0
echo -en "\e[38;2;255;68;68m"
while [[ ! -f /tmp/stop_splash ]] && [[ $count -lt 600 ]]; do
    frame="${frames:i:1}"
    printf "\r  %s  Loading probe.tex   " "$frame"
    sleep 0.1
    i=$(( (i + 1) % 4 ))
    count=$(( count + 1 ))
done

echo -en "\r\e[2K\e[0m\e[2J\e[H\e[?25h"
SPLASH
chmod +x "${ISO_ROOT}/airootfs/root/splash.sh"

# ════════════════════════════════════════════════════════════
# SECTION 8 — invariant-probe.service
# ════════════════════════════════════════════════════════════
cat > "${ISO_ROOT}/airootfs/etc/systemd/system/invariant-probe.service" << 'SERVICE'
[Unit]
Description=INVARIANT Ring-0 Boot Menu
Documentation=https://invariant.ar/
After=multi-user.target
ConditionPathExists=/root/launcher.sh
# Prevent infinite restart loops on persistent faults.
StartLimitIntervalSec=30s
StartLimitBurst=3

[Service]
Type=idle
ExecStart=/root/launcher.sh
StandardOutput=tty
StandardInput=tty
StandardError=tty
TTYPath=/dev/tty1

# --- FIX RING-0: Entorno para Python y Unicode ---
Environment=LANG=en_US.UTF-8
Environment=LC_ALL=en_US.UTF-8
Environment=TERM=linux
Environment=PYTHONUNBUFFERED=1
# Force Python stdio to UTF-8 regardless of locale misconfiguration.
Environment=PYTHONIOENCODING=utf-8

# --- FIX RING-0: Preserve TTY state on failure ---
TTYReset=no
TTYVHangup=no
TTYVTDisallocate=no
KillMode=process
# Halt on failure so the technician can read the traceback on-screen.
Restart=no

[Install]
WantedBy=multi-user.target
SERVICE

# Default getty@tty1.service provides the standard Linux virtual terminal.
# We use a drop-in to auto-login root, then /root/.bash_profile launches
# launcher.sh.  invariant-probe.service is intentionally NOT enabled here
# to avoid a TTY ownership conflict with getty.

# ════════════════════════════════════════════════════════════
# SECTION 8b — getty auto-login drop-in (FIX-GETTY-AUTOLOGIN)
#
# Restores the pre-kmscon behavior: root is automatically logged in on
# tty1 so the technician never sees a login: prompt.
# ════════════════════════════════════════════════════════════
mkdir -p "${ISO_ROOT}/airootfs/etc/systemd/system/getty@tty1.service.d"

cat > "${ISO_ROOT}/airootfs/etc/systemd/system/getty@tty1.service.d/autologin.conf" << 'AUTOLOGIN'
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -f -- \\u' --noclear --autologin root %I $TERM
AUTOLOGIN

# ════════════════════════════════════════════════════════════
# SECTION 8c — Root login shell hook
#
# When getty auto-logs root in on tty1, this profile execs launcher.sh
# directly, preserving the hardened environment variables required by
# the Python Unicode stack.
# ════════════════════════════════════════════════════════════
cat > "${ISO_ROOT}/airootfs/root/.bash_profile" << 'BASHPROFILE'
# INVARIANT probe.tex — Ring-0 Boot Hook
if [ "$(tty)" = "/dev/tty1" ]; then
    export LANG=en_US.UTF-8
    export LC_ALL=en_US.UTF-8
    export TERM=linux
    export PYTHONUNBUFFERED=1
    export PYTHONIOENCODING=utf-8
    exec python /root/probe.tex/menu.py
fi
BASHPROFILE
chmod 644 "${ISO_ROOT}/airootfs/root/.bash_profile"

# =============================================================================
# SECTION 8d — invariant-splash.service + symlink
# =============================================================================
cat > "${ISO_ROOT}/airootfs/etc/systemd/system/invariant-splash.service" << 'SERVICE'
[Unit]
Description=INVARIANT Boot Splash
DefaultDependencies=no
After=local-fs.target
Before=sysinit.target
ConditionPathExists=/root/splash.sh

[Service]
Type=simple
ExecStart=/bin/bash /root/splash.sh
StandardOutput=tty
TTYPath=/dev/tty1
TTYReset=no
TTYVHangup=no
Restart=no
KillSignal=SIGTERM
TimeoutStopSec=1

[Install]
WantedBy=sysinit.target
SERVICE

mkdir -p "${ISO_ROOT}/airootfs/etc/systemd/system/sysinit.target.wants"
ln -sf /etc/systemd/system/invariant-splash.service \
    "${ISO_ROOT}/airootfs/etc/systemd/system/sysinit.target.wants/invariant-splash.service"

# ════════════════════════════════════════════════════════════
# SECTION 9 — Silent boot suppression (FIX-15)
#
# systemd-firstboot interactively prompts for locale/timezone/password
# on any system where these are not pre-committed. Three static layers
# prevent the prompt from ever reaching the live TTY; a fourth layer
# (kernel parameter) is injected after the bootloader entries are
# written in section 10.
# ════════════════════════════════════════════════════════════

# Layer 1: pre-committed timezone symlink.
rm -f "${ISO_ROOT}/airootfs/etc/localtime"
ln -sf "/usr/share/zoneinfo/America/Argentina/Buenos_Aires" \
    "${ISO_ROOT}/airootfs/etc/localtime"

# Layer 2: static machine-id marks the system as already initialised.
printf 'b4d0f00db4d0f00db4d0f00db4d0f00d\n' > "${ISO_ROOT}/airootfs/etc/machine-id"

# Layer 3: mask the unit so it cannot be started by any dependency.
ln -sf /dev/null \
    "${ISO_ROOT}/airootfs/etc/systemd/system/systemd-firstboot.service"

# ════════════════════════════════════════════════════════════
# SECTION 10 — Boot loaders (UEFI systemd-boot + BIOS Syslinux)
# ════════════════════════════════════════════════════════════
cat > "${ISO_ROOT}/efiboot/loader/loader.conf" << 'LOADERCONF'
timeout 3
default 01-probe-tex.conf
console-mode max
editor  no
LOADERCONF

cat > "${ISO_ROOT}/efiboot/loader/entries/01-probe-tex.conf" << 'EFIENTRY'
title   INVARIANT probe.tex // Forensic Diagnostic
linux   /arch/boot/x86_64/vmlinuz-linux
initrd  /arch/boot/x86_64/initramfs-linux.img
options archisobasedir=arch archisolabel=PROBE_TEX archisodelay=5 console=tty0 quiet loglevel=3 rd.systemd.show_status=auto rd.udev.log_level=3 vt.global_cursor_default=0
EFIENTRY

cat > "${ISO_ROOT}/syslinux/syslinux.cfg" << 'SYSLINUX'
UI      menu.c32
PROMPT  0
TIMEOUT 30
MENU HIDDEN
MENU ROWS 0

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
  APPEND archisobasedir=arch archisolabel=PROBE_TEX archisodelay=5 console=tty0 quiet loglevel=3 rd.systemd.show_status=auto rd.udev.log_level=3 vt.global_cursor_default=0
SYSLINUX

# FIX-15 layer 4: kernel-level firstboot suppression (idempotent).
readonly EFI_ENTRY="${ISO_ROOT}/efiboot/loader/entries/01-probe-tex.conf"
readonly SYSLINUX_CFG="${ISO_ROOT}/syslinux/syslinux.cfg"

grep -q 'systemd.firstboot=0' "${EFI_ENTRY}" || \
    sed -i 's/^\(options .*\)$/\1 systemd.firstboot=0/' "${EFI_ENTRY}"

grep -q 'systemd.firstboot=0' "${SYSLINUX_CFG}" || \
    sed -i '/APPEND.*quiet.*loglevel=3/ s/$/ systemd.firstboot=0/' "${SYSLINUX_CFG}"

# ════════════════════════════════════════════════════════════
# SECTION 10.5 — Silent Boot Enforcement
# ════════════════════════════════════════════════════════════
# Ensures zero-visibility boot: no kernel spam, no systemd status,
# no bash xtrace, and no blinking cursor until the TUI takes over.

readonly SILENT_PARAMS="quiet loglevel=3 rd.udev.log_level=3 vt.global_cursor_default=0 rd.systemd.show_status=false systemd.show_status=false"

# ── systemd-boot (UEFI) entries ──
while IFS= read -r -d '' entry; do
    for param in ${SILENT_PARAMS}; do
        if ! grep -q "^options .*${param}" "${entry}"; then
            sed -i "s|^\(options .*\)$|\1 ${param}|" "${entry}"
        fi
    done
    # Harden: replace any existing 'auto' with 'false' for show_status
    sed -i 's/rd.systemd.show_status=auto/rd.systemd.show_status=false/g' "${entry}"
    sed -i 's/systemd.show_status=auto/systemd.show_status=false/g' "${entry}"
done < <(find "${ISO_ROOT}/efiboot/loader/entries" -maxdepth 1 -type f -name '*.conf' -print0)

# ── syslinux (BIOS) configs ──
while IFS= read -r -d '' cfg; do
    for param in ${SILENT_PARAMS}; do
        if ! grep -q "^ *APPEND .*${param}" "${cfg}"; then
            sed -i "/^ *APPEND /s|$| ${param}|" "${cfg}"
        fi
    done
    sed -i 's/rd.systemd.show_status=auto/rd.systemd.show_status=false/g' "${cfg}"
    sed -i 's/systemd.show_status=auto/systemd.show_status=false/g' "${cfg}"
done < <(find "${ISO_ROOT}/syslinux" -maxdepth 1 -type f -name '*.cfg' -print0)

# ════════════════════════════════════════════════════════════
# SECTION 11 — Repository sync
# ════════════════════════════════════════════════════════════
readonly RSYNC_EXCLUDES=(
    --exclude='.git'
    --exclude='.venv'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='probe-tex-iso'
    --exclude='reporte_generado.*'
    --exclude='*.log.txt'
    --exclude='forge.sh'
    --exclude='work'
    --exclude='work_dir'
    --exclude='out'
    --exclude='*.iso'
)

rsync -a --delete "${RSYNC_EXCLUDES[@]}" \
    "${REPO_ROOT}/" \
    "${ISO_ROOT}/airootfs/root/probe.tex/" \
    || die "rsync failed for probe.tex repository"

# =============================================================================
# SECTION 11b — menu.py preamble injection (must run after rsync)
# =============================================================================
MENU_PY="${ISO_ROOT}/airootfs/root/probe.tex/menu.py"
[[ -f "${MENU_PY}" ]] || die "menu.py not found at ${MENU_PY}"

if ! grep -q '# --- SPLASH HANDOFF ---' "${MENU_PY}"; then
    python3 - "${MENU_PY}" << 'PYINJECT'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines(keepends=True)

handoff = (
    "\n"
    "# --- SPLASH HANDOFF ---\n"
    "import pathlib as _pl, time as _time\n"
    "_pl.Path('/tmp/stop_splash').touch()\n"
    "_time.sleep(0.2)\n"
    "import sys as _sys\n"
    "_sys.stdout.write('\\033[?25h\\033[0m\\033[2J\\033[H')\n"
    "_sys.stdout.flush()\n"
    "# --- END SPLASH HANDOFF ---\n"
    "\n"
)

insert_at = len(lines)
for idx, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('#') or stripped == '':
        continue
    if stripped.startswith('import ') or stripped.startswith('from '):
        continue
    insert_at = idx
    break

lines.insert(insert_at, handoff)
path.write_text(''.join(lines))
print(f"Splash handoff injected at line {insert_at} of {path}")
PYINJECT
fi

# _ISO_BUILD_TIMESTAMP is intentionally non-deterministic: it binds
# the license validity window to this exact build epoch (Time Trap).
readonly BUILD_DATE="$(date -u +%s)"
readonly VERIFIER_FILE="${ISO_ROOT}/airootfs/root/probe.tex/core/license_verifier.py"

[[ -f "${VERIFIER_FILE}" ]] || die "license_verifier.py not found: ${VERIFIER_FILE}"

sed -i \
    "s/^_ISO_BUILD_TIMESTAMP:.*=.*$/_ISO_BUILD_TIMESTAMP: Final[int] = ${BUILD_DATE}  # FORGE_PATCH_BUILD_TIMESTAMP/" \
    "${VERIFIER_FILE}" \
    || die "Failed to inject _ISO_BUILD_TIMESTAMP into ${VERIFIER_FILE}"

# J: Ed25519 public key injection into license verifier.
# The sentinel "0" * 64 in license_verifier.py must be replaced with the
# production public key so that bootstrap.sig tokens verify correctly.
readonly PRODUCTION_PUBKEY="98ee0e2e03126d80cbd5e846acc0a6afa83d19ad3d6c84424a0a093bf46adeed"
sed -i \
    "s/^_EMBEDDED_PUBKEY_HEX: str = .*$/_EMBEDDED_PUBKEY_HEX: str = \"${PRODUCTION_PUBKEY}\"  # FORGE_PATCH_PUBKEY/" \
    "${VERIFIER_FILE}" \
    || die "Failed to inject _EMBEDDED_PUBKEY_HEX into ${VERIFIER_FILE}"

# ════════════════════════════════════════════════════════════
# SECTION 12 — Tectonic cache injection
#
# Pre-seeding the cache enables offline LaTeX compilation in the
# live OS. If absent, the ISO requires network access for tectonic
# to pull packages on first run.
# ════════════════════════════════════════════════════════════
readonly TECTONIC_CACHE_CANDIDATES=(
    "/root/.cache/Tectonic"
    "${HOME}/.cache/Tectonic"
    "/var/cache/tectonic"
)

TECTONIC_SRC=""
for _candidate in "${TECTONIC_CACHE_CANDIDATES[@]}"; do
    if [[ -d "${_candidate}" ]]; then
        TECTONIC_SRC="${_candidate}"
        break
    fi
done

if [[ -n "${TECTONIC_SRC}" ]]; then
    rsync -a "${TECTONIC_SRC}/" \
        "${ISO_ROOT}/airootfs/root/.cache/Tectonic/" \
        || die "rsync failed for Tectonic cache from ${TECTONIC_SRC}"
else
    die "Tectonic cache not found in any candidate path. The ISO will be non-functional in air-gapped mode. Generate cache first: cd /root/probe.tex && sudo python main.py --outdir /tmp"
fi

# ════════════════════════════════════════════════════════════
# SECTION 13 — Post-condition integrity verification
#
# Each guard below is a post-condition assertion for a prior section.
# Failure means the corresponding section did not complete correctly.
# ════════════════════════════════════════════════════════════
readonly MKINIT_CHECK="${ISO_ROOT}/airootfs/etc/mkinitcpio.conf"

# A: ISO label must be identical across profiledef, EFI entry, syslinux.
LABEL_PROFILEDEF="$(grep 'iso_label=' "${ISO_ROOT}/profiledef.sh" \
    | head -1 | sed 's/.*iso_label="\([^"]*\)".*/\1/')"
LABEL_EFI="$(grep 'archisolabel=' "${EFI_ENTRY}" \
    | sed 's/.*archisolabel=\([^ ]*\).*/\1/')"
LABEL_SYSLINUX="$(grep 'archisolabel=' "${SYSLINUX_CFG}" \
    | grep -v 'debug' | tail -1 | sed 's/.*archisolabel=\([^ ]*\).*/\1/')"

[[ "${LABEL_PROFILEDEF}" == "${LABEL_EFI}" ]] || die \
    "ISO label mismatch: profiledef='${LABEL_PROFILEDEF}' EFI='${LABEL_EFI}'"
[[ "${LABEL_PROFILEDEF}" == "${LABEL_SYSLINUX}" ]] || die \
    "ISO label mismatch: profiledef='${LABEL_PROFILEDEF}' syslinux='${LABEL_SYSLINUX}'"

# B+G: Full module stack — base archiso sequence + NLS/FAT for iso9660.
for _mod in loop squashfs overlay cdrom iso9660 nls_cp437 nls_iso8859_1 vfat fat; do
    grep -q "${_mod}" "${MKINIT_CHECK}" || \
        die "Module '${_mod}' missing from mkinitcpio.conf"
done

# C: modconf required for AMD Ryzen firmware application at init time.
grep -q 'modconf' "${MKINIT_CHECK}" || die "'modconf' hook missing from mkinitcpio.conf"

# D: Config must exist in both profile root and airootfs/etc/.
[[ -f "${ISO_ROOT}/mkinitcpio.conf" ]] || \
    die "mkinitcpio.conf missing from profile root"
[[ -f "${ISO_ROOT}/airootfs/etc/mkinitcpio.conf" ]] || \
    die "mkinitcpio.conf missing from airootfs/etc/"

# E: archisodelay must be identical in UEFI and BIOS entries.
DELAY_EFI="$(grep 'archisodelay=' "${EFI_ENTRY}" \
    | sed 's/.*archisodelay=\([0-9]*\).*/\1/')"
DELAY_SYSLINUX="$(grep 'archisodelay=' "${SYSLINUX_CFG}" \
    | grep -v 'debug' | grep -v '#' | tail -1 \
    | sed 's/.*archisodelay=\([0-9]*\).*/\1/')"
[[ "${DELAY_EFI}" == "${DELAY_SYSLINUX}" ]] || die \
    "archisodelay mismatch: EFI=${DELAY_EFI} syslinux=${DELAY_SYSLINUX}"

# F: archiso_udev_settle runtime and install hooks present; declared in HOOKS.
[[ -f "${ISO_ROOT}/airootfs/etc/initcpio/hooks/archiso_udev_settle" ]] || \
    die "archiso_udev_settle runtime hook missing"
[[ -f "${ISO_ROOT}/airootfs/etc/initcpio/install/archiso_udev_settle" ]] || \
    die "archiso_udev_settle install hook missing"
grep -q 'archiso_udev_settle' "${MKINIT_CHECK}" || \
    die "'archiso_udev_settle' not declared in HOOKS"

# H: Intercepted archiso hook present with FIX-14 patch signature.
#    Three sub-conditions: file exists, non-trivial content, patch present.
readonly INTERCEPTED_HOOK="${ISO_ROOT}/airootfs/etc/initcpio/hooks/archiso"
[[ -f "${INTERCEPTED_HOOK}" ]] || \
    die "Intercepted archiso hook missing: ${INTERCEPTED_HOOK}"
_line_count="$(wc -l < "${INTERCEPTED_HOOK}")"
[[ "${_line_count}" -gt 10 ]] || \
    die "Intercepted archiso hook is unexpectedly short (${_line_count} lines)"
grep -q "${PATCH_SIGNATURE}" "${INTERCEPTED_HOOK}" || \
    die "FIX-14 patch signature not found in ${INTERCEPTED_HOOK}"

# I: Silent boot — all three suppression layers applied.
[[ -L "${ISO_ROOT}/airootfs/etc/localtime" ]] || \
    die "/etc/localtime is not a symlink — FIX-15 layer 1 incomplete"

_machine_id="$(tr -d '\n' < "${ISO_ROOT}/airootfs/etc/machine-id")"
[[ "${_machine_id}" =~ ^[0-9a-f]{32}$ ]] || \
    die "Invalid machine-id '${_machine_id}' — must be exactly 32 lowercase hex chars"

readonly _FIRSTBOOT_MASK="${ISO_ROOT}/airootfs/etc/systemd/system/systemd-firstboot.service"
[[ -L "${_FIRSTBOOT_MASK}" ]] || \
    die "systemd-firstboot.service not masked — FIX-15 layer 3 incomplete"
[[ "$(readlink "${_FIRSTBOOT_MASK}")" == "/dev/null" ]] || \
    die "systemd-firstboot.service symlink does not point to /dev/null"

# J: Ed25519 public key post-condition.
# Verify that the production key was injected and the sentinel is gone.
_INJECTED_PUBKEY="$(grep '^_EMBEDDED_PUBKEY_HEX: str = ' "${VERIFIER_FILE}" \
    | sed 's/.*= "\([^"]*\)".*/\1/')"
[[ "${_INJECTED_PUBKEY}" == "${PRODUCTION_PUBKEY}" ]] || die \
    "_EMBEDDED_PUBKEY_HEX mismatch: injected='${_INJECTED_PUBKEY}' expected='${PRODUCTION_PUBKEY}'"
[[ "${_INJECTED_PUBKEY}" != "0000000000000000000000000000000000000000000000000000000000000000" ]] || die \
    "_EMBEDDED_PUBKEY_HEX is still the sentinel value — forge.sh injection failed"

# K: splash.sh present, executable, contains stop-file guard.
readonly SPLASH_SCRIPT="${ISO_ROOT}/airootfs/root/splash.sh"
[[ -f "${SPLASH_SCRIPT}" ]] || \
    die "splash.sh missing: ${SPLASH_SCRIPT}"
[[ -x "${SPLASH_SCRIPT}" ]] || \
    die "splash.sh is not executable"
grep -q 'stop_splash' "${SPLASH_SCRIPT}" || \
    die "splash.sh does not contain stop_splash guard"

# L: splash.sh declared in profiledef.sh file_permissions.
grep -q '"/root/splash.sh"' "${PROFILEDEF_FILE}" || \
    die "splash.sh not in profiledef.sh file_permissions — squashfs may drop execute bit"

# M: splash handoff injected into menu.py.
grep -q '# --- SPLASH HANDOFF ---' "${MENU_PY}" || \
    die "Splash handoff sentinel not found in menu.py"

# N: invariant-splash.service symlink present in sysinit.target.wants.
[[ -L "${ISO_ROOT}/airootfs/etc/systemd/system/sysinit.target.wants/invariant-splash.service" ]] || \
    die "invariant-splash.service symlink missing — sysinit.target.wants injection failed"

printf 'ISO successfully generated\n'