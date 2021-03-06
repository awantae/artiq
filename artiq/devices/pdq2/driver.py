# Based on code by Robert Jordens <jordens@gmail.com>, 2012

import logging
import struct

from scipy import interpolate
import numpy as np


logger = logging.getLogger("pdq2")

Ftdi = None


try:
    import pylibftdi

    class PyFtdi:
        def __init__(self, serial=None):
            self.dev = pylibftdi.Device(device_id=serial)

        def write(self, data):
            written = self.dev.write(data)
            if written < 0:
                raise pylibftdi.FtdiError(written,
                                          self.dev.get_error_string())
            return written

        def close(self):
            self.dev.close()
            del self.dev

    Ftdi = PyFtdi
except ImportError:
    pass


try:
    import ftd2xx

    class D2xxFtdi:
        def __init__(self, serial=None):
            if serial is not None:
                self.dev = ftd2xx.openEx(serial)
            else:
                self.dev = ftd2xx.open()
            self.dev.setTimeouts(read=5000, write=5000)

        def write(self, data):
            written = self.dev.write(str(data))
            return written

        def close(self):
            self.dev.close()
            del self.dev

    Ftdi = D2xxFtdi
except ImportError:
    pass


if Ftdi is None:

    class FileFtdi:
        def __init__(self, serial="unknown"):
            self.fil = open("pdq_%s_ftdi.bin" % serial, "wb")

        def write(self, data):
            self.fil.write(data)
            return len(data)

        def close(self):
            self.fil.close()
            del self.fil

    logger.warning("no ftdi library found. writing to files")
    Ftdi = FileFtdi


class Pdq2:
    """
    PDQ DAC (a.k.a. QC_Waveform)
    """

    commands = {
        "RESET_EN":    b"\x00",
        "RESET_DIS":   b"\x01",
        "TRIGGER_EN":  b"\x02",
        "TRIGGER_DIS": b"\x03",
        "ARM_EN":      b"\x04",
        "ARM_DIS":     b"\x05",
        "DCM_EN":      b"\x06",
        "DCM_DIS":     b"\x07",
        "START_EN":    b"\x08",
        "START_DIS":   b"\x09",
    }

    def __init__(self, serial=None):
        self.serial = serial
        self.dev = Ftdi(serial)

    def init(self):
        self.max_val = 1 << 15  # signed 16 bit DAC
        self.max_out = 10.
        self.freq = 50e6  # samples/s
        self.max_time = 1 << 16  # unsigned 16 bit timer
        self.num_dacs = 3
        self.num_frames = 8
        self.num_channels = 9
        self.max_data = 4*(1 << 10)  # 8kx16 8kx16 4kx16
        self.escape_char = b"\xa5"
        self.cordic_gain = 1.
        for i in range(16):
            self.cordic_gain *= np.sqrt(1 + 2**(-2*i))

    def close(self):
        self.dev.close()
        del self.dev

    def set_freq(self, f):
        self.freq = f

    def get_freq(self):
        return self.freq

    def get_num_channels(self):
        return self.num_channels

    def get_num_frames(self):
        return self.num_frames

    def get_max_out(self):
        return self.max_out

    def _cmd(self, cmd):
        return self.escape_char + self.commands[cmd]

    def _escape(self, data):
        return data.replace(self.escape_char,
                            self.escape_char + self.escape_char)

    def _write(self, *segments):
        """
        writes data segments to device
        """
        for segment in segments:
            written = self.dev.write(segment)
            if written != len(segment):
                raise IOError("wrote %i of %i" % (written, len(segment)))

    def flush_escape(self):
        self._write(b"\x00")

    def write_cmd(self, cmd):
        return self._write(self._cmd(cmd))

    def _write_data(self, *segments):
        return self._write(*(self._escape(seg) for seg in segments))

    def _line_times(self, t, shift=0):
        scale = self.freq/2**shift
        t = t*scale
        tr = np.rint(t)
        dt = np.diff(tr)
        return t, tr, dt

    def _interpolate(self, t, v, order, shift=0, tr=None):
        """
        calculate spline interpolation derivatives for data
        according to interpolation order
        also differentiates times (implicitly shifts to 0) and removes
        the last value (irrelevant since the frame ends here)
        """
        if order == 0:
            return [v[:-1]]
        spline = interpolate.splrep(t, v, k=order)
        if tr is None:
            tr = t
        dv = [interpolate.splev(tr[:-1], spline, der=i)
              for i in range(order + 1)]
        # correct for adder chain latency
        correction_map = [
            (1, -1/2., 2),
            (1, -1/6., 3),
            (2,   -1., 3),
        ]
        for i, c, j in correction_map:
            if j >= len(dv):
                break
            dv[i] -= c*dv[j]
        return dv

    def _pack_frame(self, *parts_dtypes):
        frame = []
        for part, dtype in parts_dtypes:
            if dtype == "i6":
                part = part.astype("<i8")
                frame.append(part.astype("<i4"))
                frame.append((part >> 32).astype("<i2"))
            else:
                frame.append(part.astype("<" + dtype))
        frame = np.rec.fromarrays(frame)  # interleave
        logger.debug("frame %s dtype %s shape %s length %s",
                     frame, frame.dtype, frame.shape, len(bytes(frame.data)))
        return bytes(frame.data)

    def _frame(self, t, v, p=None, f=None,
               order=3, aux=None, shift=0, trigger=True, end=True,
               silence=False, stop=True, clear=True, wait=False):
        """
        serialize frame data
        voltages in volts, times in seconds
        """
        words = [1, 2, 3, 3, 1, 2, 2]
        n = order + 1
        if f is not None:
            n += 2
            if p is None:
                p = np.zeros_like(f)
        if p is not None:
            n += 1
        length = 1 + sum(words[:n])
        parts = []

        head = np.zeros(len(t) - 1, "<u2")
        head[:] |= length  # 4
        if p is not None:
            head[:] |= 1 << 4  # typ # 2
        head[0] |= trigger << 6  # 1
        head[-1] |= (not stop and silence) << 7  # 1
        if aux is not None:
            head[:] |= aux[:len(head)] << 8  # 1
        head[:] |= shift << 9  # 4
        head[-1] |= (not stop and end) << 13  # 1
        head[0] |= clear << 14  # 1
        head[-1] |= (not stop and wait) << 15  # 1
        parts.append((head, "u2"))

        t, tr, dt = self._line_times(t, shift)
        assert np.all(dt*2**shift > 1 + length), (dt, length)
        assert np.all(dt < self.max_time), dt

        parts.append((dt, "u2"))

        v = np.clip(v/self.max_out, -1, 1)
        if p is not None:
            v /= self.cordic_gain
        for dv, w in zip(self._interpolate(t, v, order, shift, tr), words):
            parts.append((np.rint(dv*(2**(16*w - 1))), "i%i" % (2*w)))

        if p is not None:
            p = p/(2*np.pi)
            for dv, w in zip(self._interpolate(t, p, 0, shift, tr), [1]):
                parts.append((np.rint(dv*(2**(16*w))), "u%i" % (2*w)))

        if f is not None:
            f = f/self.freq
            for dv, w in zip(self._interpolate(t, f, 1, shift, tr), [2, 2]):
                parts.append((np.rint(dv*(2**(16*w))), "i%i" % (2*w)))

        frame = self._pack_frame(*parts)

        if stop:
            if p is not None:
                frame += struct.pack("<HH hiihih H ii", (15 << 0) | (1 << 4) |
                                                        (silence << 7) |
                                                        (end << 13) |
                                                        (wait << 15),
                                     1, int(v[-1]*2**15), 0, 0, 0, 0, 0,
                                     int(p[-1]*2**16), int(f[-1]*2**31), 0)
            else:
                frame += struct.pack("<HH h", (2 << 0) | (silence << 7) |
                                              (end << 13) | (wait << 15),
                                     1, int(v[-1]*2**15))
        return frame

    def _line(self, dt, v=(), a=(), p=(), f=(), typ=0,
              silence=False, end=False, trigger=False, aux=False,
              clear=False):
        raise NotImplementedError
        fmt = "<HH"
        parts = [0, int(round(dt*self.freq))]
        for vi, wi in zip(v, [1, 2, 3, 3]):
            vi = int(round(vi*(2**(16*wi - 1))))
            if wi == 3:
                fmt += "Ih"
                parts += [vi & 0xffffffff, vi >> 32]
            else:
                fmt += "bih"[wi]
                parts += [vi]
        if p is not None:
            typ = 1

    def _map_frames(self, frames, map=None):
        table = []
        adr = self.num_frames
        for frame in frames:
            table.append(adr)
            adr += len(frame)//2
        assert adr <= self.max_data, adr
        t = []
        for i in range(self.num_frames):
            if map is not None and len(map) > i:
                i = map[i]
            if i is not None and len(table) > i:
                i = table[i]
            else:
                i = 0
            t.append(i)
        t = struct.pack("<" + "H"*self.num_frames, *t)
        return t + b"".join(frames)

    def _add_mem_header(self, board, dac, data, adr=0):
        assert dac in range(self.num_dacs)
        head = struct.pack("<HHH", (board << 4) | dac,
                           adr, adr + len(data)//2 - 1)
        return head + data

    def multi_frame(self, times_voltages, channel, map=None, **kwargs):
        frames = [self._frame(t, v, **kwargs) for t, v in times_voltages]
        data = self._map_frames(frames, map)
        board, dac = divmod(channel, self.num_dacs)
        data = self._add_mem_header(board, dac, data)
        self._write_data(data)
