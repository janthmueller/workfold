# Third-party notices for standalone Workfold bundles

The standalone archives are produced with PyInstaller and include portions of
the Python runtime plus Workfold's runtime dependencies. Their exact license
files are copied into the archive's `licenses/` directory at build time.

- Python and its standard library: Python Software Foundation License.
- pathspec: Mozilla Public License 2.0.
- Rich: MIT License.
- markdown-it-py and mdurl: MIT License.
- Pygments: BSD 2-Clause License.
- tzlocal: MIT License.
- tzdata, when bundled on Windows: licenses shipped by the Python tzdata
  distribution and the underlying IANA time-zone database.
- PyInstaller bootloader: GNU General Public License with PyInstaller's
  bootloader exception; its shipped notices are included.

This notice is informational. The complete license texts included beside the
binary control the corresponding components.
