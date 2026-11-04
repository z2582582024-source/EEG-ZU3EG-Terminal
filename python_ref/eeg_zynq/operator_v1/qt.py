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
from .runtime import Manager,BUILD,atomic_json,ssh_command
from .ssvep import analyze,Typing


class Window(QMainWindow):
    def __init__(self):
        super().__init__();self.manager=Manager();self.paused=False;self.closing=False
        self.typing=Typing();self.analysis_busy=False;self.last_analysis=-1;self.latest_decision=None
        self.presentation=None;self.analysis_result=None;self.last_action='NO_DECISION';self.evidence_count=0
        self.display_reply='板端显示未请求；刺激OFF'
        font=Path('C:/Windows/Fonts/msyh.ttc')
        if font.exists():QFontDatabase.addApplicationFont(str(font));QApplication.instance().setFont(QFont('Microsoft YaHei',9))
        self.setWindowTitle('P5B Operator v1 · 真实流/静态键盘 · 新版待部署验证');self.resize(1430,960)
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
        keyboard=QWidget();kl=QVBoxLayout(keyboard);self.tabs.addTab(keyboard,'SSVEP最小键盘（默认静态）')
        self.text=QLabel('输出：');self.text.setStyleSheet('font-size:32px');kl.addWidget(self.text)
        self.pending=QLabel('待确认：');kl.addWidget(self.pending)
        tiles=QGridLayout()
        for i,label in enumerate(tuple('ABCDEFGHIJKLMNOPQRSTUVWXYZ')+('CONFIRM','DELETE')):
            tile=QLabel(label);tile.setAlignment(Qt.AlignmentFlag.AlignCenter);tile.setMinimumHeight(60)
            tile.setStyleSheet('background:#24374f;color:white;font-size:24px;border:2px solid #7790aa');tiles.addWidget(tile,i//7,i%7)
        kl.addLayout(tiles)
        self.display_start=QPushButton('启动板端静态键盘');self.display_stop=QPushButton('停止板端显示')
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
        self.tick()

    def start(self,simulation):
        try:
            self.manager.start('segments' if self.record.currentIndex() else 'off',simulation=simulation)
            self.typing=Typing();self.last_analysis=-1;self.evidence_count=0
        except Exception as exc:self.status.setText(str(exc))

    def stop(self):
        if self.manager.current:self.manager.current.stop()
        self.display_control('stop',quiet=True)

    def toggle_pause(self):
        self.paused=not self.paused;self.pause.setText('继续绘图' if self.paused else '暂停绘图（采集继续）')

    def toggle_recording(self):
        if self.manager.current and not self.manager.current.done.is_set():
            self.manager.current.set_recording(self.record.currentIndex()==1)

    def display_control(self,action,quiet=False,topic='stimulus'):
        def work():
            try:
                reply=ssh_command(['eegctl',topic,action])
                self.display_reply=reply
            except Exception as exc:
                if not quiet:self.display_reply=str(exc)
        self.display_reply='REQUEST_PENDING';threading.Thread(target=work,daemon=True).start()

    def _classify(self,snapshot,session):
        try:
            n=int(4*RATE);rows=snapshot['samples'][-n:];first=snapshot['frames']-len(rows)
            presentation=self.presentation
            simulated=snapshot['source']=='SIMULATED'
            refresh=60. if simulated else (presentation or {}).get('refresh_hz')
            d=analyze(rows,first,refresh_hz=refresh,source=snapshot['source'],fresh=snapshot['fresh'],
                      sequence_ok=not snapshot['failure'],presentation=presentation)
            # Respect Recording Off. Classification only consumes the bounded
            # memory window. Opted-in segments are referenced, never duplicated.
            references=session.window_recording(d.first_sequence,d.last_sequence)
            action=self.typing.apply(d,evidence_saved=bool(references))
            value=d.to_dict();value.update(action=action,text=self.typing.text,pending=self.typing.pending,
                raw_evidence=references,recording_provenance='RECORDED_SEGMENTS_PROVISIONAL' if references else 'UNRECORDED_ONLINE_ONLY',
                dma_completion_anchors=[a for a in snapshot.get('dma_completion_anchors',[])
                    if a['first_sequence']+a['frames']>d.first_sequence and a['first_sequence']<=d.last_sequence],
                presentation=presentation,real_ssvep_validation=False)
            session.log_json('latest_classification.json',value)
            if action in ('COMMITTED','DELETED') and snapshot['source']=='LIVE_STREAM':
                ssh_command(['eegctl','text',self.typing.text or '-'])
            self.analysis_result=value
        except Exception as exc:self.analysis_result={'reason':repr(exc),'outcome':'NO_DECISION'}
        finally:self.analysis_busy=False

    def tick(self):
        self.control_state.setText(self.display_reply)
        s=self.manager.current
        if s:s.touch_ui()
        summary=self.manager.poll()
        running=s is not None and not s.done.is_set()
        self.live.setEnabled(not running and not self.manager.blocked)
        self.sim.setEnabled(not running and not self.manager.blocked);self.stop_button.setEnabled(running)
        self.record_toggle.setEnabled(running)
        if s:
            snap=s.snapshot();self.banner.setText(f"{snap['source']} · {snap['state']} · {snap['frames']} 帧")
            self.presentation=snap['target'].get('presentation')
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
                threading.Thread(target=self._classify,args=(snap,s),name='CCA-bounded-window',daemon=True).start()
        if self.analysis_result:
            a=self.analysis_result;self.text.setText('输出：'+a.get('text',''));self.pending.setText('待确认：'+a.get('pending',''))
            self.decision.setText(json.dumps(a,ensure_ascii=False)[:2200])
        if self.closing and not running:self.manager.close();self.timer.stop();QApplication.instance().quit()

    def closeEvent(self,event):
        if self.manager.current and not self.manager.current.done.is_set():
            self.closing=True;self.stop();event.ignore();return
        self.manager.close();event.accept()


def run():
    app=QApplication.instance() or QApplication([]);window=Window();window.show();return app.exec()
