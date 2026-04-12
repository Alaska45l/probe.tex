<div align="center">
  <h1>probe.tex</h1>
  <p><strong>Enterprise-Grade Hardware Forensics, Active Benchmarking & Cryptographic Certification by INVARIANT</strong></p>

  <img src="https://img.shields.io/badge/Python-3.14+-000000?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Arch_Linux-Live_OS-000000?style=for-the-badge&logo=arch-linux&logoColor=1793D1" alt="Arch Linux">
  <img src="https://img.shields.io/badge/Jinja2_LaTeX-Reporting-000000?style=for-the-badge&logo=latex&logoColor=white" alt="LaTeX">
  <img src="https://img.shields.io/badge/License-Proprietary-FF5000?style=for-the-badge" alt="Proprietary License">
</div>

---

probe.tex is an offline, bare-metal forensic diagnostic platform designed for technical support centers and auditors. It bypasses high-level operating system abstractions and interfaces directly with hardware components through kernel-level access and active benchmarking, generating cryptographically verifiable PDF certificates.

---

## Core Features

| Module | Capability | Implementation Details |
| :--- | :--- | :--- |
| **Active Forensics** | Real-time hardware stress testing | Executes `stress-ng` (CPU), `fio` (NVMe I/O), and `memtester` (RAM) to extract factual performance data rather than relying on static SMART logs. |
| **Graceful Degradation** | Fault-tolerant extraction | If proprietary hardware blocks sensor access, the system safely falls back to neutral reporting without failure. |
| **Forensic Honesty** | Zero data fabrication | Components that do not expose telemetry (e.g., locked VRMs) are explicitly documented as unsupported rather than simulated. |
| **LaTeX Certification** | Immutable reporting | Compiles diagnostic data into professional PDF reports using Jinja2 and `tectonic` / `pdflatex`, secured with SHA-256 integrity hashes. |
| **Concurrent TUI** | Asynchronous execution | Terminal UI built with `rich`, ensuring responsiveness during hardware-bound operations. |

---

## System Architecture

The project enforces a strict 3-tier Separation of Concerns (SoC):

- **`core/`**  
  Contains strictly typed data contracts (`dataclasses`) and the scoring engine.

- **`extractors/`**  
  Independent modules responsible for low-level system interaction (`sysfs`, `smartctl`, `dmidecode`, `lspci`, `lsusb`).

- **`renderer/`**  
  Presentation layer that injects structured data into LaTeX templates via Jinja2.

---

## Prerequisites & Installation

probe.tex is designed for modern Linux kernels, optimized for Arch Linux / CachyOS live environments.

### System Dependencies

```bash
# Core forensic tools and benchmarks
sudo pacman -S smartmontools dmidecode lm_sensors stress-ng fio memtester

# LaTeX engine
sudo pacman -S tectonic
```

### Python Setup

```bash
git clone https://github.com/alaska-elaina/auditmaster-lite.git
cd auditmaster-lite
pip install -r requirements.txt
```

---

## Usage

> **Note:** Root privileges are required to access low-level hardware interfaces and perform direct I/O operations.

Launch the diagnostic suite:

```bash
sudo python tui.py
```

After completion, the generated certificate will be available as:

```
reporte_generado.pdf
```

---

## Roadmap

- [x] Passive data extraction via `sysfs` and `dmidecode`
- [x] Active forensics (`stress-ng`, `fio`, `memtester`)
- [ ] Implement `core/scoring.py` for automated grading and recommendations
- [ ] Introduce concurrency via `ThreadPoolExecutor`
- [ ] Build immutable Arch Linux Live ISO (`archiso`) with auto-login

---

## License & Contributing

**Proprietary Software**

probe.tex is a commercial product. The source code is provided strictly for evaluation purposes.

- **No Unauthorized Commercial Use**  
  You may not use this software to provide paid services without a valid commercial license.

- **No Redistribution**  
  Redistribution, sublicensing, or modification is strictly prohibited.

- **Contributions**  
  Unsolicited feature pull requests are not accepted.  
  Bug reports related to kernel panics, undocumented `sysfs` edge cases, or degradation failures are welcome via the Issue Tracker.

---

Copyright © 2026 Alaska Elaina Gonzalez. All rights reserved.