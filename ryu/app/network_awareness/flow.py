from path_delay_measure import PathDelayMeasure

class Flow(object):
    """
    General object to keep track of flows and their path.
    """
    def __init__(self, path, flow_info, bidir, monitor=False):
        super(Flow, self).__init__()
        self.path = path
        self.flow_info = flow_info
        self.bidir = bidir
        if monitor:
            self.path_delay_measure = PathDelayMeasure()
	
