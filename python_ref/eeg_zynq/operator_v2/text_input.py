"""Bounded English/Chinese typing; target IDs stay A-Z, CONFIRM, DELETE.

Chinese is exact, tone-free pinyin using an attributed offline dictionary. It is
not a neural classifier and does not infer the provenance of input targets.
"""
from functools import lru_cache
import json
from pathlib import Path

LETTERS=tuple('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
TARGETS=LETTERS+('CONFIRM','DELETE')
TEXT_LIMIT=256
COMPOSITION_LIMIT=32
PAGE_SIZE=24

@lru_cache(maxsize=1)
def dictionary():
    return json.loads((Path(__file__).with_name('resources')/'pinyin_simp.json').read_text(encoding='utf-8'))

class TextInput:
    def __init__(self,language='EN'):
        self.text='';self.pending='';self.composition='';self.candidates=();self.page=0
        self.stage='LETTERS';self.language='EN';self.revision=1;self.keyboard_revision=1
        if language!='EN':self.set_language(language)

    def _changed(self,keyboard=False):
        self.revision+=1
        if keyboard:self.keyboard_revision+=1

    def set_language(self,language):
        if language not in ('EN','ZH'):raise ValueError('LANGUAGE_MUST_BE_EN_OR_ZH')
        if language==self.language:return 'LANGUAGE_UNCHANGED'
        self.language=language;self.pending='';self.composition='';self.candidates=();self.page=0;self.stage='LETTERS'
        self._changed(keyboard=True);return 'LANGUAGE_CHANGED_PENDING_CANCELLED'

    def activate(self,target):
        if target not in TARGETS:return 'TARGET_NOT_ALLOWED'
        before=self.state();mapping_before=tuple(before['labels'])
        action=self._activate(target)
        after=self.state()
        if before!=after:self._changed(keyboard=mapping_before!=tuple(after['labels']))
        return action

    def _commit(self,word):
        if len(self.text)+len(word)>TEXT_LIMIT:return 'TEXT_LIMIT_CLEAR_OR_EXPORT'
        self.text+=word;self.pending='';self.composition='';self.candidates=();self.page=0;self.stage='LETTERS'
        return 'COMMITTED'

    def _activate(self,target):
        if target=='DELETE':
            if self.pending:self.pending='';return 'PENDING_CANCELLED'
            if self.stage=='CANDIDATES':self.stage='LETTERS';self.candidates=();self.page=0;return 'BACK_TO_PINYIN'
            if self.composition:self.composition=self.composition[:-1];return 'PINYIN_DELETED'
            self.text=self.text[:-1];return 'DELETED'
        if self.language=='EN':
            if target in LETTERS:self.pending=target;return 'SELECTED_'+target
            if target=='CONFIRM' and self.pending:return self._commit(self.pending)
            return 'NO_PENDING_TEXT'
        if self.stage=='CANDIDATES':
            pages=max(1,(len(self.candidates)+PAGE_SIZE-1)//PAGE_SIZE)
            if target in ('Y','Z'):
                step=-1 if target=='Y' else 1
                self.page=(self.page+step)%pages;self.pending='';return 'CANDIDATE_PAGE'
            if target in LETTERS[:PAGE_SIZE]:
                index=self.page*PAGE_SIZE+LETTERS.index(target)
                if index>=len(self.candidates):return 'NO_CANDIDATE_AT_TARGET'
                self.pending=self.candidates[index];return 'CANDIDATE_SELECTED'
            if target=='CONFIRM' and self.pending:return self._commit(self.pending)
            return 'SELECT_CANDIDATE_BEFORE_CONFIRM'
        if target in LETTERS:
            if len(self.composition)>=COMPOSITION_LIMIT:return 'PINYIN_LIMIT'
            self.composition+=target.lower();return 'PINYIN_APPENDED'
        if target=='CONFIRM':
            if not self.composition:return 'NO_PINYIN'
            self.candidates=tuple(dictionary().get(self.composition,()))
            if not self.candidates:return 'NO_EXACT_PINYIN_CANDIDATE_EDIT_COMPOSITION'
            self.stage='CANDIDATES';self.page=0;self.pending='';return 'CANDIDATES_OPENED'
        return 'NO_PENDING_TEXT'

    def labels(self):
        if self.language=='ZH' and self.stage=='CANDIDATES':
            visible=self.candidates[self.page*PAGE_SIZE:(self.page+1)*PAGE_SIZE]
            return [letter+'\n'+(visible[i] if i<len(visible) else '—') for i,letter in enumerate(LETTERS[:PAGE_SIZE])]+['Y\n上一页','Z\n下一页','CONFIRM\n提交','DELETE\n返回/删']
        return list(LETTERS)+(['CONFIRM\n选字','DELETE\n退格'] if self.language=='ZH' else ['CONFIRM','DELETE'])

    def state(self):
        return dict(language=self.language,stage=self.stage,text=self.text,pending=self.pending,
            composition=self.composition,page=self.page,page_count=max(1,(len(self.candidates)+PAGE_SIZE-1)//PAGE_SIZE),
            candidates=list(self.candidates[self.page*PAGE_SIZE:(self.page+1)*PAGE_SIZE]),
            labels=self.labels(),revision=self.revision,keyboard_revision=self.keyboard_revision,
            dictionary='RIME_PINYIN_SIMP_EXACT_APACHE_2_0')
