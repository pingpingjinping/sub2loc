import copy
from pathlib import Path
import tempfile
import unittest

from sub2loc.engine import Engine
from sub2loc.profiles import DEFAULT_TEMPLATE, MANGA_TEMPLATE, template_for
from sub2loc.prompt_library import OPTIONS, SOURCE_PROFILES, builtins, compile_source, library, personal
from test_identified import config, Model, ass


class PromptLibraryTests(unittest.TestCase):
    def test_general_unchanged_and_each_community_has_one_named_source(self):
        rows=builtins()
        self.assertEqual((rows[0]['anime'],rows[0]['manga']),(DEFAULT_TEMPLATE,MANGA_TEMPLATE))
        self.assertEqual(len(rows),3)
        for row in rows[1:]:
            self.assertTrue(row['experimental']);self.assertIn(row['published'],row['name'])
            self.assertEqual(row['source'],SOURCE_PROFILES[row['source_key']]['source'])
            self.assertNotIn('{{',row['anime']);self.assertNotIn('{{',row['manga'])
        self.assertFalse(rows[1]['options']['symbols'])
        self.assertTrue(rows[2]['options']['pronouns'])
        self.assertTrue(rows[2]['options']['notes'])
        self.assertNotEqual(rows[1]['manga'],rows[2]['manga'])

    def test_options_replace_the_policy_in_one_source_not_append_another_prompt(self):
        options={k:False for k in OPTIONS}
        off=compile_source('kone-mybest',options)
        on=compile_source('kone-mybest',dict(options,notes=True))
        self.assertIn(OPTIONS['notes']['off'],off['manga']);self.assertNotIn(OPTIONS['notes']['on'],off['manga'])
        self.assertIn(OPTIONS['notes']['on'],on['manga']);self.assertNotIn(OPTIONS['notes']['off'],on['manga'])
        self.assertEqual(on['manga'].count(OPTIONS['notes']['on']),1)
        for source,options in [('other',{}),('kone-mybest',{'notes':'true'}),('kone-mybest',{'unknown':True})]:
            with self.assertRaises(ValueError):compile_source(source,options)

    def test_personal_preserves_source_and_rejects_duplicates_or_invalid_storage(self):
        row=builtins()[1];item=personal('새 설정',row['anime'],row['manga'],source=row['source'],source_key=row['source_key'],options=row['options'])
        self.assertEqual(library([item])[-1],item)
        for bad in ([item,item],[dict(item,id='general')],{},[dict(item,manga='')],[dict(item,options={'x':True})]):
            with self.assertRaises(ValueError):library(bad)
        row['options']['adult']=False
        self.assertTrue(builtins()[1]['options']['adult'])

    def test_effective_prompt_only_and_isolated_context_prevents_cross_profile_history(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));model=Model();raw=ass(['猫'])
            base=config(use_summary=False)
            engine.translate(base,[('a.ass',raw)],client=model)
            count=len(model.calls)
            engine.translate(dict(base,manga_prompt_template='만화에서만 사용'),[('a.ass',raw)],client=model)
            self.assertEqual(len(model.calls),count)
            row=builtins()[1]
            choice=dict(base,prompt_template=row['anime'],manga_prompt_template=row['manga'],isolate_prompt_context=True)
            engine.translate(choice,[('a.ass',raw)],client=model)
            self.assertEqual(model.calls[-1][0]['recent_translations'],[])
            self.assertIn(row['anime'],model.calls[-1][2][0]['content'])
            self.assertNotIn(builtins()[2]['anime'],model.calls[-1][2][0]['content'])
            other=builtins()[2]
            engine.translate(dict(choice,prompt_template=other['anime'],manga_prompt_template=other['manga']),[('a.ass',raw)],client=model)
            self.assertEqual(model.calls[-1][0]['recent_translations'],[])
            count=len(model.calls)
            engine.translate(choice,[('a.ass',raw)],client=model)
            self.assertEqual(len(model.calls),count)
            self.assertEqual(template_for(dict(choice,input_format='balloon')),row['manga'])


try:
    from PySide6.QtWidgets import QApplication, QDialog
    from sub2loc.prompt_desktop import PromptLibraryDialog
    from sub2loc.desktop import MainWindow
    HAS_QT=True
except ImportError:HAS_QT=False


@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt for native UI tests')
class PromptDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def dialog(self):
        d=PromptLibraryDialog([],dict(id='current',name='현재',anime=DEFAULT_TEMPLATE,manga=MANGA_TEMPLATE));self.addCleanup(d.close);return d

    def test_source_options_preview_personal_copy_and_single_selection(self):
        d=self.dialog();self.assertEqual(d.selector.count(),4)
        d.selector.setCurrentIndex(d.selector.findData('kone-mybest'))
        original=d.manga.toPlainText();d.checks['notes'].setChecked(True);d.apply()
        self.assertIsNone(d.selected_profile);self.assertEqual(d.manga.toPlainText(),original)
        d.generate();self.assertNotEqual(d.manga.toPlainText(),original)
        d.name.setText('코네 내 설정');item=d.save_copy()
        self.assertEqual(len(d.saved),1);self.assertEqual(item['source_key'],'kone-mybest')
        d.apply();self.assertEqual(d.selected_profile['id'],item['id'])
        self.assertEqual(d.selected_profile['manga'],d.manga.toPlainText())
        self.assertEqual(builtins()[1]['manga'],original)

    def test_window_preserves_selected_pair_and_library_without_sending_unused_profiles(self):
        with tempfile.TemporaryDirectory() as root:
            w=MainWindow(Path(root));self.addCleanup(w.close)
            row=builtins()[1];saved=personal('저장본',row['anime'],row['manga'],source=row['source'])
            w.workspace.settings['prompt_library']=[saved]
            w.active_prompt_record=row;w.prompt_template.setPlainText(row['anime']);w.save()
            cfg=w.settings();self.assertNotIn('prompt_library',cfg);self.assertNotIn('active_prompt_profile',cfg)
            self.assertTrue(cfg['isolate_prompt_context'])
            self.assertEqual(template_for(dict(cfg,input_format='balloon')),row['manga'])
            restored=MainWindow(Path(root));self.addCleanup(restored.close)
            self.assertEqual(restored.workspace.settings['prompt_library'],[saved])
            self.assertEqual(template_for(dict(restored.settings(),input_format='balloon')),row['manga'])
            restored.prompt_template.setPlainText('직접 편집')
            self.assertNotIn('manga_prompt_template',restored.settings())
            self.assertEqual(template_for(dict(restored.settings(),input_format='balloon')),'직접 편집')
            restored.save();self.assertEqual(restored.workspace.settings['prompt_library'],[saved])
            w.close()

    def test_changed_source_does_not_inherit_old_dates_or_compiler(self):
        d=self.dialog();d.selector.setCurrentIndex(d.selector.findData('dc-wizone'))
        d.source.setText('https://example.org/new-prompt');d.name.setText('새 출처 직접 저장')
        item=d.save_copy()
        self.assertEqual(item['source_key'],'');self.assertEqual(item['published'],'');self.assertEqual(item['reviewed'],'')
        self.assertFalse(d.generate_button.isEnabled())


if __name__=='__main__':unittest.main()
