"""Choose one complete, independently sourced translation prompt."""
import copy

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout)
from PySide6.QtCore import Qt

from .prompt_library import OPTIONS, SOURCE_PROFILES, compile_source, library, personal


class PromptLibraryDialog(QDialog):
    def __init__(self, saved, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle('프롬프트 보관함 · 한 번에 하나만 적용')
        self.resize(940,760)
        self.saved=copy.deepcopy(saved)
        self.items=library(saved)
        self.current=copy.deepcopy(current)
        self.current['id']='current';self.current['name']='현재 편집본 · 유지'
        self.items.insert(0,self.current)
        self.selected_profile=None
        layout=QVBoxLayout(self)
        hint=QLabel('범용은 그대로 보관합니다. 커뮤니티 항목은 출처별 sub2loc 적용본이며 품질은 미검증입니다. 실행 시 한 항목의 애니/만화 본문 중 하나만 사용합니다.')
        hint.setWordWrap(True);layout.addWidget(hint)
        self.selector=QComboBox();layout.addWidget(self.selector)
        row=QHBoxLayout();row.addWidget(QLabel('저장 이름'));self.name=QLineEdit();row.addWidget(self.name);layout.addLayout(row)
        row=QHBoxLayout();row.addWidget(QLabel('출처 URL'));self.source=QLineEdit();row.addWidget(self.source);layout.addLayout(row)
        self.info=QLabel();self.info.setWordWrap(True);self.info.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.info)
        self.checks={}
        for key,spec in OPTIONS.items():
            check=QCheckBox(spec['label']);self.checks[key]=check;layout.addWidget(check)
        self.generate_button=QPushButton('선택 출처 + 체크옵션으로 미리보기 만들기')
        self.generate_button.clicked.connect(self.generate);layout.addWidget(self.generate_button)
        tabs=QTabWidget();self.anime=QPlainTextEdit();self.manga=QPlainTextEdit()
        tabs.addTab(self.anime,'애니 적용 본문');tabs.addTab(self.manga,'만화 적용 본문');layout.addWidget(tabs,1)
        self.status=QLabel('체크를 바꾼 뒤 미리보기 만들기를 누르세요. 편집 내용은 다른 이름으로 저장할 수 있습니다.')
        self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        buttons=QHBoxLayout()
        self.save_button=QPushButton('다른 이름으로 저장');self.save_button.clicked.connect(self.save_copy)
        self.delete_button=QPushButton('개인 항목 삭제');self.delete_button.clicked.connect(self.delete)
        self.apply_button=QPushButton('이 프롬프트만 적용');self.apply_button.clicked.connect(self.apply)
        self.cancel_button=QPushButton('닫기');self.cancel_button.clicked.connect(self.reject)
        for button in (self.save_button,self.delete_button,self.apply_button,self.cancel_button):buttons.addWidget(button)
        layout.addLayout(buttons)
        self.selector.currentIndexChanged.connect(self.load)
        self.fill('current')

    def fill(self,identity):
        self.selector.blockSignals(True);self.selector.clear()
        for item in self.items:self.selector.addItem(item['name'],item['id'])
        self.selector.setCurrentIndex(max(0,self.selector.findData(identity)));self.selector.blockSignals(False);self.load()

    def record(self):return self.items[self.selector.currentIndex()]

    def load(self,*_):
        item=self.record();self.name.setText(item['name']);self.source.setText(item.get('source',''))
        self.anime.setPlainText(item['anime']);self.manga.setPlainText(item['manga'])
        can_compile=item.get('source_key') in SOURCE_PROFILES
        for key,check in self.checks.items():
            check.setChecked(item.get('options',{}).get(key,False));check.setEnabled(can_compile)
        self.rendered_options={k:v.isChecked() for k,v in self.checks.items()}
        self.generate_button.setEnabled(can_compile)
        self.delete_button.setEnabled(item['id'].startswith('user:'))
        self.info.setText(' · '.join(x for x in (('게시 '+item['published']) if item.get('published') else '',
            ('확인 '+item['reviewed']) if item.get('reviewed') else '',item.get('changes',''),
            '성능 미검증' if item.get('experimental') else '') if x))
        self.status.setText('저장된 완성 본문을 표시합니다. 체크옵션은 미리보기 만들기를 눌러야 본문에 반영됩니다.')

    def generate(self):
        try:
            result=compile_source(self.record().get('source_key'),{k:v.isChecked() for k,v in self.checks.items()})
            self.anime.setPlainText(result['anime']);self.manga.setPlainText(result['manga'])
            self.rendered_options={k:v.isChecked() for k,v in self.checks.items()}
            self.status.setText('이 출처 하나로 미리보기를 갱신했습니다. 다른 이름으로 저장하거나 적용하세요.')
        except ValueError as exc:self.status.setText(str(exc))

    def draft(self):
        if self.rendered_options!={k:v.isChecked() for k,v in self.checks.items()}:
            raise ValueError('체크 변경 내용을 미리보기 만들기로 먼저 확인하세요.')
        item=self.record()
        metadata={k:copy.deepcopy(item[k]) for k in ('source_key','published','reviewed','changes','experimental') if k in item}
        metadata['source']=self.source.text().strip()
        if metadata['source']!=item.get('source',''):
            metadata.update(source_key='',published='',reviewed='',experimental=True,
                            changes='사용자가 다른 출처로 저장한 설정 · 본문과 출처 확인 필요')
        # Checkboxes are generation controls; saved text is the authoritative
        # runtime prompt, even after manual editing.
        metadata['options']=dict(self.rendered_options)
        return personal(self.name.text(),self.anime.toPlainText(),self.manga.toPlainText(),**metadata)

    def save_copy(self):
        try:
            if len(self.saved)>=50:raise ValueError('개인 프롬프트는 최대 50개까지 저장할 수 있습니다.')
            item=self.draft()
            if item['name']==self.record()['name']:item['name']=(item['name'][:90]+' · 개인본')
            self.saved.append(item);self.items=library(self.saved);self.items.insert(0,self.current);self.fill(item['id'])
            self.status.setText('개인 항목으로 저장했습니다. 기본 및 출처 항목은 유지합니다.')
            return item
        except ValueError as exc:self.status.setText(str(exc));return None

    def delete(self):
        item=self.record()
        if not item['id'].startswith('user:'):return
        self.saved=[r for r in self.saved if r['id']!=item['id']]
        self.items=library(self.saved);self.items.insert(0,self.current);self.fill('current')

    def apply(self):
        if self.rendered_options!={k:v.isChecked() for k,v in self.checks.items()}:
            self.status.setText('체크 변경 내용을 미리보기 만들기로 먼저 확인하세요.');return
        item=self.record()
        if (self.anime.toPlainText()!=item['anime'] or self.manga.toPlainText()!=item['manga']
                or self.source.text().strip()!=item.get('source','') or self.name.text()!=item['name']):
            item=self.save_copy()
            if item is None:return
        self.selected_profile=copy.deepcopy(item);self.accept()
