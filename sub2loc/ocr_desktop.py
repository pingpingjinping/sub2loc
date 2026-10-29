"""Explicit review of image-free OCR proposals, isolated from translation jobs."""
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .api import safe_preview
from .engine import Cancelled
from .ocr_review import ReviewSession


class ReviewTask(QThread):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, function, key, parent):
        super().__init__(parent)
        self.function, self.key = function, key
        self.cancel = threading.Event()

    def check(self):
        if self.cancel.is_set(): raise Cancelled('OCR 검수를 중단했습니다. 완료된 검수 기록은 보관했습니다.')

    def run(self):
        try: self.result.emit(self.function(self.check, self.progress.emit))
        except Exception as exc: self.error.emit(safe_preview(str(exc), self.key, 1000))
        finally: self.key = ''


class OcrReviewDialog(QDialog):
    def __init__(self, source, root, config, output='', parent=None):
        super().__init__(parent)
        self.setWindowTitle('벌룬 OCR 의심 구간 검수')
        self.resize(1080, 760)
        self.session = ReviewSession(source, root)
        self.config = dict(config)
        self.task = None
        self.pending_close = False
        self.index = None
        self.new_output = None
        layout = QVBoxLayout(self)
        title = QLabel('원문 유지 또는 교정 후보로 선택 블록만 다시 번역')
        title.setWordWrap(True); layout.addWidget(title)
        hint = QLabel('이미지 없이 만든 추정 후보입니다. 원문·기존 번역은 보존하며, 적용하면 별도 TXT를 저장합니다. 기본 번역·요약 기록은 변경하지 않습니다.')
        hint.setWordWrap(True); layout.addWidget(hint)
        top = QHBoxLayout()
        self.only_suspects = QCheckBox('의심 구간만 표시'); self.only_suspects.setChecked(True)
        self.only_suspects.toggled.connect(self.refresh)
        self.thinking = QComboBox()
        for label, value in [('OCR 검수 Thinking Low', 'low'), ('OCR 검수 Thinking OFF', 'none'), ('OCR 검수 Thinking Medium', 'medium')]:
            self.thinking.addItem(label, value)
        self.thinking.setEnabled(config.get('provider') == 'polystrata')
        self.thinking.setToolTip('PolyStrata의 OCR 모델 검수·후보 생성에만 적용합니다. 기본 번역과 선택 블록 재번역의 Thinking은 번역 API 설정을 따릅니다.')
        self.scan_button = QPushButton('모델로 전체 OCR 검수'); self.scan_button.clicked.connect(self.scan)
        self.load_button = QPushButton('번역 TXT 불러오기'); self.load_button.clicked.connect(self.choose_translation)
        top.addWidget(self.only_suspects); top.addWidget(self.thinking); top.addWidget(self.scan_button); top.addWidget(self.load_button)
        layout.addLayout(top)
        splitter = QSplitter()
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(['페이지 / 블록', '상태', '의심 이유'])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setColumnWidth(0, 185); self.tree.setColumnWidth(1, 95)
        self.tree.itemSelectionChanged.connect(self.selected)
        splitter.addWidget(self.tree)
        editor = QWidget(); form = QVBoxLayout(editor)
        self.original = QPlainTextEdit(); self.original.setReadOnly(True); self.original.setMaximumHeight(110)
        self.current = QPlainTextEdit(); self.current.setReadOnly(True); self.current.setMaximumHeight(110)
        self.candidate = QPlainTextEdit(); self.candidate.setMaximumHeight(110)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(110)
        self.reason = QLabel(); self.reason.setWordWrap(True); self.reason.setTextFormat(Qt.TextFormat.PlainText)
        for label, widget in [('원본 OCR', self.original), ('현재 번역', self.current), ('일본어 교정 후보 · 직접 수정 가능', self.candidate), ('재번역 미리보기', self.preview)]:
            form.addWidget(QLabel(label)); form.addWidget(widget)
        form.addWidget(self.reason); form.addStretch()
        self.candidate.textChanged.connect(self.invalidate_preview)
        splitter.addWidget(editor); splitter.setSizes([490, 570]); layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        self.keep_button = QPushButton('그대로 읽기'); self.keep_button.clicked.connect(self.keep)
        self.propose_button = QPushButton('선택 블록 교정 후보 만들기'); self.propose_button.clicked.connect(self.propose)
        self.translate_button = QPushButton('후보로 다시 번역'); self.translate_button.clicked.connect(self.retranslate)
        self.apply_button = QPushButton('이 번역 적용 · 새 TXT 저장'); self.apply_button.clicked.connect(self.apply)
        self.close_button = QPushButton('닫기'); self.close_button.clicked.connect(self.reject)
        for widget in (self.keep_button, self.propose_button, self.translate_button, self.apply_button, self.close_button): actions.addWidget(widget)
        layout.addLayout(actions)
        self.status = QLabel('기본 반복·문자 검사만 완료했습니다. 모델 검수는 버튼을 눌러 실행하세요. 의심 표시가 없어도 정확성을 보장하지 않습니다.')
        self.status.setTextFormat(Qt.TextFormat.PlainText); self.status.setWordWrap(True); layout.addWidget(self.status)
        if output and Path(output).is_file():
            try: self.session.load_translation(output)
            except (ValueError, OSError) as exc: self.status.setText(str(exc))
        self.refresh()

    def controls(self):
        return (self.scan_button, self.load_button, self.tree, self.only_suspects, self.candidate,
                self.keep_button, self.propose_button, self.translate_button, self.apply_button)

    def refresh(self, *_):
        selected = self.index
        self.tree.clear()
        for cue in self.session.doc.cues:
            row = self.session.rows[str(cue.id)]; issues = self.session.issues(cue.id)
            if self.only_suspects.isChecked() and not issues: continue
            state = {'keep':'원문 유지', 'applied':'수정본 저장'}.get(row.get('decision'), '의심' if issues else '미판정')
            item = QTreeWidgetItem([f'{cue.page} / {cue.block}', state, ' · '.join(issues)])
            item.setData(0, Qt.ItemDataRole.UserRole, cue.id); self.tree.addTopLevelItem(item)
            if cue.id == selected: self.tree.setCurrentItem(item)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount(): self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.selected()

    def selected(self):
        item = self.tree.currentItem()
        self.index = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        self.session.generated = None
        if self.index is None:
            for widget in (self.original, self.current, self.candidate, self.preview): widget.clear()
            self.reason.setText('의심 구간이 없으면 전체 표시로 바꾸거나 모델 검수를 실행하세요.')
        else:
            cue = self.session.doc.cues[self.index]; row = self.session.rows[str(self.index)]
            self.original.setPlainText(cue.text); self.current.setPlainText(self.session.current_translation(self.index))
            self.candidate.setPlainText(row.get('candidate') or '')
            self.preview.clear(); self.reason.setText(row.get('reason') or '이미지 미확인 · 의심 표시는 오류 확정이 아닙니다.')
        self.update_buttons()

    def update_buttons(self):
        busy = self.task is not None
        for widget in self.controls(): widget.setEnabled(not busy)
        self.thinking.setEnabled(not busy and self.config.get('provider') == 'polystrata')
        for widget in (self.keep_button, self.propose_button): widget.setEnabled(not busy and self.index is not None)
        self.translate_button.setEnabled(not busy and self.index is not None and self.session.translated is not None and bool(self.candidate.toPlainText().strip()))
        self.apply_button.setEnabled(not busy and self.session.generated is not None and not self.session.generated['issues'])

    def invalidate_preview(self):
        self.session.generated = None; self.preview.clear(); self.update_buttons()

    def run_task(self, function, callback):
        if self.task is not None: return
        task = ReviewTask(function, self.config.get('key', ''), self); self.task = task
        task.progress.connect(self.status.setText)
        def failed(message):
            self.refresh(); self.status.setText(message)
        task.error.connect(failed)
        task.result.connect(callback)
        def finished():
            self.task = None; task.deleteLater(); self.update_buttons()
            if self.pending_close: self.reject()
        task.finished.connect(finished); self.update_buttons(); task.start()

    def review_config(self):
        return dict(self.config, ocr_thinking=self.thinking.currentData())

    def scan(self):
        config = self.review_config()
        def done(count):
            self.refresh(); self.status.setText(f'{count}개 블록 모델 검수 완료 · 의심 판정·후보는 이미지 미확인 추정입니다.')
        self.run_task(lambda check, progress:self.session.scan(config, check=check, progress=progress), done)

    def propose(self):
        if self.index is None: return
        index = self.index; config = self.review_config()
        def done(_):
            self.refresh(); self.status.setText('교정 후보를 확인하세요. 후보가 없으면 복구 근거가 부족하거나 모델이 의심하지 않은 경우입니다.')
        self.run_task(lambda check, progress:self.session.scan(config, indices=[index], check=check, progress=progress), done)

    def retranslate(self):
        if self.index is None: return
        index, candidate = self.index, self.candidate.toPlainText().strip()
        self.invalidate_preview()
        def done(result):
            self.preview.setPlainText(result['text'])
            self.status.setText('재번역 검수: '+', '.join(result['issues']) if result['issues'] else '미리보기를 확인한 뒤 적용하세요. 아직 파일은 변경하지 않았습니다.')
        self.run_task(lambda check, progress:self.session.retranslate(index, candidate, self.config, check=check), done)

    def keep(self):
        if self.index is None: return
        try: self.session.keep(self.index); self.refresh(); self.status.setText('기존 번역을 그대로 유지했습니다. 의심 기록은 남겨 둡니다.')
        except (ValueError, OSError) as exc: self.status.setText(str(exc))

    def apply(self):
        if self.index is None: return
        try:
            self.new_output = self.session.apply(self.index, self.candidate.toPlainText().strip(), self.preview.toPlainText())
            self.refresh(); self.status.setText('선택 블록만 적용한 새 TXT 저장: '+str(self.new_output))
        except (ValueError, OSError) as exc: self.status.setText(str(exc))

    def choose_translation(self):
        path, _ = QFileDialog.getOpenFileName(self, '같은 원문의 번역 TXT 선택', '', '벌룬 번역 TXT (*.txt)')
        if not path: return
        try: self.session.load_translation(path); self.refresh(); self.status.setText('페이지·블록 구조를 확인했습니다. 같은 원문의 번역인지 내용도 확인하세요.')
        except (ValueError, OSError) as exc: self.status.setText(str(exc))

    def reject(self):
        if self.task is not None:
            self.pending_close = True; self.task.cancel.set()
            self.status.setText('중단 요청 · 진행 중인 API 응답이 끝나면 닫습니다.'); return
        self.config.pop('key', None)
        super().reject()

    def closeEvent(self, event):
        if self.task is not None: self.reject(); event.ignore()
        else: self.config.pop('key', None); super().closeEvent(event)
