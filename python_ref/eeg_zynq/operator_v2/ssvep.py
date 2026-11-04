"""Training-free CCA baseline and conservative no-decision typing state machine.

Raw quality is assessed before the explicitly processed CCA view. Frequencies
come from measured KMS mode/frame periods, never a nominal Qt timer. This module
alone does not claim that any input is neural or that a display is synchronized.
"""
from dataclasses import dataclass, asdict
import time
import numpy as np

from ..continuous.protocol import RATE, CHANNELS
from .text_input import TextInput

TARGETS=tuple('ABCDEFGHIJKLMNOPQRSTUVWXYZ')+('CONFIRM','DELETE')
# Frame-indexed, rational scheduler: bins 32..59 per 240 measured vblanks.
# At an ACTUAL 60 Hz mode this is 8..14.75 Hz, not an assumed refresh rate.
BINS=tuple(range(32,60))
SCHEDULE_FRAMES=240
OCCIPITAL=tuple(range(8,16))
WINDOW_SECONDS=4.


@dataclass
class Decision:
    outcome:str
    target:str|None
    scores:list
    reason:str
    first_sequence:int
    last_sequence:int
    frequencies:list
    quality:dict
    source:str
    alignment:str
    def to_dict(self):return asdict(self)


def analyze(samples,first_sequence,*,refresh_hz=None,source='SIMULATED',fresh=True,
            sequence_ok=True,presentation=None,keyboard_revision=None):
    x=np.asarray(samples,dtype=np.float64)
    last=first_sequence+len(x)-1
    schedule_frames=(presentation or {}).get('schedule_frames',SCHEDULE_FRAMES if source=='SIMULATED' else None)
    frequencies=[] if not refresh_hz or not schedule_frames else [float(refresh_hz)*k/schedule_frames for k in BINS]
    quality={}
    def reject(reason):return Decision('NO_DECISION',None,[],reason,first_sequence,last,frequencies,quality,source,
        'DMA_COMPLETION_ANCHOR; electrode-sample/presentation latency UNKNOWN')
    if x.ndim!=2 or x.shape[1]!=16 or len(x)<int(WINDOW_SECONDS*RATE):return reject('NEED_4_SECONDS')
    if not np.isfinite(x).all():return reject('NONFINITE')
    if not fresh or not sequence_ok:return reject('STALE_OR_SEQUENCE_GAP')
    if np.any(x< -8388608) or np.any(x>8388607):return reject('ADC_RANGE')
    saturated=((x==-8388608)|(x==8388607)).sum(axis=0)
    means=x.mean(axis=0);quality['endpoints']=saturated.astype(int).tolist();quality['raw_means_counts']=means.tolist()
    if saturated.any():return reject('RAW_SATURATION; filtered view cannot repair')
    y=x[:,OCCIPITAL]
    if np.max(np.abs(means[list(OCCIPITAL)]))>8388608*.3:return reject('LARGE_RAW_DC')
    if np.min(y.std(axis=0))<5:return reject('FLAT_OR_TOO_SMALL')
    if not refresh_hz or not 40<float(refresh_hz)<240:return reject('REFRESH_NOT_VERIFIED')
    if not isinstance(schedule_frames,int) or schedule_frames<160:return reject('PRESENTATION_SCHEDULE_UNPROVEN')
    if source=='LIVE_STREAM':
        if not presentation or presentation.get('state')!='STIMULUS_ACTIVE':return reject('NO_ACTIVE_PRESENTATION_RECEIPT')
        if presentation.get('timing_valid') is not True or presentation.get('missed_vblanks',0):return reject('PRESENTATION_TIMING_UNPROVEN_OR_GAPPED')
        if presentation.get('stable_seconds',0)<WINDOW_SECONDS+1:return reject('PRESENTATION_WINDOW_NOT_COVERED')
        if keyboard_revision is None or presentation.get('keyboard_revision')!=keyboard_revision:return reject('KEYBOARD_MAPPING_REVISION_UNPROVEN')
        if presentation.get('keyboard_stable_seconds',0)<WINDOW_SECONDS+1:return reject('KEYBOARD_MAPPING_WINDOW_NOT_COVERED')
    # Linear detrend plus FFT band selection is for classification only. The
    # unmodified EMD1 bytes remain with the recorder/evidence window.
    t=np.arange(len(y))/RATE
    trend=np.column_stack((np.ones(len(y)),t-t.mean()))
    y=y-trend@np.linalg.lstsq(trend,y,rcond=None)[0]
    fft=np.fft.rfft(y,axis=0);f=np.fft.rfftfreq(len(y),1/RATE)
    power=(np.abs(fft)**2).sum(axis=1)
    broad=float(power[(f>=4)&(f<=60)].sum())+1e-20
    line=float(power[(f>=49)&(f<=51)].sum())/broad;quality['line50_power_ratio']=line
    if line>.5:return reject('LINE50_DOMINANT')
    keep=(f>=4)&(f<=45);fft[~keep]=0
    processed=np.fft.irfft(fft,n=len(y),axis=0)
    # SVD-based canonical correlation; rank truncation handles dependent leads.
    ux,sx,_=np.linalg.svd(processed,full_matrices=False)
    ux=ux[:,sx>max(sx[0]*1e-6,1e-9)]
    if not ux.shape[1]:return reject('RANK_ZERO')
    scores=[]
    for frequency in frequencies:
        ref=np.column_stack([fun(2*np.pi*frequency*h*t) for h in (1,2,3) for fun in (np.sin,np.cos)])
        ref-=ref.mean(axis=0)
        ur,sr,_=np.linalg.svd(ref,full_matrices=False);ur=ur[:,sr>sr[0]*1e-6]
        score=float(np.linalg.svd(ux.T@ur,compute_uv=False)[0]);scores.append(score)
    order=np.argsort(scores);best=int(order[-1]);margin=scores[best]-scores[int(order[-2])]
    fundamental=((f>=frequencies[best]-.4)&(f<=frequencies[best]+.4))
    ratio=float(power[fundamental].sum())/broad
    quality.update(margin=margin,fundamental_power_ratio=ratio,processing='LINEAR_DETREND_FFT_4_45HZ_CCA_ONLY')
    if scores[best]<.65 or margin<.12 or ratio<.12:
        d=reject('LOW_CONFIDENCE_OR_REST');d.scores=scores;return d
    return Decision('CANDIDATE',TARGETS[best],scores,'CCA_BASELINE_NOT_NEURAL_PROOF',first_sequence,last,
                    frequencies,quality,source,'DMA_COMPLETION_ANCHOR; electrode-sample/presentation latency UNKNOWN')


class Typing:
    """A/B choose pending text; separate CONFIRM commits; DELETE edits.

    Two successive windows are required; overlapping windows are not claimed as
    independent trials. Optional recording is separate from online typing;
    unrecorded output must never be presented as a saved validation trial.
    """
    def __init__(self,language='EN'):
        self.input=TextInput(language);self.last=None;self.count=0;self.last_sequence=-1;self.cooldown_until=0.

    @property
    def text(self):return self.input.text

    @property
    def pending(self):return self.input.pending

    @property
    def revision(self):return self.input.revision

    @property
    def keyboard_revision(self):return self.input.keyboard_revision

    def panel_state(self):return self.input.state()

    def reset_dwell(self):self.last=None;self.count=0;self.cooldown_until=0.

    def set_language(self,language):
        action=self.input.set_language(language);self.reset_dwell();return action

    def manual_target(self,target):
        """Explicit keyboard/mouse flow test; never called a neural decision."""
        self.reset_dwell();return self.input.activate(target)

    def apply(self,d,*,evidence_saved=False,now=None):
        now=time.monotonic() if now is None else now
        if d.last_sequence<=self.last_sequence:return 'DUPLICATE_WINDOW'
        self.last_sequence=d.last_sequence
        if d.outcome!='CANDIDATE':self.last=None;self.count=0;return 'NO_DECISION'
        if d.source not in ('LIVE_STREAM','SIMULATED','FILE_REPLAY'):return 'SOURCE_NOT_ALLOWED'
        self.count=self.count+1 if self.last==d.target else 1;self.last=d.target
        if self.count<2 or now<self.cooldown_until:return 'WAIT_DWELL'
        self.count=0;self.cooldown_until=now+2
        return self.input.activate(d.target)
