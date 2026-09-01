"""
MidTerraSim Map Viewer
Top-down view of a .wrld chunk file.  Each block = 5×5 pixels.
"""

import tkinter as tk
from tkinter import filedialog
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from chunk import Chunk, AIR, GRASS, FLOWER

CELL = 5   # pixels per block

# top-down colour for each block type (hex)
BLOCK_COLOR = {
    AIR:   '#87CEEB',
    GRASS: '#4A8C3F',
}
FLOWER_DOT  = '#FFD700'   # gold dot drawn on top of grass for flowers
UNKNOWN_COLOR = '#888888'


def render_chunk(canvas, chunk):
    canvas.delete('all')
    sx, _, sz = chunk.size
    canvas.config(
        width=sx * CELL,
        height=sz * CELL,
        scrollregion=(0, 0, sx * CELL, sz * CELL),
    )
    for x, _y, z, bid in chunk.visible_surface():
        x0, y0 = x * CELL, z * CELL
        if bid == FLOWER:
            # grass background
            canvas.create_rectangle(x0, y0, x0 + CELL, y0 + CELL,
                                     fill=BLOCK_COLOR[GRASS], outline='')
            # small gold dot in the centre
            canvas.create_oval(x0 + 1, y0 + 1, x0 + CELL - 1, y0 + CELL - 1,
                                fill=FLOWER_DOT, outline='')
        else:
            fill = BLOCK_COLOR.get(bid, UNKNOWN_COLOR)
            canvas.create_rectangle(x0, y0, x0 + CELL, y0 + CELL,
                                     fill=fill, outline='')


class MapViewer:
    def __init__(self, root):
        self.root = root
        root.title('MidTerraSim — Map Viewer')
        root.geometry('700x650')

        # ── toolbar ──────────────────────────────────────────────────────────
        toolbar = tk.Frame(root, bd=1, relief='raised')
        toolbar.pack(side='top', fill='x')

        tk.Button(toolbar, text='📂  Open World…', command=self.open_file,
                  padx=8, pady=4).pack(side='left', padx=4, pady=3)

        self.status_var = tk.StringVar(value='No world loaded.')
        tk.Label(toolbar, textvariable=self.status_var,
                 anchor='w').pack(side='left', padx=8)

        # moisture / fertility readout
        self.params_var = tk.StringVar(value='')
        tk.Label(toolbar, textvariable=self.params_var,
                 anchor='w', fg='#1a6b2a', font=('Consolas', 10, 'bold')
                 ).pack(side='left', padx=12)

        # ── scrollable canvas area ────────────────────────────────────────────
        frame = tk.Frame(root)
        frame.pack(fill='both', expand=True)

        self.canvas = tk.Canvas(frame, bg='#1a1a2e',
                                 cursor='crosshair')
        vscroll = tk.Scrollbar(frame, orient='vertical',
                                command=self.canvas.yview)
        hscroll = tk.Scrollbar(root,  orient='horizontal',
                                command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vscroll.set,
                               xscrollcommand=hscroll.set)

        hscroll.pack(side='bottom', fill='x')
        vscroll.pack(side='right',  fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)

        # mouse-wheel zoom / scroll
        self.canvas.bind('<MouseWheel>',         self._on_mousewheel)
        self.canvas.bind('<Button-4>',           self._on_mousewheel)
        self.canvas.bind('<Button-5>',           self._on_mousewheel)
        self.canvas.bind('<Motion>',             self._on_mouse_move)

        # coordinate readout
        self.coord_var = tk.StringVar(value='')
        tk.Label(root, textvariable=self.coord_var,
                 anchor='e').pack(side='bottom', fill='x', padx=4)

        self._chunk = None

    # ── file handling ─────────────────────────────────────────────────────────

    def open_file(self):
        path = filedialog.askopenfilename(
            title='Open World File',
            filetypes=[('World files', '*.wrld'), ('All files', '*.*')],
        )
        if not path:
            return
        self._chunk = Chunk.load(path)
        render_chunk(self.canvas, self._chunk)
        sx, sy, sz = self._chunk.size
        name = os.path.basename(path)
        self.status_var.set(f'{name}   {sx}×{sy}×{sz} blocks')
        self.params_var.set(
            f'💧 Moisture: {self._chunk.moisture}   '
            f'🌱 Fertility: {self._chunk.fertility}'
        )
        self.root.title(f'MidTerraSim Map Viewer — {name}')

    # ── interaction ───────────────────────────────────────────────────────────

    def _on_mousewheel(self, event):
        if event.num == 4 or event.delta > 0:
            self.canvas.yview_scroll(-1, 'units')
        else:
            self.canvas.yview_scroll(1, 'units')

    def _on_mouse_move(self, event):
        if self._chunk is None:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        bx, bz = int(cx // CELL), int(cy // CELL)
        sx, _, sz = self._chunk.size
        if 0 <= bx < sx and 0 <= bz < sz:
            by = self._chunk.top_y(bx, bz)
            self.coord_var.set(f'x={bx}  y={by}  z={bz}')
        else:
            self.coord_var.set('')


if __name__ == '__main__':
    root = tk.Tk()
    MapViewer(root)
    root.mainloop()
