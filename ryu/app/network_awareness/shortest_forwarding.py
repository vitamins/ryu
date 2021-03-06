# Copyright (C) 2016 Li Cheng at Beijing University of Posts
# and Telecommunications. www.muzixing.com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
import time
import logging
import struct
import networkx as nx
from operator import attrgetter
from ryu import cfg
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.lib.packet import ipv4
from ryu.lib.packet import arp

from ryu.topology import event, switches
from ryu.topology.api import get_switch, get_link

import network_awareness
import network_monitor
import network_delay_detector

from flow import Flow


CONF = cfg.CONF

MONITOR_MATCH_SRC="00:00:00:00:00:01"

class ShortestForwarding(app_manager.RyuApp):
    """
        ShortestForwarding is a Ryu app for forwarding packets in shortest
        path.
        This App does not defined the path computation method.
        To get shortest path, this module depends on network awareness,
        network monitor and network delay detecttor modules.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {
        "network_awareness": network_awareness.NetworkAwareness,
        "network_monitor": network_monitor.NetworkMonitor,
        "network_delay_detector": network_delay_detector.NetworkDelayDetector}

    WEIGHT_MODEL = {'hop': 'weight', 'delay': "delay", "bw": "bw"}

    def __init__(self, *args, **kwargs):
        super(ShortestForwarding, self).__init__(*args, **kwargs)
        self.name = 'shortest_forwarding'
        self.awareness = kwargs["network_awareness"]
        self.monitor = kwargs["network_monitor"]
        self.delay_detector = kwargs["network_delay_detector"]
        self.datapaths = {}
        self.weight = self.WEIGHT_MODEL[CONF.weight]
        self.flows = {}

    def set_weight_mode(self, weight):
        # Unused function
        """
            set weight mode of path calculating.
        """
        self.weight = weight
        if self.weight == self.WEIGHT_MODEL['hop']:
            self.awareness.get_shortest_paths(weight=self.weight)
        return True

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        """
            Collect datapath information.
        """
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if not datapath.id in self.datapaths:
                self.logger.debug('register datapath: %016x', datapath.id)
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.debug('unregister datapath: %016x', datapath.id)
                del self.datapaths[datapath.id]

    def add_flow(self, dp, p, match, actions, idle_timeout=0, hard_timeout=0):
        """
            Send a flow entry to datapath.
        """
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]

        mod = parser.OFPFlowMod(datapath=dp, priority=p,
                                idle_timeout=idle_timeout,
                                hard_timeout=hard_timeout,
                                match=match, instructions=inst)
        dp.send_msg(mod)

    def send_flow_mod(self, datapath, flow_info, src_port, dst_port, monitor=False):
        """
            Build flow entry, and send it to datapath.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        actions = []
        actions.append(parser.OFPActionOutput(dst_port))
        if monitor:
            # also forward to controller for monitoring
            actions.append(parser.OFPActionSetField(eth_src=MONITOR_MATCH_SRC))
            actions.append(parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                                  ofproto.OFPCML_NO_BUFFER))
        match = parser.OFPMatch(in_port=src_port, eth_type=flow_info[0],
                                ipv4_src=flow_info[1], ipv4_dst=flow_info[2])

        self.add_flow(datapath, 1, match, actions,
                      #idle_timeout=15, hard_timeout=60)
                      idle_timeout=60, hard_timeout=120)

    def _build_packet_out(self, datapath, buffer_id, src_port, dst_port, data):
        """
            Build packet out object.
        """
        actions = []
        if dst_port:
            actions.append(datapath.ofproto_parser.OFPActionOutput(dst_port))

        msg_data = None
        if buffer_id == datapath.ofproto.OFP_NO_BUFFER:
            if data is None:
                return None
            msg_data = data

        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath, buffer_id=buffer_id,
            data=msg_data, in_port=src_port, actions=actions)
        return out

    def send_packet_out(self, datapath, buffer_id, src_port, dst_port, data):
        """
            Send packet out packet to assigned datapath.
        """
        out = self._build_packet_out(datapath, buffer_id,
                                     src_port, dst_port, data)
        if out:
            datapath.send_msg(out)

    def get_port(self, dst_ip, access_table):
        """
            Get access port if dst host.
            access_table: {(sw,port) :(ip, mac)}
        """
        if access_table:
            if isinstance(list(access_table.values())[0], tuple):
                for key in access_table.keys():
                    if dst_ip == access_table[key][0]:
                        dst_port = key[1]
                        return dst_port
        return None

    def get_port_pair_from_link(self, link_to_port, src_dpid, dst_dpid):
        """
            Get port pair of link, so that controller can install flow entry.
        """
        if (src_dpid, dst_dpid) in link_to_port:
            return link_to_port[(src_dpid, dst_dpid)]
        else:
            self.logger.info("dpid:%s->dpid:%s is not in links" % (
                             src_dpid, dst_dpid))
            return None

    def flood(self, msg):
        """
            Flood ARP packet to the access port
            which has no record of host.
        """
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        for dpid in self.awareness.access_ports:
            for port in self.awareness.access_ports[dpid]:
                if (dpid, port) not in self.awareness.access_table.keys():
                    datapath = self.datapaths[dpid]
                    out = self._build_packet_out(
                        datapath, ofproto.OFP_NO_BUFFER,
                        ofproto.OFPP_CONTROLLER, port, msg.data)
                    datapath.send_msg(out)
        self.logger.debug("Flooding msg")

    def arp_forwarding(self, msg, src_ip, dst_ip):
        """ Send ARP packet to the destination host,
            if the dst host record is existed,
            else, flow it to the unknow access port.
        """
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        result = self.awareness.get_host_location(dst_ip)
        if result:  # host record in access table.
            datapath_dst, out_port = result[0], result[1]
            datapath = self.datapaths[datapath_dst]
            out = self._build_packet_out(datapath, ofproto.OFP_NO_BUFFER,
                                         ofproto.OFPP_CONTROLLER,
                                         out_port, msg.data)
            datapath.send_msg(out)
            self.logger.debug("Reply ARP to knew host")
        else:
            self.flood(msg)

    def get_path(self, src, dst, weight):
        """
            Get shortest path from network awareness module.
        """
        shortest_paths = self.awareness.shortest_paths
        graph = self.awareness.graph

        cap_min = random.choice([500, 100, 5000, 1120])
        if cap_min:
            paths = self.capacity_limited_paths(self.monitor.capacity_graph, src, dst, cap_min)
            if paths:
                return paths[0]
            else:
                print('DEMAND BLOCKED')
                return None
        if weight == self.WEIGHT_MODEL['hop']:
            return shortest_paths.get(src).get(dst)[0]
        elif weight == self.WEIGHT_MODEL['delay']:
            # If paths existed, return it, else calculate it and save it.
            try:
                paths = shortest_paths.get(src).get(dst)
                return paths[0]
            except:
                paths = self.awareness.k_shortest_paths(graph, src, dst,
                                                        weight=weight)

                shortest_paths.setdefault(src, {})
                shortest_paths[src].setdefault(dst, paths)
                return paths[0]
        elif weight == self.WEIGHT_MODEL['bw']:
            # Because all paths will be calculate
            # when call self.monitor.get_best_path_by_bw
            # So we just need to call it once in a period,
            # and then, we can get path directly.
            try:
                # if path is existed, return it.
                path = self.monitor.best_paths.get(src).get(dst)
                return path
            except:
                # else, calculate it, and return.
                result = self.monitor.get_best_path_by_bw(graph,
                                                          shortest_paths)
                paths = result[1]
                best_path = paths.get(src).get(dst)
                return best_path

    def get_sw(self, dpid, in_port, src, dst):
        """
            Get pair of source and destination switches.
        """
        src_sw = dpid
        dst_sw = None

        src_location = self.awareness.get_host_location(src)
        if in_port in self.awareness.access_ports[dpid]:
            if (dpid,  in_port) == src_location:
                src_sw = src_location[0]
            else:
                return None

        dst_location = self.awareness.get_host_location(dst)
        if dst_location:
            dst_sw = dst_location[0]

        """
        # can reveal the race condition
        if src_sw == dst_sw:
            print(src_sw)
            print(dst_sw)
        """

        return src_sw, dst_sw

    def install_flow(self, datapaths, link_to_port, access_table, path,
                     flow_info, buffer_id, data=None, bidir=False, monitor=False):
        # flow_info = (eth_type, ip_src, ip_dst, in_port)
        ''' 
            Install flow entires for roundtrip: go and back.
            @parameter: path=[dpid1, dpid2...]
                        flow_info=(eth_type, src_ip, dst_ip, in_port)
        '''
        if path is None or len(path) == 0:
            self.logger.info("Path error!")
            return
        in_port = flow_info[3]
        first_dp = datapaths[path[0]]
        out_port = first_dp.ofproto.OFPP_LOCAL
        if bidir:
            # source and destination ip swapped
            back_info = (flow_info[0], flow_info[2], flow_info[1])

        # inter_link
        if len(path) > 2:
            for i in range(1, len(path)-1):
                port = self.get_port_pair_from_link(link_to_port,
                                                    path[i-1], path[i])
                port_next = self.get_port_pair_from_link(link_to_port,
                                                         path[i], path[i+1])
                if port and port_next:
                    src_port, dst_port = port[1], port_next[0]
                    datapath = datapaths[path[i]]
                    self.send_flow_mod(datapath, flow_info, src_port, dst_port)
                    if bidir:
                        self.send_flow_mod(datapath, back_info, dst_port, src_port)
                    self.logger.debug("inter_link flow install")
        if len(path) > 1:
            # the last flow entry: tor -> host
            port_pair = self.get_port_pair_from_link(link_to_port,
                                                     path[-2], path[-1])
            if port_pair is None:
                self.logger.info("Port is not found")
                return
            src_port = port_pair[1]

            dst_port = self.get_port(flow_info[2], access_table)
            if dst_port is None:
                self.logger.info("Last port is not found.")
                return

            last_dp = datapaths[path[-1]]
            self.send_flow_mod(last_dp, flow_info, src_port, dst_port)
            # add code to forward to controller from last entry
            if monitor:
                print("Installed monitor entry at destination.")
                self.send_flow_mod(last_dp, flow_info, src_port, dst_port, monitor=monitor)
            if bidir:
                self.send_flow_mod(last_dp, back_info, dst_port, src_port)

            # the first flow entry
            port_pair = self.get_port_pair_from_link(link_to_port,
                                                     path[0], path[1])
            if port_pair is None:
                self.logger.info("Port not found in first hop.")
                return
            out_port = port_pair[0]
            self.send_flow_mod(first_dp, flow_info, in_port, out_port)
            # add code to forward to controller from first entry
            if monitor:
                print("Installed monitor entry at source.")
                self.send_flow_mod(first_dp, flow_info, in_port, out_port, monitor=monitor)
            if bidir:
                self.send_flow_mod(first_dp, back_info, out_port, in_port)
            # race condition between flow mod and packet out?
            # sleeping for 1 ms avoids it (tested in mininet)
            time.sleep(0.001)
            self.send_packet_out(first_dp, buffer_id, in_port, out_port, data)

        # src and dst on the same datapath
        else:
            if monitor:
                self.logger.info("No inter-links traversed, nothing to monitor.")
            out_port = self.get_port(flow_info[2], access_table)
            if out_port is None:
                self.logger.info("Out_port is None in same dp")
                return
            self.send_flow_mod(first_dp, flow_info, in_port, out_port)
            if bidir:
                self.send_flow_mod(first_dp, back_info, out_port, in_port)
            self.send_packet_out(first_dp, buffer_id, in_port, out_port, data)

    def shortest_forwarding(self, msg, eth_type, ip_src, ip_dst):
        """
            To calculate shortest forwarding path and install them into datapaths.
        """
        # hardcoded
        track = True
        monitor = False
        if monitor and not track:
            self.logger.info("Track monitor setting error!")

        datapath = msg.datapath
        ofproto = datapath.ofproto
        in_port = msg.match['in_port']

        """
        # filter out packet ins from race conditions and just packet out them again
        # unfinished
        flow_id = (eth_type, ip_src, ip_dst)#== flow_info[0:2]
        if flow_id in self.flows:
            self.logger.info("Race condition.")
            
            dp = self.datapaths[datapath]
            # find out_port
            i = flow.path.index(datapath)
            # unfinished
            if i == len(flow.path):
                # last switch in the path
            else:
                # inter-switch

            self.send_packet_out(dp, msg.buffer_id, in_port, out_port, msg.data)
            return
        """

        result = self.get_sw(datapath.id, in_port, ip_src, ip_dst)
        if result:
            src_sw, dst_sw = result[0], result[1]
            if dst_sw:
                # path is calculated on-demand for dynamic weights
                path = self.get_path(src_sw, dst_sw, weight=self.weight)
                if not path:
                    return
                self.logger.info("[PATH]%s<-->%s: %s" % (ip_src, ip_dst, path))
                flow_info = (eth_type, ip_src, ip_dst, in_port)
                # install flow entries to datapath along side the path.
                # For number of hops metric paths are bidirectional
                if self.weight == 'weight':
                    bidir=True
                else:
                    bidir=False
                self.install_flow(self.datapaths,
                                  self.awareness.link_to_port,
                                  self.awareness.access_table, path,
                                  flow_info, msg.buffer_id, msg.data, bidir,
                                  monitor)
                if track:
                    flow_id = (eth_type, ip_src, ip_dst)#== flow_info[0:2]
                    self.flows[flow_id] = Flow(path, flow_info, bidir, monitor)
        return

    def subgraph_min_capacity(self, graph, cap_min):
        """
            Returns a subgraph (a copy) where edges with a capacity smaller than cap_min are removed.
        """
        _graph = graph.copy()
        to_remove = []
        for e in _graph.edges_iter(data='rem_cap'):
            if e[2]:
                rem_cap = e[2]
            else:
                edge = _graph[e[0]][e[1]]
                if 'capacity' in edge:
                    rem_cap = _graph[e[0]][e[1]]['capacity']
                else:
                    # this edge does not have a capacity assigned, assume it has enough capacity
                    rem_cap = cap_min
            if rem_cap < cap_min:
                to_remove.append(e)
        #to_remove = [e for e in _graph.edges_iter(data='rem_cap', default='capacity') if e[2] < cap_min]
        _graph.remove_edges_from(to_remove)
        return _graph

    def capacity_limited_paths(self, graph, src, dst, cap_min):#, weight='weight', k=CONF.k_paths):
        # Heuristic, find shortest path for reserving capacity cap_min, or block demand
        _graph = self.subgraph_min_capacity(graph, cap_min)
        paths = self.awareness.k_shortest_paths(_graph, src, dst)#, weight=weight, k=k)
        return paths

    """
    def reserve_capacity(self, path, res):
        # decrement the remaining capacity in the capacity graph, if 'rem_cap' attribute doesnt exist, create it
        # for each link in the path, remove capacity
        # pseudocode...
        for (first_switch, second_switch) in path:
            self.graph[first_switch][second_switch]['rem_cap'] -= res
    """

    def path_delay_measure_packet_in(self, msg, eth_type, ip_src, ip_dst):
    # TODO attribute to correct flow
    # => Flow object
        datapath = msg.datapath
        #ofproto = datapath.ofproto(flow_info[0], flow_info[1], 
        in_port = msg.match['in_port']
        flow_id = (eth_type, ip_src, ip_dst)
        try:
            flow = self.flows[flow_id]
        except KeyError:
            self.logger.info('Flow measurement could not be attributed.')
            return
        dst_sw = flow.path[-1]
        src_sw = flow.path[0]
        # find out if we got the packet from the source or from the destination switch
        if datapath.id == dst_sw:
            flow.path_delay_measure.rx(msg.data)
            print('Path delay: Last, average, max')
            print('%f %f %f' % (flow.path_delay_measure.get_latest(), flow.path_delay_measure.get_average(), flow.path_delay_measure.get_max()))
        elif datapath.id == src_sw:
            flow.path_delay_measure.tx(msg.data)
        else:
            self.logger.info('Flow measurement could not be attributed, wrong switch.')

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        '''
            In packet_in handler, we need to learn access_table by ARP.
            Therefore, the first packet from UNKOWN host MUST be ARP.
        '''
        msg = ev.msg
        type(msg)
        datapath = msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        eth_type = eth.ethertype
        if eth_type == ether_types.ETH_TYPE_LLDP:
            # ignore lldp packet
            return
        
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        eth_src = eth.src
        if eth_src == MONITOR_MATCH_SRC:
            self.path_delay_measure_packet_in(msg, eth_type, ip_pkt.src, ip_pkt.dst)
            return

        if isinstance(ip_pkt, ipv4.ipv4):
            self.logger.debug("IPV4 processing")
            self.shortest_forwarding(msg, eth_type, ip_pkt.src, ip_pkt.dst)

        arp_pkt = pkt.get_protocol(arp.arp)
        if isinstance(arp_pkt, arp.arp):
            self.logger.debug("ARP processing")
            self.arp_forwarding(msg, arp_pkt.src_ip, arp_pkt.dst_ip)


