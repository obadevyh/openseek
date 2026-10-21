from PySide6.QtGui import QColor, QFont

BG = "#0d0e12"
SURFACE = "#13151b"
CARD = "#181b23"
CARD_HI = "#1f2330"
BORDER = "#252a36"
TEXT = "#e9eaf0"
MUTED = "#8b91a1"
FAINT = "#5b6171"
ACCENT = "#8b7bff"
ACCENT_2 = "#5ad1c1"
GOOD = "#4fd68a"
WARN = "#f0b44c"
BAD = "#ef6a6a"

QSS = f"""
* {{ font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif; font-size: 13px; color: {TEXT}; outline: none; }}
QMainWindow, QDialog, QWidget#root {{ background: {BG}; }}
QWidget#sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 15px; font-weight: 600; letter-spacing: 0.4px; }}
QLabel#h1 {{ font-size: 22px; font-weight: 600; }}
QLabel#h2 {{ font-size: 14px; font-weight: 600; color: {TEXT}; }}
QLabel#muted, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#faint {{ color: {FAINT}; font-size: 12px; }}
QLabel#statValue {{ font-size: 26px; font-weight: 600; }}
QLabel#statLabel {{ color: {MUTED}; font-size: 12px; }}
QPushButton#nav {{ text-align: left; padding: 9px 14px; border: none; border-radius: 9px; color: {MUTED}; background: transparent; font-size: 13px; }}
QPushButton#nav:hover {{ background: {CARD}; color: {TEXT}; }}
QPushButton#nav:checked {{ background: {CARD_HI}; color: {TEXT}; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#cardFlat {{ background: {CARD}; border-radius: 14px; }}
QPushButton {{ background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 14px; }}
QPushButton:hover {{ border-color: #3a4155; background: #232838; }}
QPushButton:pressed {{ background: {CARD}; }}
QPushButton:disabled {{ color: {FAINT}; background: {CARD}; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: #0d0e12; font-weight: 600; }}
QPushButton#primary:hover {{ background: #9d8fff; }}
QPushButton#primary:disabled {{ background: #2b2846; color: {FAINT}; }}
QPushButton#danger {{ color: {BAD}; }}
QPushButton#danger:hover {{ background: #2a1d22; border-color: #5a2c33; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; padding: 6px 8px; }}
QPushButton#ghost:hover {{ color: {TEXT}; }}
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QSpinBox {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 10px; selection-background-color: {ACCENT}; selection-color: #0d0e12; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {CARD_HI}; padding: 4px; }}
QPlainTextEdit#log {{ font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; border-radius: 12px; padding: 10px; }}
QProgressBar {{ background: {SURFACE}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QProgressBar[state="good"]::chunk {{ background: {GOOD}; }}
QProgressBar[state="warn"]::chunk {{ background: {WARN}; }}
QScrollBar:vertical {{ background: {BG}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2b3040; border-radius: 4px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4155; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:horizontal {{ background: {BG}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #2b3040; border-radius: 4px; min-width: 36px; }}
QListView, QTreeWidget, QTableWidget {{ background: transparent; border: none; }}
QTreeWidget::item {{ padding: 6px 4px; border-radius: 6px; }}
QTreeWidget::item:selected {{ background: {CARD_HI}; }}
QHeaderView::section {{ background: transparent; color: {MUTED}; border: none; padding: 6px 4px; font-weight: 500; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: #2b3040; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; }}
QSlider::groove:horizontal {{ background: #2b3040; height: 6px; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 16px; margin: -6px 0; border-radius: 8px; }}
QSlider::handle:horizontal:disabled {{ background: {FAINT}; }}
QSlider::sub-page:horizontal:disabled {{ background: #2b3040; }}
QToolTip {{ background: {CARD_HI}; color: {TEXT}; border: 1px solid {BORDER}; padding: 5px; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; padding: 6px; border-radius: 10px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {CARD_HI}; }}
"""


def qc(hex_: str, a: int = 255) -> QColor:
    c = QColor(hex_)
    c.setAlpha(a)
    return c
