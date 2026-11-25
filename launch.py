import argparse
import csv
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import asar
import install

HOME = Path(__file__).resolve().parent
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", ""))
STATE_DIR = LOCAL_APP_DATA / "AntigravityZh"
APP = LOCAL_APP_DATA / "Programs" / "Antigravity" / "Antigravity.exe"
PRELOAD = "dist/preload.js"
UPDATER = "dist/updater.js"
BASE_UPDATER = "node_modules/electron-updater/out/BaseUpdater.js"
UPDATE_HOOK = "AG_ZH_RESTART_AFTER_UPDATE"
UPDATE_CALL = "electron_updater_1.autoUpdater.quitAndInstall(isSilent, true);"
MANAGED_UPDATE_CALL = "quitAndInstallThroughChineseLauncher(updater, isSilent);"
VERSION_PATTERN = r"\d+\.\d+\.\d+(?:[-+][\w.+-]+)?"


def archive_files(header, prefix=""):
    for name, entry in header["files"].items():
        if name in ("", ".", "..") or any(character in name for character in "/\\:"):
            raise ValueError(f"ASAR 文件名无效：{name!r}")
        path = prefix + name
        if "files" in entry:
            yield from archive_files(entry, path + "/")
        elif "link" in entry:
            raise ValueError("新版 ASAR 包含链接条目，需要检查兼容性。")
        else:
            yield path, entry


def read_archive_entry(archive, entry, pickle_size):
    if entry.get("unpacked"):
        raise ValueError("新版客户端的预加载文件布局已改变。")
    with archive.open("rb") as stream:
        stream.seek(asar._data_offset(pickle_size, entry["offset"]))
        data = stream.read(entry["size"])
    if len(data) != entry["size"]:
        raise ValueError("ASAR 文件未完整写入，请等客户端更新完成。")
    return data


def running_images(image_names):
    filters = [] if len(image_names) != 1 else ["/FI", f"IMAGENAME eq {next(iter(image_names))}"]
    result = subprocess.run(
        ["tasklist.exe", *filters, "/FO", "CSV", "/NH"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    wanted = {name.casefold() for name in image_names}
    rows = csv.reader(result.stdout.decode("mbcs", errors="replace").splitlines())
    return {row[0].casefold() for row in rows if len(row) > 1 and row[0].casefold() in wanted}


def antigravity_is_running():
    return bool(running_images({APP.name}))


def package_version(archive, header, pickle_size):
    entry = header["files"]["package.json"]
    package = json.loads(read_archive_entry(archive, entry, pickle_size))
    version = package.get("version", "")
    if package.get("name") != "antigravity" or package.get("main") != "dist/main.js" or not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError("新版客户端的应用包结构与汉化脚本不兼容。")
    return version


def restart_patch(archive, files, pickle_size):
    if not {UPDATER, BASE_UPDATER} <= files.keys():
        return None
    source = read_archive_entry(archive, files[UPDATER], pickle_size).decode("utf-8").replace("\r\n", "\n")
    original = source
    if UPDATE_HOOK in source:
        previous_source, marker, previous_hook = source.rpartition(f"\n// {UPDATE_HOOK}\n")
        if not marker or not previous_hook.startswith("function quitAndInstallThroughChineseLauncher("):
            return None
        original = previous_source.replace(MANAGED_UPDATE_CALL, UPDATE_CALL)

    base = read_archive_entry(archive, files[BASE_UPDATER], pickle_size)
    signature = b"this.install(isSilent, isSilent ? isForceRunAfter : this.autoRunAppAfterInstall)"
    if original.count(UPDATE_CALL) != 2 or signature not in base:
        return None

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    replacements = {
        "__PYTHONW__": json.dumps(str(pythonw)),
        "__LAUNCHER__": json.dumps(str(HOME / "launch.py")),
    }
    hook = r"""
// AG_ZH_RESTART_AFTER_UPDATE
function quitAndInstallThroughChineseLauncher(updater, isSilent) {
    const pythonw = __PYTHONW__;
    const launcher = __LAUNCHER__;
    const oldVersion = require("electron").app.getVersion();
    const targetVersion = updater.downloadedUpdateHelper?.versionInfo?.version;
    const installer = updater.installerPath;
    const originalRestart = () => updater.quitAndInstall(isSilent, true);
    if (process.platform !== "win32" || !installer || !targetVersion || targetVersion === oldVersion) {
        return originalRestart();
    }
    const installerName = require("path").basename(installer);
    if (!/^[A-Za-z0-9_. -]+[.]exe$/i.test(installerName)) {
        return originalRestart();
    }
    const { existsSync } = require("fs");
    if (!existsSync(pythonw) || !existsSync(launcher)) {
        return originalRestart();
    }

    let helper;
    try {
        helper = require("child_process").spawn(
            pythonw, [launcher, "--after-update", oldVersion, targetVersion, installerName],
            { detached: true, windowsHide: true, stdio: "ignore" }
        );
        helper.on("error", error => console.error("[Chinese launcher] " + error.message));
        if (!helper.pid) return originalRestart();
        helper.unref();
    } catch (error) {
        console.error("[Chinese launcher] " + error.message);
        return originalRestart();
    }

    try {
        // An interactive NSIS installer can still start the app on completion
        // without --force-run. Use the official silent mode for this hand-off.
        updater.quitAndInstall(true, false);
    } finally {
        if (!updater.quitAndInstallCalled) helper.kill();
    }
}
"""
    for placeholder, value in replacements.items():
        hook = hook.replace(placeholder, value)
    patched = original.replace(UPDATE_CALL, MANAGED_UPDATE_CALL) + hook
    return patched if patched != source else None


def apply_translation(archive, check_only):
    header, _, pickle_size = asar._read_header(archive)
    files = dict(archive_files(header))
    version = package_version(archive, header, pickle_size)

    preload = read_archive_entry(archive, files[PRELOAD], pickle_size)
    dictionary = install.load_dictionary()
    injection = install.build_injection(dictionary).encode("utf-8")
    already_translated = injection in preload.replace(b"\r\n", b"\n")
    updater = restart_patch(archive, files, pickle_size)
    if already_translated and updater is None:
        print(f"Antigravity {version} 已是当前汉化版。")
        return
    if check_only:
        print(f"Antigravity {version} 需要更新汉化资源或自动重启入口。")
        return
    if antigravity_is_running():
        raise RuntimeError("请先完全退出 Antigravity（包括系统托盘），再打开汉化启动入口。")

    unpacked = {path for path, entry in files.items() if entry.get("unpacked")}
    expected_unpacked = {path for path in files if asar._match_unpack(path, install.UNPACK_PATTERNS)}
    unpacked_directory = Path(str(archive) + ".unpacked")
    unpacked_on_disk = {path.relative_to(unpacked_directory).as_posix() for path in unpacked_directory.rglob("*") if path.is_file()}
    if unpacked != expected_unpacked or unpacked != unpacked_on_disk or b"ipcRenderer" not in preload:
        raise ValueError("新版客户端资源布局已改变，请先检查兼容性。")

    original_sha = install.sha256(archive)
    if not any(marker.encode("utf-8") in preload for marker in [install.TAG, *install.OLD_TAGS]):
        backup = STATE_DIR / "backups" / version / f"app-{original_sha[:16]}.asar"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            with tempfile.TemporaryDirectory(dir=backup.parent) as temp:
                staged_backup = Path(temp) / "original.asar"
                shutil.copy2(archive, staged_backup)
                if install.sha256(staged_backup) != original_sha:
                    raise RuntimeError("客户端仍在更新，请稍后重试。")
                os.replace(staged_backup, backup)
        if install.sha256(backup) != original_sha:
            raise ValueError(f"原版备份校验失败：{backup}")

    with tempfile.TemporaryDirectory(prefix="agy-zh-") as temp:
        staging = Path(temp) / "contents"
        asar.extract(str(archive), str(staging))
        if not already_translated:
            install.patch_file(staging / PRELOAD, dictionary)
            wizard = staging / "dist/ideInstall/wizardPreload.js"
            if wizard.is_file():
                install.patch_file(wizard, dictionary)
            install.patch_native_menu(staging)
            install.patch_tray(staging)
            install.patch_quit_dialog(staging)
            install.patch_loading_overlay(staging)
        if updater is not None:
            (staging / UPDATER).write_text(updater, encoding="utf-8", newline="\r\n")
        patched = Path(temp) / "app.asar"
        asar.pack(str(staging), str(patched), install.UNPACK_PATTERNS)
        patched_header, _, patched_pickle = asar._read_header(patched)
        patched_files = dict(archive_files(patched_header))
        patched_preload = read_archive_entry(patched, patched_files[PRELOAD], patched_pickle)
        if files.keys() != patched_files.keys() or injection not in patched_preload.replace(b"\r\n", b"\n"):
            raise ValueError("新汉化包没有通过校验，原版客户端仍可使用。")
        if updater is not None:
            actual = read_archive_entry(patched, patched_files[UPDATER], patched_pickle)
            if actual.decode("utf-8").replace("\r\n", "\n") != updater:
                raise ValueError("自动重启入口没有通过校验。")

        handle, name = tempfile.mkstemp(prefix="app-zh-", suffix=".stage", dir=archive.parent)
        os.close(handle)
        stage = Path(name)
        try:
            shutil.copy2(patched, stage)
            if antigravity_is_running() or install.sha256(archive) != original_sha:
                raise RuntimeError("客户端已启动或仍在更新，请退出后重试。")
            os.replace(stage, archive)
        finally:
            stage.unlink(missing_ok=True)
    print(f"已重新应用 Antigravity {version} 的汉化。")
    if updater is not None:
        print("已接入客户端更新后的汉化重启。")


def installed_version(archive):
    header, _, pickle_size = asar._read_header(archive)
    return package_version(archive, header, pickle_size)


def wait_for_update(archive, old_version, target_version, installer_name):
    if (
        not re.fullmatch(VERSION_PATTERN, old_version)
        or not re.fullmatch(VERSION_PATTERN, target_version)
        or target_version == old_version
        or not re.fullmatch(r"[A-Za-z0-9_. -]+[.]exe", installer_name, flags=re.IGNORECASE)
    ):
        raise ValueError("客户端更新参数无法识别。")

    deadline = time.monotonic() + 600
    snapshot = None
    stable_since = 0.0
    while time.monotonic() < deadline:
        candidate = None
        if not running_images({APP.name, installer_name}):
            try:
                if installed_version(archive) == target_version:
                    bundle = archive.stat()
                    executable = APP.stat()
                    candidate = (bundle.st_size, bundle.st_mtime_ns, executable.st_size, executable.st_mtime_ns)
            except (KeyError, OSError, ValueError):
                pass
        now = time.monotonic()
        if candidate != snapshot:
            snapshot = candidate
            stable_since = now
        if snapshot is not None and now - stable_since >= 3:
            return
        time.sleep(1)
    raise RuntimeError(f"未确认 {target_version} 安装完成。请退出 Antigravity 后，用桌面快捷方式打开。")


def start_antigravity():
    subprocess.Popen(
        [str(APP)],
        cwd=str(APP.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS,
    )


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查汉化状态")
    mode.add_argument("--repair-only", action="store_true", help="检查并应用汉化，随后退出")
    mode.add_argument("--after-update", nargs=3, metavar=("OLD_VERSION", "TARGET_VERSION", "INSTALLER"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32" or not os.environ.get("LOCALAPPDATA"):
        parser.error("自动恢复汉化启动器适用于 Windows 当前用户安装。其他系统请运行 install.py。")
    import msvcrt

    if not args.after_update and not APP.is_file():
        raise FileNotFoundError(f"找不到 Antigravity：{APP}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "launch.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("另一个汉化启动入口正在运行。") from error
        archive = APP.parent / "resources" / "app.asar"
        if args.after_update:
            wait_for_update(archive, *args.after_update)
        try:
            apply_translation(archive, args.check)
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            if (
                args.after_update
                and not antigravity_is_running()
                and installed_version(archive) == args.after_update[1]
            ):
                start_antigravity()
                raise RuntimeError(f"新版已打开，但汉化未应用：{error}") from error
            raise
        if not args.check and not args.repair_only:
            if not args.after_update or not antigravity_is_running():
                start_antigravity()


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        message = f"启动检查失败：{error}"
        if sys.stderr is None:
            from ctypes import windll

            windll.user32.MessageBoxW(None, message, "Antigravity 汉化启动", 0x10)
        else:
            print(message, file=sys.stderr)
        sys.exit(1)
