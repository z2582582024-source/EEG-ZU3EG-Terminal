"""Operator scope plus static minimum keyboard. No flicker on application start."""
from pathlib import Path
import json
import threading
import time

from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtWidgets import (QApplication,QCheckBox,QComboBox,QHBoxLayout,QLabel,
    QMainWindow,QPushButton,QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget,QTabWidget,
    QGridLayout)
from ..continuous.qt import Canvas
from ..continuous.protocol import CHANNELS,RATE
from .runtime import Manager,BUILD,atomic_json,ssh_command,activation
from ..operator_v2.ssvep import analyze,Typing
from ..operator_v2.text_input import TARGETS
from ..operator_v2.panel import render_panel


class Window(QMainWindow):
    def __init__(self):
        super().__init__();self.manager=Manager();self.paused=False;self.closing=False
        self.typing=Typing();self.analysis_busy=False;self.last_analysis=-1;self.latest_decision=None
        self.typing_epoch=0;self.panel_busy=False;self.panel_pending=None;self.panel_result=None
        self.display_requested=False;self.live_display_scope=False;self.published_revision=None
        self.display_start_ready=False
        self.input_provenance='STATIC; neural typing NOT VALIDATED'
        self.presentation=None;self.analysis_result=None;self.last_action='NO_DECISION';self.evidence_count=0
        self.display_reply='板端显示未请求；刺激OFF'
        font=Path('C:/Windows/Fonts/msyh.ttc')
        if font.exists():QFontDatabase.addApplicationFont(str(font));QApplication.instance().setFont(QFont('Microsoft YaHei',9))
        self.setWindowTitle('P5B SSVEP · 中英文 / 实时采集 · 主机CCA基线');self.resize(1430,960)
        root=QWidget();layout=QVBoxLayout(root);self.setCentralWidget(root)
        self.banner=QLabel('DISCONNECTED · 不自动采集 / 不自动闪烁');self.banner.setStyleSheet('font-size:20px;font-weight:bold');layout.addWidget(self.banner)
        controls=QHBoxLayout();self.live=QPushButton('Start 真实采集');self.sim=QPushButton('Start SIMULATED')
        self.stop_button=QPushButton('Stop 采集并等待目标收尾');self.pause=QPushButton('暂停绘图（采集继续）')
        self.record=QComboBox();self.record.addItems(['不录制原码，仅有界实时视图','分段录制：32MiB/段，总2GiB，满只停录制'])
        self.record_toggle=QPushButton('应用录制开关（采集独立）')
        for w in (self.live,self.sim,self.stop_button,self.pause,self.record,self.record_toggle):controls.addWidget(w)
        layout.addLayout(controls)
        self.live.clicked.connect(lambda:self.start(False));self.sim.clicked.connect(lambda:self.start(True))
        self.stop_button.clicked.connect(self.stop);self.pause.clicked.connect(self.toggle_pause)
        self.record_toggle.clicked.connect(self.toggle_recording)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        scope=QWidget();sl=QVBoxLayout(scope);self.tabs.addTab(scope,'16通道原码与质量')
        opts=QHBoxLayout();self.scale=QComboBox();self.scale.addItems(['原码全量程','±1000000 counts','±100000 counts','±10000 counts'])
        self.seconds=QComboBox();self.seconds.addItems(['2','5','10','30']);self.seconds.setCurrentIndex(1)
        self.mean=QCheckBox('窗口去均值：仅处理显示，不能修复原码饱和')
        for w in (self.scale,self.seconds,self.mean):opts.addWidget(w)
        sl.addLayout(opts)
        channels=QGridLayout();self.checks=[]
        for i,c in enumerate(CHANNELS):
            ch=QCheckBox(c);ch.setChecked(True);channels.addWidget(ch,i//8,i%8);self.checks.append(ch)
        sl.addLayout(channels);self.canvas=Canvas();sl.addWidget(self.canvas,1)
        self.table=QTableWidget(3,16);self.table.setHorizontalHeaderLabels(CHANNELS)
        self.table.setVerticalHeaderLabels(['最新原码','全次均值 counts','原码端点 数量 / 比例']);self.table.setMaximumHeight(142);sl.addWidget(self.table)
        keyboard=QWidget();kl=QVBoxLayout(keyboard);self.tabs.addTab(keyboard,'SSVEP中英双语键盘（默认静态）')
        language_bar=QHBoxLayout();language_bar.addWidget(QLabel('打字模式 / Typing mode'))
        self.language=QComboBox();self.language.addItems(['EN · English','ZH · 中文拼音']);language_bar.addWidget(self.language)
        self.manual=QCheckBox('SIMULATED 键鼠流程测试（不是脑电输入；真实采集时禁用）');language_bar.addWidget(self.manual)
        kl.addLayout(language_bar);self.language.currentIndexChanged.connect(self.change_language)
        self.text=QLabel('输出：');self.text.setStyleSheet('font-size:32px');kl.addWidget(self.text)
        self.pending=QLabel('待确认：');self.pending.setWordWrap(True);kl.addWidget(self.pending)
        self.input_help=QLabel();self.input_help.setWordWrap(True);kl.addWidget(self.input_help)
        tiles=QGridLayout();self.tiles=[]
        for i,label in enumerate(TARGETS):
            tile=QPushButton(label);tile.setMinimumHeight(60)
            tile.setStyleSheet('background:#24374f;color:white;font-size:20px;border:2px solid #7790aa');tiles.addWidget(tile,i//7,i%7)
            tile.clicked.connect(lambda checked=False,target=label:self.manual_target(target));self.tiles.append(tile)
        kl.addLayout(tiles)
        self.display_start=QPushButton('同步已启动的板端键盘');self.display_stop=QPushButton('停止板端显示')
        dp=QHBoxLayout();dp.addWidget(self.display_start);dp.addWidget(self.display_stop);kl.addLayout(dp)
        self.display_start.clicked.connect(lambda:self.display_control('start',topic='display'))
        self.display_stop.clicked.connect(lambda:self.display_control('stop',topic='display'))
        self.stimulus=QPushButton('启用已批准的板端刺激（待部署/启用）');self.cancel_stimulus=QPushButton('取消刺激 / 返回静态')
        st=QHBoxLayout();st.addWidget(self.stimulus);st.addWidget(self.cancel_stimulus);kl.addLayout(st)
        self.stimulus.clicked.connect(lambda:self.display_control('start'));self.cancel_stimulus.clicked.connect(lambda:self.display_control('stop'))
        self.control_state=QLabel(self.display_reply);self.control_state.setWordWrap(True);kl.addWidget(self.control_state)
        self.decision=QLabel('NO_DECISION · 等待4秒数据及有效呈现时序；本机页面不闪烁');self.decision.setWordWrap(True);kl.addWidget(self.decision)
        notice=QLabel('PO7/PO3/POz/PO4/PO8/O1/Oz/O2；CCA为去趋势/4–45Hz处理视图。原码端点、50Hz主导、断流或低置信时不提交。\nSIMULATED/FILE_REPLAY只证明流程。实际脑电输入字符尚未验证。')
        notice.setWordWrap(True);kl.addWidget(notice);kl.addStretch()
        self.status=QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        self.paths=QLabel(str(BUILD/'runs'));layout.addWidget(self.paths)
        self.timer=QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.tick);self.timer.start()
        self._refresh_typing()
        self.tick()

    def start(self,simulation):
        try:
            if not simulation and not self.display_start_ready:
                # An existing ready static display may be adopted only from
                # this candidate's real deployment receipt; never restart it.
                receipt=activation()
                if receipt.get('static_display_enabled') is not True:raise RuntimeError('STATIC_DISPLAY_READY_REQUIRED')
            self.manager.start('segments' if self.record.currentIndex() else 'off',simulation=simulation)
            self.typing_epoch+=1;self.typing=Typing('ZH' if self.language.currentIndex() else 'EN')
            self.last_analysis=-1;self.evidence_count=0;self.analysis_result=None
            self.live_display_scope=not simulation and activation().get('static_display_enabled') is True
            self.input_provenance='SIMULATED; NOT neural input' if simulation else 'LIVE_STREAM; neural validation pending'
            self._refresh_typing();self._queue_panel()
        except Exception as exc:self.status.setText(str(exc))

    def stop(self):
        if self.manager.current:self.manager.current.stop()
        if self.live_display_scope or self.display_requested:self.display_control('stop',quiet=True)

    def _refresh_typing(self):
        state=self.typing.panel_state();self.text.setText('输出：'+state['text'])
        self.pending.setText(f"{state['language']} / {state['stage']} · 拼音：{state['composition']} · 待确认：{state['pending']} · 页 {state['page']+1}/{state['page_count']}")
        self.input_help.setText('EN：A–Z选择字母，CONFIRM提交；DELETE取消待确认或删除。' if state['language']=='EN' else
            '中文：A–Z输入无声调拼音（ü用V），CONFIRM选字；候选页A–X选择、Y上一页/Z下一页，CONFIRM提交。DELETE取消选择/返回拼音/退格。精确离线词典，不支持任意自动分词。')
        for tile,label in zip(self.tiles,state['labels']):tile.setText(label)

    def change_language(self,index):
        self.typing_epoch+=1;self.typing.set_language('ZH' if index else 'EN');self.analysis_result=None
        self._refresh_typing();self._queue_panel()

    def manual_target(self,target):
        s=self.manager.current
        if not self.manual.isChecked() or (s and not s.done.is_set() and s.snapshot(False)['source']=='LIVE_STREAM'):return
        self.typing_epoch+=1;self.analysis_result=None;self.input_provenance='SIMULATED_MANUAL; NOT neural input'
        action=self.typing.manual_target(target);self._refresh_typing()
        self._queue_panel()
        self.decision.setText('SIMULATED_MANUAL_KEYBOARD · '+action+' · 不证明SSVEP或真实脑电输入')

    def _queue_panel(self):
        # A language click at initial DISCONNECTED must never connect to a target.
        if not (self.display_requested or self.live_display_scope):return
        if (self.presentation or {}).get('state')=='STIMULUS_ACTIVE':
            if self.published_revision==self.typing.keyboard_revision:
                # Keep the presentation map stable while letters are decoded;
                # output text remains current in the host GUI.
                return
            self.display_control('stop',topic='stimulus')
            self.panel_waits_for_static=True;return
        packet,_=render_panel(self.typing.panel_state(),provenance=self.input_provenance)
        self.panel_pending=(packet,self.typing.keyboard_revision,self.typing_epoch,self.typing.revision)

    def _publish_keyboard_panel(self):
        if self.panel_busy or not self.panel_pending:return
        packet,revision,epoch,state_revision=self.panel_pending;self.panel_pending=None;self.panel_busy=True
        def work():
            try:
                response=json.loads(ssh_command(['eegctl','panel'],input_data=packet))
                if response.get('panel_published') is not True or response.get('keyboard_revision')!=revision:
                    raise RuntimeError('PANEL_PUBLICATION_RECEIPT_MISMATCH')
                self.panel_result=(epoch,state_revision,revision,response,None)
            except Exception as exc:self.panel_result=(epoch,state_revision,revision,None,repr(exc))
            finally:self.panel_busy=False
        threading.Thread(target=work,name='panel-bounded-publication',daemon=True).start()

    def toggle_pause(self):
        self.paused=not self.paused;self.pause.setText('继续绘图' if self.paused else '暂停绘图（采集继续）')

    def toggle_recording(self):
        if self.manager.current and not self.manager.current.done.is_set():
            self.manager.current.set_recording(self.record.currentIndex()==1)

    def display_control(self,action,quiet=False,topic='stimulus'):
        if topic=='display' and action=='start':
            try:
                if activation().get('static_display_enabled') is not True:raise RuntimeError('STATIC_READY_NOT_PROVEN')
                self.display_requested=True;self._queue_panel();self.display_reply='同步键盘；复用已就绪显示进程'
            except Exception as exc:self.display_reply=str(exc)
            return
        if topic=='stimulus' and action=='start':
            s=self.manager.current;presentation=self.presentation or {}
            if s is None or s.done.is_set() or s.snapshot(False)['source']!='LIVE_STREAM':
                self.display_reply='先开始真实连续采集；静态键盘保持';return
            if presentation.get('timing_valid') is not True or presentation.get('missed_vblanks'):
                self.display_reply='垂直同步尚未稳定；刺激保持关闭';return
            if presentation.get('keyboard_revision')!=self.published_revision:
                self.display_reply='板端键盘映射尚未确认；刺激保持关闭';return
        if topic=='display' and action=='stop':self.display_requested=False;self.panel_pending=None
        def work():
            try:
                reply=ssh_command(['eegctl',topic,action])
                self.display_reply=reply
                if topic=='display' and action=='start':self.display_requested=True;self.display_start_ready=True
            except Exception as exc:
                if not quiet:self.display_reply=str(exc)
        self.display_reply='REQUEST_PENDING';threading.Thread(target=work,daemon=True).start()

    def _classify(self,snapshot,session,epoch,revision,keyboard_revision,presentation):
        try:
            n=int(4*RATE);rows=snapshot['samples'][-n:];first=snapshot['frames']-len(rows)
            simulated=snapshot['source']=='SIMULATED'
            refresh=60. if simulated else (presentation or {}).get('refresh_hz')
            d=analyze(rows,first,refresh_hz=refresh,source=snapshot['source'],fresh=snapshot['fresh'],
                      sequence_ok=not snapshot['failure'],presentation=presentation,keyboard_revision=keyboard_revision)
            # Respect Recording Off. Classification only consumes the bounded
            # memory window. Opted-in segments are referenced, never duplicated.
            references=session.window_recording(d.first_sequence,d.last_sequence)
            value=d.to_dict();value.update(
                raw_evidence=references,recording_provenance='RECORDED_SEGMENTS_PROVISIONAL' if references else 'UNRECORDED_ONLINE_ONLY',
                dma_completion_anchors=[a for a in snapshot.get('dma_completion_anchors',[])
                    if a['first_sequence']+a['frames']>d.first_sequence and a['first_sequence']<=d.last_sequence],
                presentation=presentation,real_ssvep_validation=False)
            self.analysis_result=(session,epoch,revision,d,value)
        except Exception as exc:self.analysis_result=(session,epoch,revision,None,{'reason':repr(exc),'outcome':'NO_DECISION'})
        finally:self.analysis_busy=False

    def _consume_analysis(self):
        result=self.analysis_result;self.analysis_result=None
        if not result:return
        session,epoch,revision,d,value=result
        # Only the GUI thread can mutate typing. An old worker cannot commit a
        # candidate after session, language, composition or target labels change.
        if session is not self.manager.current or epoch!=self.typing_epoch or revision!=self.typing.revision:return
        if session.done.is_set():self.typing.reset_dwell();return
        action=self.typing.apply(d,evidence_saved=bool(value.get('raw_evidence'))) if d else 'NO_DECISION'
        if d is None:self.typing.reset_dwell()
        value.update(action=action,**self.typing.panel_state(),input_provenance=self.input_provenance)
        session.log_json('latest_classification.json',value);self.decision.setText(json.dumps(value,ensure_ascii=False)[:2200])
        if self.typing.revision!=revision:
            self._refresh_typing()
            if value.get('source')=='LIVE_STREAM':self._queue_panel()

    def tick(self):
        self._consume_analysis()
        if self.display_start_ready:self.display_start_ready=False;self._queue_panel()
        if self.panel_result:
            epoch,state_revision,revision,response,error=self.panel_result;self.panel_result=None
            if epoch==self.typing_epoch and state_revision==self.typing.revision:
                if error:self.display_reply='PANEL_PUBLICATION_FAILED; no automatic retry: '+error
                else:self.published_revision=revision;self.display_reply=json.dumps(response)
        self._publish_keyboard_panel()
        self.control_state.setText(self.display_reply)
        s=self.manager.current
        if s:s.touch_ui()
        summary=self.manager.poll()
        running=s is not None and not s.done.is_set()
        live_running=running and s.snapshot(False)['source']=='LIVE_STREAM'
        if not running:self.live_display_scope=False
        self.manual.setEnabled(not live_running)
        for tile in self.tiles:tile.setEnabled(self.manual.isChecked() and not live_running)
        self.live.setEnabled(not running and not self.manager.blocked)
        try:deployed=activation();scope_ready=True
        except Exception:deployed={};scope_ready=False
        self.live.setEnabled(not running and not self.manager.blocked and scope_ready)
        self.display_start.setEnabled(scope_ready)
        self.display_stop.setEnabled(scope_ready)
        self.stimulus.setEnabled(scope_ready and live_running and deployed.get('stimulus_enabled') is True)
        self.sim.setEnabled(not running and not self.manager.blocked);self.stop_button.setEnabled(running)
        self.record_toggle.setEnabled(running)
        if s:
            snap=s.snapshot();self.banner.setText(f"{snap['source']} · {snap['state']} · {snap['frames']} 帧")
            self.presentation=snap['target'].get('presentation')
            if getattr(self,'panel_waits_for_static',False) and (self.presentation or {}).get('state')=='STATIC':
                self.panel_waits_for_static=False;self._queue_panel()
            self.paths.setText(str(s.folder))
            self.status.setText(f"记录={snap['recording']} / {snap['saved_bytes']} bytes；录制提示={snap['recording_error']}；日志={snap['logging_error'] or '可写'}；原码CRC={snap['raw_crc32']}；队列={snap['queue_current']}/{snap['queue_high']}；新鲜度={snap['data_age_seconds']}；目标错误={snap['target'].get('core_errors','UNKNOWN')}/{snap['target'].get('dma_errors','UNKNOWN')}；{snap['failure'] or snap['stop_reason'] or ''}")
            if not self.paused:
                self.canvas.rows=snap['samples'];self.canvas.selected=[i for i,w in enumerate(self.checks) if w.isChecked()]
                self.canvas.half_range=(8388608,1000000,100000,10000)[self.scale.currentIndex()]
                self.canvas.seconds=float(self.seconds.currentText());self.canvas.center=self.mean.isChecked();self.canvas.update()
            for i in range(16):
                vals=[snap['samples'][-1][i] if snap['samples'] else 0,f"{snap['means'][i]:.1f}",f"{snap['saturation'][i]}/{snap['saturation'][i]/max(1,snap['frames']):.1%}"]
                for r,v in enumerate(vals):self.table.setItem(r,i,QTableWidgetItem(str(v)))
            if not self.analysis_busy and snap['frames']-self.last_analysis>=244 and running:
                self.analysis_busy=True;self.last_analysis=snap['frames']
                threading.Thread(target=self._classify,args=(snap,s,self.typing_epoch,self.typing.revision,self.typing.keyboard_revision,dict(self.presentation or {})),name='CCA-bounded-window',daemon=True).start()
        if self.closing and not running:self.manager.close();self.timer.stop();QApplication.instance().quit()

    def closeEvent(self,event):
        if self.manager.current and not self.manager.current.done.is_set():
            self.closing=True;self.stop();event.ignore();return
        self.manager.close();event.accept()


def run():
    app=QApplication.instance() or QApplication([]);window=Window();window.show();return app.exec()
