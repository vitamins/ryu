import time

class PathDelayMeasure(object):
    def __init__(self):
        super(PathDelayMeasure, self).__init__()
        self.tx_record = []
        self.delays = []
        self.dropped = 0
	
    def tx(self, tx_pkt):
        tx_time = time.time()
        self.tx_record.append( (tx_pkt, tx_time) )

    def rx(self, rx_pkt):
        rx_time = time.time()
        while len(self.tx_record):
            tx_pkt, tx_time = self.tx_record.pop(0)
            if tx_pkt == rx_pkt:
                self.delays.append(rx_time - tx_time)
                break
            else:
                self.dropped += 1

    def get_latest(self):
        if self.delays:
            return self.delays[-1]
        else:
            #return None
            return 0

    def get_average(self, n=False):
        if self.delays:
            if n:
                # moving average over last n measurements
                return sum(self.delays[-n:]) / len(self.delays[-n:])
            else:
                return sum(self.delays) / len(self.delays)
        else:
            return 0
            #return None

    def get_max(self, n=False):
        if self.delays:
            if n:
                return max(self.delays[-n:])
            else:
                return max(self.delays)
        else:
            return 0
            #return None

    def cut_tail(self, n):
        # keep only the last n measurements, to save memory
        self.delays = self.delays[-n:]
