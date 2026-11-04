"""Render a glyph-only bilingual miniDP panel on the GUI thread.

The board draws target brightness and timing. This raster contains text only;
it neither generates flicker nor treats a Windows timer as display timing.
"""
import struct
import zlib
import numpy as np
from PySide6.QtCore import Qt,QRect
from PySide6.QtGui import QFont,QFontMetrics,QImage,QPainter

WIDTH=1024
HEIGHT=768
STRIDE=128
HEADER=struct.Struct('<8sIIIIIIQII16s')

def render_panel(state,*,provenance='STATIC; actual neural typing NOT VALIDATED'):
    image=QImage(WIDTH,HEIGHT,QImage.Format.Format_Grayscale8);image.fill(0)
    painter=QPainter(image);painter.setPen(Qt.GlobalColor.white)
    def line(rect,text,size=20):
        font=QFont('Microsoft YaHei',size);painter.setFont(font)
        fitted=QFontMetrics(font).elidedText(text,Qt.TextElideMode.ElideRight,rect.width())
        painter.drawText(rect,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,fitted)
    line(QRect(21,12,982,35),'P5B SSVEP · 中英双语 / EN + 中文拼音',22)
    mode=f"{state['language']} / {state['stage']} / {state.get('composition','')} / {state['page']+1}:{state['page_count']} · {provenance}"
    line(QRect(21,50,982,35),mode,14)
    line(QRect(21,88,982,28),'输出 / OUTPUT    待确认 / PENDING: '+state['pending'],16)
    line(QRect(21,116,982,49),state['text'][-50:] or '（等待明确选择与确认 / awaiting input）',22)
    for i,label in enumerate(state['labels']):
        r=QRect(21+(i%7)*143,180+(i//7)*121,122,100)
        font=QFont('Microsoft YaHei',17 if '\n' in label else 22);painter.setFont(font)
        painter.drawText(r,Qt.AlignmentFlag.AlignCenter,label)
    # y>=668 is reserved for the target's live safety/state overlay.
    painter.end()
    values=np.frombuffer(image.constBits(),dtype=np.uint8,count=image.sizeInBytes()).reshape(HEIGHT,image.bytesPerLine())[:,:WIDTH]
    payload=np.packbits(values>=96,axis=1,bitorder='big').tobytes()
    revision=int(state['keyboard_revision'])
    if revision<1:raise ValueError('KEYBOARD_REVISION_MUST_BE_POSITIVE')
    packet=HEADER.pack(b'EEGPNL2\0',1,64,WIDTH,HEIGHT,STRIDE,len(payload),revision,zlib.crc32(payload)&0xffffffff,0,b'\0'*16)+payload
    return packet,image
