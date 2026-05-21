"""
Gateway subcommand for hermes CLI.

Handles: hermes gateway [run|start|stop|restart|status|install|uninstall|setup]
"""

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from gateway.status import terminate_pid
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    parse_restart_drain_timeout,
)
from hermes_cli.config import (
    get_env_value,
    get_hermes_home,
    is_managed,
    managed_error,
    read_raw_config,
    save_env_value,
)
# display_hermes_home is imported lazily at call sites to avoid ImportError
# when hermes_constants is cached from a pre-update version during `hermes update`.
from hermes_cli.setup import (
    print_header, print_info, print_success, print_warning, print_error,
    prompt, prompt_choice, prompt_yes_no,
)
from hermes_cli.colors import Colors, color


def is_macos() -> bool:
    """Return True on macOS/launchd hosts."""
    import platform as _platform
    return _platform.system() == "Darwin"


def is_linux() -> bool:
    """Return True on Linux hosts."""
    import platform as _platform
    return _platform.system() == "Linux"


def is_windows() -> bool:
    """Return True on Windows hosts."""
    import platform as _platform
    return _platform.system() == "Windows"


def is_termux() -> bool:
    from hermes_constants import is_termux as _is_termux
    return _is_termux()


def is_wsl() -> bool:
    from hermes_constants import is_wsl as _is_wsl
    return _is_wsl()


def supports_systemd_services() -> bool:
    """Return True when systemd service management is available."""
    if not is_linux() or is_termux():
        return False
    if shutil.which("systemctl") is None:
        return False
    if is_wsl():
        return _wsl_systemd_operational()
    if is_container():
        return _systemd_operational(system=False) or _systemd_operational(system=True)
    return True


def get_service_name() -> str:
    return "hermes-gateway.service"


def get_systemd_unit_path(system: bool = False) -> Path:
    if system:
        return Path("/etc/systemd/system") / get_service_name()
    return Path.home() / ".config" / "systemd" / "user" / get_service_name()


class UserSystemdUnavailableError(RuntimeError):
    """Raised when user systemd commands cannot reach the user bus."""


def is_container() -> bool:
    from hermes_constants import is_container as _is_container
    return _is_container()


def _detect_venv_dir() -> Path | None:
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return Path(sys.prefix)
    env = os.environ.get("VIRTUAL_ENV")
    if env:
        return Path(env)
    for name in (".venv", "venv"):
        candidate = PROJECT_ROOT / name
        if candidate.exists():
            return candidate
    return None


def get_python_path() -> str:
    venv = _detect_venv_dir()
    if venv is not None:
        candidate = venv / ("Scripts" if is_windows() else "bin") / ("python.exe" if is_windows() else "python")
        return str(candidate)
    return sys.executable


def _profile_arg(hermes_home: str) -> str:
    import re
    home = Path.home() / ".hermes"
    profiles = home / "profiles"
    try:
        path = Path(hermes_home).resolve()
        profiles = profiles.resolve()
        if path.parent == profiles:
            name = path.name
        elif ".hermes" in path.parts and "profiles" in path.parts:
            parts = path.parts
            idx = parts.index("profiles")
            if idx == 0 or parts[idx - 1] != ".hermes" or idx + 2 != len(parts):
                return ""
            name = parts[idx + 1]
        else:
            return ""
    except Exception:
        return ""
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        return f"--profile {name}"
    return ""


def _profile_suffix() -> str:
    arg = _profile_arg(str(get_hermes_home()))
    return f"-{arg.split()[-1]}" if arg else ""


def get_launchd_label() -> str:
    return f"ai.hermes.gateway{_profile_suffix()}"


def get_launchd_plist_path() -> Path:
    try:
        import pwd as _pwd
        home = Path(_pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        home = Path.home()
    return home / "Library" / "LaunchAgents" / f"{get_launchd_label()}.plist"


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _remap_path_for_user(path: str, target_home: str) -> str:
    current_home = str(Path.home())
    if path == current_home:
        return target_home
    prefix = current_home.rstrip("/") + "/"
    if path.startswith(prefix):
        return str(Path(target_home) / path[len(prefix):])
    return path


def _hermes_home_for_target_user(target_home: str) -> str:
    raw = os.environ.get("HERMES_HOME")
    current_default = Path.home() / ".hermes"
    current = Path(raw) if raw else current_default
    try:
        rel = current.resolve().relative_to(current_default.resolve())
        return str(Path(target_home) / ".hermes" / rel)
    except Exception:
        if current == current_default:
            return str(Path(target_home) / ".hermes")
        return str(current)


def _system_service_identity(run_as_user: str | None = None) -> tuple[str, str, str]:
    import getpass
    import grp
    import pwd
    username = run_as_user or os.environ.get("SUDO_USER") or getpass.getuser()
    if username == "root" and run_as_user is None:
        raise ValueError("Refusing to auto-detect root for system service; pass --run-as-user root to override")
    try:
        user_info = pwd.getpwnam(username)
    except KeyError as exc:
        raise ValueError(f"Unknown user: {username}") from exc
    group = grp.getgrgid(user_info.pw_gid).gr_name
    return username, group, user_info.pw_dir


def _build_user_local_paths(home: Path, existing: list[str]) -> list[str]:
    paths = [str(home / ".local" / "bin")]
    return [p for p in paths if p not in existing]


def generate_systemd_unit(system: bool = False, run_as_user: str | None = None) -> str:
    if system:
        username, group, target_home = _system_service_identity(run_as_user)
        hermes_home = _hermes_home_for_target_user(target_home)
        working_dir = _remap_path_for_user(str(PROJECT_ROOT), target_home)
        python_path = _remap_path_for_user(get_python_path(), target_home)
        venv = _detect_venv_dir()
        venv_path = _remap_path_for_user(str(venv), target_home) if venv else ""
        path_home = Path(target_home)
        user_lines = f"User={username}\nGroup={group}\n"
        wanted_by = "multi-user.target"
    else:
        hermes_home = str(get_hermes_home().resolve())
        working_dir = str(PROJECT_ROOT)
        python_path = get_python_path()
        venv = _detect_venv_dir()
        venv_path = str(venv) if venv else ""
        path_home = Path.home()
        user_lines = ""
        wanted_by = "default.target"

    path_entries = _build_user_local_paths(path_home, os.environ.get("PATH", "").split(os.pathsep))
    node_path = shutil.which("node")
    if node_path:
        path_entries.append(str(Path(node_path).parent))
    if venv_path:
        path_entries.append(str(Path(venv_path) / ("Scripts" if is_windows() else "bin")))
    path_entries.extend(os.environ.get("PATH", "").split(os.pathsep))
    profile = _profile_arg(hermes_home).split()
    exec_args = " ".join([python_path, "-m", "hermes_cli.main", *profile, "gateway", "run", "--replace"])
    env_lines = [f"Environment=HERMES_HOME={hermes_home}", f"Environment=PATH={os.pathsep.join(p for p in path_entries if p)}"]
    if venv_path:
        env_lines.append(f"Environment=VIRTUAL_ENV={venv_path}")
    return "\n".join([
        "[Unit]",
        "Description=Hermes Gateway",
        "After=network-online.target",
        "",
        "[Service]",
        user_lines.rstrip(),
        f"WorkingDirectory={working_dir}",
        *env_lines,
        f"ExecStart={exec_args}",
        "ExecReload=/bin/kill -USR1 $MAINPID",
        "Restart=on-failure",
        f"RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}",
        "TimeoutStopSec=90",
        "",
        "[Install]",
        f"WantedBy={wanted_by}",
        "",
    ])


def generate_launchd_plist() -> str:
    hermes_home = str(get_hermes_home().resolve())
    profile = _profile_arg(hermes_home).split()
    args = [get_python_path(), "-m", "hermes_cli.main", *profile, "gateway", "run", "--replace"]
    arg_xml = "\n".join(f"        <string>{arg}</string>" for arg in args)
    env_xml = "\n".join(
        f"        <key>{key}</key>\n        <string>{value}</string>"
        for key, value in _launchd_environment_variables(hermes_home).items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{get_launchd_label()}</string>
    <key>ProgramArguments</key>
    <array>
{arg_xml}
    </array>
    <key>WorkingDirectory</key><string>{PROJECT_ROOT}</string>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
    <key>KeepAlive</key><true/>
</dict>
</plist>
"""


def _user_dbus_socket_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "bus"


def _user_systemd_private_socket_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "systemd" / "private"


def _launchd_environment_variables(hermes_home: str) -> dict[str, str]:
    env = {"HERMES_HOME": hermes_home}
    path_parts: list[str] = []
    venv = _detect_venv_dir()
    if venv is not None:
        venv_bin = str(venv / ("Scripts" if is_windows() else "bin"))
        path_parts.append(venv_bin)
        env["VIRTUAL_ENV"] = str(venv)
    node_bin = str(PROJECT_ROOT / "node_modules" / ".bin")
    path_parts.append(node_bin)
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part and part not in path_parts:
            path_parts.append(part)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def _normalize_launchd_plist(plist: str) -> str:
    import re
    return re.sub(
        r"(<key>PATH</key>\s*<string>).*?(</string>)",
        r"\1__PATH__\2",
        plist,
        flags=re.DOTALL,
    )


def _ensure_user_systemd_env() -> None:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if "XDG_RUNTIME_DIR" not in os.environ and runtime.exists():
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
    if not hasattr(runtime, "__truediv__"):
        return
    bus = runtime / "bus"
    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ and bus.exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"


def _systemd_operational(system: bool = False) -> bool:
    if shutil.which("systemctl") is None:
        return False
    cmd = (["systemctl"] if system else ["systemctl", "--user"]) + ["is-system-running"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return (result.stdout or "").strip() in {"running", "degraded", "starting"}


def _wsl_systemd_operational() -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return (result.stdout or "").strip() in {"running", "degraded", "starting"}


def _systemctl_cmd(system: bool = False) -> list[str]:
    if system:
        return ["systemctl"]
    _ensure_user_systemd_env()
    return ["systemctl", "--user"]


def _run_systemctl(args, system: bool = False, **kwargs) -> subprocess.CompletedProcess[str]:
    cmd_args = list(args) if isinstance(args, (list, tuple)) else [str(args)]
    cmd = _systemctl_cmd(system=system) + cmd_args
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 30)
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise RuntimeError("systemctl is not available") from exc


def _select_systemd_scope(system: bool = False) -> bool:
    if system:
        return True
    if get_systemd_unit_path(system=False).exists():
        return False
    if get_systemd_unit_path(system=True).exists():
        return True
    return False


def get_systemd_linger_status() -> tuple[bool | None, str]:
    if is_termux():
        return None, "not supported in Termux"
    if shutil.which("loginctl") is None:
        return None, "loginctl not found"
    try:
        import getpass
        result = subprocess.run(["loginctl", "show-user", getpass.getuser(), "--property", "Linger"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, result.stderr.strip()
    value = (result.stdout or "").strip()
    if value in {"yes", "no"}:
        return value == "yes", ""
    if value.startswith("Linger="):
        return value.split("=", 1)[1].strip().lower() == "yes", ""
    return "Linger=yes" in value, ""


def _wait_for_user_dbus_socket(timeout: float = 3.0) -> bool:
    import time
    _ensure_user_systemd_env()
    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        if _user_dbus_socket_path().exists() or _user_systemd_private_socket_path().exists():
            return True
        time.sleep(0.05)
    return False


def _preflight_user_systemd(auto_enable_linger: bool = True) -> None:
    _ensure_user_systemd_env()
    if _user_dbus_socket_path().exists() or _user_systemd_private_socket_path().exists():
        return
    linger, detail = get_systemd_linger_status()
    if linger is True:
        if _wait_for_user_dbus_socket(timeout=3.0):
            return
        raise UserSystemdUnavailableError("user systemd linger is enabled, but the user D-Bus socket is still unavailable; run hermes gateway run in foreground or re-login")
    if not auto_enable_linger or shutil.which("loginctl") is None:
        raise UserSystemdUnavailableError(f"User systemd is unavailable. Run: sudo loginctl enable-linger {os.environ.get('USER', '')}\nThen retry, or use: hermes gateway run\n{detail}")
    result = subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise UserSystemdUnavailableError(f"Could not enable linger. Run: sudo loginctl enable-linger {os.environ.get('USER', '')}\nThen retry, or use: hermes gateway run\n{result.stderr.strip()}")
    print_success("Enabled linger for user systemd.")
    if not _wait_for_user_dbus_socket(timeout=5.0):
        raise UserSystemdUnavailableError("Enabled linger, but user systemd socket did not appear; try logging out/in or run hermes gateway run")


def systemd_unit_is_current(system: bool = False, run_as_user: str | None = None) -> bool:
    path = get_systemd_unit_path(system=system)
    return path.exists() and path.read_text(encoding="utf-8") == generate_systemd_unit(system=system, run_as_user=run_as_user)


def refresh_systemd_unit_if_needed(system: bool = False, run_as_user: str | None = None) -> None:
    path = get_systemd_unit_path(system=system)
    unit = generate_systemd_unit(system=system, run_as_user=run_as_user)
    if not path.exists() or path.read_text(encoding="utf-8") != unit:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit, encoding="utf-8")
        _run_systemctl(["daemon-reload"], system=system)


def _ensure_linger_enabled() -> None:
    if is_termux() or not is_linux():
        return
    import getpass

    username = getpass.getuser()
    linger_file = Path(f"/var/lib/systemd/linger/{username}")
    if linger_file.exists():
        print_success("Systemd linger is enabled")
        return

    linger, detail = get_systemd_linger_status()
    if linger is True:
        print_success("Systemd linger is enabled")
        return

    if shutil.which("loginctl") is None or linger is None:
        print_warning("Systemd linger is disabled or unavailable")
        if detail:
            print_warning(detail)
        print_info(f"Enable linger for the gateway user service: sudo loginctl enable-linger {username}")
        return

    print_info(f"Enabling linger for {username}...")
    result = subprocess.run(["loginctl", "enable-linger", username], capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        print_success("Linger enabled")
        return

    print_warning("Could not enable linger automatically")
    if result.stderr.strip():
        print_warning(result.stderr.strip())
    print_info(f"Enable linger for the gateway user service: sudo loginctl enable-linger {username}")


def _require_root_for_system_service(action: str) -> None:
    if os.geteuid() != 0:
        raise PermissionError(f"Root required to {action} system gateway service")


def _default_system_service_user() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"


def prompt_linux_gateway_install_scope() -> str:
    idx = prompt_choice(
        "Install Linux gateway service scope?",
        ["User service (recommended)", "System service"],
        0,
    )
    return "system" if idx == 1 else "user"


def systemd_install(force: bool = False, system: bool = False, run_as_user: str | None = None) -> None:
    if has_legacy_hermes_units():
        print_legacy_unit_warning()
        if prompt_yes_no("Remove legacy Hermes gateway units first?", True):
            remove_legacy_hermes_units(interactive=False)
    if system:
        _require_root_for_system_service("install")
    else:
        _ensure_linger_enabled()
    path = get_systemd_unit_path(system=system)
    unit = generate_systemd_unit(system=system, run_as_user=run_as_user)
    if force or not path.exists() or path.read_text(encoding="utf-8") != unit:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit, encoding="utf-8")
    _run_systemctl(["daemon-reload"], system=system)
    _run_systemctl(["enable", get_service_name()], system=system)
    print_success(f"{'System' if system else 'User'} service installed and enabled")


def systemd_start(system: bool = False) -> None:
    selected = _select_systemd_scope(system)
    refresh_systemd_unit_if_needed(system=selected)
    _run_systemctl(["start", get_service_name()], system=selected)


def systemd_stop(system: bool = False) -> None:
    selected = _select_systemd_scope(system)
    _run_systemctl(["stop", get_service_name()], system=selected)


def systemd_uninstall(system: bool = False) -> None:
    selected = _select_systemd_scope(system)
    _run_systemctl(["disable", get_service_name()], system=selected)
    path = get_systemd_unit_path(system=selected)
    if path.exists():
        path.unlink()
    _run_systemctl(["daemon-reload"], system=selected)


def systemd_restart(system: bool = False) -> None:
    selected = _select_systemd_scope(system)
    refresh_systemd_unit_if_needed(system=selected)
    props = _read_systemd_unit_properties(system=selected)
    try:
        from gateway.status import get_running_pid, read_runtime_status
        pid = get_running_pid()
    except Exception:
        pid = None
    if pid and _request_gateway_self_restart(pid):
        _wait_for_pid_exit(pid, timeout=_get_restart_drain_timeout())
        _run_systemctl(["reset-failed", get_service_name()], system=selected)
        _run_systemctl(["start", get_service_name()], system=selected)
        print_success("Gateway restarted")
        return
    try:
        runtime = read_runtime_status() if "read_runtime_status" in locals() else {}
    except Exception:
        runtime = {}
    planned_failed = props.get("ActiveState") == "failed" and props.get("ExecMainStatus") == str(GATEWAY_SERVICE_RESTART_EXIT_CODE)
    if planned_failed or (runtime or {}).get("restart_requested"):
        _run_systemctl(["reset-failed", get_service_name()], system=selected)
        _run_systemctl(["start", get_service_name()], system=selected)
        print_success("Gateway restarted")
        return
    _run_systemctl(["reset-failed", get_service_name()], system=selected)
    _run_systemctl(["reload-or-restart", get_service_name()], system=selected)


def systemd_status(deep: bool = False, system: bool = False, full: bool = False) -> None:
    selected = _select_systemd_scope(system)
    props = _read_systemd_unit_properties(system=selected)
    if props.get("ActiveState") == "active":
        print_success("gateway service is running")
    linger, _detail = get_systemd_linger_status()
    if not selected and linger is False:
        import getpass
        print_warning("Systemd linger is disabled")
        print_info(f"Enable linger for the gateway user service: sudo loginctl enable-linger {getpass.getuser()}")
    if props.get("ActiveState") == "failed" and props.get("ExecMainStatus") == str(GATEWAY_SERVICE_RESTART_EXIT_CODE):
        print_warning("Planned restart is stuck in systemd failed state")
    _run_systemctl(["status", get_service_name(), "--no-pager"], system=selected)
    for line in _runtime_health_lines():
        print(line)


def install_linux_gateway_from_setup(force: bool = False) -> tuple[str | None, bool]:
    if not supports_systemd_services():
        return None, False
    scope = prompt_linux_gateway_install_scope()
    if scope == "system":
        run_as_user = _default_system_service_user()
        if os.geteuid() != 0:
            print_info(f"sudo hermes gateway install --system --run-as-user {run_as_user}")
            print_info("sudo hermes gateway start --system")
            return "system", False
        systemd_install(force=force, system=True, run_as_user=run_as_user)
        return "system", True
    systemd_install(force=force, system=False)
    return "user", True


def launchd_install(force: bool = False) -> None:
    path = get_launchd_plist_path()
    plist = generate_launchd_plist()
    path.parent.mkdir(parents=True, exist_ok=True)
    if force or not path.exists() or path.read_text(encoding="utf-8") != plist:
        path.write_text(plist, encoding="utf-8")
    target = f"{_launchd_domain()}/{get_launchd_label()}"
    try:
        subprocess.run(["launchctl", "bootout", target], check=False)
    except subprocess.CalledProcessError:
        pass
    subprocess.run(["launchctl", "bootstrap", _launchd_domain(), str(path)], check=False)


def launchd_plist_is_current() -> bool:
    path = get_launchd_plist_path()
    return path.exists() and _normalize_launchd_plist(path.read_text(encoding="utf-8")) == _normalize_launchd_plist(generate_launchd_plist())


def refresh_launchd_plist_if_needed() -> bool:
    path = get_launchd_plist_path()
    if not path.exists() or launchd_plist_is_current():
        return False
    path.write_text(generate_launchd_plist(), encoding="utf-8")
    target = f"{_launchd_domain()}/{get_launchd_label()}"
    try:
        subprocess.run(["launchctl", "bootout", target], check=False)
    except subprocess.CalledProcessError:
        pass
    subprocess.run(["launchctl", "bootstrap", _launchd_domain(), str(path)], check=False)
    return True


def launchd_start() -> None:
    target = f"{_launchd_domain()}/{get_launchd_label()}"
    path = get_launchd_plist_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generate_launchd_plist(), encoding="utf-8")
        subprocess.run(["launchctl", "bootstrap", _launchd_domain(), str(path)], check=False)
    else:
        refresh_launchd_plist_if_needed()
    try:
        result = subprocess.run(["launchctl", "kickstart", target], check=False)
    except subprocess.CalledProcessError as exc:
        if exc.returncode not in (3, 113):
            raise
        subprocess.run(["launchctl", "bootstrap", _launchd_domain(), str(path)], check=False)
        result = subprocess.run(["launchctl", "kickstart", target], check=False)
    else:
        if result.returncode in (3, 113):
            subprocess.run(["launchctl", "bootstrap", _launchd_domain(), str(path)], check=False)
            result = subprocess.run(["launchctl", "kickstart", target], check=False)
        elif result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, ["launchctl", "kickstart", target], stderr=getattr(result, "stderr", ""))
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, ["launchctl", "kickstart", target], stderr=getattr(result, "stderr", ""))


def launchd_restart() -> None:
    try:
        from gateway.status import get_running_pid
        pid = get_running_pid()
    except Exception:
        pid = None
    if pid and _request_gateway_self_restart(pid):
        print_success("Gateway restart requested")
        return
    if pid:
        terminate_pid(pid, force=False)
        _wait_for_gateway_exit(timeout=_get_restart_drain_timeout(), force_after=None)
    cmd = ["launchctl", "kickstart", "-k", f"{_launchd_domain()}/{get_launchd_label()}"]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, stderr=getattr(result, "stderr", ""))


def launchd_stop() -> None:
    target = f"{_launchd_domain()}/{get_launchd_label()}"
    try:
        subprocess.run(["launchctl", "bootout", target], check=False)
    except subprocess.CalledProcessError as exc:
        if exc.returncode not in (3, 113):
            raise
    print_success("Gateway stopped")
    _wait_for_gateway_exit(timeout=10.0, force_after=5.0)


def launchd_uninstall() -> None:
    launchd_stop()
    path = get_launchd_plist_path()
    if path.exists():
        path.unlink()


def launchd_status(deep: bool = False) -> None:
    path = get_launchd_plist_path()
    result = subprocess.run(["launchctl", "list", get_launchd_label()], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        print_success(f"Gateway is loaded: {get_launchd_label()}")
    elif path.exists():
        print_warning(f"Launchd plist exists but service is stale/not loaded: {path}")
    else:
        print_warning("Gateway launchd service is not installed")


def _get_restart_drain_timeout() -> float:
    raw = os.environ.get("HERMES_RESTART_DRAIN_TIMEOUT")
    if raw:
        try:
            return parse_restart_drain_timeout(raw)
        except Exception:
            return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    try:
        cfg = read_raw_config() or {}
        agent_cfg = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
        if "restart_drain_timeout" in agent_cfg:
            return parse_restart_drain_timeout(agent_cfg["restart_drain_timeout"])
    except Exception:
        pass
    return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT


def _wait_for_gateway_exit(timeout: float = 10.0, force_after: float | None = 5.0) -> bool:
    import time
    from gateway.status import get_running_pid

    deadline = time.monotonic() + timeout
    force_deadline = time.monotonic() + force_after if force_after is not None else None
    forced = False
    while time.monotonic() < deadline:
        pid = get_running_pid()
        if not pid:
            return True
        if force_deadline is not None and not forced and time.monotonic() >= force_deadline:
            try:
                terminate_pid(pid, force=True)
            except ProcessLookupError:
                return True
            forced = True
        time.sleep(0.1)
    return False


def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    import time
    deadline = time.monotonic() + max(timeout, 0.1)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.1)
    return False


def stop_profile_gateway() -> bool:
    import time

    try:
        from gateway.status import get_running_pid, remove_pid_file
        pid = get_running_pid()
    except Exception:
        pid = None
        remove_pid_file = lambda: None
    if not pid:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid_file()
        return True

    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            remove_pid_file()
            break
    return True


def kill_gateway_processes(force: bool = False, all_profiles: bool = False) -> int:
    count = 0
    for pid in find_gateway_pids(all_profiles=all_profiles):
        try:
            terminate_pid(pid, force=force)
            count += 1
        except Exception:
            pass
    return count


def run_gateway(verbose: int = 0, quiet: bool = False, replace: bool = False) -> None:
    from gateway.run import start_gateway

    verbosity = None if quiet else verbose
    print("Press Ctrl+C to stop")
    try:
        success = asyncio.run(start_gateway(replace=replace, verbosity=verbosity))
    except KeyboardInterrupt:
        print("Gateway stopped.")
        return

    if not success:
        sys.exit(1)


def _legacy_unit_search_paths() -> list[tuple[bool, Path]]:
    return [
        (False, Path.home() / ".config" / "systemd" / "user"),
        (True, Path("/etc/systemd/system")),
    ]


def _legacy_unit_is_ours(text: str) -> bool:
    markers = (
        "hermes_cli.main gateway run",
        "hermes_cli/main.py gateway run",
        "hermes gateway run",
        "gateway/run.py",
    )
    return any(marker in text for marker in markers)


def _find_legacy_hermes_units() -> list[tuple[str, Path, bool]]:
    results: list[tuple[str, Path, bool]] = []
    for is_system, directory in _legacy_unit_search_paths():
        path = directory / "hermes.service"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if _legacy_unit_is_ours(text):
            results.append((path.name, path, is_system))
    return results


def has_legacy_hermes_units() -> bool:
    return bool(_find_legacy_hermes_units())


def print_legacy_unit_warning() -> None:
    units = _find_legacy_hermes_units()
    if not units:
        return
    print_warning("Legacy Hermes gateway systemd units were found:")
    for name, path, _is_system in units:
        print_warning(f"  {name}: {path}")
    print_info("Run: hermes gateway migrate-legacy")


def remove_legacy_hermes_units(interactive: bool = True, dry_run: bool = False) -> tuple[int, list[Path]]:
    units = _find_legacy_hermes_units()
    if not units:
        print_info("No legacy Hermes gateway units found.")
        return 0, []
    if interactive and not prompt_yes_no("Remove legacy Hermes gateway units?", False):
        return 0, [path for _name, path, _is_system in units]
    if dry_run:
        print_info("Legacy gateway unit dry-run:")
        for _name, path, _is_system in units:
            print_info(f"  {path}")
        return 0, [path for _name, path, _is_system in units]
    removed = 0
    remaining: list[Path] = []
    for name, path, is_system in units:
        if is_system and os.geteuid() != 0:
            print_warning("System-scope legacy unit requires sudo. Run: sudo hermes gateway migrate-legacy --yes")
            remaining.append(path)
            continue
        subprocess.run(_systemctl_cmd(system=is_system) + ["stop", name], capture_output=True, text=True, timeout=30)
        subprocess.run(_systemctl_cmd(system=is_system) + ["disable", name], capture_output=True, text=True, timeout=30)
        try:
            path.unlink()
            removed += 1
        except OSError:
            remaining.append(path)
            continue
        subprocess.run(_systemctl_cmd(system=is_system) + ["daemon-reload"], capture_output=True, text=True, timeout=30)
    return removed, remaining


def has_conflicting_systemd_units() -> bool:
    return get_systemd_unit_path(system=False).exists() and get_systemd_unit_path(system=True).exists()


def print_systemd_scope_conflict_warning() -> None:
    print_warning("Both user and system gateway services are installed; use one service scope to avoid duplicate bots.")
    print_info("  hermes gateway uninstall")
    print_info("  hermes gateway uninstall --system")


def _setup_standard_platform(platform: dict) -> None:
    for var in platform.get("vars", []):
        name = var.get("name") if isinstance(var, dict) else None
        if not name:
            continue
        value = prompt(var.get("prompt", name), password=var.get("password", False))
        if value:
            save_env_value(name, value)


def _is_service_installed() -> bool:
    if supports_systemd_services():
        return get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()
    if is_macos():
        try:
            return get_launchd_plist_path().exists()
        except NameError:
            return False
    return False


def _is_service_running() -> bool:
    if supports_systemd_services():
        for system in (False, True):
            if not get_systemd_unit_path(system=system).exists():
                continue
            cmd = ["systemctl"] + ([] if system else ["--user"]) + ["is-active", get_service_name()]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
            if result.stdout.strip() == "active":
                return True
        return False
    if is_macos():
        try:
            label = get_launchd_label()
            result = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (NameError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
    return False


# =============================================================================
# Process Management (for manual gateway runs)
# =============================================================================


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str
    service_installed: bool = False
    service_running: bool = False
    gateway_pids: tuple[int, ...] = ()
    service_scope: str | None = None

    @property
    def running(self) -> bool:
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        return self.service_installed and self.running and not self.service_running


@dataclass(frozen=True)
class ProfileGatewayProcess:
    profile: str
    path: Path
    pid: int

def _get_service_pids(all_profiles: bool = False) -> set:
    """Return PIDs currently managed by systemd or launchd gateway services.

    Used to avoid killing freshly-restarted service processes when sweeping
    for stale manual gateway processes after a service restart.  Relies on the
    service manager having committed the new PID before the restart command
    returns (true for both systemd and launchd in practice).
    """
    pids: set = set()
    current_home = str(get_hermes_home().resolve())
    current_profile_arg = _profile_arg(current_home)
    current_profile_name = current_profile_arg.split()[-1] if current_profile_arg else ""

    # --- systemd (Linux): user and system scopes ---
    if supports_systemd_services():
        for scope_args in [["systemctl", "--user"], ["systemctl"]]:
            try:
                result = subprocess.run(
                    scope_args + ["list-units", "hermes-gateway*",
                                  "--plain", "--no-legend", "--no-pager"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if not parts or not parts[0].endswith(".service"):
                        continue
                    svc = parts[0]
                    try:
                        show = subprocess.run(
                            scope_args + [
                                "show",
                                svc,
                                "--property=MainPID,Environment,ExecStart",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        values: dict[str, str] = {}
                        for raw_line in (show.stdout or "").splitlines():
                            if "=" not in raw_line:
                                continue
                            key, value = raw_line.split("=", 1)
                            values[key] = value.strip()
                        pid = int(values.get("MainPID", "0"))
                        profile_hint = " ".join(
                            value for key, value in values.items() if key in {"Environment", "ExecStart"}
                        )
                        if not all_profiles and not _matches_current_gateway_profile(
                            f"{svc} {profile_hint}",
                            current_home,
                            current_profile_name,
                        ):
                            continue
                        if pid > 0:
                            pids.add(pid)
                    except (ValueError, subprocess.TimeoutExpired):
                        pass
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    # --- launchd (macOS) ---
    if is_macos():
        for label in _launchd_labels(all_profiles=all_profiles):
            try:
                result = subprocess.run(
                    ["launchctl", "list", label],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    # Output: "PID\tStatus\tLabel" header, then one data line
                    for line in result.stdout.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 3 and parts[2] == label:
                            try:
                                pid = int(parts[0])
                                if pid > 0:
                                    pids.add(pid)
                            except ValueError:
                                pass
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    return pids


def _get_parent_pid(pid: int) -> int | None:
    """Return the parent PID for ``pid``, or ``None`` when unavailable."""
    if pid <= 1:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        parent_pid = int(raw.splitlines()[-1].strip())
    except ValueError:
        return None
    return parent_pid if parent_pid > 0 else None


def _is_pid_ancestor_of_current_process(target_pid: int) -> bool:
    """Return True when ``target_pid`` is this process or one of its ancestors."""
    if target_pid <= 0:
        return False

    pid = os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen:
        if pid == target_pid:
            return True
        seen.add(pid)
        pid = _get_parent_pid(pid) or 0
    return False


def _request_gateway_self_restart(pid: int) -> bool:
    """Ask a running gateway ancestor to restart itself asynchronously."""
    if not hasattr(signal, "SIGUSR1"):
        return False
    if not _is_pid_ancestor_of_current_process(pid):
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _graceful_restart_via_sigusr1(pid: int, drain_timeout: float) -> bool:
    """Send SIGUSR1 to a gateway PID and wait for it to exit gracefully.

    SIGUSR1 is wired in gateway/run.py to ``request_restart(via_service=True)``
    which drains in-flight agent runs (up to ``agent.restart_drain_timeout``
    seconds), then exits with code 75.  Both systemd (``Restart=on-failure``
    + ``RestartForceExitStatus=75``) and launchd (``KeepAlive.SuccessfulExit
    = false``) relaunch the process after the graceful exit.

    This is the drain-aware alternative to ``systemctl restart`` / ``SIGTERM``,
    which SIGKILL in-flight agents after a short timeout.

    Args:
        pid: Gateway process PID (systemd MainPID, launchd PID, or bare
            process PID).
        drain_timeout: Seconds to wait for the process to exit after sending
            SIGUSR1.  Should be slightly larger than the gateway's
            ``agent.restart_drain_timeout`` to allow the drain loop to
            finish cleanly.

    Returns:
        True if the PID was signalled and exited within the timeout.
        False if SIGUSR1 couldn't be sent or the process didn't exit in
        time (caller should fall back to a harder restart path).
    """
    if not hasattr(signal, "SIGUSR1"):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        # Already gone — nothing to drain.
        return True
    except (PermissionError, OSError):
        return False

    import time as _time

    deadline = _time.monotonic() + max(drain_timeout, 1.0)
    while _time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # signal 0 — probe liveness
        except ProcessLookupError:
            return True
        except PermissionError:
            # Process still exists but we can't signal it.  Treat as alive
            # so the caller falls back.
            pass
        _time.sleep(0.5)
    # Drain didn't finish in time.
    return False


def _append_unique_pid(pids: list[int], pid: int | None, exclude_pids: set[int]) -> None:
    if pid is None or pid <= 0:
        return
    if pid == os.getpid() or pid in exclude_pids or pid in pids:
        return
    pids.append(pid)


def _matches_current_gateway_profile(command: str, current_home: str, current_profile_name: str) -> bool:
    profile_match = re.search(r"(?:^|\s)(?:--profile|-p)\s+(\S+)", command)
    home_match = re.search(r"HERMES_HOME=([^\s\"]+)", command)

    if current_profile_name:
        if profile_match and profile_match.group(1) == current_profile_name:
            return True
        if home_match and home_match.group(1) == current_home:
            return True
        return False

    if profile_match:
        return False
    if home_match and home_match.group(1) != current_home:
        return False
    return True


def _launchd_labels(all_profiles: bool = False) -> list[str]:
    labels = {get_launchd_label()}
    if not all_profiles:
        return sorted(labels)

    try:
        from hermes_cli.profiles import list_profiles

        for profile in list_profiles():
            name = getattr(profile, "name", "")
            if not name or name == "default":
                labels.add("ai.hermes.gateway")
            else:
                labels.add(f"ai.hermes.gateway-{name}")
    except Exception:
        pass

    return sorted(labels)


def _scan_gateway_pids(exclude_pids: set[int], all_profiles: bool = False) -> list[int]:
    """Best-effort process-table scan for gateway PIDs.

    This supplements the profile-scoped PID file so status views can still spot
    a live gateway when the PID file is stale/missing, and ``--all`` sweeps can
    discover gateways outside the current profile.
    """
    pids: list[int] = []
    patterns = [
        "hermes_cli.main gateway",
        "hermes_cli.main --profile",
        "hermes_cli.main -p",
        "hermes_cli/main.py gateway",
        "hermes_cli/main.py --profile",
        "hermes_cli/main.py -p",
        "hermes gateway",
        "gateway/run.py",
    ]
    current_home = str(get_hermes_home().resolve())
    current_profile_arg = _profile_arg(current_home)
    current_profile_name = current_profile_arg.split()[-1] if current_profile_arg else ""

    try:
        if is_windows():
            result = subprocess.run(
                ["wmic", "process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
            )
            if result.returncode != 0 or result.stdout is None:
                return []
            current_cmd = ""
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("CommandLine="):
                    current_cmd = line[len("CommandLine="):]
                elif line.startswith("ProcessId="):
                    pid_str = line[len("ProcessId="):]
                    if any(p in current_cmd for p in patterns) and (
                        all_profiles or _matches_current_gateway_profile(current_cmd, current_home, current_profile_name)
                    ):
                        try:
                            _append_unique_pid(pids, int(pid_str), exclude_pids)
                        except ValueError:
                            pass
                    current_cmd = ""
        else:
            result = subprocess.run(
                ["ps", "-A", "eww", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return []
            for line in result.stdout.split("\n"):
                stripped = line.strip()
                if not stripped or "grep" in stripped:
                    continue

                pid = None
                command = ""

                parts = stripped.split(None, 1)
                if len(parts) == 2:
                    try:
                        pid = int(parts[0])
                        command = parts[1]
                    except ValueError:
                        pid = None

                if pid is None:
                    aux_parts = stripped.split()
                    if len(aux_parts) > 10 and aux_parts[1].isdigit():
                        pid = int(aux_parts[1])
                        command = " ".join(aux_parts[10:])

                if pid is None:
                    continue
                if any(pattern in command for pattern in patterns) and (
                    all_profiles or _matches_current_gateway_profile(command, current_home, current_profile_name)
                ):
                    _append_unique_pid(pids, pid, exclude_pids)
    except (OSError, subprocess.TimeoutExpired):
        return []

    return pids


def find_gateway_pids(exclude_pids: set | None = None, all_profiles: bool = False) -> list:
    """Find PIDs of running gateway processes.

    Args:
        exclude_pids: PIDs to exclude from the result (e.g. service-managed
            PIDs that should not be killed during a stale-process sweep).
        all_profiles: When ``True``, return gateway PIDs across **all**
            profiles (the pre-7923 global behaviour).  ``hermes update``
            needs this because a code update affects every profile.
            When ``False`` (default), only PIDs belonging to the current
            Hermes profile are returned.
    """
    _exclude = set(exclude_pids or set())
    pids: list[int] = []
    if not all_profiles:
        try:
            from gateway.status import get_running_pid

            _append_unique_pid(pids, get_running_pid(), _exclude)
        except Exception:
            pass
    service_pids = _get_service_pids(all_profiles=True) if all_profiles else _get_service_pids()
    for pid in service_pids:
        _append_unique_pid(pids, pid, _exclude)
    for pid in _scan_gateway_pids(_exclude, all_profiles=all_profiles):
        _append_unique_pid(pids, pid, _exclude)
    return pids


def find_profile_gateway_processes(
    exclude_pids: set | None = None,
) -> list[ProfileGatewayProcess]:
    """Return running gateway PIDs mapped to Hermes profiles via PID files."""
    _exclude = set(exclude_pids or set())
    processes: list[ProfileGatewayProcess] = []
    try:
        from gateway.status import get_running_pid
        from hermes_cli.profiles import list_profiles
    except Exception:
        return processes

    seen: set[int] = set()
    for profile in list_profiles():
        try:
            pid = get_running_pid(profile.path / "gateway.pid", cleanup_stale=False)
        except Exception:
            continue
        if pid is None or pid <= 0 or pid in _exclude or pid in seen:
            continue
        seen.add(pid)
        processes.append(ProfileGatewayProcess(profile=profile.name, path=profile.path, pid=pid))
    return processes


def _gateway_run_args_for_profile(profile: str) -> list[str]:
    args = [get_python_path(), "-m", "hermes_cli.main"]
    if profile != "default":
        args.extend(["--profile", profile])
    args.extend(["gateway", "run", "--replace"])
    return args


def launch_detached_profile_gateway_restart(profile: str, old_pid: int) -> bool:
    """Relaunch a manually-run profile gateway after its current PID exits."""
    if old_pid <= 0:
        return False

    watcher = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        import time

        pid = int(sys.argv[1])
        cmd = sys.argv[2:]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
            time.sleep(0.2)
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        """
    ).strip()

    try:
        subprocess.Popen(
            [sys.executable, "-c", watcher, str(old_pid), *_gateway_run_args_for_profile(profile)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def _probe_systemd_service_running(system: bool = False) -> tuple[bool, bool]:
    scopes = [True] if system else [False, True]
    first_existing = system
    for selected_system in scopes:
        if not get_systemd_unit_path(system=selected_system).exists():
            continue
        first_existing = selected_system
        try:
            result = _run_systemctl(
                ["is-active", get_service_name()],
                system=selected_system,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            continue
        if result.stdout.strip() == "active":
            return selected_system, True
    return first_existing, False


def _read_systemd_unit_properties(
    system: bool = False,
    properties: tuple[str, ...] = (
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
    ),
) -> dict[str, str]:
    """Return selected ``systemctl show`` properties for the gateway unit."""
    selected_system = _select_systemd_scope(system)
    try:
        result = _run_systemctl(
            [
                "show",
                get_service_name(),
                "--no-pager",
                "--property",
                ",".join(properties),
            ],
            system=selected_system,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return {}

    values: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip()
    return values


def _format_gateway_pids(pids: tuple[int, ...] | list[int]) -> str:
    return ", ".join(str(pid) for pid in pids)


def get_gateway_runtime_snapshot(system: bool = False) -> GatewayRuntimeSnapshot:
    if supports_systemd_services():
        selected_system, service_running = _probe_systemd_service_running(system)
        return GatewayRuntimeSnapshot(
            manager="systemd",
            service_installed=get_systemd_unit_path(system=selected_system).exists(),
            service_running=service_running,
            gateway_pids=tuple(find_gateway_pids()),
            service_scope="system" if selected_system else "user",
        )

    if is_macos():
        service_installed = get_launchd_plist_path().exists()
        service_running = False
        if service_installed:
            try:
                label = get_launchd_label()
                result = subprocess.run(
                    ["launchctl", "list", label],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                service_running = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                service_running = False
        return GatewayRuntimeSnapshot(
            manager="launchd",
            service_installed=service_installed,
            service_running=service_running,
            gateway_pids=tuple(find_gateway_pids()),
        )

    return GatewayRuntimeSnapshot(
        manager="manual",
        gateway_pids=tuple(find_gateway_pids()),
    )


def _is_service_installed(system: bool = False) -> bool:
    return get_gateway_runtime_snapshot(system=system).service_installed


def _is_service_running(system: bool = False) -> bool:
    if supports_systemd_services():
        scopes = [True] if system else [False, True]
        for selected_system in scopes:
            if not get_systemd_unit_path(system=selected_system).exists():
                continue
            cmd = ["systemctl"] + ([] if selected_system else ["--user"]) + ["is-active", get_service_name()]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
            if result.stdout.strip() == "active":
                return True
        return False
    return get_gateway_runtime_snapshot(system=system).service_running


def _runtime_health_lines() -> list[str]:
    try:
        from gateway.status import read_runtime_status

        runtime = read_runtime_status() or {}
        platforms = runtime.get("platforms") or {}
        lines = []
        for name, data in sorted(platforms.items()):
            if not isinstance(data, dict):
                continue
            state = data.get("platform_state") or data.get("state") or "unknown"
            err = data.get("error_message")
            if err and state in {"fatal", "retrying"}:
                line = f"⚠ {name}: {err}"
            else:
                line = f"{name}: {state}"
                if err:
                    line += f" — {err}"
            lines.append(line)

        gateway_state = runtime.get("gateway_state")
        exit_reason = runtime.get("exit_reason")
        if exit_reason:
            if gateway_state == "startup_failed":
                lines.append(f"⚠ Last startup issue: {exit_reason}")
            elif gateway_state == "stopped":
                lines.append(f"⚠ Last shutdown reason: {exit_reason}")
        return lines
    except Exception:
        return []


def _print_gateway_process_mismatch(snapshot: GatewayRuntimeSnapshot) -> None:
    if snapshot.has_process_service_mismatch:
        print_warning(
            "A gateway process is running, but the configured service manager does not report it as active."
        )
        if snapshot.gateway_pids:
            print_warning("Gateway process is running for this profile")
            print_warning(f"PID(s): {_format_gateway_pids(snapshot.gateway_pids)}")


def _setup_signal() -> None:
    from hermes_cli import setup as _setup_mod

    _setup_mod.print_header("Signal")
    existing_url = get_env_value("SIGNAL_HTTP_URL")
    existing_account = get_env_value("SIGNAL_ACCOUNT")
    if existing_url and existing_account and not _setup_mod.prompt_yes_no("Reconfigure Signal?", False):
        return
    http_url = _setup_mod.prompt("Signal daemon HTTP URL", password=False)
    if http_url:
        save_env_value("SIGNAL_HTTP_URL", http_url)
    account = _setup_mod.prompt("Signal account phone number", password=False)
    if account:
        save_env_value("SIGNAL_ACCOUNT", account)
    allowed = _setup_mod.prompt("Allowed Signal users (comma-separated, optional)", password=False)
    save_env_value("SIGNAL_ALLOWED_USERS", allowed.replace(" ", ""))


def _setup_email() -> None:
    from hermes_cli import setup as _setup_mod

    _setup_mod.print_header("Email")
    existing = get_env_value("EMAIL_ADDRESS")
    if existing and not _setup_mod.prompt_yes_no("Reconfigure Email?", False):
        return
    address = _setup_mod.prompt("Email address", password=False)
    if address:
        save_env_value("EMAIL_ADDRESS", address)
    password = _setup_mod.prompt("Email password / app password", password=True)
    if password:
        save_env_value("EMAIL_PASSWORD", password)
    imap_host = _setup_mod.prompt("Email IMAP host", password=False)
    if imap_host:
        save_env_value("EMAIL_IMAP_HOST", imap_host)
    smtp_host = _setup_mod.prompt("Email SMTP host", password=False)
    if smtp_host:
        save_env_value("EMAIL_SMTP_HOST", smtp_host)
    allowed = _setup_mod.prompt("Allowed email addresses (comma-separated, optional)", password=False)
    save_env_value("EMAIL_ALLOWED_USERS", allowed.replace(" ", ""))


def _setup_homeassistant() -> None:
    from hermes_cli import setup as _setup_mod

    _setup_mod.print_header("Home Assistant")
    existing = get_env_value("HASS_URL")
    if existing and not _setup_mod.prompt_yes_no("Reconfigure Home Assistant?", False):
        return
    hass_url = _setup_mod.prompt("Home Assistant URL", password=False)
    if hass_url:
        save_env_value("HASS_URL", hass_url)
    token = _setup_mod.prompt("Home Assistant token", password=True)
    if token:
        save_env_value("HASS_TOKEN", token)


def _setup_api_server() -> None:
    from hermes_cli import setup as _setup_mod

    _setup_mod.print_header("API Server")
    save_env_value("API_SERVER_ENABLED", "true")
    host = _setup_mod.prompt("API server host (default 127.0.0.1)", password=False)
    if host:
        save_env_value("API_SERVER_HOST", host)
    port = _setup_mod.prompt("API server port (default 8642)", password=False)
    if port:
        save_env_value("API_SERVER_PORT", port)
    key = _setup_mod.prompt("API server auth key (optional on loopback)", password=True)
    if key:
        save_env_value("API_SERVER_KEY", key)


def _builtin_setup_fn(key: str):
    from hermes_cli import setup as _setup_mod

    return {
        "telegram": _setup_mod._setup_telegram,
        "discord": _setup_mod._setup_discord,
        "slack": _setup_mod._setup_slack,
        "signal": _setup_signal,
        "email": _setup_email,
        "homeassistant": _setup_homeassistant,
        "webhook": _setup_mod._setup_webhooks,
        "api_server": _setup_api_server,
    }.get(key)


def _platform_status(platform: dict) -> str:
    entry = platform.get("_registry_entry")
    if entry is not None:
        try:
            if not entry.check_fn():
                return "plugin disabled"
        except Exception:
            return "plugin disabled"

        try:
            from gateway.config import Platform, load_gateway_config

            config = load_gateway_config()
            plugin_platform = Platform(entry.name)
            pconfig = config.platforms.get(plugin_platform)
            if not pconfig or not pconfig.enabled:
                return "not configured"
            return "configured" if config._is_platform_connected(plugin_platform, pconfig) else "not configured"
        except Exception:
            return "not configured"

    key = platform["key"]
    if key == "telegram":
        return "configured" if get_env_value("TELEGRAM_BOT_TOKEN") else "not configured"
    if key == "discord":
        return "configured" if get_env_value("DISCORD_BOT_TOKEN") else "not configured"
    if key == "slack":
        bot = get_env_value("SLACK_BOT_TOKEN")
        app = get_env_value("SLACK_APP_TOKEN")
        return "configured" if bot and app else "partially configured" if bot else "not configured"
    if key == "signal":
        url = get_env_value("SIGNAL_HTTP_URL")
        account = get_env_value("SIGNAL_ACCOUNT")
        return "configured" if url and account else "partially configured" if url or account else "not configured"
    if key == "email":
        required = [
            get_env_value("EMAIL_ADDRESS"),
            get_env_value("EMAIL_PASSWORD"),
            get_env_value("EMAIL_IMAP_HOST"),
            get_env_value("EMAIL_SMTP_HOST"),
        ]
        return "configured" if all(required) else "partially configured" if any(required) else "not configured"
    if key == "homeassistant":
        url = get_env_value("HASS_URL")
        token = get_env_value("HASS_TOKEN")
        return "configured" if url and token else "partially configured" if url or token else "not configured"
    if key == "webhook":
        enabled = (get_env_value("WEBHOOK_ENABLED") or "").lower() in {"1", "true", "yes", "on"}
        return "configured" if enabled else "not configured"
    if key == "api_server":
        enabled = (get_env_value("API_SERVER_ENABLED") or "").lower() in {"1", "true", "yes", "on"}
        return "configured" if enabled else "not configured"
    return "not configured"


_BUILTIN_PLATFORMS: list[dict[str, object]] = [
    {"key": "telegram", "label": "Telegram", "emoji": "📱", "token_var": "TELEGRAM_BOT_TOKEN"},
    {"key": "discord", "label": "Discord", "emoji": "💬", "token_var": "DISCORD_BOT_TOKEN"},
    {"key": "slack", "label": "Slack", "emoji": "💼", "token_var": "SLACK_BOT_TOKEN"},
    {"key": "signal", "label": "Signal", "emoji": "📡", "token_var": "SIGNAL_HTTP_URL"},
    {"key": "email", "label": "Email", "emoji": "📧", "token_var": "EMAIL_ADDRESS"},
    {"key": "homeassistant", "label": "Home Assistant", "emoji": "🏠", "token_var": "HASS_URL"},
    {"key": "webhook", "label": "Webhooks", "emoji": "🔗"},
    {"key": "api_server", "label": "API Server", "emoji": "🌐"},
]

# Backward-compatible built-in platform list for callers that still import the
# old gateway metadata surface directly.
_PLATFORMS: list[dict[str, object]] = [dict(platform) for platform in _BUILTIN_PLATFORMS]


def _all_platforms() -> list[dict[str, object]]:
    platforms: list[dict[str, object]] = [dict(platform) for platform in _BUILTIN_PLATFORMS]
    try:
        from gateway.platform_registry import platform_registry

        for entry in platform_registry.plugin_entries():
            platforms.append(
                {
                    "key": entry.name,
                    "label": entry.label,
                    "emoji": entry.emoji or "🔌",
                    "_registry_entry": entry,
                }
            )
    except Exception:
        pass
    return platforms


def _configure_platform(platform: dict) -> None:
    """Run the interactive setup flow for a single platform.

    Dispatch order:
      1. Plugin-provided ``setup_fn`` on the registry entry.
      2. Built-in setup function matched by platform key.
      3. ``_setup_standard_platform`` when the entry has a ``vars`` schema.
      4. Env-var hint fallback for plugins that offer no setup helper.

    Built-in retained platforms do not need a plugin enable step. User-installed
    platform plugins under ~/.hermes/plugins/
    must already be in ``plugins.enabled`` before they appear in this menu.
    """
    entry = platform.get("_registry_entry")

    if entry is not None and entry.setup_fn is not None:
        entry.setup_fn()
        return

    fn = _builtin_setup_fn(platform["key"])
    if fn is not None:
        fn()
        return

    if platform.get("vars"):
        _setup_standard_platform(platform)
        return

    # Plugin with no setup helper — show env-var instructions.
    label = platform.get("label", platform["key"])
    emoji = platform.get("emoji", "🔌")
    print()
    print(color(f"  ─── {emoji} {label} Setup ───", Colors.CYAN))
    required = entry.required_env if entry else []
    if required:
        print_info(f"  Set these env vars in ~/.hermes/.env: {', '.join(required)}")
    else:
        print_info(f"  Configure {label} in config.yaml under gateway.platforms.{platform['key']}")
    if platform.get("install_hint"):
        print_info(f"  {platform['install_hint']}")


def gateway_setup():
    """Interactive setup for messaging platforms + gateway service."""
    if is_managed():
        managed_error("run gateway setup")
        return

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.MAGENTA))
    print(color("│             ⚕ Gateway Setup                            │", Colors.MAGENTA))
    print(color("├─────────────────────────────────────────────────────────┤", Colors.MAGENTA))
    print(color("│  Configure messaging platforms and the gateway service. │", Colors.MAGENTA))
    print(color("│  Press Ctrl+C at any time to exit.                     │", Colors.MAGENTA))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.MAGENTA))

    # ── Gateway service status ──
    print()
    service_installed = _is_service_installed()
    service_running = _is_service_running()

    if supports_systemd_services() and has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()

    if supports_systemd_services() and has_legacy_hermes_units():
        print_legacy_unit_warning()
        print()

    if service_installed and service_running:
        print_success("Gateway service is installed and running.")
    elif service_installed:
        print_warning("Gateway service is installed but not running.")
        if prompt_yes_no("  Start it now?", True):
            try:
                if supports_systemd_services():
                    systemd_start()
                elif is_macos():
                    launchd_start()
            except UserSystemdUnavailableError as e:
                print_error("  Failed to start — user systemd not reachable:")
                for line in str(e).splitlines():
                    print(f"  {line}")
            except subprocess.CalledProcessError as e:
                print_error(f"  Failed to start: {e}")
    else:
        print_info("Gateway service is not installed yet.")
        print_info("You'll be offered to install it after configuring platforms.")

    # ── Platform configuration loop ──
    while True:
        print()
        print_header("Messaging Platforms")

        platforms = _all_platforms()

        menu_items = [
            f"{p['emoji']} {p['label']}  ({_platform_status(p)})"
            for p in platforms
        ]
        menu_items.append("Done")

        choice = prompt_choice("Select a platform to configure:", menu_items, len(menu_items) - 1)
        if choice == len(platforms):
            break

        _configure_platform(platforms[choice])

    # ── Post-setup: offer to install/restart gateway ──
    # Consider any platform (built-in or plugin) where the user has made
    # meaningful progress. ``_platform_status`` already handles plugin
    # entries via their check_fn and partial configuration states.
    def _is_progress(status: str) -> bool:
        s = status.lower()
        return not (
            s == "not configured"
            or s.startswith("partially")
            or s.startswith("plugin disabled")
        )

    any_configured = any(
        _is_progress(_platform_status(p)) for p in _all_platforms()
    )

    if any_configured:
        print()
        print(color("─" * 58, Colors.DIM))
        service_installed = _is_service_installed()
        service_running = _is_service_running()

        if service_running:
            if prompt_yes_no("  Restart the gateway to pick up changes?", True):
                try:
                    if supports_systemd_services():
                        systemd_restart()
                    elif is_macos():
                        launchd_restart()
                    else:
                        stop_profile_gateway()
                        print_info("Start manually: hermes gateway")
                except UserSystemdUnavailableError as e:
                    print_error("  Restart failed — user systemd not reachable:")
                    for line in str(e).splitlines():
                        print(f"  {line}")
                except subprocess.CalledProcessError as e:
                    print_error(f"  Restart failed: {e}")
        elif service_installed:
            if prompt_yes_no("  Start the gateway service?", True):
                try:
                    if supports_systemd_services():
                        systemd_start()
                    elif is_macos():
                        launchd_start()
                except UserSystemdUnavailableError as e:
                    print_error("  Start failed — user systemd not reachable:")
                    for line in str(e).splitlines():
                        print(f"  {line}")
                except subprocess.CalledProcessError as e:
                    print_error(f"  Start failed: {e}")
        else:
            print()
            if supports_systemd_services() or is_macos():
                platform_name = "systemd" if supports_systemd_services() else "launchd"
                wsl_note = " (note: services may not survive WSL restarts)" if is_wsl() else ""
                if prompt_yes_no(f"  Install the gateway as a {platform_name} service?{wsl_note} (runs in background, starts on boot)", True):
                    try:
                        installed_scope = None
                        did_install = False
                        if supports_systemd_services():
                            installed_scope, did_install = install_linux_gateway_from_setup(force=False)
                        else:
                            launchd_install(force=False)
                            did_install = True
                        print()
                        if did_install and prompt_yes_no("  Start the service now?", True):
                            try:
                                if supports_systemd_services():
                                    systemd_start(system=installed_scope == "system")
                                else:
                                    launchd_start()
                            except UserSystemdUnavailableError as e:
                                print_error("  Start failed — user systemd not reachable:")
                                for line in str(e).splitlines():
                                    print(f"  {line}")
                            except subprocess.CalledProcessError as e:
                                print_error(f"  Start failed: {e}")
                    except subprocess.CalledProcessError as e:
                        print_error(f"  Install failed: {e}")
                        print_info("  You can try manually: hermes gateway install")
                else:
                    print_info("  You can install later: hermes gateway install")
                    if supports_systemd_services():
                        print_info("  Or as a boot-time service: sudo hermes gateway install --system")
                    print_info("  Or run in foreground:  hermes gateway run")
            elif is_wsl():
                print_info("  WSL detected but systemd is not running.")
                print_info("  Run in foreground: hermes gateway run")
                print_info("  For persistence:   tmux new -s hermes 'hermes gateway run'")
                print_info("  To enable systemd: add systemd=true to /etc/wsl.conf, then 'wsl --shutdown'")
            else:
                if is_termux():
                    from hermes_constants import display_hermes_home as _dhh
                    print_info("  Termux does not use systemd/launchd services.")
                    print_info("  Run in foreground: hermes gateway run")
                    print_info(f"  Or start it manually in the background (best effort): nohup hermes gateway run >{_dhh()}/logs/gateway.log 2>&1 &")
                else:
                    print_info("  Service install not supported on this platform.")
                    print_info("  Run in foreground: hermes gateway run")
    else:
        print()
        print_info("No platforms configured. Run 'hermes gateway setup' when ready.")

    print()


# =============================================================================
# Main Command Handler
# =============================================================================

def gateway_command(args):
    """Handle gateway subcommands."""
    try:
        return _gateway_command_inner(args)
    except UserSystemdUnavailableError as e:
        # Clean, actionable message instead of a traceback when the user D-Bus
        # session is unreachable (fresh SSH shell, no linger, container, etc.).
        print_error("User systemd not reachable:")
        for line in str(e).splitlines():
            print(f"  {line}")
        sys.exit(1)


def _gateway_command_inner(args):
    subcmd = getattr(args, 'gateway_command', None)
    
    # Default to run if no subcommand
    if subcmd is None or subcmd == "run":
        verbose = getattr(args, 'verbose', 0)
        quiet = getattr(args, 'quiet', False)
        replace = getattr(args, 'replace', False)
        run_gateway(verbose, quiet=quiet, replace=replace)
        return

    if subcmd == "setup":
        gateway_setup()
        return

    # Service management commands
    if subcmd == "install":
        if is_managed():
            managed_error("install gateway service (managed by NixOS)")
            return
        force = getattr(args, 'force', False)
        system = getattr(args, 'system', False)
        run_as_user = getattr(args, 'run_as_user', None)
        if is_termux():
            print("Gateway service installation is not supported on Termux.")
            print("Run manually: hermes gateway")
            sys.exit(1)
        if supports_systemd_services():
            if is_wsl():
                print_warning("WSL detected — systemd services may not survive WSL restarts.")
                print_info("  Consider running in foreground instead: hermes gateway run")
                print_info("  Or use tmux/screen for persistence: tmux new -s hermes 'hermes gateway run'")
                print()
            systemd_install(force=force, system=system, run_as_user=run_as_user)
        elif is_macos():
            launchd_install(force)
        elif is_wsl():
            print("WSL detected but systemd is not running.")
            print("Either enable systemd (add systemd=true to /etc/wsl.conf and restart WSL)")
            print("or run the gateway in foreground mode:")
            print()
            print("  hermes gateway run                              # direct foreground")
            print("  tmux new -s hermes 'hermes gateway run'         # persistent via tmux")
            print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background")
            sys.exit(1)
        elif is_container():
            print("Service installation is not needed inside a Docker container.")
            print("The container runtime is your service manager — use Docker restart policies instead:")
            print()
            print("  docker run --restart unless-stopped ...   # auto-restart on crash/reboot")
            print("  docker restart <container>                # manual restart")
            print()
            print("To run the gateway: hermes gateway run")
            sys.exit(0)
        else:
            print("Service installation not supported on this platform.")
            print("Run manually: hermes gateway run")
            sys.exit(1)
    
    elif subcmd == "uninstall":
        if is_managed():
            managed_error("uninstall gateway service (managed by NixOS)")
            return
        system = getattr(args, 'system', False)
        if is_termux():
            print("Gateway service uninstall is not supported on Termux because there is no managed service to remove.")
            print("Stop manual runs with: hermes gateway stop")
            sys.exit(1)
        if supports_systemd_services():
            systemd_uninstall(system=system)
        elif is_macos():
            launchd_uninstall()
        elif is_container():
            print("Service uninstall is not applicable inside a Docker container.")
            print("To stop the gateway, stop or remove the container:")
            print()
            print("  docker stop <container>")
            print("  docker rm <container>")
            sys.exit(0)
        else:
            print("Not supported on this platform.")
            sys.exit(1)

    elif subcmd == "start":
        system = getattr(args, 'system', False)
        start_all = getattr(args, 'all', False)

        if start_all:
            # Kill all stale gateway processes across all profiles before starting
            killed = kill_gateway_processes(all_profiles=True)
            if killed:
                print(f"✓ Killed {killed} stale gateway process(es) across all profiles")
                _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

        if is_termux():
            print("Gateway service start is not supported on Termux because there is no system service manager.")
            print("Run manually: hermes gateway")
            sys.exit(1)
        if supports_systemd_services():
            systemd_start(system=system)
        elif is_macos():
            launchd_start()
        elif is_wsl():
            print("WSL detected but systemd is not available.")
            print("Run the gateway in foreground mode instead:")
            print()
            print("  hermes gateway run                              # direct foreground")
            print("  tmux new -s hermes 'hermes gateway run'         # persistent via tmux")
            print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background")
            print()
            print("To enable systemd: add systemd=true to /etc/wsl.conf and run 'wsl --shutdown' from PowerShell.")
            sys.exit(1)
        elif is_container():
            print("Service start is not applicable inside a Docker container.")
            print("The gateway runs as the container's main process.")
            print()
            print("  docker start <container>     # start a stopped container")
            print("  docker restart <container>   # restart a running container")
            print()
            print("Or run the gateway directly: hermes gateway run")
            sys.exit(0)
        else:
            print("Not supported on this platform.")
            sys.exit(1)

    elif subcmd == "stop":
        stop_all = getattr(args, 'all', False)
        system = getattr(args, 'system', False)

        if stop_all:
            # --all: kill every gateway process on the machine
            service_available = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            killed = kill_gateway_processes(all_profiles=True)
            total = killed + (1 if service_available else 0)
            if total:
                print(f"✓ Stopped {total} gateway process(es) across all profiles")
            else:
                print("✗ No gateway processes found")
        else:
            # Default: stop only the current profile's gateway
            service_available = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_available = True
                except subprocess.CalledProcessError:
                    pass

            if not service_available:
                # No systemd/launchd — use profile-scoped PID file
                if stop_profile_gateway():
                    print("✓ Stopped gateway for this profile")
                else:
                    print("✗ No gateway running for this profile")
            else:
                print(f"✓ Stopped {get_service_name()} service")
    
    elif subcmd == "restart":
        # Try service first, fall back to killing and restarting
        service_available = False
        system = getattr(args, 'system', False)
        restart_all = getattr(args, 'all', False)
        service_configured = False

        if restart_all:
            # --all: stop every gateway process across all profiles, then start fresh
            service_stopped = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_stopped = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_stopped = True
                except subprocess.CalledProcessError:
                    pass
            killed = kill_gateway_processes(all_profiles=True)
            total = killed + (1 if service_stopped else 0)
            if total:
                print(f"✓ Stopped {total} gateway process(es) across all profiles")
            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

            # Start the current profile's service fresh
            print("Starting gateway...")
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                systemd_start(system=system)
            elif is_macos() and get_launchd_plist_path().exists():
                launchd_start()
            else:
                run_gateway(verbose=0)
            return
        
        if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            service_configured = True
            try:
                systemd_restart(system=system)
                service_available = True
            except subprocess.CalledProcessError:
                pass
        elif is_macos() and get_launchd_plist_path().exists():
            service_configured = True
            try:
                launchd_restart()
                service_available = True
            except subprocess.CalledProcessError:
                pass
        
        if not service_available:
            # systemd/launchd restart failed — check if linger is the issue
            if supports_systemd_services():
                linger_ok, _detail = get_systemd_linger_status()
                if linger_ok is not True:
                    import getpass
                    _username = getpass.getuser()
                    print()
                    print("⚠ Cannot restart gateway as a service — linger is not enabled.")
                    print("  The gateway user service requires linger to function on headless servers.")
                    print()
                    print(f"  Run:  sudo loginctl enable-linger {_username}")
                    print()
                    print("  Then restart the gateway:")
                    print("    hermes gateway restart")
                    return

            if service_configured:
                print()
                print("✗ Gateway service restart failed.")
                print("  The service definition exists, but the service manager did not recover it.")
                print("  Fix the service, then retry: hermes gateway start")
                sys.exit(1)

            # Manual restart: stop only this profile's gateway
            if stop_profile_gateway():
                print("✓ Stopped gateway for this profile")

            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

            # Start fresh
            print("Starting gateway...")
            run_gateway(verbose=0)
    
    elif subcmd == "status":
        deep = getattr(args, 'deep', False)
        full = getattr(args, 'full', False)
        system = getattr(args, 'system', False)
        snapshot = get_gateway_runtime_snapshot(system=system)
        
        # Check for service first
        if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            systemd_status(deep, system=system, full=full)
            _print_gateway_process_mismatch(snapshot)
        elif is_macos() and get_launchd_plist_path().exists():
            launchd_status(deep)
            _print_gateway_process_mismatch(snapshot)
        else:
            # Check for manually running processes
            pids = list(snapshot.gateway_pids)
            if pids:
                print(f"✓ Gateway is running (PID: {', '.join(map(str, pids))})")
                print("  (Running manually, not as a system service)")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                if is_termux():
                    print("Termux note:")
                    print("  Android may stop background jobs when Termux is suspended")
                elif is_wsl():
                    print("WSL note:")
                    print("  The gateway is running in foreground/manual mode (recommended for WSL).")
                    print("  Use tmux or screen for persistence across terminal closes.")
                else:
                    print("To install as a service:")
                    print("  hermes gateway install")
                    print("  sudo hermes gateway install --system")
            else:
                print("✗ Gateway is not running")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                print("To start:")
                print("  hermes gateway run      # Run in foreground")
                if is_termux():
                    print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # Best-effort background start")
                elif is_wsl():
                    print("  tmux new -s hermes 'hermes gateway run'         # persistent via tmux")
                    print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background")
                else:
                    print("  hermes gateway install  # Install as user service")
                    print("  sudo hermes gateway install --system  # Install as boot-time system service")

    elif subcmd == "migrate-legacy":
        # Stop, disable, and remove legacy Hermes gateway unit files from
        # pre-rename installs (e.g. hermes.service). Profile units and
        # unrelated third-party services are never touched.
        dry_run = getattr(args, 'dry_run', False)
        yes = getattr(args, 'yes', False)
        if not supports_systemd_services() and not is_macos():
            print("Legacy unit migration only applies to systemd-based Linux hosts.")
            return
        remove_legacy_hermes_units(interactive=not yes, dry_run=dry_run)
