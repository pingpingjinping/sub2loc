"""Native desktop application. Optional dependency: PySide6-Essentials."""
import argparse
import copy
import json
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from . import __version__
from .api import Client
from .catalog import Catalog
from .connection import test_translation
from .jako import mode_for
from .engine import Cancelled
from .workspace import Workspace, Work, default_data_dir, translate_queue, validate_settings

STYLE = '''
QWidget { font-family: "Malgun Gothic", "Noto Sans CJK KR", "Segoe UI", sans-serif; font-size: 12px; color: #283a32; }
QMainWindow, QDialog { background: #f4f5ef; }
QLabel#brand { font-size: 25px; font-weight: 800; color: #246341; }
QLabel#hero { font-size: 21px; font-weight: 700; }
QLabel#muted { color: #748176; }
QLabel#section { font-size: 14px; font-weight: 700; }
QLabel#badge { background: #e7efdf; color: #4d7051; border-radius: 12px; padding: 6px 12px; }
QFrame#card { background: white; border: 1px solid #dde3d8; border-radius: 12px; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox { background: white; border: 1px solid #cfd9cd; border-radius: 6px; padding: 7px; selection-background-color: #d7e9d3; selection-color: #193e28; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border: 1px solid #54835c; }
QPushButton { background: white; border: 1px solid #cdd8c8; border-radius: 6px; padding: 8px 12px; }
QPushButton:hover { background: #eaf2e4; border-color: #91ae86; }
QPushButton:disabled { color: #a6afa1; border-color: #e2e6de; background: #f5f6f2; }
QPushButton#primary { background: #256745; color: white; border: 1px solid #256745; font-weight: 700; }
QPushButton#primary:hover { background: #195536; }
QPushButton#primary:disabled { background: #a1b49d; border-color: #a1b49d; }
QTreeWidget, QListWidget { background: white; border: 1px solid #e0e6dc; border-radius: 7px; outline: none; }
QTreeWidget::item { padding: 8px 3px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #dcebd7; color: #20432d; }
QListWidget::item { padding: 12px; border-bottom: 1px solid #edf0e8; }
QHeaderView::section { background: #f4f7ef; color: #6d7c68; padding: 9px; border: 0; font-size: 11px; }
QTabWidget::pane { background: white; border: 1px solid #dde3d8; border-radius: 6px; }
QTabBar::tab { background: #edf0e6; padding: 10px 18px; margin-right: 3px; border-radius: 5px; }
QTabBar::tab:selected { background: white; color: #235c38; font-weight: 700; }
QScrollArea { background: transparent; border: none; }
QProgressBar { border: none; background: #dfe8d8; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #53845d; border-radius: 4px; }
QSplitter::handle { background: transparent; width: 16px; }
'''


def label(text='', name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName('primary')
    widget.clicked.connect(callback)
    return widget


class Task(QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as exc:
            self.error.emit(str(exc))


class TranslationTask(QThread):
    progress = Signal(dict)
    completed = Signal(object)
    error = Signal(str)

    def __init__(self, workspace, settings, root, output, parent=None):
        super().__init__(parent)
        self.workspace, self.settings, self.root, self.output = copy.deepcopy(workspace), settings, root, output
        self.cancel = threading.Event()

    def run(self):
        try:
            results = translate_queue(self.workspace, self.settings, self.root, self.output, self.cancel,
                                      lambda **data: self.progress.emit(data))
            self.completed.emit(results)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.settings.pop('key', None)


class MatchDialog(QDialog):
    def __init__(self, works, initial='', catalog=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('작품 매칭')
        self.resize(680, 600)
        self.selected_work = None
        self.catalog = catalog or Catalog()
        self.task = None
        layout = QVBoxLayout(self)
        layout.addWidget(label('선택한 자막에 연결할 작품을 골라주세요.', 'section'))
        layout.addWidget(label('시즌과 연도를 확인하세요. 검색 결과를 자동으로 확정하지 않습니다.', 'muted'))
        self.tabs = QTabWidget()
        online = QWidget(); online_layout = QVBoxLayout(online)
        search_row = QHBoxLayout()
        self.query = QLineEdit(initial); self.query.setPlaceholderText('일본어·영문·로마자 작품명')
        self.query.setMaxLength(200); self.query.returnPressed.connect(self.search)
        self.search_button = button('검색', self.search, True)
        search_row.addWidget(self.query); search_row.addWidget(self.search_button)
        online_layout.addLayout(search_row)
        self.hint = label('입력한 검색어만 AniList로 전송됩니다. 자막 파일은 전송되지 않습니다.', 'muted')
        online_layout.addWidget(self.hint)
        self.results = QListWidget(); self.results.itemDoubleClicked.connect(lambda _: self.accept_work())
        online_layout.addWidget(self.results)
        self.tabs.addTab(online, '온라인 작품 검색')
        saved = QWidget(); saved_layout = QVBoxLayout(saved)
        self.saved_query = QLineEdit(); self.saved_query.setPlaceholderText('저장한 작품 필터')
        saved_layout.addWidget(self.saved_query)
        self.saved = QListWidget()
        for work in sorted(works.values(), key=lambda w: w.title.casefold()):
            self.add_work(self.saved, work)
        self.saved_query.textChanged.connect(self.filter_saved)
        self.saved.itemDoubleClicked.connect(lambda _: self.accept_work())
        saved_layout.addWidget(self.saved)
        saved_layout.addWidget(label('같은 작품 ID는 기존 인물 메모와 번역 맥락을 이어받습니다.', 'muted'))
        self.tabs.addTab(saved, '내 작품 목록')
        manual = QWidget(); manual_layout = QVBoxLayout(manual)
        manual_layout.addWidget(label('검색에 없는 작품은 직접 등록할 수 있어요.', 'section'))
        self.manual = QLineEdit(initial); self.manual.setMaxLength(200)
        self.manual.setPlaceholderText('작품명 · 시즌 1')
        manual_layout.addWidget(self.manual)
        manual_layout.addWidget(label('직접 등록한 작품은 입력한 이름으로 구분합니다. 다른 시즌은 이름을 구분해주세요.', 'muted'))
        manual_layout.addStretch()
        self.tabs.addTab(manual, '직접 등록')
        layout.addWidget(self.tabs)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('선택한 작품으로 매칭')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('취소')
        self.buttons.accepted.connect(self.accept_work); self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @staticmethod
    def add_work(widget, work):
        details = ' · '.join(x for x in (work.native, work.year, work.format,
                                        work.episodes + '화' if work.episodes else '') if x)
        item = QListWidgetItem(work.title + ('\n' + details if details else ''))
        item.setData(Qt.ItemDataRole.UserRole, work)
        widget.addItem(item)

    def filter_saved(self, text):
        for i in range(self.saved.count()):
            item = self.saved.item(i)
            item.setHidden(text.casefold() not in item.text().casefold())

    def search(self):
        if self.task is not None:
            return
        query = self.query.text().strip()
        if not query:
            self.hint.setText('검색할 작품명을 입력하세요.')
            return
        self.search_button.setEnabled(False)
        self.hint.setText('작품을 검색하고 있습니다…')
        self.results.clear()
        self.task = Task(lambda: self.catalog.search(query), self)
        self.task.result.connect(self.search_results)
        self.task.error.connect(self.hint.setText)
        self.task.finished.connect(self.search_finished)
        self.task.start()

    def search_results(self, works):
        for work in works:
            self.add_work(self.results, work)
        self.hint.setText(f'{len(works)}개 작품 · 시즌·연도를 확인한 뒤 선택하세요.' if works else
                          '결과가 없습니다. 일본어·영문으로 검색하거나 직접 등록하세요.')

    def search_finished(self):
        self.task.deleteLater(); self.task = None
        self.search_button.setEnabled(True)

    def accept_work(self):
        tab = self.tabs.currentIndex()
        if tab == 2:
            try:
                work = Work.manual(self.manual.text())
            except ValueError as exc:
                QMessageBox.information(self, '작품명 확인', str(exc)); return
        else:
            selected = (self.results if tab == 0 else self.saved).currentItem()
            if selected is None or selected.isHidden():
                return
            work = copy.deepcopy(selected.data(Qt.ItemDataRole.UserRole))
        if self.task is not None:
            self.hint.setText('검색이 끝나면 매칭할 수 있어요.'); return
        self.selected_work = work
        self.accept()

    def reject(self):
        if self.task is not None:
            self.hint.setText('진행 중인 검색이 끝날 때까지 기다려주세요.'); return
        super().reject()


class MainWindow(QMainWindow):
    def __init__(self, data_dir=None):
        super().__init__()
        self.data_dir = Path(data_dir or default_data_dir())
        self.workspace_path = self.data_dir / 'workspace.json'
        self.load_error = ''
        try:
            self.workspace = Workspace.load(self.workspace_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Keep the invalid file intact until the user explicitly imports/edits new state.
            self.workspace = Workspace(); self.load_error = '저장된 목록을 읽지 못했습니다. 기존 파일은 유지합니다.'
        self.worker = None
        self.background_tasks = []
        self.pending_close = False
        self.current_work = ''
        self.loading_notes = False
        self.results = []
        self.request_started = None
        self.wait_message = ''
        self.wait_timer = QTimer(self); self.wait_timer.setInterval(1000)
        self.wait_timer.timeout.connect(self.show_wait_time)
        self.rows = {}
        self.setWindowTitle('sub2loc · 자막 번역 워크스페이스')
        self.resize(1240, 870)
        self.setMinimumSize(1040, 730)
        self.setAcceptDrops(True)
        central = QWidget(); self.setCentralWidget(central)
        layout = QVBoxLayout(central); layout.setContentsMargins(26, 22, 26, 20); layout.setSpacing(15)
        heading = QHBoxLayout(); heading.addWidget(label('sub2loc', 'brand'))
        heading.addWidget(label('자막 번역 워크스페이스', 'muted')); heading.addStretch()
        heading.addWidget(label('DESKTOP  /  ' + __version__, 'badge'))
        layout.addLayout(heading)
        layout.addWidget(label('작품을 연결하고, 다음 화까지 자연스럽게.', 'hero'))
        layout.addWidget(label('폴더나 ASS 자막을 추가한 뒤 목록에서 작품을 매칭하세요. 폴더를 끌어 놓아도 됩니다.', 'muted'))
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QFrame(); left.setObjectName('card'); left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(17, 17, 17, 17); left_layout.setSpacing(12)
        toolbar = QHBoxLayout()
        self.folder_button = button('＋ 폴더 추가', self.choose_folder)
        self.file_button = button('＋ 자막 파일', self.choose_files)
        self.match_button = button('작품 매칭', self.match_selected, True)
        self.remove_button = button('제거', self.remove_selected)
        for widget in (self.folder_button, self.file_button, self.match_button, self.remove_button): toolbar.addWidget(widget)
        toolbar.addStretch(); left_layout.addLayout(toolbar)
        self.summary = label('', 'muted'); left_layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['폴더 / 자막 파일', '매칭 작품', '상태'])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAlternatingRowColors(False)
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(1, 200); self.tree.setColumnWidth(2, 75)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        self.tree.itemDoubleClicked.connect(lambda *_: self.match_selected())
        left_layout.addWidget(self.tree, 1)
        row_actions = QHBoxLayout()
        self.up = button('↑ 위로', lambda: self.move_selected(-1))
        self.down = button('↓ 아래로', lambda: self.move_selected(1))
        self.unmatch = button('매칭 해제', self.clear_match)
        row_actions.addWidget(self.up); row_actions.addWidget(self.down); row_actions.addStretch(); row_actions.addWidget(self.unmatch)
        left_layout.addLayout(row_actions)
        left_layout.addWidget(label('폴더 행을 선택하면 안의 자막 전체에 적용됩니다. 같은 작품은 표시된 파일 순서로 처리합니다.', 'muted'))
        splitter.addWidget(left)
        right = QTabWidget(); right.setMinimumWidth(330)
        work_page = QWidget(); work_layout = QVBoxLayout(work_page); work_layout.setContentsMargins(18, 18, 18, 18)
        work_layout.addWidget(label('작품과 인물 메모', 'section'))
        self.work_title = label('목록에서 자막을 선택하세요.'); self.work_title.setStyleSheet('font-size:16px;font-weight:700;')
        work_layout.addWidget(self.work_title)
        self.work_details = label('작품별로 맥락이 분리되어 저장됩니다.', 'muted'); work_layout.addWidget(self.work_details)
        self.character_button = button('기본 등장인물 이름 가져오기', self.fetch_characters)
        work_layout.addWidget(self.character_button)
        work_layout.addWidget(label('호칭 · 말투 · 고정 용어'))
        self.notes = QPlainTextEdit(); self.notes.setPlaceholderText('예: 아키라는 반말, 미사키는 존댓말.\n고유명사 표기와 알고 있는 관계를 적어주세요.')
        self.notes.setMinimumHeight(150); self.notes.textChanged.connect(self.notes_changed)
        work_layout.addWidget(self.notes, 1)
        work_layout.addWidget(label('가져온 원제 · 등장인물'))
        self.reference = QPlainTextEdit(); self.reference.setReadOnly(True)
        self.reference.setPlaceholderText('등장인물을 가져오면 여기에서 확인할 수 있어요.')
        self.reference.setMaximumHeight(125); work_layout.addWidget(self.reference)
        work_layout.addWidget(label('입력한 메모를 우선합니다. 작품을 매칭해도 화자나 인물 관계를 자동으로 확정하지 않습니다.', 'muted'))
        work_scroll = QScrollArea(); work_scroll.setWidgetResizable(True); work_scroll.setWidget(work_page)
        right.addTab(work_scroll, '매칭 작품')
        api_container = QWidget(); api_layout = QVBoxLayout(api_container); api_layout.setContentsMargins(18, 18, 18, 18)
        form = QFormLayout(); form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.url = QLineEdit(); self.url.setPlaceholderText('Bionic에 표시된 서버 주소 + /v1')
        self.model = QComboBox(); self.model.setEditable(True); self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.lineEdit().setPlaceholderText('정확한 모델 ID')
        self.key = QLineEdit(); self.key.setEchoMode(QLineEdit.EchoMode.Password); self.key.setPlaceholderText('필요한 경우에만 · 저장하지 않음')
        self.api_mode = QComboBox()
        for text, value in [('자동 (모델명으로 선택)', 'auto'), ('ja-ko-vn-12b-v2 전용', 'ja-ko-vn'), ('범용 대화형 JSON', 'chat')]:
            self.api_mode.addItem(text, value)
        self.source = QLineEdit('auto'); self.target = QLineEdit('한국어')
        self.encoding = QComboBox()
        for text, value in [('UTF-8 (BOM 보존)', 'utf-8-sig'), ('UTF-16', 'utf-16'), ('일본어 CP932', 'cp932'), ('한국어 CP949', 'cp949')]: self.encoding.addItem(text, value)
        self.context_tokens = QSpinBox(); self.context_tokens.setRange(1024, 131072)
        self.context_tokens.setSingleStep(1024); self.context_tokens.setValue(4096)
        self.context_tokens.setToolTip('Bionic에 실제 로드한 컨텍스트 크기와 맞추세요. 서버 설정을 자동으로 바꾸지는 않습니다.')
        self.batch = QSpinBox(); self.batch.setRange(1, 64); self.batch.setValue(12)
        self.tokens = QSpinBox(); self.tokens.setRange(256, 32768); self.tokens.setSingleStep(256); self.tokens.setValue(4096)
        self.tokens.setToolTip('ja-ko-vn 전용 모드에서는 컨텍스트 절반 이하로 출력 공간을 예약합니다.')
        self.timeout = QSpinBox(); self.timeout.setRange(5, 1800); self.timeout.setValue(180); self.timeout.setSuffix(' 초')
        for text, widget in [('API 기본 주소', self.url), ('모델', self.model), ('호출 방식', self.api_mode), ('API 키', self.key)]:
            form.addRow(text, widget)
        api_layout.addLayout(form)
        self.models_button = button('연결 확인 · 모델 조회', self.fetch_models)
        api_layout.addWidget(self.models_button)
        self.probe_button = button('짧은 번역 테스트', self.probe_connection)
        api_layout.addWidget(self.probe_button)
        self.probe_result = QPlainTextEdit(); self.probe_result.setReadOnly(True)
        self.probe_result.setPlaceholderText('오늘은 좋은 날씨네요 — 같은 짧은 번역으로 실제 API 응답을 확인해요.')
        self.probe_result.setMaximumHeight(140); api_layout.addWidget(self.probe_result)
        self.copy_result_button = button('테스트 · 오류 내용 복사',
                                         lambda: QApplication.clipboard().setText(self.probe_result.toPlainText()))
        api_layout.addWidget(self.copy_result_button)
        self.api_hint = label('', 'muted')
        api_layout.addWidget(self.api_hint)
        advanced_button = QPushButton('상세 번역 설정 펼치기'); advanced_button.setCheckable(True)
        advanced = QWidget(); advanced_form = QFormLayout(advanced)
        for text, widget in [('원문 언어', self.source), ('번역 언어', self.target), ('원본 인코딩', self.encoding),
                             ('묶음 최대 대사', self.batch), ('컨텍스트 토큰', self.context_tokens), ('최대 출력 토큰', self.tokens), ('응답 제한 시간', self.timeout)]:
            advanced_form.addRow(text, widget)
        advanced.setVisible(False); advanced_button.toggled.connect(advanced.setVisible)
        api_layout.addWidget(advanced_button); api_layout.addWidget(advanced); api_layout.addStretch()
        self.api_mode.currentIndexChanged.connect(self.update_api_hint)
        self.model.currentTextChanged.connect(self.update_api_hint)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(api_container)
        right.addTab(scroll, '번역 API')
        self.settings_tabs = right
        splitter.addWidget(right); splitter.setSizes([790, 365]); splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)
        output_row = QHBoxLayout(); output_row.addWidget(label('결과 폴더'))
        self.output = QLineEdit(); self.output.setPlaceholderText('번역 결과를 저장할 폴더를 선택하세요.')
        self.output_button = button('찾아보기', self.choose_output)
        self.open_button = button('결과 열기', self.open_output)
        output_row.addWidget(self.output, 1); output_row.addWidget(self.output_button); output_row.addWidget(self.open_button)
        layout.addLayout(output_row)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 10000); self.progress_bar.setValue(0); self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        footer = QHBoxLayout()
        self.status = label('자막을 추가해 시작하세요.', 'muted'); footer.addWidget(self.status, 1)
        self.stop_button = button('중단', self.stop); self.stop_button.setEnabled(False)
        self.start_button = button('전체 번역 시작', self.start, True)
        footer.addWidget(self.stop_button); footer.addWidget(self.start_button)
        layout.addLayout(footer)
        self.mutating_controls = [self.folder_button, self.file_button, self.match_button, self.remove_button,
                                  self.up, self.down, self.unmatch, self.settings_tabs, self.output, self.output_button]
        self.load_settings()
        self.update_api_hint()
        self.refresh()
        if self.load_error: self.status.setText(self.load_error)

    def load_settings(self):
        s = self.workspace.settings
        for key, widget in [('url', self.url), ('source', self.source), ('target', self.target), ('output', self.output)]:
            if key in s: widget.setText(str(s[key]))
        self.model.setCurrentText(s.get('model', ''))
        for key, widget in [('batch_size', self.batch), ('context_tokens', self.context_tokens), ('max_tokens', self.tokens), ('timeout', self.timeout)]:
            if isinstance(s.get(key), int): widget.setValue(s[key])
        mode_index = self.api_mode.findData(s.get('api_mode', 'auto'))
        if mode_index >= 0: self.api_mode.setCurrentIndex(mode_index)
        index = self.encoding.findData(s.get('encoding', 'utf-8-sig'))
        if index >= 0: self.encoding.setCurrentIndex(index)

    def settings(self):
        return dict(url=self.url.text().strip(), model=self.model.currentText().strip(), key=self.key.text(),
                    api_mode=self.api_mode.currentData(), context_tokens=self.context_tokens.value(),
                    source=self.source.text().strip() or 'auto', target=self.target.text().strip(),
                    encoding=self.encoding.currentData(), batch_size=self.batch.value(),
                    max_tokens=self.tokens.value(), timeout=self.timeout.value())

    def save(self):
        settings = self.settings(); settings.pop('key', None); settings['output'] = self.output.text().strip()
        self.workspace.settings = settings
        try:
            if self.load_error and self.workspace_path.exists():
                # Preserve corrupt input before writing a new workspace.
                backup = self.workspace_path.with_name('workspace.unreadable.json')
                if backup.exists():
                    raise OSError('기존 복구 사본이 있습니다. 저장 폴더를 확인하세요.')
                self.workspace_path.rename(backup)
                self.load_error = ''
            self.workspace.save(self.workspace_path)
        except OSError:
            self.status.setText('목록 저장에 실패했습니다. 저장 폴더의 권한을 확인하세요.')

    def selected_indices(self):
        indices = set()
        for item in self.tree.selectedItems():
            indices.update(item.data(0, Qt.ItemDataRole.UserRole) or [])
        return sorted(indices)

    def refresh(self, select_paths=None):
        self.loading_notes = True
        self.tree.clear(); self.rows = {}; parents = {}
        for index, entry in enumerate(self.workspace.entries):
            path = Path(entry.path); folder = str(path.parent)
            if folder not in parents:
                parent = QTreeWidgetItem([path.parent.name or folder, '', ''])
                parent.setToolTip(0, folder); parent.setData(0, Qt.ItemDataRole.UserRole, [])
                self.tree.addTopLevelItem(parent); parent.setExpanded(True); parents[folder] = parent
            parent = parents[folder]
            parent.setData(0, Qt.ItemDataRole.UserRole, parent.data(0, Qt.ItemDataRole.UserRole) + [index])
            work = self.workspace.works.get(entry.work_id)
            item = QTreeWidgetItem([path.name, work.title if work else '작품을 매칭하세요', entry.status])
            item.setToolTip(0, entry.path); item.setToolTip(1, work.title if work else '')
            item.setData(0, Qt.ItemDataRole.UserRole, [index]); parent.addChild(item); self.rows[index] = item
            if select_paths and entry.path in select_paths: item.setSelected(True)
        for parent in parents.values():
            ids = {self.workspace.entries[i].work_id for i in parent.data(0, Qt.ItemDataRole.UserRole)}
            parent.setText(1, self.workspace.works[next(iter(ids))].title if len(ids) == 1 and next(iter(ids)) in self.workspace.works else '혼합 / 미매칭')
            parent.setText(2, f'{parent.childCount()}개')
        unmatched = sum(e.work_id not in self.workspace.works for e in self.workspace.entries)
        used = len({e.work_id for e in self.workspace.entries if e.work_id in self.workspace.works})
        self.summary.setText(f'자막 {len(self.workspace.entries)}개   ·   작품 {used}개   ·   미매칭 {unmatched}개')
        self.loading_notes = False
        self.selection_changed()

    def add_paths(self, paths):
        if self.worker is not None: return
        count, errors = self.workspace.add_paths(paths)
        self.refresh(); self.save()
        self.status.setText(f'{count}개 자막을 추가했습니다.' + (f'  {len(errors)}개 경로는 읽지 못했습니다.' if errors else ''))

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'ASS 자막이 있는 폴더 선택')
        if path: self.add_paths([path])

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '자막 파일 선택', '', 'ASS 자막 (*.ass *.ASS)')
        if paths: self.add_paths(paths)

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '번역 결과 저장 폴더', self.output.text())
        if path: self.output.setText(path); self.save()

    def open_output(self):
        path = Path(self.output.text().strip())
        if self.output.text().strip() and path.is_dir(): QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def selection_changed(self):
        if self.loading_notes: return
        indices = self.selected_indices()
        ids = {self.workspace.entries[i].work_id for i in indices}
        work_id = next(iter(ids)) if len(ids) == 1 else ''
        work = self.workspace.works.get(work_id)
        self.current_work = work_id if work else ''
        self.loading_notes = True
        self.notes.setEnabled(work is not None and self.worker is None)
        self.notes.setPlainText(work.notes if work else '')
        self.work_title.setText(work.title if work else ('여러 작품이 선택되었습니다.' if len(ids) > 1 else '작품을 매칭하세요.'))
        self.work_details.setText(' · '.join(x for x in (work.native, work.year, work.format, work.id) if x) if work else
                                  '자막이나 폴더를 선택하고 작품 매칭을 누르세요.')
        self.character_button.setEnabled(work is not None and work.id.startswith('anilist:') and self.worker is None)
        self.reference.setPlainText(work.reference if work else '')
        self.loading_notes = False

    def notes_changed(self):
        if self.loading_notes or not self.current_work: return
        text = self.notes.toPlainText()
        if len(text) > 5000:
            text = text[:5000]
            self.loading_notes = True
            self.notes.setPlainText(text)
            self.loading_notes = False
            self.status.setText('인물·용어 메모는 5,000자까지 사용할 수 있습니다.')
        self.workspace.works[self.current_work].notes = text
        self.save()

    def match_selected(self):
        indices = self.selected_indices()
        if not indices:
            self.status.setText('매칭할 폴더나 자막을 먼저 선택하세요.'); return
        initial = Path(self.workspace.entries[indices[0]].path).parent.name
        dialog = MatchDialog(self.workspace.works, initial, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.workspace.assign(indices, dialog.selected_work)
            paths = {self.workspace.entries[i].path for i in indices}
            self.refresh(paths); self.save()
            self.status.setText(f'{len(indices)}개 자막을 {dialog.selected_work.title}에 연결했습니다.')

    def clear_match(self):
        for i in self.selected_indices():
            self.workspace.entries[i].work_id = ''; self.workspace.entries[i].status = '미매칭'
        self.refresh(); self.save()

    def remove_selected(self):
        selected = set(self.selected_indices())
        self.workspace.entries = [e for i, e in enumerate(self.workspace.entries) if i not in selected]
        self.refresh(); self.save()

    def move_selected(self, direction):
        indices = self.selected_indices()
        if len(indices) != 1:
            self.status.setText('순서를 바꿀 자막 한 개를 선택하세요.'); return
        i = indices[0]; entry = self.workspace.entries[i]; parent = Path(entry.path).parent
        siblings = [j for j, e in enumerate(self.workspace.entries) if Path(e.path).parent == parent]
        position = siblings.index(i) + direction
        if not 0 <= position < len(siblings): return
        j = siblings[position]
        self.workspace.entries[i], self.workspace.entries[j] = self.workspace.entries[j], self.workspace.entries[i]
        self.refresh({entry.path}); self.save()

    def background(self, function, on_result, on_error=None):
        task = Task(function, self); self.background_tasks.append(task)
        task.result.connect(on_result)
        task.error.connect(on_error or self.status.setText)
        def finished():
            self.background_tasks.remove(task); task.deleteLater()
            if self.pending_close and self.worker is None and not self.background_tasks: self.close()
        task.finished.connect(finished); task.start()
        return task

    def update_api_hint(self, *_):
        try:
            special = mode_for(self.settings()) == 'ja-ko-vn'
            self.batch.setEnabled(True)
            self.api_hint.setText(
                'ja-ko-vn 전용: 컨텍스트 안에 들어가는 연속 대사를 묶어 번역합니다. '
                '입력·메모·이전 대사·출력 공간을 함께 계산하고, 잘리거나 줄 대응이 다르면 더 작게 나눕니다. '
                '컨텍스트는 Bionic의 실제 로드 설정과 맞추세요. 출력 예약은 설정값과 컨텍스트 절반 중 작은 값입니다.' if special else
                '범용 대화형: 대사 묶음과 JSON 응답을 사용합니다. 자막과 작품 메모는 설정한 API 서버로 전송됩니다.')
        except ValueError as exc:
            self.api_hint.setText(str(exc))

    def probe_connection(self):
        if self.worker is not None or self.background_tasks:
            self.status.setText('진행 중인 작업이 끝나면 번역 테스트를 실행할 수 있습니다.'); return
        try:
            config = validate_settings(self.settings())
        except ValueError as exc:
            self.probe_result.setPlainText(str(exc)); return
        self.probe_button.setEnabled(False)
        self.probe_result.setPlainText('선택한 모델로 실제 번역을 요청하고 있습니다…')
        self.start_button.setEnabled(False)
        def result(data):
            mode = 'ja-ko-vn 전용 Completions' if data['mode'] == 'ja-ko-vn' else '대화형 JSON'
            self.probe_result.setPlainText('API 응답 확인 · ' + mode + '\n원문: ' + data['source'] +
                                           '\n번역: ' + data['translation'])
            self.probe_button.setEnabled(True); self.start_button.setEnabled(True); self.save()
        def failed(message):
            self.probe_result.setPlainText(message)
            self.probe_button.setEnabled(True); self.start_button.setEnabled(True)
        self.background(lambda: test_translation(config), result, failed)

    def fetch_models(self):
        config = self.settings(); self.models_button.setEnabled(False)
        self.api_hint.setText('모델 목록을 가져오고 있습니다…')
        def result(models):
            current = self.model.currentText(); self.model.clear(); self.model.addItems(models)
            if current: self.model.setCurrentText(current)
            self.api_hint.setText(f'{len(models)}개 모델을 확인했습니다. 사용할 모델을 선택하세요.')
            self.models_button.setEnabled(True)
        def failed(message):
            self.api_hint.setText(message + ' 모델 ID를 직접 입력할 수도 있습니다.'); self.models_button.setEnabled(True)
        self.background(lambda: Client(config['url'], key=config['key'], timeout=10).models(), result, failed)

    def fetch_characters(self):
        work_id = self.current_work
        if not work_id: return
        self.character_button.setEnabled(False); self.status.setText('기본 등장인물 이름을 가져오고 있습니다…')
        def result(text):
            work = self.workspace.works[work_id]
            # Replace the catalog section, preserving the stable ID/title reference.
            work.reference = work.reference.split('\n\nAniList 등장인물', 1)[0] + '\n\n' + text
            self.selection_changed(); self.save(); self.status.setText('등장인물 이름을 가져왔습니다. 말투·관계는 메모로 보완하세요.')
        def failed(message): self.selection_changed(); self.status.setText(message)
        self.background(lambda: Catalog().character_notes(work_id), result, failed)

    def set_busy(self, busy):
        for control in self.mutating_controls: control.setEnabled(not busy)
        self.start_button.setEnabled(not busy); self.stop_button.setEnabled(busy)
        self.tree.setEnabled(not busy)

    def start(self):
        if self.worker is not None or self.background_tasks: return
        try:
            self.workspace.groups(); config = validate_settings(self.settings())
            if not self.output.text().strip(): raise ValueError('결과를 저장할 폴더를 선택하세요.')
            output = Path(self.output.text().strip()).expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc)); return
        for entry in self.workspace.entries: entry.status = '대기'
        self.refresh(); self.save(); self.results = []; self.progress_bar.setValue(0)
        self.worker = TranslationTask(self.workspace, config, self.data_dir, output, self)
        self.worker.progress.connect(self.job_progress); self.worker.completed.connect(self.job_complete)
        self.worker.error.connect(self.job_error); self.worker.finished.connect(self.job_finished)
        self.set_busy(True); self.status.setText('자막 파일을 확인하고 있습니다…'); self.worker.start()

    def job_progress(self, data):
        if 'index' in data:
            entry = self.workspace.entries[data['index']]
            entry.status = data.get('status', '번역 중'); self.rows[data['index']].setText(2, entry.status)
            if data.get('status') == '완료': self.save()
        if 'message' in data:
            self.wait_message = f"{data.get('work', '')} · {data['message']}"
            self.status.setText(self.wait_message)
        if data.get('phase') in ('request', 'retry'):
            self.request_started = time.monotonic(); self.wait_timer.start()
            self.show_wait_time()
        elif data.get('phase') == 'validated':
            self.wait_timer.stop(); self.request_started = None
        if data.get('total'):
            within = data.get('done', 0) / data['total']
            self.progress_bar.setValue(int(self.progress_bar.maximum() * (data.get('work_index', 1) - 1 + within) / data.get('work_total', 1)))

    def show_wait_time(self):
        if self.request_started is not None:
            elapsed = int(time.monotonic() - self.request_started)
            self.status.setText(self.wait_message + f' · 응답 대기 {elapsed}초')

    def job_complete(self, paths):
        self.wait_timer.stop(); self.request_started = None
        self.results = paths; self.progress_bar.setValue(self.progress_bar.maximum())
        self.status.setText(f'완료! {len(paths)}개 자막을 저장했습니다. 결과 열기에서 확인하세요.')

    def job_error(self, message):
        self.wait_timer.stop(); self.request_started = None
        for entry in self.workspace.entries:
            if entry.status == '번역 중': entry.status = '중단' if self.worker.cancel.is_set() else '오류'
            elif entry.status == '대기': entry.status = '준비'
        self.probe_result.setPlainText(message)
        self.settings_tabs.setCurrentIndex(1)
        self.status.setText(message.splitlines()[0] + ' · 번역 API 탭에서 상세 확인')

    def job_finished(self):
        self.wait_timer.stop(); self.request_started = None
        self.worker.deleteLater(); self.worker = None
        self.set_busy(False); self.refresh(); self.save()
        if self.pending_close and not self.background_tasks: self.close()

    def stop(self):
        self.wait_timer.stop(); self.request_started = None
        if self.worker is not None:
            self.worker.cancel.set(); self.stop_button.setEnabled(False)
            self.status.setText('중단 요청을 보냈습니다. 진행 중인 API 응답과 저장을 기다립니다…')

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and self.worker is None: event.acceptProposedAction()

    def dropEvent(self, event):
        if self.worker is None:
            self.add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()

    def closeEvent(self, event):
        if self.worker is not None or self.background_tasks:
            self.pending_close = True
            if self.worker is not None: self.stop()
            self.status.setText('진행 중인 요청을 마무리하고 종료합니다…'); event.ignore(); return
        self.save(); event.accept()


def main():
    parser = argparse.ArgumentParser(description='sub2loc 데스크톱 자막 번역기')
    parser.add_argument('--data-dir', type=Path, default=None)
    parser.add_argument('--smoke-test', type=Path, metavar='SCREENSHOT', help='GUI를 열고 스크린샷을 저장한 뒤 종료')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1]); app.setApplicationName('sub2loc'); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    window = MainWindow(args.data_dir); window.show()
    if args.smoke_test:
        def finish():
            args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.smoke_test)):
                app.exit(2); return
            app.quit()
        QTimer.singleShot(700, finish)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
