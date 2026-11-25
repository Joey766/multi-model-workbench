from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QLabel, QPushButton


DARK = dict(canvas="#12161f", sidebar="#0d1119", surface="#1b2130", raised="#252e41", line="#333e54",
            text="#edf1fa", muted="#a4aec3", subtle="#7f8ba7", accent="#b8c5ff", accentink="#161e39",
            highlight="#293455", success="#8fdcb2", danger="#f5aba4", note="#f4ca90", code="#0e1320",
            added="#1f3a33", removed="#402b38")
LIGHT = dict(canvas="#f4f6fc", sidebar="#e8edf7", surface="#ffffff", raised="#e8eefb", line="#c9d2e5",
             text="#1a2238", muted="#4e5d7a", subtle="#606e8a", accent="#4658a7", accentink="#ffffff",
             highlight="#d9e2ff", success="#176141", danger="#992d35", note="#77501b", code="#eef2fa",
             added="#d8efe2", removed="#f7e1e5")


def label(text, style=""):
    widget = QLabel(text)
    if style:
        widget.setObjectName(style)
    return widget


def button(text, action=None, style="", hint=""):
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if action:
        widget.clicked.connect(action)
    if style:
        widget.setObjectName(style)
    if hint:
        widget.setToolTip(hint)
        widget.setAccessibleName(hint)
    return widget


def separator():
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedHeight(1)
    return line


class Combo(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().color(QPalette.ColorRole.Text if self.isEnabled() else QPalette.ColorRole.PlaceholderText)
        painter.setPen(QPen(color, 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x, y = self.width() - 10, self.height() * .5
        painter.drawPolyline(QPolygonF([QPointF(x - 3, y - 1), QPointF(x, y + 2), QPointF(x + 3, y - 1)]))


def apply_theme(p):
    app = QApplication.instance()
    app.setStyle("Fusion")
    font = QFont("Segoe UI", 10)
    font.setFamilies(["Segoe UI", "Microsoft YaHei UI"])
    app.setFont(font)
    palette = app.palette()
    for role, value in (
        (QPalette.ColorRole.Window, "canvas"), (QPalette.ColorRole.WindowText, "text"),
        (QPalette.ColorRole.Base, "sidebar"), (QPalette.ColorRole.AlternateBase, "surface"),
        (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "surface"),
        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "highlight"),
        (QPalette.ColorRole.HighlightedText, "text"), (QPalette.ColorRole.PlaceholderText, "subtle"),
        (QPalette.ColorRole.ToolTipBase, "surface"), (QPalette.ColorRole.ToolTipText, "text"),
    ):
        palette.setColor(role, QColor(p[value]))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { color: %(text)s; background-color: %(canvas)s; }
        QMainWindow, QDialog { background: %(canvas)s; }
        QFrame#Sidebar, QWidget#Sidebar { background: %(sidebar)s; border-right: 1px solid %(line)s; }
        QFrame#Header { background: %(canvas)s; border-bottom: 1px solid %(line)s; }
        QWidget#Sidebar QLabel, QFrame#Header QLabel, QLabel#Transparent { background: transparent; }
        QFrame#Card { background: %(surface)s; border: 1px solid %(line)s; border-radius: 14px; }
        QFrame#Card QLabel { background: transparent; }
        QFrame#Composer { background: %(surface)s; border: 1px solid %(line)s; border-radius: 15px; }
        QFrame#Approval { background: %(surface)s; border: 1px solid %(note)s; border-radius: 9px; }
        QFrame#Approval QLabel { color: %(note)s; background: transparent; }
        QFrame#Divider { background: %(line)s; border: 0; }
        QLabel#Title { font-size: 25px; font-weight: 650; }
        QLabel#Hero { font-size: 38px; font-weight: 650; }
        QLabel#Brand { font-size: 17px; font-weight: 650; }
        QLabel#Badge { background: %(highlight)s; color: %(accent)s; font-size: 18px; font-weight: 720; border-radius: 10px; }
        QLabel#Muted { color: %(muted)s; }
        QLabel#Subtle { color: %(subtle)s; }
        QLabel#Accent { color: %(accent)s; font-weight: 620; }
        QLabel#Error { color: %(danger)s; }
        QPushButton { background: %(surface)s; border: 1px solid %(line)s; border-radius: 8px;
                      padding: 6px 11px; min-height: 22px; }
        QPushButton:hover { background: %(raised)s; }
        QPushButton:pressed { background: %(highlight)s; }
        QPushButton:disabled { color: %(subtle)s; background: %(surface)s; }
        QPushButton#Primary { background: %(accent)s; color: %(accentink)s; border: 1px solid %(accent)s; font-weight: 650; }
        QPushButton#Primary:hover { border-color: %(text)s; }
        QPushButton#Primary:disabled { background: %(surface)s; color: %(subtle)s; border: 1px solid %(line)s; }
        QPushButton#Quiet { background: transparent; border: 1px solid transparent; text-align: left; }
        QPushButton#Quiet:hover { background: %(surface)s; border-color: %(line)s; }
        QPushButton#Pill { padding: 2px 7px; min-height: 20px; border-radius: 8px; font-size: 12px; }
        QLineEdit, QComboBox, QPlainTextEdit, QTextBrowser, QTreeWidget, QListWidget {
            border: 1px solid %(line)s; border-radius: 8px; background: %(surface)s;
            selection-background-color: %(highlight)s; selection-color: %(text)s; padding: 5px; }
        QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: %(accent)s; }
        QLineEdit { padding: 6px 10px; }
        QPlainTextEdit#Prompt { background: %(surface)s; border: 0; padding: 0; font-size: 14px; }
        QTextBrowser#Conversation { background: %(canvas)s; border: 0; padding: 0; }
        QComboBox { min-height: 22px; padding: 3px 8px; }
        QComboBox::drop-down { border-left: 1px solid %(line)s; width: 19px; }
        QComboBox QAbstractItemView, QMenu { background: %(surface)s; border: 1px solid %(line)s; }
        QMenu::item { background: transparent; padding: 7px 24px 7px 10px; }
        QMenu::item:selected { background: %(highlight)s; }
        QTabWidget::pane { border: 1px solid %(line)s; border-radius: 8px; top: -1px; }
        QTabBar::tab { background: transparent; color: %(muted)s; padding: 9px 10px; border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { color: %(accent)s; border-bottom-color: %(accent)s; }
        QTreeWidget, QListWidget { outline: 0; }
        QTreeWidget#TaskTree { background: %(sidebar)s; border: 0; padding: 0; }
        QTreeWidget#TaskTree::branch:selected { background: transparent; }
        QTreeWidget::item, QListWidget::item { min-height: 25px; padding: 2px 4px; }
        QTreeWidget::item:selected, QListWidget::item:selected { background: %(highlight)s; color: %(text)s; border-radius: 6px; }
        QTreeWidget::item:hover, QListWidget::item:hover { background: %(raised)s; }
        QSplitter::handle { background: %(line)s; width: 1px; height: 1px; }
        QScrollBar:vertical { background: transparent; width: 9px; margin: 1px; }
        QScrollBar::handle:vertical { background: %(line)s; min-height: 30px; border-radius: 3px; }
        QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; subcontrol-origin: margin; }
        QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
        QStatusBar { border-top: 1px solid %(line)s; color: %(muted)s; font-size: 12px; }
        QToolTip { background: %(surface)s; color: %(text)s; border: 1px solid %(line)s; }
    """ % p)
