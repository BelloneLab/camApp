"""
Camera backend discovery helpers.

This module keeps optional camera stack imports isolated so the application can
start even when vendor SDKs are not installed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import cv2

try:
    from pypylon import pylon as _pylon
except Exception as exc:  # pragma: no cover - depends on local SDK install
    _pylon = None
    PYPYLON_IMPORT_ERROR = exc
else:
    PYPYLON_IMPORT_ERROR = None

pylon = _pylon
PYPYLON_AVAILABLE = pylon is not None

try:
    import PySpin as _PySpin
except Exception as exc:  # pragma: no cover - depends on local SDK install
    _PySpin = None
    PYSPIN_IMPORT_ERROR = exc
else:
    PYSPIN_IMPORT_ERROR = None

PySpin = _PySpin
PYSPIN_AVAILABLE = PySpin is not None
PYSPIN_IMPORT_DIAGNOSTIC = ""

try:
    from flirpy.camera.boson import Boson as _Boson
except Exception as exc:  # pragma: no cover - optional dependency
    _Boson = None
    FLIRPY_BOSON_IMPORT_ERROR = exc
else:
    FLIRPY_BOSON_IMPORT_ERROR = None

try:
    from flirpy.camera.lepton import Lepton as _Lepton
except Exception as exc:  # pragma: no cover - optional dependency
    _Lepton = None
    FLIRPY_LEPTON_IMPORT_ERROR = exc
else:
    FLIRPY_LEPTON_IMPORT_ERROR = None

try:
    from flirpy.camera.tau import TeaxGrabber as _TeaxGrabber
except Exception as exc:  # pragma: no cover - optional dependency
    _TeaxGrabber = None
    FLIRPY_TEAX_IMPORT_ERROR = exc
else:
    FLIRPY_TEAX_IMPORT_ERROR = None

try:
    import usb.core as _usb_core
except Exception as exc:  # pragma: no cover - optional dependency
    _usb_core = None
    USB_IMPORT_ERROR = exc
else:
    USB_IMPORT_ERROR = None

Boson = _Boson
Lepton = _Lepton
TeaxGrabber = _TeaxGrabber
usb_core = _usb_core


def discover_basler_cameras() -> List[Dict]:
    """Enumerate Basler cameras through Pylon when available."""
    if not PYPYLON_AVAILABLE:
        return []

    cameras: List[Dict] = []
    tl_factory = pylon.TlFactory.GetInstance()
    devices = tl_factory.EnumerateDevices()
    for index, dev in enumerate(devices):
        model = dev.GetModelName()
        serial = dev.GetSerialNumber()
        cameras.append(
            {
                "label": f"Basler: {model} ({serial})",
                "type": "basler",
                "index": index,
                "serial": serial,
                "model": model,
            }
        )
    return cameras


def discover_flir_cameras() -> Tuple[List[Dict], Set[int]]:
    """
    Enumerate FLIR cameras available through flirpy.

    Returns both the discovered camera descriptors and any USB video indices
    already claimed by FLIR-specific backends so the generic OpenCV USB scan can
    skip duplicates.
    """

    cameras: List[Dict] = []
    reserved_usb_indices: Set[int] = set()

    cameras.extend(discover_flir_spinnaker_cameras())

    if Boson is not None:
        try:
            video_index = Boson.find_video_device()
            serial_port = Boson.find_serial_device()
            if video_index is not None or serial_port is not None:
                if video_index is not None:
                    reserved_usb_indices.add(int(video_index))
                location = _format_flir_location(video_index, serial_port)
                cameras.append(
                    {
                        "label": f"FLIR Boson{location}",
                        "type": "flir",
                        "backend": "boson",
                        "index": int(video_index) if video_index is not None else 0,
                        "video_index": video_index,
                        "serial_port": serial_port,
                    }
                )
        except Exception:
            pass

    if Lepton is not None:
        try:
            video_index = Lepton.find_video_device()
            if video_index is not None:
                reserved_usb_indices.add(int(video_index))
                cameras.append(
                    {
                        "label": f"FLIR Lepton (video {video_index})",
                        "type": "flir",
                        "backend": "lepton",
                        "index": int(video_index),
                        "video_index": int(video_index),
                        "serial_port": None,
                    }
                )
        except Exception:
            pass

    if TeaxGrabber is not None and usb_core is not None:
        try:
            device = usb_core.find(idVendor=0x0403, idProduct=0x6010)
            if device is not None:
                cameras.append(
                    {
                        "label": "FLIR Tau / TeAx Grabber",
                        "type": "flir",
                        "backend": "teax",
                        "index": 0,
                        "video_index": None,
                        "serial_port": None,
                    }
                )
        except Exception:
            pass

    return cameras, reserved_usb_indices


def discover_flir_spinnaker_cameras() -> List[Dict]:
    """Enumerate FLIR machine-vision cameras through Spinnaker/PySpin."""
    if not PYSPIN_AVAILABLE or PySpin is None:
        return []

    cameras: List[Dict] = []
    system = None
    cam_list = None

    try:
        system = PySpin.System.GetInstance()
        cam_list = system.GetCameras()
        for index in range(cam_list.GetSize()):
            camera = cam_list.GetByIndex(index)
            try:
                tl_map = camera.GetTLDeviceNodeMap()
                model = _read_pyspin_string_node(tl_map, "DeviceModelName") or "Unknown Model"
                serial = _read_pyspin_string_node(tl_map, "DeviceSerialNumber") or f"index-{index}"
                vendor = _read_pyspin_string_node(tl_map, "DeviceVendorName") or "FLIR"
                cameras.append(
                    {
                        "label": f"FLIR Spinnaker: {model} ({serial})",
                        "type": "flir",
                        "backend": "spinnaker",
                        "index": index,
                        "serial": serial,
                        "model": model,
                        "vendor": vendor,
                    }
                )
            finally:
                del camera
    except Exception:
        return cameras
    finally:
        if cam_list is not None:
            try:
                cam_list.Clear()
            except Exception:
                pass
        if system is not None:
            try:
                system.ReleaseInstance()
            except Exception:
                pass

    return cameras


def get_camera_backend_diagnostics() -> Dict[str, str]:
    """Return backend import diagnostics that are useful to surface in the UI."""
    diagnostics: Dict[str, str] = {}
    if PYSPIN_IMPORT_DIAGNOSTIC:
        diagnostics["pyspin"] = PYSPIN_IMPORT_DIAGNOSTIC
    return diagnostics


def discover_usb_cameras(
    skip_indices: Optional[Set[int]] = None,
    max_devices: int = 10,
) -> List[Dict]:
    """Enumerate generic OpenCV USB cameras, excluding reserved indices."""
    cameras: List[Dict] = []
    skip = {int(idx) for idx in (skip_indices or set())}
    backend = cv2.CAP_MSMF if os.name == "nt" else cv2.CAP_V4L2

    for index in range(max_devices):
        if index in skip:
            continue
        cap = cv2.VideoCapture(index, backend)
        try:
            if cap.isOpened():
                cameras.append(
                    {
                        "label": f"USB: Device {index}",
                        "type": "usb",
                        "index": index,
                    }
                )
        finally:
            cap.release()

    return cameras


def _format_flir_location(video_index: Optional[int], serial_port: Optional[str]) -> str:
    parts: List[str] = []
    if video_index is not None:
        parts.append(f"video {video_index}")
    if serial_port:
        parts.append(str(serial_port))
    if not parts:
        return ""
    return " (" + ", ".join(parts) + ")"


def _read_pyspin_string_node(node_map, node_name: str) -> str:
    """Safely read a string node from a PySpin transport-layer node map."""
    if not PYSPIN_AVAILABLE or PySpin is None or node_map is None:
        return ""
    try:
        node = PySpin.CStringPtr(node_map.GetNode(node_name))
    except Exception:
        return ""
    try:
        if not (PySpin.IsAvailable(node) and PySpin.IsReadable(node)):
            return ""
    except Exception:
        return ""
    try:
        return str(node.GetValue()).strip()
    except Exception:
        return ""


def _build_pyspin_import_diagnostic(import_error: Optional[Exception]) -> str:
    """Explain why PySpin is unavailable in the current interpreter."""
    import_error_text = str(import_error or "")
    lowered_error = import_error_text.lower()
    if (
        "_array_api" in lowered_error
        or "numpy.core.multiarray failed to import" in lowered_error
        or "multiarray failed to import" in lowered_error
    ):
        return (
            "PySpin is installed but cannot load against NumPy 2.x. "
            "Use numpy<2 in the CamApp environment for Spinnaker support."
        )

    current_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    candidates = _find_local_pyspin_wheels()
    if not candidates:
        if import_error is None:
            return ""
        return (
            "PySpin is not importable in this interpreter. Install the Spinnaker "
            f"SDK wheel that matches Python {sys.version_info.major}.{sys.version_info.minor}."
        )

    messages: List[str] = []
    for wheel_path, wheel_tag in candidates:
        if wheel_tag and wheel_tag != current_tag:
            messages.append(
                f"Found local PySpin wheel '{wheel_path.name}' for {wheel_tag}, "
                f"but CamApp is running on {current_tag}."
            )
        else:
            messages.append(
                f"Found local PySpin wheel '{wheel_path.name}', but it is not installed "
                "into the active Python environment."
            )

    messages.append(
        "Use a matching Python runtime or install the correct PySpin wheel into "
        "the environment that launches CamApp."
    )
    return " ".join(messages)


def _find_local_pyspin_wheels() -> List[Tuple[Path, str]]:
    """Find PySpin wheels stored inside the repository."""
    repo_root = Path(__file__).resolve().parent
    pyspin_root = repo_root / "PySpin"
    if not pyspin_root.is_dir():
        return []

    candidates: List[Tuple[Path, str]] = []
    for wheel_path in pyspin_root.rglob("*.whl"):
        tag = _extract_python_tag_from_wheel_name(wheel_path.name)
        candidates.append((wheel_path, tag))
    return candidates


def _extract_python_tag_from_wheel_name(filename: str) -> str:
    """Extract cpXY from a wheel filename when present."""
    match = re.search(r"-(cp\d{2,3})-(cp\d{2,3})-", filename)
    if not match:
        return ""
    return match.group(1)


if not PYSPIN_AVAILABLE:
    PYSPIN_IMPORT_DIAGNOSTIC = _build_pyspin_import_diagnostic(PYSPIN_IMPORT_ERROR)
