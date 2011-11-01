#!/usr/bin/python
import os
import re
import math
import sys
import socket
import time
import traceback

sys.path.append("../../")
from TorCtl.TorUtil import plog
from TorCtl import TorCtl,TorUtil
from TorCtl.PathSupport import VersionRangeRestriction, NodeRestrictionList, NotNodeRestriction

bw_files = []
timestamps = {}
nodes = {}
prev_consensus = {}

# Hack to kill voting on guards while the network rebalances
IGNORE_GUARDS = 0

# The guard measurement period is based on the client turnover
# rate for guard nodes
GUARD_SAMPLE_RATE = 2*7*24*60*60 # 2wks

# PID constants
# See https://en.wikipedia.org/wiki/PID_controller#Ideal_versus_standard_PID_form
K_p = 1.0

# We expect to correct steady state error in 5 samples (guess)
T_i = 5.0

# We can only expect to predict less than one sample into the future, as
# after 1 sample, clients will have migrated
# FIXME: Our prediction ability is a function of the consensus time
# vs measurement rate
T_d = 0.5

K_i = K_p/T_i
K_d = K_p*T_d

NODE_CAP = 0.05

MIN_REPORT = 60 # Percent of the network we must measure before reporting

# Keep most measurements in consideration. The code below chooses
# the most recent one. 15 days is just to stop us from choking up 
# all the CPU once these things run for a year or so.
MAX_AGE = 60*60*24*15

# If the resultant scan file is older than 1.5 days, something is wrong
MAX_SCAN_AGE = 60*60*24*1.5


def base10_round(bw_val):
  # This keeps the first 3 decimal digits of the bw value only
  # to minimize changes for consensus diffs.
  # Resulting error is +/-0.5%
  if bw_val == 0:
    plog("INFO", "Zero input bandwidth.. Upping to 1")
    return 1
  else:
    ret = int(max((1000,
                   round(round(bw_val,-(int(math.log10(bw_val))-2)),
                                                       -3)))/1000)
    if ret == 0:
      plog("INFO", "Zero output bandwidth.. Upping to 1")
      return 1
    return ret

class Node:
  def __init__(self):
    self.ignore = False
    self.idhex = None
    self.nick = None
    self.sbw_ratio = None
    self.fbw_ratio = None
    self.pid_bw = 0
    self.pid_error = 0
    self.prev_error = 0
    self.prev_measured_at = 0
    self.pid_error_sum = 0
    self.derror_dt = 0
    self.ratio = None
    self.new_bw = None
    self.change = None

    # measurement vars from bwauth lines
    self.measured_at = 0
    self.strm_bw = 0
    self.filt_bw = 0
    self.ns_bw = 0
    self.desc_bw = 0
    self.circ_fail_rate = 0
    self.strm_fail_rate = 0

  def revert_to_vote(self, vote):
    self.new_bw = vote.bw
    self.pid_error = vote.pid_error
    self.measured_at = vote.measured_at

  # Derivative of error for pid control
  def get_pid_bw(self, prev_vote, kp):
    self.prev_error = prev_vote.pid_error
    self.prev_measured_at = prev_vote.measured_at
    # We decay the interval by 1/T_i each round to keep it bounded.
    # This is non-standard
    self.pid_error_sum = prev_vote.pid_error_sum*(1 - 1.0/T_i) + self.pid_error

    self.pid_bw = self.ns_bw \
             + kp*(self.ns_bw*self.pid_error \
             +     self.ns_bw*self.integral_error()/T_i \
             +     self.ns_bw*self.d_error_dt()*T_d)
    return self.pid_bw

  # Time-weighted sum of error per unit of time (measurement sample)
  def integral_error(self):
    if self.prev_error == 0:
      return 0
    return self.pid_error_sum

  # Rate of change in error from the last measurement sample
  def d_error_dt(self):
    if self.prev_measured_at == 0 or self.prev_error == 0:
      self.derror_dt = 0
    else:
      self.derror_dt = self.pid_error - self.prev_error
    return self.derror_dt

  def add_line(self, line):
    if self.idhex and self.idhex != line.idhex:
      raise Exception("Line mismatch")
    self.idhex = line.idhex
    self.nick = line.nick
    if line.measured_at > self.measured_at:
      self.measured_at = line.measured_at
      self.strm_bw = line.strm_bw
      self.filt_bw = line.filt_bw
      self.ns_bw = line.ns_bw
      self.desc_bw = line.desc_bw
      self.circ_fail_rate = line.circ_fail_rate
      self.strm_fail_rate = line.strm_fail_rate

class Line:
  def __init__(self, line, slice_file, timestamp):
    self.idhex = re.search("[\s]*node_id=([\S]+)[\s]*", line).group(1)
    self.nick = re.search("[\s]*nick=([\S]+)[\s]*", line).group(1)
    self.strm_bw = int(re.search("[\s]*strm_bw=([\S]+)[\s]*", line).group(1))
    self.filt_bw = int(re.search("[\s]*filt_bw=([\S]+)[\s]*", line).group(1))
    self.ns_bw = int(re.search("[\s]*ns_bw=([\S]+)[\s]*", line).group(1))
    self.desc_bw = int(re.search("[\s]*desc_bw=([\S]+)[\s]*", line).group(1))
    self.slice_file = slice_file
    self.measured_at = timestamp
    try:
      self.circ_fail_rate = float(re.search("[\s]*circ_fail_rate=([\S]+)[\s]*", line).group(1))
      self.strm_fail_rate = float(re.search("[\s]*strm_fail_rate=([\S]+)[\s]*", line).group(1))
    except:
      self.circ_fail_rate = 0
      self.strm_fail_rate = 0

class Vote:
  def __init__(self, line):
    # node_id=$DB8C6D8E0D51A42BDDA81A9B8A735B41B2CF95D1 bw=231000 diff=209281 nick=rainbowwarrior measured_at=1319822504
    self.idhex = re.search("[\s]*node_id=([\S]+)[\s]*", line).group(1)
    self.nick = re.search("[\s]*nick=([\S]+)[\s]*", line).group(1)
    self.bw = int(re.search("[\s]*bw=([\S]+)[\s]*", line).group(1))
    self.measured_at = int(re.search("[\s]*measured_at=([\S]+)[\s]*", line).group(1))
    try:
      self.pid_error = float(re.search("[\s]*pid_error=([\S]+)[\s]*", line).group(1))
      self.pid_error_sum = float(re.search("[\s]*pid_error_sum=([\S]+)[\s]*", line).group(1))
      self.pid_bw = float(re.search("[\s]*pid_bw=([\S]+)[\s]*", line).group(1))
    except:
      plog("NOTICE", "No previous PID data.")
      self.pid_bw = 0
      self.pid_error = 0
      self.pid_error_sum = 0

class VoteSet:
  def __init__(self, filename):
    self.vote_map = {}
    try:
      f = file(filename, "r")
      f.readline()
      for line in f.readlines():
        vote = Vote(line)
        self.vote_map[vote.idhex] = vote
    except IOError:
      plog("NOTICE", "No previous vote data.")
      pass

# Misc items we need to get out of the consensus
class ConsensusJunk:
  def __init__(self, c):
    cs_bytes = c.sendAndRecv("GETINFO dir/status-vote/current/consensus\r\n")[0][2]
    self.bwauth_pid_control = False
    try:
      cs_params = re.search("^params ((?:[\S]+=[\d]+[\s]?)+)",
                                     cs_bytes, re.M).split()
      for p in cs_params:
        if p == "bwauthpid=1":
          self.bwauth_pid_control = True
    except:
      plog("NOTICE", "Bw auth PID control disabled due to parse error.")
      traceback.print_exc()

    self.bw_weights = {}
    try:
      bw_weights = re.search("^bandwidth-weights ((?:[\S]+=[\d]+[\s]?)+)",
                           cs_bytes, re.M).groups(1)[0].split()
      for b in bw_weights:
        pair = b.split("=")
        self.bw_weights[pair[0]] = int(pair[1])/10000.0
    except:
      plog("WARN", "No bandwidth weights in consensus!")
      self.bw_weights["Wgd"] = 0
      self.bw_weights["Wgg"] = 1.0

def main(argv):
  TorUtil.read_config(argv[1]+"/scanner.1/bwauthority.cfg")
  TorUtil.loglevel = "NOTICE"
 
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect((TorUtil.control_host,TorUtil.control_port))
  c = TorCtl.Connection(s)
  c.debug(file(argv[1]+"/aggregate-control.log", "w", buffering=0))
  c.authenticate_cookie(file(argv[1]+"/tor/control_auth_cookie",
                         "r"))

  ns_list = c.get_network_status()
  for n in ns_list:
    if n.bandwidth == None: n.bandwidth = -1
  ns_list.sort(lambda x, y: y.bandwidth - x.bandwidth)
  for n in ns_list:
    if n.bandwidth == -1: n.bandwidth = None
  got_ns_bw = False
  max_rank = len(ns_list)

  cs_junk = ConsensusJunk(c)

  # TODO: This is poor form.. We should subclass the Networkstatus class
  # instead of just adding members
  for i in xrange(max_rank):
    n = ns_list[i]
    n.list_rank = i
    if n.bandwidth == None:
      plog("NOTICE", "Your Tor is not providing NS w bandwidths for "+n.idhex)
    else:
      got_ns_bw = True
    n.measured = False
    prev_consensus["$"+n.idhex] = n

  if not got_ns_bw:
    # Sometimes the consensus lacks a descriptor. In that case,
    # it will skip outputting 
    plog("ERROR", "Your Tor is not providing NS w bandwidths!")
    sys.exit(0)

  # Take the most recent timestamp from each scanner 
  # and use the oldest for the timestamp of the result.
  # That way we can ensure all the scanners continue running.
  scanner_timestamps = {}
  for da in argv[1:-1]:
    # First, create a list of the most recent files in the
    # scan dirs that are recent enough
    for root, dirs, f in os.walk(da):
      for ds in dirs:
        if re.match("^scanner.[\d+]$", ds):
          newest_timestamp = 0
          for sr, sd, files in os.walk(da+"/"+ds+"/scan-data"):
            for f in files:
              if re.search("^bws-[\S]+-done-", f):
                fp = file(sr+"/"+f, "r")
                slicenum = sr+"/"+fp.readline()
                timestamp = float(fp.readline())
                fp.close()
                # old measurements are probably
                # better than no measurements. We may not
                # measure hibernating routers for days.
                # This filter is just to remove REALLY old files
                if time.time() - timestamp > MAX_AGE:
                  plog("DEBUG", "Skipping old file "+f)
                  # FIXME: Unlink this file + sql-
                  continue
                if timestamp > newest_timestamp:
                  newest_timestamp = timestamp
                bw_files.append((slicenum, timestamp, sr+"/"+f))
                # FIXME: Can we kill this?
                if slicenum not in timestamps or \
                     timestamps[slicenum] < timestamp:
                  timestamps[slicenum] = timestamp
          scanner_timestamps[ds] = newest_timestamp

  # Need to only use most recent slice-file for each node..
  for (s,t,f) in bw_files:
    fp = file(f, "r")
    fp.readline() # slicenum
    fp.readline() # timestamp
    for l in fp.readlines():
      try:
        line = Line(l,s,t)
        if line.idhex not in nodes:
          n = Node()
          nodes[line.idhex] = n
        else:
          n = nodes[line.idhex]
        n.add_line(line)
      except ValueError,e:
        plog("NOTICE", "Conversion error "+str(e)+" at "+l)
      except AttributeError, e:
        plog("NOTICE", "Slice file format error "+str(e)+" at "+l)
      except Exception, e:
        plog("WARN", "Unknown slice parse error "+str(e)+" at "+l)
        traceback.print_exc()
    fp.close()

  if len(nodes) == 0:
    plog("NOTICE", "No scan results yet.")
    sys.exit(1)

  true_strm_avg = sum(map(lambda n: n.strm_bw,
                       nodes.itervalues()))/float(len(nodes))
  true_filt_avg = sum(map(lambda n: n.filt_bw,
                       nodes.itervalues()))/float(len(nodes))

  plog("DEBUG", "Network true_strm_avg: "+str(true_strm_avg))
  plog("DEBUG", "Network true_filt_avg: "+str(true_filt_avg))

  prev_votes = None
  if cs_junk.bwauth_pid_control:
    prev_votes = VoteSet(argv[-1])

    guard_cnt = 0
    node_cnt = 0
    guard_measure_time = 0
    node_measure_time = 0
    for n in nodes.itervalues():
      if n.idhex in prev_votes.vote_map and n.idhex in prev_consensus:
        if "Guard" in prev_consensus[n.idhex].flags:
          guard_cnt += 1
          guard_measure_time += (n.measured_at - \
                                  prev_votes.vote_map[n.idhex].measured_at)
        else:
          node_cnt += 1
          node_measure_time += (n.measured_at - \
                                  prev_votes.vote_map[n.idhex].measured_at)

  # There is a difference between measure period and sample rate.
  # Measurement period is how fast the bandwidth auths can actually measure
  # the network. Sample rate is how often we want the PID feedback loop to
  # run. 
  plog("INFO", "Average node measurement interval: "+str(node_measure_time/node_cnt))
  plog("INFO", "Average gaurd measurement interval: "+str(guard_measure_time/guard_cnt))

  tot_net_bw = 0
  for n in nodes.itervalues():
    n.fbw_ratio = n.filt_bw/true_filt_avg
    n.sbw_ratio = n.strm_bw/true_strm_avg
    if n.sbw_ratio > n.fbw_ratio:
      # Does this ever happen?
      plog("NOTICE", "sbw > fbw for "+n.nick)
      n.ratio = n.sbw_ratio
      n.pid_error = (n.strm_bw - true_strm_avg)/true_strm_avg
    else:
      n.ratio = n.fbw_ratio
      n.pid_error = (n.filt_bw - true_filt_avg)/true_filt_avg

    if cs_junk.bwauth_pid_control:
      if n.idhex in prev_votes.vote_map:
        # If there is a new sample, let's use it for all but guards
        if n.measured_at > prev_votes.vote_map[n.idhex].measured_at:
          # Nodes with the Guard flag will respond slowly to feedback,
          # so they should be sampled less often, and in proportion to
          # the appropriate Wgx weight.
          if n.idhex in prev_consensus and \
            ("Guard" in prev_consensus[n.idhex].flags \
             and "Exit" not in prev_consensus[n.idhex].flags):
            # Do full feedback if our previous vote > 2.5 weeks old
            if n.idhex not in prev_votes.vote_map or \
                n.measured_at - prev_votes.vote_map[n.idhex].measured_at > GUARD_SAMPLE_RATE:
              n.new_bw = n.get_pid_bw(prev_votes.vote_map[n.idhex], K_p)
            else:
              pid_error = n.pid_error
              n.revert_to_vote(prev_votes.vote_map[n.idhex])
              # Don't use feedback here, but we might as well use our
              # new measurement against the previous vote.
              n.new_bw = prev_votes.vote_map[n.idhex].pid_bw + \
                     K_p*prev_votes.vote_map[n.idhex].pid_bw*pid_error
          else:
            # Everyone else should be pretty instantenous to respond.
            # Full feedback should be fine for them (we hope),
            # except for Guard+Exits, we want to dampen just a little
            # bit for them. Wgd seems a good choice, but might not be exact.
            # We really want to magically combine Wgd and something that
            # represents the client migration rate for Guards.. But who
            # knows how to represent that and still KISS?
            if n.idhex in prev_consensus and \
              ("Guard" in prev_consensus[n.idhex].flags \
               and "Exit" not in prev_consensus[n.idhex].flags):
              n.new_bw = n.get_pid_bw(prev_votes.vote_map[n.idhex], 1.0-cs_junk.bw_weights["Wgd"])
            else:
              n.new_bw = n.get_pid_bw(prev_votes.vote_map[n.idhex], K_p)
        else:
          # Reset values. Don't vote/sample this measurement round.
          n.revert_to_vote(prev_votes.vote_map[n.idhex])
      else: # No prev vote, pure consensus feedback this round
        n.new_bw = n.ns_bw + K_p*n.ns_bw*n.pid_error
        n.pid_error_sum = n.pid_error
        n.pid_bw = n.new_bw
        plog("INFO", "No prev vote for node "+n.nick+": Consensus feedback")
    else: # No PID feedback
      n.pid_bw = 0
      n.pid_error = 0
      n.pid_error_sum = 0
      n.new_bw = n.desc_bw*n.ratio

    n.change = n.new_bw - n.desc_bw

    if n.idhex in prev_consensus:
      if prev_consensus[n.idhex].bandwidth != None:
        prev_consensus[n.idhex].measured = True
        tot_net_bw += n.new_bw
      if IGNORE_GUARDS \
           and ("Guard" in prev_consensus[n.idhex].flags and not "Exit" in \
                  prev_consensus[n.idhex].flags):
        plog("INFO", "Skipping voting for guard "+n.nick)
        n.ignore = True
      elif "Authority" in prev_consensus[n.idhex].flags:
        plog("INFO", "Skipping voting for authority "+n.nick)
        n.ignore = True

  # Go through the list and cap them to NODE_CAP
  for n in nodes.itervalues():
    if n.new_bw >= 0xffffffff*1000:
      plog("WARN", "Bandwidth of node "+n.nick+"="+n.idhex+" exceeded maxint32: "+str(n.new_bw))
      n.new_bw = 0xffffffff*1000
    if n.new_bw > tot_net_bw*NODE_CAP:
      plog("INFO", "Clipping extremely fast node "+n.idhex+"="+n.nick+
           " at "+str(100*NODE_CAP)+"% of network capacity ("
           +str(n.new_bw)+"->"+str(int(tot_net_bw*NODE_CAP))+")")
      n.new_bw = int(tot_net_bw*NODE_CAP)
      n.pid_error_sum = 0 # Don't let unused error accumulate...

  # WTF is going on here?
  oldest_timestamp = min(map(lambda n: n.measured_at,
             filter(lambda n: n.idhex in prev_consensus,
                       nodes.itervalues())))
  plog("INFO", "Oldest measured node: "+time.ctime(oldest_timestamp))

  missed_nodes = 0.0
  for n in prev_consensus.itervalues():
    if not n.measured:
      if "Fast" in n.flags and "Running" in n.flags:
        try:
          r = c.get_router(n)
        except TorCtl.ErrorReply:
          r = None
        if r and not r.down and r.bw > 0:
          #if time.mktime(r.published.utctimetuple()) - r.uptime \
          #       < oldest_timestamp:
          missed_nodes += 1.0
          # We still tend to miss about 80 nodes even with these
          # checks.. Possibly going in and out of hibernation?
          plog("DEBUG", "Didn't measure "+n.idhex+"="+n.nickname+" at "+str(round((100.0*n.list_rank)/max_rank,1))+" "+str(n.bandwidth))

  measured_pct = round(100.0*len(nodes)/(len(nodes)+missed_nodes),1)
  if measured_pct < MIN_REPORT:
    plog("NOTICE", "Did not measure "+str(MIN_REPORT)+"% of nodes yet ("+str(measured_pct)+"%)")
    sys.exit(1)

  plog("INFO", "Measured "+str(measured_pct)+"% of all tor nodes.")

  n_print = nodes.values()
  n_print.sort(lambda x,y: int(y.change) - int(x.change))

  for scanner in scanner_timestamps.iterkeys():
    scan_age = int(round(scanner_timestamps[scanner],0))
    if scan_age < time.time() - MAX_SCAN_AGE:
      plog("WARN", "Bandwidth scanner "+scanner+" stale. Possible dead bwauthority.py. Timestamp: "+time.ctime(scan_age))

  out = file(argv[-1], "w")
  out.write(str(scan_age)+"\n")

  # FIXME: Split out debugging data
  for n in n_print:
    if not n.ignore:
      out.write("node_id="+n.idhex+" bw="+str(base10_round(n.new_bw))+" nick="+n.nick+ " measured_at="+str(int(n.measured_at))+" pid_error="+str(n.pid_error)+" pid_error_sum="+str(n.pid_error_sum)+" pid_bw="+str(n.pid_bw)+" pid_delta="+str(n.derror_dt)+"\n")
  out.close()

if __name__ == "__main__":
  try:
    main(sys.argv)
  except socket.error, e:
    traceback.print_exc()
    plog("WARN", "Socket error. Are the scanning Tors running?")
    sys.exit(1)
  except Exception, e:
    plog("ERROR", "Exception during aggregate: "+str(e))
    traceback.print_exc()
    sys.exit(1)
  sys.exit(0)
