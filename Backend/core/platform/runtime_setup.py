from Backend.core.paths import (
    cache_root,
    default_input_dir,
    default_output_dir,
    executable_root,
    project_cache_root,
    user_data_root,
    user_log_dir,
)
from Backend.core.platform.platform_paths import is_macos


def ensure_user_dirs() -> None:
    user_data_root().mkdir(parents=True, exist_ok=True)
    cache_root().mkdir(parents=True, exist_ok=True)
    project_cache_root().mkdir(parents=True, exist_ok=True)
    user_log_dir().mkdir(parents=True, exist_ok=True)
    # 文本翻译默认的输入/输出目录：配置里只存了路径，目录本身从未创建，
    # 于是首次「开始读取」会因为「输入路径不存在」直接 400。这里补建一次，
    # 不改动任何既有取值逻辑。
    for directory in (default_input_dir(), default_output_dir()):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 便携版放在只读目录时不要因此让整个后端启动失败。
            pass


def prepare_working_directory():
    ensure_user_dirs()
    if not is_macos():
        return executable_root()

    # macOS 把 cwd 切到 user_data_root，避免相对路径误写到只读 .app bundle 内
    resource_link = user_data_root() / "Resource"
    if resource_link.is_symlink():
        resource_link.unlink()
    return user_data_root()
