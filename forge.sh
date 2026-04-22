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

# Idempotent: skip if patch signature already present in destination.
if ! { [[ -f "${HOOK_DEST}" ]] && grep -q "${PATCH_SIGNATURE}" "${HOOK_DEST}"; }; then
    cp "${HOOK_SOURCE}" "${HOOK_DEST}"

    sed -i \
        "/${HOOK_FUNC_PATTERN}/a\\    set -x  # ${PATCH_SIGNATURE}" \
        "${HOOK_DEST}"

    # sed does not error on no-match; verify the insertion explicitly.
    if ! grep -q "${PATCH_SIGNATURE}" "${HOOK_DEST}"; then
        rm -f "${HOOK_DEST}"
        die "FIX-14 patch not applied — sed pattern '${HOOK_FUNC_PATTERN}' not matched in ${HOOK_SOURCE}"
    fi
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
printf 'root::0:0:99999:7:::\n' > "${ISO_ROOT}/airootfs/etc/shadow"

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
iso_publisher="INVARIANT SYSTEMS <https://invariant.systems>"
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
kmscon
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

    # --- FIX: Synchronous mount of INVARIANT data partition ---
    # The USB block devices are fully settled by the time the user
    # interacts with the menu. Mount synchronously here to guarantee
    # bootstrap.sig / license.sig are visible to the Python verifier.
    mkdir -p /mnt/invariant_data
    if ! mountpoint -q /mnt/invariant_data; then
        echo -e "\e[1;33m[*]\e[0m Reparando y montando partición de datos (INVARIANT)..."
        # Aggressive auto-repair (-y) maximizes probability of clearing the
        # exFAT volume dirty bit before the first mount.
        fsck.exfat -y /dev/disk/by-label/INVARIANT 2>/dev/null || true
        if ! mount -t exfat -L INVARIANT /mnt/invariant_data 2> /tmp/mount_err.log; then
            echo -e "\e[1;31m[!]\e[0m ERROR CRÍTICO: No se pudo montar la partición INVARIANT."
            cat /tmp/mount_err.log
            return 1
        fi
    fi
    # Real-world write test: verify the kernel actually granted RW.
    if ! touch /mnt/invariant_data/.rw_probe 2>/dev/null; then
        echo -e "\e[1;31m[!]\e[0m ERROR CRÍTICO: Partición INVARIANT montada en modo SOLO LECTURA."
        return 1
    fi
    rm -f /mnt/invariant_data/.rw_probe

    # Ejecuta Python con captura determinista de stderr.
    # Usa procesos sustituidos + wait para garantizar flush completo
    # antes de que el shell continúe (evita race condition en tee).
    python main.py --outdir "${outdir}" > >(cat) 2> >(tee -a /tmp/invariant_error.log >&2)
    local rc=$?
    wait  # Espera a que los subshells de tee terminen de flushear

    if [[ ${rc} -eq 0 ]]; then
        # Auto-save: verify RW state, copy report, clean unmount.
        local inv_dev inv_mnt="/mnt/invariant_data"
        inv_dev="$(blkid -L INVARIANT 2>/dev/null || true)"
        if [[ -n "${inv_dev}" && -b "${inv_dev}" ]]; then
            mkdir -p "${inv_mnt}"

            # Phase 1: Ensure the filesystem is actually writable.
            # mount(8) may return 0 even when the kernel silently forces RO
            # on a dirty exFAT volume. We trust a real write probe, not mount.
            local _actually_rw=0
            if mountpoint -q "${inv_mnt}"; then
                if touch "${inv_mnt}/.rw_probe" 2>/dev/null; then
                    rm -f "${inv_mnt}/.rw_probe"
                    _actually_rw=1
                else
                    # Mounted but RO — dirty-bit trap. Unmount, repair, remount.
                    umount "${inv_mnt}" 2>/dev/null || true
                    fsck.exfat -y "${inv_dev}" 2>/dev/null || true
                    if mount -t exfat "${inv_dev}" "${inv_mnt}" 2>/dev/null; then
                        touch "${inv_mnt}/.rw_probe" 2>/dev/null && rm -f "${inv_mnt}/.rw_probe" && _actually_rw=1
                    fi
                fi
            else
                # Not mounted at all — mount fresh.
                if mount -t exfat "${inv_dev}" "${inv_mnt}" 2>/dev/null; then
                    touch "${inv_mnt}/.rw_probe" 2>/dev/null && rm -f "${inv_mnt}/.rw_probe" && _actually_rw=1
                fi
            fi

            # Phase 2: Copy PDF only if we confirmed real writability.
            if [[ ${_actually_rw} -eq 1 ]]; then
                local ts
                ts="$(date +%Y%m%d_%H%M%S)"
                if cp "${outdir}/reporte_generado.pdf" \
                      "${inv_mnt}/reporte_${ts}.pdf" 2>/dev/null; then
                    sync
                    echo -e "${GRN}     [+] Auto-saved to INVARIANT partition${RST}"
                else
                    echo -e "${YLW}     [!] Auto-save copy failed (filesystem full?)${RST}"
                fi
            else
                echo -e "${YLW}     [!] INVARIANT partition is read-only (dirty bit).${RST}"
                echo -e "${YLW}         PDF remains available at: ${outdir}/reporte_generado.pdf${RST}"
            fi

            # Phase 3: Clean unmount to clear the exFAT dirty bit.
            sync
            umount "${inv_mnt}" 2>/dev/null || true
        fi
        echo ""
        echo -e "${GRN}════════════════════════════════════════════════════════════${RST}"
        echo -e "${GRN} [+] DIAGNÓSTICO COMPLETADO${RST}"
        echo -e "${GRN}     Reporte disponible en: ${outdir}/reporte_generado.pdf${RST}"
        echo -e "${GRN}════════════════════════════════════════════════════════════${RST}"
    else
        echo ""
        echo -e "${RED}[✗] El diagnóstico terminó con errores.${RST}"
        if [[ -s /tmp/invariant_error.log ]]; then
            echo -e "${RED}--- Últimas líneas del log de error ---${RST}"
            tail -n 20 /tmp/invariant_error.log
            echo -e "${RED}---------------------------------------${RST}"
        fi
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
           echo -e "${YLW}════════════════════════════════════════════════════════════${RST}"
           echo -e "${RED}  [!] WARNING: Root shell access is logged and recorded.${RST}"
           echo -e "${YLW}════════════════════════════════════════════════════════════${RST}"
           echo ""
           PS1="\[\033[1;31m\][ring-0] \W #\[\033[0m\] " \
               script -q /tmp/shell_session_$(date +%s).log -c bash
           ;;
        4) _shutdown        ;;
    esac
done
LAUNCHER
chmod +x "${ISO_ROOT}/airootfs/root/launcher.sh"

# ════════════════════════════════════════════════════════════
# SECTION 8 — invariant-probe.service
# ════════════════════════════════════════════════════════════
cat > "${ISO_ROOT}/airootfs/etc/systemd/system/invariant-probe.service" << 'SERVICE'
[Unit]
Description=INVARIANT Ring-0 Boot Menu
Documentation=https://invariant-web.alaska45l.workers.dev/
After=multi-user.target kmscon@tty1.service
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

# Static enablement — avoids systemctl inside the build chroot.
ln -sf \
    "/etc/systemd/system/invariant-probe.service" \
    "${ISO_ROOT}/airootfs/etc/systemd/system/multi-user.target.wants/invariant-probe.service"

# kmscon provides DRM/KMS hardware-accelerated terminal on tty1.
# invariant-probe.service runs launcher.sh on top of it.
ln -sf \
    "/usr/lib/systemd/system/kmscon@.service" \
    "${ISO_ROOT}/airootfs/etc/systemd/system/multi-user.target.wants/kmscon@tty1.service"

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

# _ISO_BUILD_TIMESTAMP is intentionally non-deterministic: it binds
# the license validity window to this exact build epoch (Time Trap).
readonly BUILD_DATE="$(date -u +%s)"
readonly VERIFIER_FILE="${ISO_ROOT}/airootfs/root/probe.tex/core/license_verifier.py"

[[ -f "${VERIFIER_FILE}" ]] || die "license_verifier.py not found: ${VERIFIER_FILE}"

sed -i \
    "s/^_ISO_BUILD_TIMESTAMP:.*=.*$/_ISO_BUILD_TIMESTAMP: Final[int] = ${BUILD_DATE}  # FORGE_PATCH_BUILD_TIMESTAMP/" \
    "${VERIFIER_FILE}" \
    || die "Failed to inject _ISO_BUILD_TIMESTAMP into ${VERIFIER_FILE}"

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

printf 'ISO successfully generated\n'