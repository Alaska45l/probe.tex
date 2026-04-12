# Contributing to probe.tex

Thank you for your interest in probe.tex. 

As this project is a proprietary, commercially-oriented software product, our contribution model differs from permissive open-source projects. 

## Code Contributions

We do **not** accept unsolicited Pull Requests containing new features, architectural changes, or modifications to the LaTeX reporting templates. 

Pull Requests will only be considered if they strictly address:
1. **Critical Bug Fixes:** Corrections to unhandled exceptions or crashes in the hardware extraction layers (e.g., handling undocumented `sysfs` edge cases).
2. **Graceful Degradation Enhancements:** Improvements to the `try/except` blocks ensuring the software fails safely on unsupported hardware.

## Issue Reporting

If you encounter a bug during your evaluation, please open an issue using the following format:

1. **Environment:** Specify your Linux distribution, kernel version (`uname -r`), and the specific hardware component causing the issue.
2. **Reproduction:** Provide the exact command run and the terminal output.
3. **Logs:** Attach the generated `.log` files (ensure no sensitive personal data is included).

## Code of Conduct

By interacting with this repository, you agree to maintain a professional, respectful, and constructive tone. Harassment or abusive behavior will result in an immediate ban.