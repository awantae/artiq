from math import sqrt, cos, pi
import time
import random

import numpy as np
from scipy.optimize import curve_fit

from artiq import *


def model(x, F0):
    t = 0.02
    tpi = 0.03
    A = 80
    B = 40
    return A+(B-A)/2/(4*tpi**2*(x-F0)**2+1)*(1-cos(pi*t/tpi*sqrt(4*tpi**2*(x-F0)**2+1)))


class FloppingF(AutoDB):
    class DBKeys:
        implicit_core = False

        npoints = Argument(100)
        min_freq = Argument(1000)
        max_freq = Argument(2000)

        F0 = Argument(1500)
        noise_amplitude = Argument(0.1)

        frequency = Result()
        brightness = Result()

        flopping_freq = Parameter()

    @staticmethod
    def realtime_results():
        return {
            ("frequency", "brightness"): "xy"
        }

    def run(self):
        for i in range(self.npoints):
            frequency = (self.max_freq-self.min_freq)*i/(self.npoints - 1) + self.min_freq
            brightness = model(frequency, self.F0) + self.noise_amplitude*random.random()
            self.frequency.append(frequency)
            self.brightness.append(brightness)
            time.sleep(0.1)
        self.analyze()

    def analyze(self):
        popt, pcov = curve_fit(lambda xdata, F0: [model(x, F0) for x in xdata],
                               self.frequency.read, self.brightness.read,
                               p0=[self.flopping_freq])
        perr = np.sqrt(np.diag(pcov))
        if perr < 0.1:
            self.flopping_freq = float(popt)
