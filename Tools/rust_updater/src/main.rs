use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use clap::Parser;
use zip::ZipArchive;

const MAX_WAIT_SECONDS: u64 = 300;

#[derive(Parser, Debug)]
#[clap(author, version, about = "AiNiee 前后端更新器")]
struct Args {
    #[clap(index = 1)]
    src: PathBuf,
    #[clap(index = 2)]
    dst: PathBuf,
    /// Arguments passed unchanged to the updated desktop (including backend URL).
    #[clap(long)]
    restart_arg: Vec<String>,
    /// PID of the desktop that launched this helper.
    #[clap(long)]
    wait_pid: Option<u32>,
    /// Run a verified Windows setup executable instead of unpacking a ZIP.
    #[clap(long)]
    installer: bool,
}

fn log(dst: &Path, message: &str) {
    eprintln!("{message}");
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(dst.join("update.log")) {
        let _ = writeln!(file, "{message}");
    }
}

fn unique_name(prefix: &str) -> String {
    let time = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
    format!("{prefix}-{}-{time}", std::process::id())
}

fn command(executable: &Path) -> Command {
    let mut command = Command::new(executable);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    command
}

fn relaunch_from_temp(args: &Args) -> Result<bool> {
    let current = env::current_exe()?.canonicalize()?;
    if !current.starts_with(&args.dst) {
        return Ok(false);
    }
    let temp = env::temp_dir().join("AiNieeUpdater");
    fs::create_dir_all(&temp)?;
    let executable = temp.join(format!("{}.exe", unique_name("updater")));
    fs::copy(current, &executable)?;
    let mut child = command(&executable);
    child.arg(&args.src).arg(&args.dst).current_dir(&args.dst);
    for arg in &args.restart_arg {
        child.arg(format!("--restart-arg={arg}"));
    }
    if let Some(pid) = args.wait_pid {
        child.arg(format!("--wait-pid={pid}"));
    }
    if args.installer {
        child.arg("--installer");
    }
    child.spawn().context("无法启动临时更新器")?;
    Ok(true)
}

fn extract_zip(src: &Path, dst: &Path) -> Result<()> {
    let mut archive = ZipArchive::new(File::open(src)?)?;
    for index in 0..archive.len() {
        let mut file = archive.by_index(index)?;
        let name = file.name().replace('\\', "/");
        if name.starts_with('/') || name.split('/').any(|part| part == ".." || part.contains(':')) {
            bail!("更新包包含不安全路径: {name}");
        }
        if file.unix_mode().map(|mode| mode & 0o170000 == 0o120000).unwrap_or(false) {
            bail!("更新包不能包含符号链接: {name}");
        }
        let output = dst.join(file.enclosed_name().context("更新包路径无效")?);
        if file.is_dir() {
            fs::create_dir_all(output)?;
        } else {
            fs::create_dir_all(output.parent().context("更新文件缺少父目录")?)?;
            let mut target = OpenOptions::new().write(true).create_new(true).open(output)?;
            io::copy(&mut file, &mut target).context("更新包损坏，解压校验失败")?;
        }
    }
    Ok(())
}

fn find_release(dir: &Path) -> Result<PathBuf> {
    fn collect(dir: &Path, found: &mut Vec<PathBuf>) -> Result<()> {
        if dir.join("AiNiee.exe").is_file() {
            found.push(dir.to_path_buf());
        }
        for entry in fs::read_dir(dir)? {
            let path = entry?.path();
            if path.is_dir() {
                collect(&path, found)?;
            }
        }
        Ok(())
    }
    let mut found = Vec::new();
    collect(dir, &mut found)?;
    if found.len() != 1 {
        bail!("更新包必须包含唯一的 AiNiee.exe");
    }
    let root = found.remove(0);
    if !root.join("backend/AiNieeBackend.exe").is_file()
        || !root.join("_internal").is_dir()
        || !root.join("backend/_internal").is_dir()
    {
        bail!("更新包缺少配套前端或后端，拒绝安装不完整的发布包");
    }
    Ok(root)
}

fn protected(relative: &Path) -> bool {
    let first = relative.components().next().unwrap().as_os_str().to_string_lossy().to_lowercase();
    matches!(first.as_str(), "data" | "projectcache" | "logs" | "downloads" | "portable.txt" | "update.log")
}

fn release_files(root: &Path, relative: &Path, files: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(root.join(relative))? {
        let path = relative.join(entry?.file_name());
        if protected(&path) {
            continue;
        }
        if root.join(&path).is_dir() {
            release_files(root, &path, files)?;
        } else {
            files.push(path);
        }
    }
    Ok(())
}

fn is_file_locked(path: &Path) -> bool {
    path.exists() && OpenOptions::new().read(true).write(true).open(path).is_err()
}

#[cfg(windows)]
fn wait_for_desktop(pid: Option<u32>) -> Result<()> {
    use std::ffi::c_void;
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn WaitForSingleObject(handle: *mut c_void, millis: u32) -> u32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }
    let Some(pid) = pid else { return Ok(()); };
    let handle = unsafe { OpenProcess(0x00100000, 0, pid) }; // SYNCHRONIZE
    if handle.is_null() {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(87) { // ERROR_INVALID_PARAMETER: already exited
            return Ok(());
        }
        return Err(error).context("无法等待旧版 AiNiee 退出");
    }
    let status = unsafe { WaitForSingleObject(handle, MAX_WAIT_SECONDS as u32 * 1000) };
    let error = io::Error::last_os_error();
    unsafe { CloseHandle(handle) };
    match status {
        0 => Ok(()),
        258 => bail!("等待旧版 AiNiee 退出超时"),
        _ => Err(error).context("等待旧版 AiNiee 退出失败"),
    }
}

#[cfg(not(windows))]
fn wait_for_desktop(_: Option<u32>) -> Result<()> {
    Ok(())
}

fn setup_args(dst: &Path) -> Vec<std::ffi::OsString> {
    let mut directory = std::ffi::OsString::from("/DIR=");
    directory.push(dst.as_os_str());
    let mut log_path = std::ffi::OsString::from("/LOG=");
    log_path.push(dst.join("setup-update.log").as_os_str());
    vec![
        "/SP-".into(), "/SILENT".into(), "/SUPPRESSMSGBOXES".into(),
        "/NORESTART".into(), "/RESTARTEXITCODE=3010".into(), "/NOCLOSEAPPLICATIONS".into(),
        directory, log_path,
    ]
}

fn run_setup(args: &Args) -> Result<()> {
    wait_for_exit(&args.dst, &[], Duration::from_secs(MAX_WAIT_SECONDS))?;
    // Inno Setup compares /DIR to its existing install path; pass a normal
    // Windows path rather than the extended path returned by canonicalize().
    let dst_text = args.dst.to_string_lossy();
    let dst = Path::new(dst_text.strip_prefix(r"\\?\").unwrap_or(&dst_text));
    let result = command(&args.src).args(setup_args(dst)).current_dir(&args.dst)
        .status().context("无法启动 Windows 安装程序")?;
    if !result.success() {
        bail!("安装程序未完成（{result}），请查看 setup-update.log");
    }
    Ok(())
}

fn wait_for_exit(dst: &Path, files: &[PathBuf], timeout: Duration) -> Result<()> {
    let start = Instant::now();
    loop {
        let fixed = [PathBuf::from("AiNiee.exe"), PathBuf::from("backend/AiNieeBackend.exe")];
        let locked = fixed.iter().chain(files).find(|file| is_file_locked(&dst.join(file)));
        match locked {
            None => return Ok(()),
            Some(file) if start.elapsed() >= timeout => bail!("等待前后端退出超时，文件仍被占用: {}", file.display()),
            Some(file) => {
                if start.elapsed().as_secs() % 10 == 0 {
                    log(dst, &format!("等待前后端退出并保存数据: {}", file.display()));
                }
                sleep(Duration::from_secs(1));
            }
        }
    }
}

fn install_files(src: &Path, dst: &Path, backup: &Path, files: &[PathBuf]) -> Result<()> {
    // Complete all backups before the first replacement.
    for relative in files {
        let target = dst.join(relative);
        let mut ancestor = target.clone();
        while ancestor != dst {
            if let Ok(metadata) = fs::symlink_metadata(&ancestor) {
                if metadata.file_type().is_symlink() {
                    bail!("安装目录包含符号链接，拒绝覆盖: {}", ancestor.display());
                }
            }
            if !ancestor.pop() {
                bail!("安装路径不在目标目录内");
            }
        }
        if target.exists() {
            let saved = backup.join(relative);
            fs::create_dir_all(saved.parent().unwrap())?;
            fs::copy(target, saved).context("备份原文件失败")?;
        }
    }
    let mut changed = Vec::new();
    let result = (|| -> Result<()> {
        for relative in files {
            let target = dst.join(relative);
            fs::create_dir_all(target.parent().unwrap())?;
            changed.push(relative); // Include partially written files in rollback.
            fs::copy(src.join(relative), target).with_context(|| format!("替换失败: {}", relative.display()))?;
        }
        Ok(())
    })();
    if let Err(error) = result {
        let mut failures = Vec::new();
        for relative in changed.into_iter().rev() {
            let saved = backup.join(relative);
            let target = dst.join(relative);
            let restored = if saved.exists() {
                fs::copy(saved, target).map(|_| ())
            } else if target.exists() {
                fs::remove_file(target)
            } else {
                Ok(())
            };
            if let Err(failure) = restored {
                failures.push(format!("{}: {failure}", relative.display()));
            }
        }
        if !failures.is_empty() {
            bail!("{error:#}; 回滚未完成，原文件备份位于 {}: {}", backup.display(), failures.join(", "));
        }
        return Err(error.context("更新失败，已恢复原程序；更新包已保留"));
    }
    Ok(())
}

fn apply_update(args: &Args) -> Result<()> {
    let staging = args.dst.join(unique_name(".ainiee-update"));
    fs::create_dir(&staging)?;
    let extracted = staging.join("package");
    fs::create_dir(&extracted)?;
    // Failures retain staging/backups for diagnosis and manual recovery.
    extract_zip(&args.src, &extracted)?;
    let root = find_release(&extracted)?;
    let mut files = Vec::new();
    release_files(&root, Path::new(""), &mut files)?;
    wait_for_exit(&args.dst, &files, Duration::from_secs(MAX_WAIT_SECONDS))?;
    install_files(&root, &args.dst, &staging.join("backup"), &files)?;
    // Only remove our unique directory directly inside the verified install root.
    let resolved = staging.canonicalize()?;
    if resolved.parent() == Some(args.dst.as_path()) {
        if let Err(error) = fs::remove_dir_all(&resolved) {
            log(&args.dst, &format!("更新成功，临时目录清理失败: {error}"));
        }
    }
    Ok(())
}

fn run(mut args: Args) -> Result<()> {
    args.dst = args.dst.canonicalize().context("安装目录不存在")?;
    if !args.src.is_absolute() {
        args.src = args.dst.join(&args.src);
    }
    args.src = args.src.canonicalize().context("更新包不存在")?;
    if !args.dst.join("AiNiee.exe").is_file() {
        bail!("目标目录不是 AiNiee 安装目录");
    }
    if relaunch_from_temp(&args)? {
        return Ok(());
    }
    log(&args.dst, "开始更新前端和后端");
    let installation = wait_for_desktop(args.wait_pid).and_then(|()| {
        if args.installer { run_setup(&args) } else { apply_update(&args) }
    });
    if let Err(error) = installation {
        log(&args.dst, &format!("更新失败，保留更新包，请查看此日志并重试: {error:#}"));
        return Err(error);
    }
    log(&args.dst, "前端和后端更新成功，正在重新启动");
    let mut restart = command(&args.dst.join("AiNiee.exe"));
    restart.args(&args.restart_arg).current_dir(&args.dst).env("PYINSTALLER_RESET_ENVIRONMENT", "1");
    for (name, _) in env::vars_os() {
        if name.to_string_lossy().starts_with("_PYI_") {
            restart.env_remove(name);
        }
    }
    restart.spawn().context("文件更新成功，但重新启动失败，请手动启动 AiNiee.exe")?;
    if let Err(error) = fs::remove_file(&args.src) {
        log(&args.dst, &format!("无法清理已安装的更新包: {error}"));
    }
    Ok(())
}

fn main() -> Result<()> {
    let args = Args::parse();
    let dst = args.dst.clone();
    let result = run(args);
    if let Err(error) = &result {
        log(&dst, &format!("{error:#}"));
        #[cfg(windows)]
        if env::var_os("CI").is_none() {
            show_failure(&format!("{error:#}\n\n日志：{}", dst.join("update.log").display()));
        }
    }
    result
}

#[cfg(windows)]
fn show_failure(message: &str) {
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(window: *mut std::ffi::c_void, text: *const u16,
                       caption: *const u16, flags: u32) -> i32;
    }
    let text: Vec<u16> = message.encode_utf16().chain(Some(0)).collect();
    let caption: Vec<u16> = "AiNieePlus 更新失败".encode_utf16().chain(Some(0)).collect();
    unsafe { MessageBoxW(std::ptr::null_mut(), text.as_ptr(), caption.as_ptr(), 0x10); }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn copy_failure_restores_both_components() -> Result<()> {
        let root = env::temp_dir().join(unique_name("ainiee-rollback-test"));
        let source = root.join("source");
        let target = root.join("target");
        fs::create_dir_all(source.join("backend"))?;
        fs::create_dir_all(target.join("backend"))?;
        fs::write(source.join("AiNiee.exe"), b"new frontend")?;
        fs::write(target.join("AiNiee.exe"), b"old frontend")?;
        fs::write(target.join("backend/AiNieeBackend.exe"), b"old backend")?;
        let files = vec![PathBuf::from("AiNiee.exe"), PathBuf::from("backend/AiNieeBackend.exe")];
        assert!(install_files(&source, &target, &root.join("backup"), &files).is_err());
        assert_eq!(fs::read(target.join("AiNiee.exe"))?, b"old frontend");
        assert_eq!(fs::read(target.join("backend/AiNieeBackend.exe"))?, b"old backend");
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn release_does_not_replace_user_state() {
        for path in ["Data/config.json", "ProjectCache/project.json", "Logs/test.log", "downloads/package.zip", "portable.txt"] {
            assert!(protected(Path::new(path)));
        }
        assert!(!protected(Path::new("backend/_internal/Backend/resources/default.json")));
    }

    #[test]
    fn old_single_process_package_is_rejected() -> Result<()> {
        let root = env::temp_dir().join(unique_name("ainiee-package-test"));
        fs::create_dir(&root)?;
        fs::write(root.join("AiNiee.exe"), b"old release")?;
        assert!(find_release(&root).is_err());
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn setup_uses_current_directory_and_disables_system_restart() {
        let path = Path::new(r"C:\Users\Test User\AiNieePlus");
        let args = setup_args(path);
        assert!(args.contains(&"/NORESTART".into()));
        assert!(args.contains(&"/NOCLOSEAPPLICATIONS".into()));
        assert!(args.contains(&r"/DIR=C:\Users\Test User\AiNieePlus".into()));
    }
}
