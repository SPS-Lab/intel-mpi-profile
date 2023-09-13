#!/usr/bin/env python

import geni.portal as portal
import geni.rspec.pg as RSpec
import geni.rspec.igext as IG
# Emulab specific extensions.
import geni.rspec.emulab as emulab
from lxml import etree as ET
import crypt
import random
import os
import hashlib
import os.path
import sys

TBCMD = "sudo mkdir -p /local/setup && sudo chown `geni-get user_urn | cut -f4 -d+` /local/setup && sudo -u `geni-get user_urn | cut -f4 -d+` -Hi /bin/bash -c '/local/repository/setup-driver.sh >/local/logs/setup.log 2>&1'"

#
# For now, disable the testbed's root ssh key service until we can remove ours.
# It seems to race (rarely) with our startup scripts.
#
disableTestbedRootKeys = True

#
# Create our in-memory model of the RSpec -- the resources we're going
# to request in our experiment, and their configuration.
#
rspec = RSpec.Request()

#
# This geni-lib script is designed to run in the CloudLab Portal.
#
pc = portal.Context()

#
# Define some parameters.
#
pc.defineParameter(
    "nodeCount","Number of Nodes",
    portal.ParameterType.INTEGER,4,
    longDescription="Number of nodes in your kubernetes cluster.  Should be either 1, or >= 3.")
pc.defineParameter(
    "nodeType","Hardware Type",
    portal.ParameterType.NODETYPE,"m510",
    longDescription="A specific hardware type to use for each node.  Cloudlab clusters all have machines of specific types.  When you set this field to a value that is a specific hardware type, you will only be able to instantiate this profile on clusters with machines of that type.  If unset, when you instantiate the profile, the resulting experiment may have machines of any available type allocated.")
pc.defineParameter(
    "linkSpeed","Experiment Link Speed",
    portal.ParameterType.INTEGER,0,
    [(0,"Any"),(1000000,"1Gb/s"),(10000000,"10Gb/s"),(25000000,"25Gb/s"),(40000000,"40Gb/s"),(100000000,"100Gb/s")],
    longDescription="A specific link speed to use for each link/LAN.  All experiment network interfaces will request this speed.")
pc.defineParameter(
    "diskImage","Disk Image",
    portal.ParameterType.STRING,
    "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD",
    advanced=True,
    longDescription="An image URN or URL that every node will run.")
pc.defineParameter(
    "multiplexLans", "Multiplex Networks",
    portal.ParameterType.BOOLEAN,False,
    longDescription="Multiplex any networks over physical interfaces using VLANs.  Some physical machines have only a single experiment network interface, so if you want multiple links/LANs, you have to enable multiplexing.  Currently, if you select this option.",
    advanced=True)
pc.defineParameter(
    "publicIPCount", "Number of public IP addresses",
    portal.ParameterType.INTEGER,1,
    longDescription="Set the number of public IP addresses you will need for externally-published services",
    advanced=True)
pc.defineParameter(
    "sslCertType","SSL Certificate Type",
    portal.ParameterType.STRING,"self",
    [("none","None"),("self","Self-Signed"),("letsencrypt","Let's Encrypt")],
    advanced=True,
    longDescription="Choose an SSL Certificate strategy.  By default, we generate self-signed certificates, and only use them for a reverse web proxy to allow secure remote access to the Kubernetes Dashboard.  However, you may choose `None` if you prefer to arrange remote access differently (e.g. ssh port forwarding).  You may also choose to use Let's Encrypt certificates whose trust root is accepted by all modern browsers.")
pc.defineParameter(
    "sslCertConfig","SSL Certificate Configuration",
    portal.ParameterType.STRING,"proxy",
    [("proxy","Web Proxy")],
    advanced=True,
    longDescription="Choose where you want the SSL certificates deployed.  Currently the only option is for them to be configured as part of the web proxy to the dashboard.")
pc.defineStructParameter(
    "sharedVlans","Add Shared VLAN",[],
    multiValue=True,itemDefaultValue={},min=0,max=None,
    members=[
        portal.Parameter(
            "createConnectableSharedVlan","Create Connectable Shared VLAN",
            portal.ParameterType.BOOLEAN,False,
            longDescription="Create a placeholder, connectable shared VLAN stub and 'attach' the first node to it.  You can use this during the experiment to connect this experiment interface to another experiment's shared VLAN."),
        portal.Parameter(
            "createSharedVlan","Create Shared VLAN",
            portal.ParameterType.BOOLEAN,False,
            longDescription="Create a new shared VLAN with the name above, and connect the first node to it."),
        portal.Parameter(
            "connectSharedVlan","Connect to Shared VLAN",
            portal.ParameterType.BOOLEAN,False,
            longDescription="Connect an existing shared VLAN with the name below to the first node."),
        portal.Parameter(
            "sharedVlanName","Shared VLAN Name",
            portal.ParameterType.STRING,"",
            longDescription="A shared VLAN name (functions as a private key allowing other experiments to connect to this node/VLAN), used when the 'Create Shared VLAN' or 'Connect to Shared VLAN' options above are selected.  Must be fewer than 32 alphanumeric characters."),
        portal.Parameter(
            "sharedVlanAddress","Shared VLAN IP Address",
            portal.ParameterType.STRING,"10.254.254.1",
            longDescription="Set the IP address for the shared VLAN interface.  Make sure to use an unused address within the subnet of an existing shared vlan!"),
        portal.Parameter(
            "sharedVlanNetmask","Shared VLAN Netmask",
            portal.ParameterType.STRING,"255.255.255.0",
            longDescription="Set the subnet mask for the shared VLAN interface, as a dotted quad.")])
pc.defineStructParameter(
    "datasets","Datasets",[],
    multiValue=True,itemDefaultValue={},min=0,max=None,
    members=[
        portal.Parameter(
            "urn","Dataset URN",
            portal.ParameterType.STRING,"",
            longDescription="The URN of an *existing* remote dataset (a remote block store) that you want attached to the node you specified (defaults to the first node).  The block store must exist at the cluster at which you instantiate the profile."),
        portal.Parameter(
            "mountNode","Dataset Mount Node",
            portal.ParameterType.STRING,"node-0",
            longDescription="The node on which you want your remote block store mounted; defaults to the first node."),
        portal.Parameter(
            "mountPoint","Dataset Mount Point",
            portal.ParameterType.STRING,"/dataset",
            longDescription="The mount point at which you want your remote dataset mounted.  Be careful where you mount it -- something might already be there (i.e., /storage is already taken).  Note also that this option requires a network interface, because it creates a link between the dataset and the node where the dataset is available.  Thus, just as for creating extra LANs, you might need to select the Multiplex Flat Networks option, which will also multiplex the blockstore link here."),
        portal.Parameter(
            "readOnly","Mount Dataset Read-only",
            portal.ParameterType.BOOLEAN,True,
            longDescription="Mount the remote dataset in read-only mode.")])

#
# Get any input parameter values that will override our defaults.
#
params = pc.bindParameters()

if params.publicIPCount > 8:
    perr = portal.ParameterWarning(
        "You cannot request more than 8 public IP addresses, at least not without creating your own modified version of this profile!",
        ["publicIPCount"])
    pc.reportWarning(perr)
i = 0
for x in params.sharedVlans:
    n = 0
    if x.createConnectableSharedVlan:
        n += 1
    if x.createSharedVlan:
        n += 1
    if x.connectSharedVlan:
        n += 1
    if n > 1:
        err = portal.ParameterError(
            "Must choose only a single shared vlan operation (create, connect, create connectable)",
        [ 'sharedVlans[%d].createConnectableSharedVlan' % (i,),
          'sharedVlans[%d].createSharedVlan' % (i,),
          'sharedVlans[%d].connectSharedVlan' % (i,) ])
        pc.reportError(err)
    if n == 0:
        err = portal.ParameterError(
            "Must choose one of the shared vlan operations: create, connect, create connectable",
        [ 'sharedVlans[%d].createConnectableSharedVlan' % (i,),
          'sharedVlans[%d].createSharedVlan' % (i,),
          'sharedVlans[%d].connectSharedVlan' % (i,) ])
        pc.reportError(err)
    i += 1

#
# Give the library a chance to return nice JSON-formatted exception(s) and/or
# warnings; this might sys.exit().
#
pc.verifyParameters()

#
# General kubernetes instruction text.
#
kubeInstructions = \
  """
## Waiting for your Experiment to Complete Setup

Once the initial phase of experiment creation completes (disk load and node configuration), the profile's setup scripts begin the complex process of installing software according to profile parameters, so you must wait to access software resources until they complete.  The Kubernetes dashboard link will not be available immediately.  There are multiple ways to determine if the scripts have finished.
  - First, you can watch the experiment status page: the overall State will say \"booted (startup services are still running)\" to indicate that the nodes have booted up, but the setup scripts are still running.
  - Second, the Topology View will show you, for each node, the status of the startup command on each node (the startup command kicks off the setup scripts on each node).  Once the startup command has finished on each node, the overall State field will change to \"ready\".  If any of the startup scripts fail, you can mouse over the failed node in the topology viewer for the status code.
  - Third, the profile configuration scripts send emails: one to notify you that profile setup has started, and another notify you that setup has completed.
  - Finally, you can view [the profile setup script logfiles](http://{host-node-0}:7999/) as the setup scripts run.  Use the `admin` username and the automatically-generated random password `{password-adminPass}` .  This URL is available very quickly after profile setup scripts begin work.
"""

#
# Customizable area for forks.
#
tourDescription = \
  "This profile creates a cluster with 3 nodes.  When you click the Instantiate button, you'll be presented with a list of parameters that you can change to control what your cluster will look like; read the parameter documentation on that page (or in the Instructions)."

tourInstructions = kubeInstructions

#
# Setup the Tour info with the above description and instructions.
#  
tour = IG.Tour()
tour.Description(IG.Tour.TEXT,tourDescription)
tour.Instructions(IG.Tour.MARKDOWN,tourInstructions)
rspec.addTour(tour)

datalans = []

if params.nodeCount > 1:
    datalan = RSpec.LAN("datalan-1")
    if params.linkSpeed > 0:
        datalan.bandwidth = int(params.linkSpeed)
    if params.multiplexLans:
        datalan.link_multiplexing = True
        datalan.best_effort = True
        # Need this cause LAN() sets the link type to lan, not sure why.
        datalan.type = "vlan"
    datalans.append(datalan)

nodes = dict({})

sharedvlans = []
for i in range(0,params.nodeCount):
    nodename = "node-%d" % (i,)
    node = RSpec.RawPC(nodename)
    bs = node.Blockstore("blockstore111", "/dataset")
    bs.size = "30GB"
    if params.nodeType:
        node.hardware_type = params.nodeType
    if params.diskImage:
        node.disk_image = params.diskImage
    j = 0
    for datalan in datalans:
        iface = node.addInterface("if%d" % (j,))
        datalan.addInterface(iface)
        j += 1
    if TBCMD is not None:
        node.addService(RSpec.Execute(shell="sh",command=TBCMD))
    if disableTestbedRootKeys:
        node.installRootKeys(False, False)
    nodes[nodename] = node
    if i == 0:
        k = 0
        for x in params.sharedVlans:
            iface = node.addInterface("ifSharedVlan%d" % (k,))
            if x.sharedVlanAddress:
                iface.addAddress(
                    RSpec.IPv4Address(x.sharedVlanAddress,x.sharedVlanNetmask))
            sharedvlan = RSpec.Link('shared-vlan-%d' % (k,))
            sharedvlan.addInterface(iface)
            if x.createConnectableSharedVlan:
                sharedvlan.enableSharedVlan()
            else:
                if x.createSharedVlan:
                    svn = x.sharedVlanName
                    if not svn:
                        # Create a random name
                        svn = "sv-" + str(hashlib.sha256(os.urandom(128)).hexdigest()[:28])
                    sharedvlan.createSharedVlan(svn)
                else:
                    sharedvlan.connectSharedVlan(x.sharedVlanName)
            if params.multiplexLans:
                sharedvlan.link_multiplexing = True
                sharedvlan.best_effort = True
            sharedvlans.append(sharedvlan)
            k += 1

#
# Add the dataset(s), if requested.
#
bsnodes = []
bslinks = []
i = 0
for x in params.datasets:
    if not x.urn:
        err = portal.ParameterError(
            "Must provide a non-null dataset URN",
            [ 'datasets[%d].urn' % (i,) ])
        pc.reportError(err)
        pc.verifyParameters()
    if x.mountNode not in nodes:
        perr = portal.ParameterError(
            "The node on which you mount your dataset must exist, and does not.",
            [ 'datasets[%d].mountNode' % (i,) ])
        pc.reportError(perr)
        pc.verifyParameters()
    bsn = nodes[x.mountNode]
    myintf = bsn.addInterface("ifbs%d" % (i,))
    bsnode = IG.RemoteBlockstore("bsnode-%d" % (i,),x.mountPoint)
    bsintf = bsnode.interface
    bsnode.dataset = x.urn
    bsnode.readonly = x.readOnly
    bsnodes.append(bsnode)

    bslink = RSpec.Link("bslink-%d" % (i,))
    bslink.addInterface(myintf)
    bslink.addInterface(bsintf)
    bslink.best_effort = True
    bslink.vlan_tagging = True
    bslinks.append(bslink)

for nname in nodes.keys():
    rspec.addResource(nodes[nname])
for datalan in datalans:
    rspec.addResource(datalan)
for x in sharedvlans:
    rspec.addResource(x)
for x in bsnodes:
    rspec.addResource(x)
for x in bslinks:
    rspec.addResource(x)

class EmulabEncrypt(RSpec.Resource):
    def _write(self, root):
        ns = "{http://www.protogeni.net/resources/rspec/ext/emulab/1}"
        el = ET.SubElement(root,"%spassword" % (ns,),attrib={'name':'adminPass'})

adminPassResource = EmulabEncrypt()
rspec.addResource(adminPassResource)

#
# Grab a few public IP addresses.
#
apool = IG.AddressPool("node-0",params.publicIPCount)
rspec.addResource(apool)

pc.printRequestRSpec(rspec)
