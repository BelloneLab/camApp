## Highlights

- Expanded camera support across Basler and FLIR machine-vision workflows, including richer FLIR Spinnaker integration and better color / full-resolution handling.
- Added camera-native pixel format and bit-depth controls in Advanced Settings so supported Basler and FLIR cameras expose more of their real acquisition modes.
- Improved acquisition stability by separating capture from downstream processing, adding configurable preview cadence, larger frame buffering, and lighter per-frame metadata work.
- Refined the recording workspace with better max-length handling, editable filename overrides with persistence, and keyboard-triggered recording from the main view.
- Tightened synchronization tooling with barcode timing fixes, mirrored barcode output support, camera line defaults, and more flexible TTL / behavior setup controls.

## Included Assets

- Windows x64 CamApp build
- SHA-256 checksum file
- PyInstaller warning log captured during the release build

## Runtime Notes

- FFmpeg must still be available on `PATH` on the target machine.
- Basler support still depends on the Pylon runtime.
- FLIR Spinnaker support still depends on the vendor SDK and matching `PySpin` installation on the target machine.
