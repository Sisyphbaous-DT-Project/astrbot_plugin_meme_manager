import logging
import os
import shutil

from ..backend.models import validate_category_name, InvalidCategoryError
from ..config import DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH, MEMES_DIR
from ..utils import ensure_dir_exists, load_json, save_json_atomic

logger = logging.getLogger(__name__)


class CategoryManager:
    """分类资料管理。

    所有写操作遵循同一规则：先在副本上算好新状态，磁盘操作全部成功后才替换
    内存；任何一步失败都尽量把磁盘恢复到操作前状态，内存保持不变。
    save_json_atomic 以返回值表达成败（不抛异常），必须显式检查。
    """

    def __init__(self):
        """初始化类别管理器"""
        ensure_dir_exists(MEMES_DIR)
        self._ensure_data_file()
        self.descriptions = self._load_descriptions()

    def _ensure_data_file(self) -> None:
        """确保 memes_data.json 文件存在，不存在则创建并写入默认数据"""
        if not os.path.exists(MEMES_DATA_PATH):
            save_json_atomic(DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH)
            logger.info(f"创建默认类别描述文件: {MEMES_DATA_PATH}")

    def _load_descriptions(self) -> dict[str, str]:
        """加载类别描述配置"""
        return load_json(MEMES_DATA_PATH, DEFAULT_CATEGORY_DESCRIPTIONS)

    def get_local_categories(self) -> set[str]:
        """获取本地文件夹中的类别"""
        try:
            return {
                d
                for d in os.listdir(MEMES_DIR)
                if os.path.isdir(os.path.join(MEMES_DIR, d))
            }
        except Exception as e:
            logger.error(f"获取本地类别失败: {e}")
            return set()

    def get_sync_status(self) -> tuple[list[str], list[str]]:
        """获取同步状态
        返回: (missing_in_config, deleted_categories)
        """
        local_categories = self.get_local_categories()
        config_categories = set(self.descriptions.keys())

        return (
            list(local_categories - config_categories),  # 本地有但配置没有
            list(config_categories - local_categories),  # 配置有但本地没有
        )

    def update_description(self, category: str, description: str) -> bool:
        """更新类别描述。保存失败时内存保持不变。"""
        new_descriptions = dict(self.descriptions)
        new_descriptions[category] = description
        if not save_json_atomic(new_descriptions, MEMES_DATA_PATH):
            logger.error(f"更新类别描述失败（保存未成功）: {category}")
            return False
        self.descriptions = new_descriptions
        return True

    def rename_category(self, old_name: str, new_name: str) -> bool:
        """重命名类别。

        顺序：校验 → 目录改名 → 原子保存描述文件。
        保存失败时把目录改回旧名，内存不动。
        """
        if old_name not in self.descriptions:
            return False
        try:
            new_name = validate_category_name(new_name)
        except InvalidCategoryError as e:
            logger.error(f"重命名类别失败（新名非法）: {e}")
            return False
        if new_name == old_name:
            return True
        # 目标名冲突检查：描述里已有，或磁盘上已存在目录
        if new_name in self.descriptions:
            return False
        new_path = os.path.join(MEMES_DIR, new_name)
        if os.path.exists(new_path):
            return False

        old_path = os.path.join(MEMES_DIR, old_name)
        renamed = False
        try:
            if os.path.exists(old_path):
                os.rename(old_path, new_path)
                renamed = True

            new_descriptions = dict(self.descriptions)
            new_descriptions[new_name] = new_descriptions.pop(old_name)
            if not save_json_atomic(new_descriptions, MEMES_DATA_PATH):
                # 保存失败：撤回目录改名，保持磁盘与内存一致
                if renamed:
                    try:
                        os.rename(new_path, old_path)
                    except OSError as e:
                        logger.error(
                            f"重命名保存失败且目录回滚失败，磁盘处于新名 {new_name}、描述未更新: {e}"
                        )
                logger.error(f"重命名类别失败（保存未成功）: {old_name} -> {new_name}")
                return False

            self.descriptions = new_descriptions
            return True
        except Exception as e:
            logger.error(f"重命名类别失败: {e}")
            return False

    def delete_category(self, category: str) -> bool:
        """删除类别。

        可恢复顺序：先原子写好删除后的描述文件（失败则中止、目录不动），
        再删目录；删目录失败则恢复描述文件。
        """
        if category not in self.descriptions:
            return False

        new_descriptions = dict(self.descriptions)
        del new_descriptions[category]
        if not save_json_atomic(new_descriptions, MEMES_DATA_PATH):
            logger.error(f"删除类别失败（描述文件保存未成功）: {category}")
            return False

        category_path = os.path.join(MEMES_DIR, category)
        try:
            if os.path.exists(category_path):
                shutil.rmtree(category_path)
        except Exception as e:
            # 目录删除失败：恢复描述文件，保持一致
            logger.error(f"删除类别目录失败，恢复描述文件: {e}")
            save_json_atomic(self.descriptions, MEMES_DATA_PATH)
            return False

        self.descriptions = new_descriptions
        return True

    def get_descriptions(self) -> dict[str, str]:
        """获取所有类别描述"""
        return self.descriptions.copy()  # 返回字典的副本

    def sync_with_filesystem(self) -> bool:
        """同步文件系统和配置。保存失败时内存回滚。"""
        local_categories = self.get_local_categories()
        new_descriptions = dict(self.descriptions)
        changed = False

        # 为新类别添加默认描述（优先从 DEFAULT_CATEGORY_DESCRIPTIONS 查找）
        for category in local_categories:
            if category not in new_descriptions:
                default_desc = DEFAULT_CATEGORY_DESCRIPTIONS.get(category)
                new_descriptions[category] = default_desc or "请添加描述"
                changed = True

        if not changed:
            return True
        if not save_json_atomic(new_descriptions, MEMES_DATA_PATH):
            logger.error("同步文件系统失败（保存未成功）")
            return False
        self.descriptions = new_descriptions
        return True
