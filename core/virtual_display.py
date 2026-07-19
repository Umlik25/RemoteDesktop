"""One-time Windows virtual display driver setup helpers.

The application supports any active IDD output.  For a friendly default setup
we point Windows Package Manager at the signed VirtualDrivers package; the
installation is intentionally explicit because it changes a system driver.
"""
import platform
import shutil
import subprocess


PACKAGE_ID = "VirtualDrivers.Virtual-Display-Driver"
PROJECT_URL = "https://github.com/VirtualDrivers/Virtual-Display-Driver"
INSTALL_COMMAND = f"winget install --id={PACKAGE_ID} -e"


def state_payload(outputs=None):
    outputs = list(outputs or [])
    virtual = [output for output in outputs if output.get("virtual")]
    return {
        "supported": platform.system() == "Windows",
        "ready": bool(virtual),
        "outputs": len(virtual),
        "package": PACKAGE_ID,
        "project_url": PROJECT_URL,
        "install_command": INSTALL_COMMAND,
        "requires_local_confirmation": True,
    }


def launch_installer():
    """Open an elevated winget install on the interactive Windows desktop."""
    if platform.system() != "Windows":
        return False, "Установщик виртуального дисплея запускается только на Windows-хосте"
    if not shutil.which("winget.exe"):
        return False, ("Windows Package Manager не найден. Откройте страницу драйвера "
                       "и установите Virtual Driver Control вручную")
    script = (
        "$args = @('install','--id','VirtualDrivers.Virtual-Display-Driver','-e',"
        "'--accept-package-agreements','--accept-source-agreements'); "
        "$winget = (Get-Command 'winget.exe' -ErrorAction Stop).Source; "
        "Start-Process -FilePath $winget -ArgumentList $args -Verb RunAs"
    )
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except OSError as exc:
        return False, f"Не удалось открыть Windows Package Manager: {exc}"
    return True, None
