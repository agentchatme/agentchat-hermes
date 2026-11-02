# Versioning

Semantic Versioning, with the first slot frozen at `0` until the founder declares the product stable.

- **Rebuilt or added behaviour** → minor +1, patch resets to 0 (`0.2.x` → `0.3.0`).
- **Fixed or adjusted existing behaviour** → patch +1 (`0.3.0` → `0.3.1`).
- **Always +1 numerically.** Never append digits, never skip, never touch the first slot.
- **Pre-release for testers, only if needed:** same number with an `rc` suffix (`0.3.0rc1` on PyPI, `0.3.0-rc.1` under the `next` tag on npm). The stable number moves only at release.
- **The founder sets the number for every release.** Open a PR with the code; the number is applied at the release step.

History: before 2026-11-02 numbers were bumped by appending a digit (`0.2.3` → `0.2.31` → `0.2.311`), which inflated the patch slot into the millions. Version comparison is per slot, numerically, so `0.7.9` is OLDER than `0.7.8211111`. Each package's next release is therefore one minor bump to a clean number, and the rule above applies from there.
