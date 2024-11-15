# SPDX-License-Identifier: CC0-1.0

import array
import fcntl
import os
import re
import sys

from io import BytesIO
import termios
import tty
from base64 import standard_b64encode
from contextlib import suppress
from subprocess import run

import matplotlib as mpl
from matplotlib import interactive, is_interactive
from matplotlib._pylab_helpers import Gcf
from matplotlib.backend_bases import (_Backend, FigureManagerBase)
from matplotlib.backends.backend_agg import FigureCanvasAgg

try:
    from IPython import get_ipython
except ModuleNotFoundError:
    def get_ipython():
        return None


# XXX heuristic for interactive repl
if hasattr(sys, 'ps1') or sys.flags.interactive:
    interactive(True)

def term_size_px():
    width_px = height_px = 0

    # try to get terminal size from ioctl
    with suppress(OSError):
        buf = array.array('H', [0, 0, 0, 0])
        fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, buf)
        height_rows, _, width_px, height_px = buf

    # Remove the height of 3 rows (prompt, newline and possible `<matplotlib.axes>...`
    if height_rows > 0 and height_px != 0:
        row_height = height_px / height_rows
        height_px -= int(3 * row_height)

    if width_px != 0 and height_px != 0:
        return height_px, width_px

    # fallback to ANSI escape code if ioctl fails
    buf = ''
    stdin = sys.stdin.fileno()
    tattr = termios.tcgetattr(stdin)

    try:
        tty.setcbreak(stdin, termios.TCSANOW)
        sys.stdout.write('\x1b[14t')
        sys.stdout.flush()

        while True:
            buf += sys.stdin.read(1)
            if buf[-1] == 't':
                break

    finally:
        termios.tcsetattr(stdin, termios.TCSANOW, tattr)

    # reading the actual values, but what if a keystroke appears while reading
    # from stdin? As dirty work around, getpos() returns if this fails: None
    try:
        matches = re.match(r'^\x1b\[4;(\d*);(\d*)t', buf)
        groups = matches.groups()
    except AttributeError:
        return None

    return (int(groups[0]), int(groups[1]))


def serialize_gr_command(**cmd):
    payload = cmd.pop('payload', None)
    cmd = ','.join(f'{k}={v}' for k, v in cmd.items())
    ans = []
    w = ans.append
    w(b'\033_G'), w(cmd.encode('ascii'))
    if payload:
        w(b';')
        w(payload)
    w(b'\033\\')
    return b''.join(ans)


def write_chunked(**cmd):
    data = standard_b64encode(cmd.pop('data'))
    while data:
        chunk, data = data[:4096], data[4096:]
        m = 1 if data else 0
        sys.stdout.buffer.write(serialize_gr_command(payload=chunk, m=m, **cmd))
        sys.stdout.flush()
        cmd.clear()

class FigureManagerICat(FigureManagerBase):

    @classmethod
    def _run(cls, *cmd):
        def f(*args, output=True, **kwargs):
            if output:
                kwargs['capture_output'] = True
                kwargs['text'] = True
            r = run(cmd + args, **kwargs)
            if output:
                return r.stdout.rstrip()
        return f

    def show(self):
        sizing_strategy = os.environ.get('MPLBACKEND_KITTY_SIZING', 'preserve_aspect_ratio')
        if sizing_strategy in ['automatic', 'preserve_aspect_ratio']:

            # gather terminal dimensions
            term_height_px, term_width_px = term_size_px()
            ipd = 1 / self.canvas.figure.dpi
            term_width_inch, term_height_inch = term_width_px * ipd, term_height_px * ipd

            if sizing_strategy == 'automatic':
                # resize figure to terminal size & aspect ratio
                self.canvas.figure.set_size_inches(term_width_inch, term_height_inch)
            else:
                fig_w, fig_h = self.canvas.figure.get_size_inches()
                # Try to fit width
                new_w = term_width_inch
                new_h = new_w * fig_h / fig_w
                if new_h > term_height_inch:
                    # Fit height
                    new_h = term_height_inch
                    new_w = new_h * fig_w / fig_h
                self.canvas.figure.set_size_inches(new_w, new_h)

        with BytesIO() as buf:
            self.canvas.figure.savefig(buf, format='png')
            write_chunked(a='T', f=100, data=buf.getvalue())


class FigureCanvasICat(FigureCanvasAgg):
    manager_class = FigureManagerICat


@_Backend.export
class _BackendICatAgg(_Backend):

    FigureCanvas = FigureCanvasICat
    FigureManager = FigureManagerICat

    # Noop function instead of None signals that
    # this is an "interactive" backend
    mainloop = lambda: None

    _to_show = []
    _draw_called = False

    # XXX: `draw_if_interactive` isn't really intended for
    # on-shot rendering. We run the risk of being called
    # on a figure that isn't completely rendered yet, so
    # we skip draw calls for figures that we detect as
    # not being fully initialized yet. Our heuristic for
    # that is the presence of axes on the figure.
    @classmethod
    def draw_if_interactive(cls):
        manager = Gcf.get_active()
        if is_interactive() and manager.canvas.figure.get_axes():
            cls.show()

    @classmethod
    def show(cls, *args, **kwargs):
        _Backend.show(*args, **kwargs)
        Gcf.destroy_all()

    @staticmethod
    def new_figure_manager_given_figure(num, figure):
        # From ipympl code
        canvas = FigureCanvasICat(figure)
        manager = FigureManagerICat(canvas, num)
        if is_interactive():
            _BackendICatAgg._to_show.append(figure)
            figure.canvas.draw_idle()

        def destroy(event):
            canvas.mpl_disconnect(cid)

        cid = canvas.mpl_connect('close_event', destroy)

        # Only register figure for showing when in interactive mode (otherwise
        # we'll generate duplicate plots, since a user who set ioff() manually
        # expects to make separate draw/show calls).
        if is_interactive():
            # ensure current figure will be drawn.
            try:
                _BackendICatAgg._to_show.remove(figure)
            except ValueError:
                # ensure it only appears in the draw list once
                pass
            # Queue up the figure for drawing in next show() call
            _BackendICatAgg._to_show.append(figure)
            _BackendICatAgg._draw_called = True

        return manager

def flush_figures():
    # Adapted from ipympl code
    backend = mpl.get_backend()
    if backend == 'module://matplotlib-backend-kitty':
        if not _BackendICatAgg._draw_called:
            return

        try:
            # exclude any figures that were closed:
            active = {fm.canvas.figure for fm in Gcf.get_all_fig_managers()}

            for fig in [
                    fig for fig in _BackendICatAgg._to_show if fig in active
            ]:
                fig.show()
        finally:
            # clear flags for next round
            _BackendICatAgg._to_show = []
            _BackendICatAgg._draw_called = False

ip = get_ipython()
if ip is not None:
    ip.events.register('post_execute', flush_figures)
