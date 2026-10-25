"""页签基类：当前页立即刷新，其余页标脏，切过去时再补。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from dstools.qt.context import AppContext


class Page(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.stale = True  # 还没按当前存档加载过
        # 点页面空白处（标签、卡片等不接收焦点的区域，鼠标事件会一路冒泡到这里）
        # 时由页面接走焦点，输入框随之退出编辑状态。
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    def mousePressEvent(self, event):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def on_cluster_changed(self, cluster) -> None:
        """选中的存档变了（或需要重新加载）时调用；子类重写，负责按存档刷新内容。"""

    def load(self) -> None:
        """按当前选中的存档加载一次，并清除脏标记。"""
        self.stale = False
        self.on_cluster_changed(self.ctx.selected_cluster())

    def retranslate(self) -> None:
        """界面语言切换后调用；子类重写，重设所有文案。"""

    # ── 未保存改动：有编辑功能的页面重写，状态变化时发 ctx.unsaved_changed ──
    def has_unsaved_changes(self) -> bool:
        return False

    def save_unsaved_changes(self) -> bool:
        """离开前保存（同步完成，不弹"保存成功"）；失败或被拦下时返回 False，调用方随之取消离开。"""
        return True

    def discard_unsaved_changes(self) -> None:
        """放弃改动，恢复为磁盘上已保存的内容。"""
